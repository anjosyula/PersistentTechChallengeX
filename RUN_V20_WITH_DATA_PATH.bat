@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: RUN_V20_WITH_DATA_PATH.bat "C:\path\to\data_v18_full"
  pause
  exit /b 2
)
set "REFLECT_V18_2_DATA=%~1"
if not defined REFLECT_DEMO_HOURLY_RATE set "REFLECT_DEMO_HOURLY_RATE=120"
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail
python tools\check_v20_data.py "%REFLECT_V18_2_DATA%"
if errorlevel 1 goto :fail
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8020'"
echo Starting V20 with: %REFLECT_V18_2_DATA%
python server.py
exit /b %errorlevel%
:fail
echo V20 startup failed.
pause
exit /b 1
