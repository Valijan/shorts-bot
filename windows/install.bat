@echo off
chcp 65001 >nul
title Shorts Bot setup
echo Checking Python 3.12...
call :check
if not errorlevel 1 goto have
echo Installing Python 3.12 (1-3 min)...
py install -y 3.12
call :check
if not errorlevel 1 goto have
py install 3.12
call :check
if not errorlevel 1 goto have
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
call :check
if not errorlevel 1 goto have
echo Python 3.12 install failed. Take a photo of this window and send it to Claude.
pause
exit /b 1
:have
echo Python 3.12 OK
py -V:3.12 -c "import urllib.request;urllib.request.urlretrieve('https://raw.githubusercontent.com/Valijan/shorts-bot/main/windows/setup.py', r'%TEMP%\shorts_setup.py')"
py -V:3.12 "%TEMP%\shorts_setup.py"
pause
exit /b 0
:check
py -V:3.12 -c "import sys;print(sys.version)" 2>nul | findstr /b "3.12" >nul
exit /b %errorlevel%
