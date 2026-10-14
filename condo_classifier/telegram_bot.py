"""Resident-facing Telegram bot.

Run with `uv run --extra telegram python -m condo_classifier.telegram_bot` after setting
TELEGRAM_BOT_TOKEN. Message text is never logged; only the outcome is.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from pydantic import ValidationError
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from condo_classifier.backends import ROOT, backend_from_name
from condo_classifier.replies import INVALID, TEXT_ONLY, UNAVAILABLE, WELCOME, resident_reply
from condo_classifier.schema import ResidentRequest
from condo_classifier.service import Classifier

log = logging.getLogger(__name__)


class ResidentBot:
    def __init__(self, classifier: Classifier):
        self.classifier = classifier

    def reply_for(self, text: str) -> str:
        try:
            request = ResidentRequest(text=text, channel="Telegram")
        except ValidationError:
            return INVALID
        result = self.classifier.classify(request)
        log.info(
            "classified category=%s priority=%s status=%s",
            result.category,
            result.priority,
            result.status,
        )
        return resident_reply(result)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(WELCOME)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        try:
            # The classifier is synchronous (scikit-learn or a blocking Ollama call).
            reply = await asyncio.to_thread(self.reply_for, message.text)
        except Exception:
            log.exception("Classification failed")
            reply = UNAVAILABLE
        await message.reply_text(reply)

    async def other(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(TEXT_ONLY)

    def application(self, token: str) -> Application:
        app = Application.builder().token(token).build()
        app.add_handler(CommandHandler(["start", "help"], self.start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text))
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, self.other))
        return app


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level="INFO")
    # httpx logs every request URL, which includes the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (create a bot with @BotFather).")
    backend_name = os.getenv("CLASSIFIER_BACKEND", "baseline")
    try:
        backend = backend_from_name(backend_name)
    except ValueError as exc:
        raise SystemExit(f"CLASSIFIER_BACKEND: {exc}") from exc
    bot = ResidentBot(Classifier(backend))
    log.info("Starting Telegram bot with the %s backend", backend_name)
    bot.application(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
