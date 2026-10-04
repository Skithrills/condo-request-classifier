@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run uv sync --locked in this folder first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m streamlit run app.py
