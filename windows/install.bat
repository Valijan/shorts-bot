@echo off
chcp 65001 >nul
title Установка Shorts Bot
echo Проверяю Python 3.12...
py -3.12 -V >nul 2>&1
if errorlevel 1 py install 3.12
py -3.12 -V >nul 2>&1
if errorlevel 1 winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
py -3.12 -V >nul 2>&1
if errorlevel 1 (echo Не удалось поставить Python 3.12. Сфотографируй окно и пришли Claude. & pause & exit /b 1)
py -3.12 -c "import urllib.request;urllib.request.urlretrieve('https://raw.githubusercontent.com/Valijan/shorts-bot/main/windows/setup.py', r'%TEMP%\shorts_setup.py')"
py -3.12 "%TEMP%\shorts_setup.py"
pause
