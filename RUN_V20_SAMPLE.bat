@echo off
setlocal
cd /d "%~dp0"
set "REFLECT_V18_2_DATA=%~dp0data_sample_v18_2"
if not defined REFLECT_DEMO_HOURLY_RATE set "REFLECT_DEMO_HOURLY_RATE=120"
python -m pip install -r requirements.txt
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8020'"
python server.py
