@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)
echo Opening the staff dashboard at http://localhost:8502 ...
uv run --locked --extra telegram streamlit run dashboard.py --server.port 8502
if errorlevel 1 pause
