@echo off
setlocal
cd /d "%~dp0"
set "DROP=%~dp0data_v18_2"
set "DATA="
if exist "%DROP%\core\reflect_core_v13.db" if exist "%DROP%\billing_master_v18_2\reflect_billing_core_v18_2.db" set "DATA=%DROP%"
if not defined DATA if exist "%DROP%\data_v18_full\core\reflect_core_v13.db" if exist "%DROP%\data_v18_full\billing_master_v18_2\reflect_billing_core_v18_2.db" set "DATA=%DROP%\data_v18_full"
if not defined DATA if exist "%DROP%\data_v13\core\reflect_core_v13.db" if exist "%DROP%\data_v13\billing_master_v18_2\reflect_billing_core_v18_2.db" set "DATA=%DROP%\data_v13"
if not defined DATA (
  echo ================================================================
  echo V20 full V18.2 data was not found in data_v18_2.
  echo Starting with bundled sample data instead.
  echo See START_HERE.txt for the full-data folder layout.
  echo ================================================================
  set "DATA=%~dp0data_sample_v18_2"
)
set "REFLECT_V18_2_DATA=%DATA%"
if not defined REFLECT_DEMO_HOURLY_RATE set "REFLECT_DEMO_HOURLY_RATE=120"
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail
python tools\check_v20_data.py "%DATA%"
if errorlevel 1 goto :fail
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8020'"
echo.
echo ================================================================
echo Reflect Provider Dashboard V20
echo Data root: %DATA%
echo URL: http://127.0.0.1:8020
echo Synthetic demo rate: $%REFLECT_DEMO_HOURLY_RATE% per provider hour
echo Keep this window open while using the dashboard.
echo ================================================================
python server.py
exit /b %errorlevel%
:fail
echo.
echo V20 startup failed. Review the messages above.
pause
exit /b 1
