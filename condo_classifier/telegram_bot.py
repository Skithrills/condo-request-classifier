"""Resident-facing Telegram bot.

Run with `run_bot.cmd` (Windows), `./run_bot.sh` (macOS/Linux) or
`uv run --extra telegram python -m condo_classifier.telegram_bot`. Message text is never
logged; only the outcome is. Conversations live in memory and reset on restart; requests
are saved to the SQLite store that the staff dashboard reads.
"""

import asyncio
import logging
import os
import time

from dotenv import load_dotenv
from pydantic import ValidationError
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ReactionEmoji
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
from condo_classifier.chat import (
    IDLE_RESET_SECONDS,
    Conversation,
    ResidentChat,
    ready_to_submit,
)
from condo_classifier.replies import (
    CANCELLED,
    HUMAN,
    HUMAN_ALREADY,
    HUMAN_FIRST,
    INVALID,
    NEW_REQUEST,
    NOT_READY,
    PHOTO_TOO_BIG,
    PHOTO_UNAVAILABLE,
    PHOTO_UNREADABLE,
    STALE,
    SUBMITTED,
    TEXT_ONLY,
    UNAVAILABLE,
    WELCOME,
    quick_reply,
    resident_reply,
    work_order_summary,
)
from condo_classifier.schema import ResidentRequest
from condo_classifier.service import Classifier
from condo_classifier.staff import new_request_id, resident_label, staff_card
from condo_classifier.store import RequestRecord, RequestStore, default_store_path
from condo_classifier.vision import (
    ClaudePhotoDescriber,
    PhotoDescriber,
    photo_message,
    prepare_image,
)

log = logging.getLogger(__name__)

# Reactions mark only status changes worth noticing; the typing indicator covers the rest.
REACTIONS = {
    "ready": ReactionEmoji.OK_HAND_SIGN,
    "submitted": ReactionEmoji.SALUTING_FACE,
    "needs_human": ReactionEmoji.PERSON_WITH_FOLDED_HANDS,
    "emergency": ReactionEmoji.FACE_SCREAMING_IN_FEAR,
    "problem": ReactionEmoji.THINKING_FACE,
}


MAX_PHOTO_BYTES = 10 * 1024 * 1024
# Saved copies for staff: big enough to see small defects, small enough to keep.
SAVED_PHOTO_EDGE = 2048


class ResidentBot:
    def __init__(
        self,
        classifier: Classifier,
        chat: ResidentChat | None = None,
        staff_chat_id: int | None = None,
        store: RequestStore | None = None,
        describer: PhotoDescriber | None = None,
    ):
        self.classifier = classifier
        self.chat = chat
        self.staff_chat_id = staff_chat_id
        self.store = store
        self.describer = describer
        self.conversations: dict[int, Conversation] = {}
        self.locks: dict[int, asyncio.Lock] = {}

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
        conversation = self.conversation(chat_id)
        conversation.resident_texts.append(text)
        # Classify the whole conversation so details sent later fill in what was missing.
        request = ResidentRequest(text=conversation.request_text(), channel="Telegram")
        result = self.classifier.classify(request)
        conversation.last_result = result
        conversation.request_id = conversation.request_id or new_request_id()
        if result.status == "safety_escalation" or conversation.escalated:
            conversation.status = "emergency"
        elif conversation.human_requested:
            conversation.status = "needs_human"
        else:
            conversation.status = "ready" if ready_to_submit(result) else "open"
        log.info(
            "classified category=%s priority=%s status=%s request=%s",
            result.category,
            result.priority,
            conversation.status,
            conversation.request_id,
        )
        self.save(chat_id, conversation)
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

    @staticmethod
    def worth_tracking(conversation: Conversation) -> bool:
        """Skip greetings and unclear chatter until the request is understood or escalated."""
        result = conversation.last_result
        if result is None or conversation.request_id is None:
            return False
        return result.category.value != "other" or conversation.status != "open"

    def save(self, chat_id: int, conversation: Conversation) -> None:
        result = conversation.last_result
        if self.store is None or not self.worth_tracking(conversation):
            return
        try:
            # Photos are written only once a request is worth tracking.
            while conversation.pending_photos:
                jpeg = conversation.pending_photos.pop(0)
                conversation.photos.append(self.store.save_photo(conversation.request_id, jpeg))
            self.store.save(
                RequestRecord(
                    id=conversation.request_id,
                    chat_id=chat_id,
                    resident=conversation.resident,
                    status=conversation.status,
                    category=result.category.value,
                    priority=result.priority.value,
                    team=result.suggested_team,
                    location=result.location,
                    missing_details=result.missing_details,
                    review_reasons=result.review_reasons,
                    messages=conversation.resident_texts,
                    photos=conversation.photos,
                )
            )
        except Exception:
            log.exception("Could not save request %s", conversation.request_id)

    def keyboard(self, conversation: Conversation) -> InlineKeyboardMarkup | None:
        """Buttons only once the work order is complete, so they never look like a choice the
        resident must make while the bot is still asking a question. /human covers the rest."""
        request_id = conversation.request_id
        if request_id is None or conversation.status != "ready":
            return None
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Submit", callback_data=f"submit:{request_id}"),
                    InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{request_id}"),
                ],
                [InlineKeyboardButton("🙋 Talk to a person", callback_data=f"human:{request_id}")],
            ]
        )

    async def react(self, bot: Bot, chat_id: int, message_id: int | None, state: str) -> None:
        if message_id is None or state not in REACTIONS:
            return
        try:
            await bot.set_message_reaction(chat_id, message_id, REACTIONS[state])
        except TelegramError as exc:
            log.debug("Reaction failed: %s", type(exc).__name__)

    async def clear_buttons(self, bot: Bot, chat_id: int, conversation: Conversation) -> None:
        """Remove buttons from the previous reply so only the latest one is tappable."""
        if conversation.buttons_message_id is None:
            return
        message_id, conversation.buttons_message_id = conversation.buttons_message_id, None
        try:
            await bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
        except TelegramError:
            pass

    async def notify_staff(self, bot: Bot, conversation: Conversation, fresh=False) -> None:
        """Post or update this request's card in the staff chat, then any new photos under
        it. Never blocks the resident."""
        await self.post_card(bot, conversation, fresh)
        if self.staff_chat_id is None or self.store is None:
            return
        while conversation.staff_photos_sent < len(conversation.photos):
            relative = conversation.photos[conversation.staff_photos_sent]
            conversation.staff_photos_sent += 1
            try:
                with self.store.photo_path(relative).open("rb") as photo:
                    await bot.send_photo(
                        self.staff_chat_id,
                        photo,
                        caption=f"📷 {conversation.request_id}",
                        reply_to_message_id=conversation.staff_message_id,
                    )
            except (OSError, TelegramError) as exc:
                log.warning("Could not send a photo to staff: %s", type(exc).__name__)

    async def post_card(self, bot: Bot, conversation: Conversation, fresh=False) -> None:
        result = conversation.last_result
        if self.staff_chat_id is None or not self.worth_tracking(conversation):
            return
        card = staff_card(
            conversation.request_id,
            conversation.resident,
            result,
            conversation.resident_texts,
            conversation.status,
        )
        # A first emergency gets a fresh message so it notifies; otherwise edit the card.
        if conversation.status == "emergency" and not conversation.staff_alerted:
            fresh = True
            conversation.staff_alerted = True
        try:
            if conversation.staff_message_id is not None and not fresh:
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
            log.info("Staff card %s posted", conversation.request_id)
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
            existing = self.conversations.get(chat_id)
            canned = quick_reply(message.text, existing is None or not existing.resident_texts)
            if canned:
                # Greetings, and "thanks" after a submitted request, don't open a request.
                await message.reply_text(canned)
                return
            await context.bot.send_chat_action(chat_id, "typing")
            await self.respond(update, context, message.text)

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A photo (or an image sent as a file) becomes a described turn in the conversation."""
        message = update.effective_message
        chat_id = update.effective_chat.id
        if self.describer is None:
            await message.reply_text(PHOTO_UNAVAILABLE)
            return
        media = message.photo[-1] if message.photo else message.document
        if media.file_size and media.file_size > MAX_PHOTO_BYTES:
            await message.reply_text(PHOTO_TOO_BIG)
            return
        async with self.lock(chat_id):
            await context.bot.send_chat_action(chat_id, "typing")
            try:
                data = bytes(await (await media.get_file()).download_as_bytearray())
                jpeg = await asyncio.to_thread(prepare_image, data, SAVED_PHOTO_EDGE)
            except Exception:
                log.warning("Could not download or read a photo")
                jpeg = None
            caption = message.caption
            description = None
            if jpeg is not None:
                description = await asyncio.to_thread(self.describer.describe, jpeg, caption)
            if description is None:
                await self.react(context.bot, chat_id, message.message_id, "problem")
                await message.reply_text(PHOTO_UNREADABLE)
                return
            log.info("Photo described (%d chars)", len(description))
            await self.respond(update, context, photo_message(description, caption), jpeg)

    async def respond(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        photo: bytes | None = None,
    ) -> None:
        """Classify a resident turn, reply, and update the staff card. Caller holds the lock."""
        message = update.effective_message
        chat_id = update.effective_chat.id
        bot = context.bot
        conversation = self.conversation(chat_id)
        if photo is not None:
            conversation.pending_photos.append(photo)
        user = update.effective_user
        conversation.resident = resident_label(user.full_name, user.username, user.id)
        conversation.last_resident_message_id = message.message_id
        previous = conversation.last_result
        try:
            # The classifier and Claude client are synchronous.
            reply = await asyncio.to_thread(self.reply_for, chat_id, text)
        except Exception:
            log.exception("Reply failed")
            reply = UNAVAILABLE
        conversation = self.conversations.get(chat_id)
        classified = conversation is not None and conversation.last_result is not previous
        if not classified or conversation.last_result.status == "provider_error":
            await self.react(bot, chat_id, message.message_id, "problem")
            await message.reply_text(reply)
            return
        if conversation.status == "ready":
            reply = f"{reply}\n\n{work_order_summary(conversation.last_result)}"
        await self.clear_buttons(bot, chat_id, conversation)
        sent = await message.reply_text(reply, reply_markup=self.keyboard(conversation))
        if self.keyboard(conversation) is not None:
            conversation.buttons_message_id = sent.message_id
        await self.react(bot, chat_id, message.message_id, conversation.status)
        await self.notify_staff(bot, conversation)

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        action, _, request_id = (query.data or "").partition(":")
        chat_id = query.message.chat.id
        bot = context.bot
        async with self.lock(chat_id):
            conversation = self.conversations.get(chat_id)
            if conversation is None or conversation.request_id != request_id:
                await query.answer(STALE)
                await query.edit_message_reply_markup(reply_markup=None)
                return
            if action == "submit" and conversation.status != "ready":
                await query.answer(NOT_READY)
                return
            await query.answer()
            await self.apply(bot, chat_id, conversation, action, query.message.reply_text)

    async def apply(self, bot: Bot, chat_id: int, conversation: Conversation, action, send):
        """Submit, cancel or hand a request to a person, from a button or /human."""
        request_id = conversation.request_id
        if action == "submit":
            conversation.status = "submitted"
            reply = SUBMITTED.format(request_id=request_id)
        elif action == "cancel":
            conversation.status = "cancelled"
            reply = CANCELLED
        elif action == "human":
            conversation.status = "needs_human"
            conversation.human_requested = True
            reply = HUMAN.format(request_id=request_id)
        else:
            return
        log.info("Resident chose %s for %s", action, request_id)
        self.save(chat_id, conversation)
        await self.clear_buttons(bot, chat_id, conversation)
        await send(reply)
        await self.react(bot, chat_id, conversation.last_resident_message_id, conversation.status)
        # Asking for a person posts a fresh card so it notifies staff.
        await self.notify_staff(bot, conversation, fresh=action == "human")
        if action in {"submit", "cancel"}:
            self.conversations.pop(chat_id, None)

    async def human(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        message = update.effective_message
        async with self.lock(chat_id):
            conversation = self.conversations.get(chat_id)
            if conversation is None or conversation.last_result is None:
                await message.reply_text(HUMAN_FIRST)
                return
            if conversation.human_requested:
                await message.reply_text(HUMAN_ALREADY)
                return
            await self.apply(context.bot, chat_id, conversation, "human", message.reply_text)

    async def other(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(TEXT_ONLY)

    async def chat_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Setup helper: send /chatid in the staff group or channel to get STAFF_CHAT_ID."""
        await update.effective_message.reply_text(
            f"This chat's ID is {update.effective_chat.id}. "
            "Put it in .env as STAFF_CHAT_ID to send resident requests here."
        )

    @staticmethod
    async def set_commands(app: Application) -> None:
        """Show /new and /human in Telegram's command menu."""
        try:
            await app.bot.set_my_commands(
                [
                    BotCommand("new", "Start a new request"),
                    BotCommand("human", "Talk to a staff member"),
                ]
            )
        except TelegramError as exc:
            log.warning("Could not set the command menu: %s", type(exc).__name__)

    def application(self, token: str) -> Application:
        app = Application.builder().token(token).post_init(self.set_commands).build()
        # Residents only talk to the bot in private chats; groups are for staff.
        private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
        app.add_handler(CommandHandler(["start", "help"], self.start, filters=private))
        app.add_handler(CommandHandler("new", self.new, filters=private))
        app.add_handler(CommandHandler(["human", "person"], self.human, filters=private))
        app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, self.text))
        app.add_handler(
            MessageHandler(private & (filters.PHOTO | filters.Document.IMAGE), self.photo)
        )
        app.add_handler(MessageHandler(private & ~filters.COMMAND, self.other))
        app.add_handler(CallbackQueryHandler(self.button, pattern=r"^(submit|cancel|human):"))
        # CommandHandler ignores channel posts, so match /chatid as text to cover channels.
        chat_id_command = filters.Regex(r"^/chatid(@\w+)?$") & ~filters.UpdateType.EDITED
        app.add_handler(MessageHandler(chat_id_command, self.chat_id))
        return app


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
    store = RequestStore(default_store_path())
    describer = None
    if os.getenv("PHOTOS", "on") == "on" and os.getenv("ANTHROPIC_API_KEY"):
        try:
            describer = ClaudePhotoDescriber.from_env()
        except ValueError as exc:
            raise SystemExit(f"Check .env: {exc}") from exc
    log.info("Photos: %s.", "on" if describer else "off")
    log.info("Classifier backend: %s. Replies: %s.", backend_name, replies)
    log.info("Saving requests to %s", store.path)
    return ResidentBot(
        Classifier(backend),
        chat,
        int(staff_chat_id) if staff_chat_id else None,
        store,
        describer,
    )


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level="INFO")
    # httpx logs every Telegram request URL, which includes the bot token; httpx2 (used by
    # the Anthropic SDK) logs every API call. Keep both quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    print("Starting the bot. The first run can take a minute...", flush=True)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (create a bot with @BotFather).")
    bot = build_bot()
    log.info("Bot is running. Message it on Telegram; press Ctrl+C here to stop.")
    bot.application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
