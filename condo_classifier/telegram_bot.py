"""Resident-facing Telegram bot.

Run with `run_bot.cmd` (Windows), `./run_bot.sh` (macOS/Linux) or
`uv run --extra telegram python -m condo_classifier.telegram_bot`. Message text is never
logged; only the outcome is. Conversations live in memory and reset on restart.
"""

import asyncio
import logging
import os
import re
import time
from datetime import date

from dotenv import load_dotenv
from pydantic import ValidationError
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from condo_classifier.backends import ROOT, backend_from_name
from condo_classifier.chat import IDLE_RESET_SECONDS, Conversation, ResidentChat
from condo_classifier.replies import (
    BUTTON_BUSY,
    BUTTON_EXPIRED,
    INVALID,
    LIMIT_CHOICES,
    LIMIT_REACHED,
    NEW_REQUEST,
    TEXT_ONLY,
    UNAVAILABLE,
    WELCOME,
    resident_reply,
)
from condo_classifier.schema import Classification, Priority, ResidentRequest
from condo_classifier.service import Classifier
from condo_classifier.signals import location_hint, safety_signal
from condo_classifier.staff import new_request_id, resident_label, staff_card
from condo_classifier.taxonomy import LABELS

log = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT = 20
BUTTON_REQUESTS_PER_HOUR = 3  # Non-emergency button requests; Emergency is never capped.
MAX_HELD_TEXTS = 5  # Messages kept past the limit until the resident picks a button.
STAFF_LIMIT_COMMAND = re.compile(r"^/(unlimit|relimit)(?:@\w+)?(?:\s+(-?\d+))?\s*$")


class ResidentBot:
    def __init__(
        self,
        classifier: Classifier,
        chat: ResidentChat | None = None,
        staff_chat_id: int | None = None,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        unlimited_ids: set[int] | None = None,
    ):
        self.classifier = classifier
        self.chat = chat
        self.staff_chat_id = staff_chat_id
        self.conversations: dict[int, Conversation] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        # Residents talk in private chats, where the chat ID is the user's Telegram ID.
        # 0 disables the limit. Counts and lifted limits live in memory like conversations.
        self.daily_limit = daily_limit
        self.unlimited_ids = set(unlimited_ids or ())
        self.message_counts: dict[int, tuple[date, int]] = {}
        # Past the limit, messages wait here for a button; staff get them only after a tap.
        self.held_texts: dict[int, list[str]] = {}
        self.button_requests: dict[int, list[float]] = {}

    def over_limit(self, chat_id: int, text: str) -> bool:
        """Count this message and say whether it is past today's limit.

        Possible emergencies are never blocked: the safety screen is rule-based and free.
        """
        if not self.daily_limit or chat_id in self.unlimited_ids:
            return False
        today = date.today()
        day, count = self.message_counts.get(chat_id, (today, 0))
        count = count + 1 if day == today else 1
        self.message_counts[chat_id] = (today, count)
        return count > self.daily_limit and safety_signal(text) is None

    def staff_limit_command(self, text: str) -> str:
        """Handle /unlimit or /relimit <Telegram ID> sent in the staff chat."""
        found = STAFF_LIMIT_COMMAND.match(text.strip())
        if not found or not found.group(2):
            return (
                "Use /unlimit <Telegram ID> or /relimit <Telegram ID>. "
                "The ID is on the request card."
            )
        command, user_id = found.group(1), int(found.group(2))
        if command == "unlimit":
            self.unlimited_ids.add(user_id)
            return f"Lifted the daily limit for Telegram ID {user_id} until the bot restarts."
        self.unlimited_ids.discard(user_id)
        self.message_counts.pop(user_id, None)
        return f"Telegram ID {user_id} is back on the daily limit of {self.daily_limit} messages."

    def limit_choice(
        self, chat_id: int, choice: str
    ) -> tuple[str, Classification | None, list[str]]:
        """Turn a button tap past the daily limit into a fixed reply and a staff request.

        Returns the reply, plus the request and its messages when one should reach staff.
        """
        texts = self.held_texts.get(chat_id)
        option = LIMIT_CHOICES.get(choice)
        if not texts or option is None:
            return BUTTON_EXPIRED, None, []
        emergency = option.priority == Priority.EMERGENCY
        if not emergency:
            now = time.monotonic()
            recent = [
                tapped for tapped in self.button_requests.get(chat_id, []) if now - tapped < 3600
            ]
            if len(recent) >= BUTTON_REQUESTS_PER_HOUR:
                # Keep the messages so the resident can still tap Emergency.
                return BUTTON_BUSY, None, []
            self.button_requests[chat_id] = recent + [now]
        del self.held_texts[chat_id]
        text = "\n".join(texts)
        result = Classification(
            category=option.category,
            priority=option.priority,
            suggested_team=LABELS[option.category].team,
            review_required=True,
            review_reasons=[
                "Resident was past the daily message limit and chose this button; "
                "the message was not classified."
            ],
            missing_details=[],
            location=location_hint(text),
            confidence=None,
            confidence_kind="unavailable",
            category_scores={},
            evidence=[],
            backend="resident_button",
            model="none",
            status="safety_escalation" if emergency else "needs_review",
            elapsed_ms=0,
        )
        log.info("Limit button chosen: %s", choice)
        return option.reply, result, texts

    def conversation(self, chat_id: int) -> Conversation:
        conversation = self.conversations.get(chat_id)
        now = time.monotonic()
        if conversation is None or now - conversation.last_seen > IDLE_RESET_SECONDS:
            conversation = self.conversations[chat_id] = Conversation()
        conversation.last_seen = now
        return conversation

    def reply_for(self, chat_id: int, text: str) -> str:
        try:
            ResidentRequest(text=text, channel="Telegram")
        except ValidationError:
            return INVALID
        if self.over_limit(chat_id, text):
            log.info("Daily message limit reached")
            held = self.held_texts.setdefault(chat_id, [])
            held.append(text)
            del held[:-MAX_HELD_TEXTS]
            return LIMIT_REACHED
        conversation = self.conversation(chat_id)
        conversation.resident_texts.append(text)
        # Classify the whole conversation so details sent later fill in what was missing.
        request = ResidentRequest(text=conversation.request_text(), channel="Telegram")
        result = self.classifier.classify(request)
        conversation.last_result = result
        log.info(
            "classified category=%s priority=%s status=%s",
            result.category,
            result.priority,
            result.status,
        )
        reply = None
        if result.status == "safety_escalation" and not conversation.escalated:
            # Emergencies always get the fixed safety text, never generated wording.
            conversation.escalated = True
        elif self.chat and result.status != "provider_error":
            reply = self.chat.reply(conversation, text, result)
        if reply is None:
            reply = resident_reply(result)
            conversation.messages += [
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ]
        return reply

    async def notify_staff(self, bot: Bot, chat_id: int, user: User) -> None:
        """Post or update this request's card in the staff chat. Never blocks the resident."""
        conversation = self.conversations.get(chat_id)
        if self.staff_chat_id is None or conversation is None:
            return
        result = conversation.last_result
        if result is None:
            return
        conversation.request_id = conversation.request_id or new_request_id()
        card = staff_card(
            conversation.request_id,
            resident_label(user.full_name, user.username, user.id),
            result,
            conversation.resident_texts,
        )
        # A first emergency gets a fresh message so it notifies; otherwise edit the card.
        urgent = result.status == "safety_escalation" and not conversation.staff_alerted
        try:
            if conversation.staff_message_id is not None and not urgent:
                try:
                    await bot.edit_message_text(
                        card, chat_id=self.staff_chat_id, message_id=conversation.staff_message_id
                    )
                    return
                except BadRequest as exc:
                    if "not modified" in str(exc).lower():
                        return
                    # The card was deleted or is too old to edit: post a new one.
            sent = await bot.send_message(self.staff_chat_id, card)
            conversation.staff_message_id = sent.message_id
            conversation.staff_alerted = conversation.staff_alerted or urgent
            log.info("Staff card %s posted", conversation.request_id)
        except TelegramError as exc:
            log.warning("Could not post to the staff chat: %s", type(exc).__name__)

    async def post_staff_card(
        self, bot: Bot, result: Classification, texts: list[str], user: User
    ) -> None:
        """Post a one-off card, used for button requests made past the daily limit."""
        if self.staff_chat_id is None:
            return
        card = staff_card(
            new_request_id(), resident_label(user.full_name, user.username, user.id), result, texts
        )
        try:
            await bot.send_message(self.staff_chat_id, card)
        except TelegramError as exc:
            log.warning("Could not post to the staff chat: %s", type(exc).__name__)

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(chat_id, asyncio.Lock())

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.conversations.pop(update.effective_chat.id, None)
        await update.effective_message.reply_text(WELCOME)

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.conversations.pop(update.effective_chat.id, None)
        await update.effective_message.reply_text(NEW_REQUEST)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat_id = update.effective_chat.id
        # One message at a time per chat so the conversation history stays in order.
        async with self.lock(chat_id):
            await context.bot.send_chat_action(chat_id, "typing")
            conversation = self.conversations.get(chat_id)
            previous = conversation.last_result if conversation else None
            try:
                # The classifier and Claude client are synchronous.
                reply = await asyncio.to_thread(self.reply_for, chat_id, message.text)
            except Exception:
                log.exception("Reply failed")
                reply = UNAVAILABLE
            # Past the limit, buttons replace Claude so residents can still reach staff.
            markup = limit_keyboard() if reply == LIMIT_REACHED else None
            await message.reply_text(reply, reply_markup=markup)
            conversation = self.conversations.get(chat_id)
            if conversation and conversation.last_result is not previous:
                await self.notify_staff(context.bot, chat_id, update.effective_user)

    async def limit_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        chat_id = update.effective_chat.id
        async with self.lock(chat_id):
            reply, result, texts = self.limit_choice(chat_id, query.data.removeprefix("limit:"))
            if result is None:
                await query.answer(reply, show_alert=True)
                if reply == BUTTON_EXPIRED:
                    await query.edit_message_reply_markup(None)
                return
            await query.answer()
            # Remove the buttons so one menu sends one request.
            await query.edit_message_reply_markup(None)
            await query.message.reply_text(reply)
            await self.post_staff_card(context.bot, result, texts, update.effective_user)

    async def other(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(TEXT_ONLY)

    async def chat_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Setup helper: send /chatid in the staff group or channel to get STAFF_CHAT_ID."""
        await update.effective_message.reply_text(
            f"This chat's ID is {update.effective_chat.id}. "
            "Put it in .env as STAFF_CHAT_ID to send resident requests here."
        )

    async def staff_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            self.staff_limit_command(update.effective_message.text)
        )

    def application(self, token: str) -> Application:
        app = Application.builder().token(token).build()
        # Residents only talk to the bot in private chats; groups are for staff.
        private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
        app.add_handler(CommandHandler(["start", "help"], self.start, filters=private))
        app.add_handler(CommandHandler("new", self.new, filters=private))
        app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, self.text))
        app.add_handler(MessageHandler(private & ~filters.COMMAND, self.other))
        app.add_handler(CallbackQueryHandler(self.limit_button, pattern=r"^limit:"))
        # CommandHandler ignores channel posts, so match /chatid as text to cover channels.
        chat_id_command = filters.Regex(r"^/chatid(@\w+)?$") & ~filters.UpdateType.EDITED
        app.add_handler(MessageHandler(chat_id_command, self.chat_id))
        if self.staff_chat_id is not None:
            # Only the staff chat can lift limits. Regex also covers channel posts.
            staff_command = (
                filters.Chat(self.staff_chat_id)
                & filters.Regex(r"^/(unlimit|relimit)(@\w+)?(\s|$)")
                & ~filters.UpdateType.EDITED
            )
            app.add_handler(MessageHandler(staff_command, self.staff_limit))
        return app


def limit_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(option.label, callback_data=f"limit:{key}")
        for key, option in LIMIT_CHOICES.items()
    ]
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


def build_bot() -> ResidentBot:
    """Build the bot from .env, exiting with a plain message when setup is incomplete."""
    backend_name = os.getenv("CLASSIFIER_BACKEND", "baseline")
    replies = os.getenv("BOT_REPLIES", "chat")
    if replies not in {"chat", "fixed"}:
        raise SystemExit("BOT_REPLIES must be chat or fixed.")
    needs_claude = replies == "chat" or backend_name == "claude"
    if needs_claude and not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "Set ANTHROPIC_API_KEY in .env for Claude chat replies, "
            "or set BOT_REPLIES=fixed to run without Claude."
        )
    try:
        backend = backend_from_name(backend_name)
        chat = ResidentChat.from_env() if replies == "chat" else None
    except ValueError as exc:
        raise SystemExit(f"Check .env: {exc}") from exc
    staff_chat_id = os.getenv("STAFF_CHAT_ID", "").strip()
    if staff_chat_id and not staff_chat_id.lstrip("-").isdigit():
        raise SystemExit(
            "STAFF_CHAT_ID must be a number. Send /chatid in the staff chat to get it."
        )
    if not staff_chat_id:
        log.warning(
            "STAFF_CHAT_ID is not set, so requests are not sent to staff. "
            "Add the bot to the staff group and send /chatid there to get the ID."
        )
    daily_limit = os.getenv("DAILY_MESSAGE_LIMIT", str(DEFAULT_DAILY_LIMIT)).strip()
    if not daily_limit.isdigit():
        raise SystemExit("DAILY_MESSAGE_LIMIT must be a whole number (0 turns the limit off).")
    unlimited = [part.strip() for part in os.getenv("BOT_UNLIMITED_IDS", "").split(",")]
    unlimited = [part for part in unlimited if part]
    if not all(part.lstrip("-").isdigit() for part in unlimited):
        raise SystemExit("BOT_UNLIMITED_IDS must be Telegram IDs separated by commas.")
    log.info("Classifier backend: %s. Replies: %s.", backend_name, replies)
    return ResidentBot(
        Classifier(backend),
        chat,
        int(staff_chat_id) if staff_chat_id else None,
        daily_limit=int(daily_limit),
        unlimited_ids={int(part) for part in unlimited},
    )


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level="INFO")
    # httpx logs every request URL, which includes the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (create a bot with @BotFather).")
    bot = build_bot()
    log.info("Bot is running. Message it on Telegram; press Ctrl+C here to stop.")
    bot.application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
