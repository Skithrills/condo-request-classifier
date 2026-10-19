"""Resident-facing Telegram bot.

Run with `run_bot.cmd` (Windows), `./run_bot.sh` (macOS/Linux) or
`uv run --extra telegram python -m condo_classifier.telegram_bot`. Message text and audio are
never logged or saved; only the outcome is. Conversations live in memory and reset on restart.
"""

import asyncio
import logging
import os
import time

from dotenv import load_dotenv
from pydantic import ValidationError
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
    User,
)
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
    ResidentMessage,
)
from condo_classifier.language import TranslationError, TranslatorChain, translator_from_env
from condo_classifier.replies import (
    CHOOSE_LANGUAGE,
    HEARD,
    HEARD_HINT,
    INVALID,
    LANGUAGE_SET,
    NEW_REQUEST,
    TEXT_ONLY,
    TEXT_OR_VOICE,
    UNAVAILABLE,
    VOICE_FAILED,
    VOICE_TOO_LONG,
    WELCOME,
    resident_reply,
)
from condo_classifier.schema import ResidentRequest
from condo_classifier.service import Classifier
from condo_classifier.staff import new_request_id, resident_label, staff_card
from condo_classifier.voice import VOICE_LANGUAGES, TranscriberChain, transcriber_from_env

log = logging.getLogger(__name__)

MAX_VOICE_SECONDS = 120


class ResidentBot:
    def __init__(
        self,
        classifier: Classifier,
        chat: ResidentChat | None = None,
        staff_chat_id: int | None = None,
        translator: TranslatorChain | None = None,
        transcriber: TranscriberChain | None = None,
    ):
        self.classifier = classifier
        self.chat = chat
        self.staff_chat_id = staff_chat_id
        self.translator = translator
        self.transcriber = transcriber
        self.conversations: dict[int, Conversation] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        # Voice language each resident picked with /language. Survives /new, not restarts.
        self.voice_languages: dict[int, str] = {}

    def conversation(self, chat_id: int) -> Conversation:
        conversation = self.conversations.get(chat_id)
        now = time.monotonic()
        if conversation is None or now - conversation.last_seen > IDLE_RESET_SECONDS:
            conversation = self.conversations[chat_id] = Conversation()
        conversation.last_seen = now
        return conversation

    def english_for(self, conversation: Conversation, text: str) -> str:
        """Translate for the classifier. On failure, classify the words as written."""
        if self.translator is None:
            return text
        try:
            translation = self.translator.translate(text)
            ResidentRequest(text=translation.english, channel="Telegram")
        except (TranslationError, ValidationError):
            conversation.translation_failed = True
            return text
        if translation.translated:
            conversation.language = translation.language
        return translation.english

    def reply_for(self, chat_id: int, text: str, voice: bool = False) -> str:
        try:
            ResidentRequest(text=text, channel="Telegram")
        except ValidationError:
            return INVALID
        conversation = self.conversation(chat_id)
        english = self.english_for(conversation, text)
        conversation.said.append(ResidentMessage(text, english, voice))
        # Classify the whole conversation so details sent later fill in what was missing.
        request = ResidentRequest(
            text=conversation.request_text(),
            original_text=conversation.original_text(),
            channel="Telegram",
        )
        result = self.classifier.classify(request)
        conversation.last_result = result
        log.info(
            "classified category=%s priority=%s status=%s language=%s voice=%s",
            result.category,
            result.priority,
            result.status,
            conversation.language or "en",
            voice,
        )
        reply = None
        if result.status == "safety_escalation" and not conversation.escalated:
            # Emergencies always get the fixed safety text, never generated wording.
            conversation.escalated = True
        elif self.chat and result.status != "provider_error":
            # Claude sees the resident's own words so it replies in their language.
            reply = self.chat.reply(conversation, text, result)
        if reply is None:
            reply = resident_reply(result, conversation.language)
            conversation.messages += [
                {"role": "user", "content": text},
                {"role": "assistant", "content": reply},
            ]
        return reply

    async def notify_staff(
        self, bot: Bot, chat_id: int, user: User, voice_file_id: str | None = None
    ) -> None:
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
            conversation.said,
            language=conversation.language,
            translation_failed=conversation.translation_failed,
        )
        # A first emergency gets a fresh message so it notifies; otherwise edit the card.
        urgent = result.status == "safety_escalation" and not conversation.staff_alerted
        try:
            await self.post_card(bot, conversation, card, urgent)
            if voice_file_id:
                # Staff can listen to the original when the transcript looks wrong.
                await bot.send_voice(
                    self.staff_chat_id,
                    voice_file_id,
                    caption=f"🎙️ {conversation.request_id}",
                    reply_parameters=ReplyParameters(
                        conversation.staff_message_id, allow_sending_without_reply=True
                    ),
                )
        except TelegramError as exc:
            log.warning("Could not post to the staff chat: %s", type(exc).__name__)

    async def post_card(
        self, bot: Bot, conversation: Conversation, card: str, urgent: bool
    ) -> None:
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

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(chat_id, asyncio.Lock())

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.conversations.pop(update.effective_chat.id, None)
        await update.effective_message.reply_text(WELCOME)

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.conversations.pop(update.effective_chat.id, None)
        await update.effective_message.reply_text(NEW_REQUEST)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        # One message at a time per chat so the conversation history stays in order.
        async with self.lock(chat_id):
            await context.bot.send_chat_action(chat_id, "typing")
            await self.respond(update, context, update.effective_message.text)

    async def voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat_id = update.effective_chat.id
        voice = message.voice
        duration = voice.duration
        seconds = duration.total_seconds() if hasattr(duration, "total_seconds") else duration
        if seconds is not None and seconds < 1:
            # Usually an accidental tap; Whisper tends to invent words from near-silence.
            await message.reply_text(VOICE_FAILED)
            return
        if seconds and seconds > MAX_VOICE_SECONDS:
            await message.reply_text(VOICE_TOO_LONG.format(seconds=MAX_VOICE_SECONDS))
            return
        language = self.voice_languages.get(chat_id)
        async with self.lock(chat_id):
            await context.bot.send_chat_action(chat_id, "typing")
            try:
                file = await voice.get_file()
                audio = bytes(await file.download_as_bytearray())
                transcript = await asyncio.to_thread(self.transcriber.transcribe, audio, language)
            except Exception as exc:
                log.warning("Voice message failed: %s", type(exc).__name__)
                await message.reply_text(VOICE_FAILED)
                return
            transcript = transcript[:4000]
            # Show what was heard so the resident can correct a misheard unit number.
            heard = HEARD.format(text=transcript)
            await message.reply_text(heard if language else heard + HEARD_HINT)
            await self.respond(update, context, transcript, voice.file_id)

    async def language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Let the resident fix the voice language, since Whisper's guess can be wrong."""
        buttons = [[InlineKeyboardButton("Auto-detect", callback_data="lang:auto")]] + [
            [InlineKeyboardButton(name, callback_data=f"lang:{code}")]
            for code, name in VOICE_LANGUAGES.items()
        ]
        await update.effective_message.reply_text(
            CHOOSE_LANGUAGE, reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def set_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        code = query.data.removeprefix("lang:")
        chat_id = update.effective_chat.id
        if code in VOICE_LANGUAGES:
            self.voice_languages[chat_id] = code
        else:
            self.voice_languages.pop(chat_id, None)
        await query.answer()
        await query.edit_message_text(
            LANGUAGE_SET.format(language=VOICE_LANGUAGES.get(code, "auto-detect"))
        )

    async def respond(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
        voice_file_id: str | None = None,
    ) -> None:
        message = update.effective_message
        chat_id = update.effective_chat.id
        conversation = self.conversations.get(chat_id)
        previous = conversation.last_result if conversation else None
        try:
            # The classifier, translators and Claude client are synchronous.
            reply = await asyncio.to_thread(
                self.reply_for, chat_id, text, voice_file_id is not None
            )
        except Exception:
            log.exception("Reply failed")
            reply = UNAVAILABLE
        await message.reply_text(reply)
        conversation = self.conversations.get(chat_id)
        if conversation and conversation.last_result is not previous:
            await self.notify_staff(context.bot, chat_id, update.effective_user, voice_file_id)

    async def other(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(TEXT_OR_VOICE if self.transcriber else TEXT_ONLY)

    async def chat_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Setup helper: send /chatid in the staff group or channel to get STAFF_CHAT_ID."""
        await update.effective_message.reply_text(
            f"This chat's ID is {update.effective_chat.id}. "
            "Put it in .env as STAFF_CHAT_ID to send resident requests here."
        )

    def application(self, token: str) -> Application:
        app = Application.builder().token(token).build()
        # Residents only talk to the bot in private chats; groups are for staff.
        private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
        app.add_handler(CommandHandler(["start", "help"], self.start, filters=private))
        app.add_handler(CommandHandler("new", self.new, filters=private))
        app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, self.text))
        if self.transcriber:
            app.add_handler(CommandHandler("language", self.language, filters=private))
            app.add_handler(CallbackQueryHandler(self.set_language, pattern=r"^lang:"))
            app.add_handler(MessageHandler(private & filters.VOICE, self.voice))
        app.add_handler(MessageHandler(private & ~filters.COMMAND, self.other))
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
        translator = translator_from_env()
        transcriber = transcriber_from_env()
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
    if transcriber and not translator:
        log.warning(
            "TRANSCRIBE_WITH is set but TRANSLATE_WITH is not, so voice messages in other "
            "languages reach the English classifier untranslated."
        )
    log.info(
        "Classifier backend: %s. Replies: %s. Translation: %s. Voice: %s.",
        backend_name,
        replies,
        os.getenv("TRANSLATE_WITH") or "off",
        os.getenv("TRANSCRIBE_WITH") or "off",
    )
    return ResidentBot(
        Classifier(backend),
        chat,
        int(staff_chat_id) if staff_chat_id else None,
        translator,
        transcriber,
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
