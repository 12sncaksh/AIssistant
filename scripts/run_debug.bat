@echo off

cd /d "%~dp0.."

call conda activate ai_assistant
python src\main.py
pause
