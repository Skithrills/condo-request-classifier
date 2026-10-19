#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi
if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env. Fill in TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY, then run ./run_bot.sh again."
    exit 0
fi
extras="--extra telegram"
if grep -Eq '^TRANSCRIBE_WITH=.*local' .env; then
    extras="$extras --extra voice-local"
fi
exec uv run --locked $extras python -m condo_classifier.telegram_bot
