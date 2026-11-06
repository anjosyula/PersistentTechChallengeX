@echo off
setlocal
cd /d "%~dp0"
set "DATA=%~1"
if not defined DATA set "DATA=%~dp0data_v18_2"
python tools\check_v20_data.py "%DATA%"
pause
