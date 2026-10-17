@echo off
if /i "%~1"=="__hidden__" goto run_hidden

wscript.exe "%~dp0run_hidden.vbs" "%~f0"
exit /b

:run_hidden
cd /d "%~dp0.."
call conda activate ai_assistant
rem 限制 OpenBLAS 线程数：numpy 默认按 CPU 核心数（本机 20）预留线程缓冲，
rem 每个 python 进程会白占约 600MB 提交内存；本项目的 numpy 只用于向量打分，单线程够用。
set OPENBLAS_NUM_THREADS=1
python src\main.py
