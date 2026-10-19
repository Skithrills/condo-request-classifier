@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env. Fill in TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY, save it, then run this again.
    notepad ".env"
    exit /b 0
)
set "EXTRAS=--extra telegram"
findstr /R /C:"^TRANSCRIBE_WITH=.*local" ".env" >nul && set "EXTRAS=--extra telegram --extra voice-local"
uv run --locked %EXTRAS% python -m condo_classifier.telegram_bot
if errorlevel 1 pause
