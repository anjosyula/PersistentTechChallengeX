@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: DEEP_CHECK_V20_DATA.bat "C:\path\to\data_v18_full"
  pause
  exit /b 2
)
python tools\deep_check_v20_data.py "%~1"
pause
