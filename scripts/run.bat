@echo off
if /i "%~1"=="__hidden__" goto run_hidden

wscript.exe "%~dp0run_hidden.vbs" "%~f0"
exit /b

:run_hidden
cd /d "%~dp0.."
call conda activate ai_assistant
python src\main.py
