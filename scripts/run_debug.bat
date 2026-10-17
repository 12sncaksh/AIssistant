@echo off

cd /d "%~dp0.."

call conda activate ai_assistant
rem 限制 OpenBLAS 线程数，避免每个 python 进程白占约 600MB 提交内存（详见 run.bat）。
set OPENBLAS_NUM_THREADS=1
python src\main.py --debug
pause
