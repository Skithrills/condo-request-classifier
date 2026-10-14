"""Resident-facing Telegram bot.

Run with `run_bot.cmd` (Windows), `./run_bot.sh` (macOS/Linux) or
`uv run --extra telegram python -m condo_classifier.telegram_bot`. Message text is never
logged; only the outcome is. Conversations live in memory and reset on restart.
"""

import asyncio
import logging
import os
import time

from dotenv import load_dotenv
from pydantic import ValidationError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from condo_classifier.backends import ROOT, backend_from_name
from condo_classifier.chat import IDLE_RESET_SECONDS, Conversation, ResidentChat
from condo_classifier.replies import (
    INVALID,
    NEW_REQUEST,
    TEXT_ONLY,
    UNAVAILABLE,
    WELCOME,
    resident_reply,
)
from condo_classifier.schema import ResidentRequest
from condo_classifier.service import Classifier

log = logging.getLogger(__name__)


class ResidentBot:
    def __init__(self, classifier: Classifier, chat: ResidentChat | None = None):
        self.classifier = classifier
        self.chat = chat
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
            try:
                # The classifier and Claude client are synchronous.
                reply = await asyncio.to_thread(self.reply_for, chat_id, message.text)
            except Exception:
                log.exception("Reply failed")
                reply = UNAVAILABLE
        await message.reply_text(reply)

    async def other(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(TEXT_ONLY)

    def application(self, token: str) -> Application:
        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler(["start", "help"], self.start))
        app.add_handler(CommandHandler("new", self.new))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text))
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, self.other))
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
    log.info("Classifier backend: %s. Replies: %s.", backend_name, replies)
    return ResidentBot(Classifier(backend), chat)


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
