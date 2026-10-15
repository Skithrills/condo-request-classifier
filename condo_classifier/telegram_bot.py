"""Resident-facing Telegram bot.

Run with `run_bot.cmd` (Windows), `./run_bot.sh` (macOS/Linux) or
`uv run --extra telegram python -m condo_classifier.telegram_bot`. Message text is never
logged; only the outcome is. Conversations live in memory and reset on restart; requests
are saved to the SQLite store that the staff dashboard reads.
"""

import asyncio
import logging
import os
import re
import time

from dotenv import load_dotenv
from pydantic import ValidationError
from telegram import (
    Bot,
    BotCommand,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
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
    EMERGENCY,
    HANDED_BACK,
    HUMAN,
    HUMAN_ALREADY,
    HUMAN_FIRST,
    INVALID,
    NEW_REQUEST,
    NOT_FOR_YOU,
    NOT_READY,
    PHOTO_TOO_BIG,
    PHOTO_UNAVAILABLE,
    PHOTO_UNREADABLE,
    STAFF_MESSAGE,
    STAFF_TEXT_ONLY,
    STAFF_UNDELIVERED,
    STAFF_UNKNOWN,
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
from condo_classifier.signals import safety_signal
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
    # On a staff member's message in the staff group: it reached the resident.
    "delivered": ReactionEmoji.THUMBS_UP,
}


MAX_PHOTO_BYTES = 10 * 1024 * 1024
BOT_TO_BOT_WINDOW = 10 * 60
# Saved copies for staff: big enough to see small defects, small enough to keep.
SAVED_PHOTO_EDGE = 2048


# A conversation belongs to a private chat (its ID) or, in a group, to one person in it.
Key = int | tuple[int, int]


def key_of(chat: Chat, user: User | None) -> Key:
    if chat.type == Chat.PRIVATE or user is None:
        return chat.id
    return (chat.id, user.id)


def chat_of(key: Key) -> int:
    return key if isinstance(key, int) else key[0]


def addressed_text(message: Message, bot: Bot) -> str | None:
    """The message text without the bot's @mention, if it mentions or replies to this bot.

    None means the message isn't for this bot, so stay quiet. Messages from other bots count
    too (Telegram only delivers them when they reply to this bot or use /ask@thisbot); the
    caller limits how often the bot answers them.
    """
    if message.from_user is None:
        return None
    replied = message.reply_to_message
    is_reply = (
        replied is not None
        and replied.from_user is not None
        and replied.from_user.id == bot.id
        # In forum topics every message "replies" to the topic's first message.
        and not (message.is_topic_message and replied.forum_topic_created)
    )
    raw = message.text or message.caption or ""
    handle = f"@{bot.username}" if bot.username else None
    mentioned = handle is not None and re.search(rf"{re.escape(handle)}\b", raw, re.I)
    if not (is_reply or mentioned):
        return None
    if handle:
        raw = re.sub(rf"{re.escape(handle)}\b", "", raw, flags=re.I)
    return " ".join(raw.split())


class ResidentBot:
    def __init__(
        self,
        classifier: Classifier,
        chat: ResidentChat | None = None,
        staff_chat_id: int | None = None,
        store: RequestStore | None = None,
        describer: PhotoDescriber | None = None,
        bot_to_bot_limit: int = 3,
    ):
        self.classifier = classifier
        self.chat = chat
        self.staff_chat_id = staff_chat_id
        self.store = store
        self.describer = describer
        self.conversations: dict[Key, Conversation] = {}
        self.locks: dict[Key, asyncio.Lock] = {}
        # Loop guard for groups shared with other bots: recent replies to bots, per chat.
        self.bot_to_bot_limit = bot_to_bot_limit
        self.bot_replies: dict[int, list[float]] = {}
        # Staff-group message IDs (cards, photos, forwarded messages) -> resident conversation,
        # so a staff member's reply reaches the right resident.
        self.staff_threads: dict[int, Key] = {}

    def conversation(self, key: Key) -> Conversation:
        conversation = self.conversations.get(key)
        now = time.monotonic()
        if conversation is None or now - conversation.last_seen > IDLE_RESET_SECONDS:
            conversation = self.conversations[key] = Conversation(key=key)
        conversation.last_seen = now
        return conversation

    def reply_for(self, key: Key, text: str) -> str:
        try:
            ResidentRequest(text=text, channel="Telegram")
        except ValidationError:
            return INVALID
        conversation = self.conversation(key)
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
        self.save(key, conversation)
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
        if result is None or conversation.request_id is None or conversation.deleted:
            return False
        return result.category.value != "other" or conversation.status != "open"

    def save(self, key: Key, conversation: Conversation) -> None:
        result = conversation.last_result
        if self.store is None or not self.worth_tracking(conversation):
            return
        try:
            if conversation.saved and self.store.get(conversation.request_id) is None:
                # Staff deleted it on the dashboard; don't bring it back.
                conversation.deleted = True
                conversation.pending_photos.clear()
                log.info(
                    "Request %s was deleted by staff; not saving it again", conversation.request_id
                )
                return
            # Photos are written only once a request is worth tracking.
            while conversation.pending_photos:
                jpeg = conversation.pending_photos.pop(0)
                conversation.photos.append(self.store.save_photo(conversation.request_id, jpeg))
            self.store.save(
                RequestRecord(
                    id=conversation.request_id,
                    chat_id=chat_of(key),
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
            conversation.saved = True
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
                    sent = await bot.send_photo(
                        self.staff_chat_id,
                        photo,
                        caption=f"📷 {conversation.request_id}",
                        reply_to_message_id=conversation.staff_message_id,
                    )
                self.staff_threads[sent.message_id] = conversation.key
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
            self.staff_threads[sent.message_id] = conversation.key
            log.info("Staff card %s posted", conversation.request_id)
        except TelegramError as exc:
            log.warning("Could not post to the staff chat: %s", type(exc).__name__)

    def lock(self, key: Key) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    @staticmethod
    def key(update: Update) -> Key:
        return key_of(update.effective_chat, update.effective_user)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.conversations.pop(self.key(update), None)
        await update.effective_message.reply_text(WELCOME)

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.conversations.pop(self.key(update), None)
        await update.effective_message.reply_text(NEW_REQUEST)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handle_text(update, context, update.effective_message.text)

    async def photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.handle_photo(update, context, update.effective_message.caption)

    def allow_reply(self, chat_id: int, sender: User) -> bool:
        """Cap replies to other bots so two bots can't keep answering each other forever.

        A person addressing the bot resets the count for that chat.
        """
        if not sender.is_bot:
            self.bot_replies.pop(chat_id, None)
            return True
        now = time.monotonic()
        recent = [t for t in self.bot_replies.get(chat_id, []) if now - t < BOT_TO_BOT_WINDOW]
        if len(recent) >= self.bot_to_bot_limit:
            self.bot_replies[chat_id] = recent
            log.info("Not replying to a bot: %d bot replies in this chat recently", len(recent))
            return False
        self.bot_replies[chat_id] = [*recent, now]
        return True

    async def group(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """In groups, answer only messages that mention this bot or reply to it."""
        message = update.effective_message
        text = addressed_text(message, context.bot)
        sender = "a bot" if message.from_user and message.from_user.is_bot else "a person"
        if text is None:
            # Seeing this line proves Telegram delivers group messages to the bot.
            log.info("Group message from %s ignored: not a mention or reply", sender)
            return
        log.info("Group message from %s addressed to me", sender)
        if not self.allow_reply(update.effective_chat.id, message.from_user):
            return
        if message.photo or message.document:
            await self.handle_photo(update, context, text or None)
        else:
            # A bare "@bot" is treated like a greeting.
            await self.handle_text(update, context, text or "hi")

    async def ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/ask@thisbot <message>: how other bots (and people) address this bot in a group."""
        message = update.effective_message
        if message.from_user is None:
            return
        if not self.allow_reply(update.effective_chat.id, message.from_user):
            return
        await self.handle_text(update, context, " ".join(context.args or []) or "hi")

    async def handle_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
    ) -> None:
        message = update.effective_message
        key = self.key(update)
        # One message at a time per conversation so the history stays in order.
        async with self.lock(key):
            existing = self.conversations.get(key)
            if existing is not None and existing.human_active:
                await self.forward_to_staff(update, context, existing, text)
                return
            canned = quick_reply(text, existing is None or not existing.resident_texts)
            if canned:
                # Greetings, and "thanks" after a submitted request, don't open a request.
                await message.reply_text(canned)
                return
            await context.bot.send_chat_action(update.effective_chat.id, "typing")
            await self.respond(update, context, text)

    async def handle_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, caption: str | None
    ) -> None:
        """A photo (or an image sent as a file) becomes a described turn in the conversation."""
        message = update.effective_message
        chat_id = update.effective_chat.id
        existing = self.conversations.get(self.key(update))
        if existing is not None and existing.human_active:
            async with self.lock(self.key(update)):
                await self.forward_to_staff(update, context, existing, caption, photo=True)
            return
        if self.describer is None:
            await message.reply_text(PHOTO_UNAVAILABLE)
            return
        media = message.photo[-1] if message.photo else message.document
        if media.file_size and media.file_size > MAX_PHOTO_BYTES:
            await message.reply_text(PHOTO_TOO_BIG)
            return
        async with self.lock(self.key(update)):
            await context.bot.send_chat_action(chat_id, "typing")
            try:
                data = bytes(await (await media.get_file()).download_as_bytearray())
                jpeg = await asyncio.to_thread(prepare_image, data, SAVED_PHOTO_EDGE)
            except Exception:
                log.warning("Could not download or read a photo")
                jpeg = None
            description = None
            if jpeg is not None:
                description = await asyncio.to_thread(self.describer.describe, jpeg, caption)
            if description is None:
                await self.react(context.bot, chat_id, message.message_id, "problem")
                await message.reply_text(PHOTO_UNREADABLE)
                return
            log.info("Photo described (%d chars)", len(description))
            await self.respond(update, context, photo_message(description, caption), jpeg)

    async def forward_to_staff(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        conversation: Conversation,
        text: str | None,
        photo: bool = False,
    ) -> None:
        """While a staff member has taken over, pass the resident's messages to the staff
        group under the request card instead of answering with the AI."""
        message = update.effective_message
        bot = context.bot
        if text and safety_signal(text):
            # Staff are in the chat, but a new emergency still gets the safety text at once.
            await message.reply_text(EMERGENCY)
        name = conversation.resident.split(" (")[0]
        label = f"💬 {conversation.request_id} · {name}"
        conversation.last_resident_message_id = message.message_id
        if text:
            conversation.resident_texts.append(text if not photo else f"[Photo] {text}")
        elif photo:
            conversation.resident_texts.append("[Photo]")
        self.save(conversation.key, conversation)
        if self.staff_chat_id is None:
            return
        try:
            if photo:
                sent = await bot.copy_message(
                    self.staff_chat_id,
                    update.effective_chat.id,
                    message.message_id,
                    caption=f"{label}: {text}" if text else label,
                    reply_to_message_id=conversation.staff_message_id,
                )
            else:
                sent = await bot.send_message(
                    self.staff_chat_id,
                    f"{label}: {text}",
                    reply_to_message_id=conversation.staff_message_id,
                )
            self.staff_threads[sent.message_id] = conversation.key
        except TelegramError as exc:
            log.warning("Could not forward a message to staff: %s", type(exc).__name__)

    def staff_target(self, replied: Message) -> Key | None:
        """Which resident a staff reply is for: the card it replies to, or its request ID."""
        if replied.message_id in self.staff_threads:
            return self.staff_threads[replied.message_id]
        found = re.search(r"\bR-[0-9A-F]{6}\b", replied.text or replied.caption or "")
        if not found:
            return None
        request_id = found.group()
        for key, conversation in self.conversations.items():
            if conversation.request_id == request_id:
                return key
        record = self.store.get(request_id) if self.store else None
        # After a restart only private chats can be reached: their key is the chat ID.
        if record is not None and record.chat_id > 0:
            return record.chat_id
        return None

    async def staff_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A staff member replied to a request card (or a forwarded message) in the staff
        group: send it to the resident and take the conversation over from the AI."""
        message = update.effective_message
        replied = message.reply_to_message
        bot = context.bot
        if replied is None or replied.from_user is None or replied.from_user.id != bot.id:
            return  # staff talking among themselves
        key = self.staff_target(replied)
        if key is None:
            await message.reply_text(STAFF_UNKNOWN)
            return
        if not message.text:
            await message.reply_text(STAFF_TEXT_ONLY)
            return
        async with self.lock(key):
            conversation = self.conversations.get(key)
            staff_name = message.from_user.first_name if message.from_user else "Staff"
            try:
                await bot.send_message(
                    chat_of(key),
                    STAFF_MESSAGE.format(name=staff_name, text=message.text),
                    reply_to_message_id=(
                        conversation.last_resident_message_id
                        if conversation is not None and not isinstance(key, int)
                        else None
                    ),
                )
            except TelegramError as exc:
                log.warning("Could not deliver a staff reply: %s", type(exc).__name__)
                await message.reply_text(STAFF_UNDELIVERED)
                return
            self.staff_threads[message.message_id] = key
            await self.react(bot, self.staff_chat_id, message.message_id, "delivered")
            if conversation is None:
                return
            log.info("Staff replied on %s", conversation.request_id)
            if not conversation.human_active:
                conversation.human_active = True
                conversation.human_requested = True
                conversation.status = "with_staff"
                await self.clear_buttons(bot, chat_of(key), conversation)
                self.save(key, conversation)
                await self.post_card(bot, conversation)

    async def staff_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/done as a reply to a request card: hand the resident back to the AI."""
        message = update.effective_message
        replied = message.reply_to_message
        bot = context.bot
        if replied is None or replied.from_user is None or replied.from_user.id != bot.id:
            await message.reply_text("Reply /done to the request card you're finishing.")
            return
        key = self.staff_target(replied)
        conversation = self.conversations.get(key) if key is not None else None
        if conversation is None:
            await message.reply_text(STAFF_UNKNOWN)
            return
        async with self.lock(key):
            conversation.status = "resolved"
            conversation.human_active = False
            self.save(key, conversation)
            await self.post_card(bot, conversation)
            try:
                await bot.send_message(
                    chat_of(key), HANDED_BACK.format(request_id=conversation.request_id)
                )
            except TelegramError as exc:
                log.warning("Could not tell the resident: %s", type(exc).__name__)
            await self.react(bot, self.staff_chat_id, message.message_id, "delivered")
            log.info("Staff finished %s", conversation.request_id)
            self.conversations.pop(key, None)

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
        key = self.key(update)
        bot = context.bot
        conversation = self.conversation(key)
        if photo is not None:
            conversation.pending_photos.append(photo)
        user = update.effective_user
        conversation.resident = resident_label(user.full_name, user.username, user.id)
        conversation.last_resident_message_id = message.message_id
        previous = conversation.last_result
        try:
            # The classifier and Claude client are synchronous.
            reply = await asyncio.to_thread(self.reply_for, key, text)
        except Exception:
            log.exception("Reply failed")
            reply = UNAVAILABLE
        conversation = self.conversations.get(key)
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
        key = key_of(query.message.chat, query.from_user)
        bot = context.bot
        async with self.lock(key):
            conversation = self.conversations.get(key)
            if conversation is None or conversation.request_id != request_id:
                if any(c.request_id == request_id for c in self.conversations.values()):
                    # In a group, someone else tapped another person's buttons.
                    await query.answer(NOT_FOR_YOU)
                    return
                await query.answer(STALE)
                await query.edit_message_reply_markup(reply_markup=None)
                return
            if action == "submit" and conversation.status != "ready":
                await query.answer(NOT_READY)
                return
            await query.answer()
            await self.apply(bot, chat_id, key, conversation, action, query.message.reply_text)

    async def apply(
        self, bot: Bot, chat_id: int, key: Key, conversation: Conversation, action, send
    ):
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
        self.save(key, conversation)
        await self.clear_buttons(bot, chat_id, conversation)
        await send(reply)
        await self.react(bot, chat_id, conversation.last_resident_message_id, conversation.status)
        # Asking for a person posts a fresh card so it notifies staff.
        await self.notify_staff(bot, conversation, fresh=action == "human")
        if action in {"submit", "cancel"}:
            self.conversations.pop(key, None)

    async def human(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        key = self.key(update)
        message = update.effective_message
        async with self.lock(key):
            conversation = self.conversations.get(key)
            if conversation is None or conversation.last_result is None:
                await message.reply_text(HUMAN_FIRST)
                return
            if conversation.human_requested:
                await message.reply_text(HUMAN_ALREADY)
                return
            await self.apply(
                context.bot,
                update.effective_chat.id,
                key,
                conversation,
                "human",
                message.reply_text,
            )

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

    def application(self, token: str, group_replies: bool = False) -> Application:
        app = Application.builder().token(token).post_init(self.set_commands).build()
        new_messages = filters.UpdateType.MESSAGE & ~filters.IS_AUTOMATIC_FORWARD
        private = filters.ChatType.PRIVATE & new_messages
        commands = private
        if group_replies:
            # Test groups: answer only when mentioned or replied to. Never the staff group.
            groups = filters.ChatType.GROUPS & new_messages
            if self.staff_chat_id is not None:
                groups &= ~filters.Chat(self.staff_chat_id)
            commands = private | groups
            media = filters.TEXT | filters.PHOTO | filters.Document.IMAGE
            app.add_handler(MessageHandler(groups & media & ~filters.COMMAND, self.group))
            app.add_handler(CommandHandler("ask", self.ask, filters=groups))
        app.add_handler(CommandHandler(["start", "help"], self.start, filters=commands))
        app.add_handler(CommandHandler("new", self.new, filters=commands))
        app.add_handler(CommandHandler(["human", "person"], self.human, filters=commands))
        app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, self.text))
        app.add_handler(
            MessageHandler(private & (filters.PHOTO | filters.Document.IMAGE), self.photo)
        )
        app.add_handler(MessageHandler(private & ~filters.COMMAND, self.other))
        app.add_handler(CallbackQueryHandler(self.button, pattern=r"^(submit|cancel|human):"))
        if self.staff_chat_id is not None:
            # Staff reply to a request card to message the resident; /done hands back to the AI.
            staff = filters.Chat(self.staff_chat_id) & filters.UpdateType.MESSAGE & filters.REPLY
            app.add_handler(CommandHandler("done", self.staff_done, filters=staff))
            app.add_handler(MessageHandler(staff & ~filters.COMMAND, self.staff_reply))
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
    if os.getenv("GROUP_REPLIES", "off") not in {"on", "off"}:
        raise SystemExit("GROUP_REPLIES must be on or off.")
    limit = os.getenv("BOT_TO_BOT_LIMIT", "3")
    if not limit.isdigit():
        raise SystemExit("BOT_TO_BOT_LIMIT must be a whole number (0 ignores other bots).")
    log.info("Classifier backend: %s. Replies: %s.", backend_name, replies)
    log.info("Saving requests to %s", store.path)
    return ResidentBot(
        Classifier(backend),
        chat,
        int(staff_chat_id) if staff_chat_id else None,
        store,
        describer,
        int(limit),
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
    group_replies = os.getenv("GROUP_REPLIES", "off") == "on"
    if group_replies:
        log.info("Group replies on: answering in groups only when mentioned or replied to.")
    bot.application(token, group_replies).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
