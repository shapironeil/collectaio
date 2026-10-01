@echo off
rem Test completo senza inviare notifiche: test unitari + una scansione reale del sito in dry-run.
setlocal
cd /d "%~dp0.."
set "PYEXE=.venv\Scripts\python.exe"
if exist "portable\python\python.exe" set "PYEXE=portable\python\python.exe"
if not exist "%PYEXE%" (
    echo Esegui prima setup-windows.bat
    pause & exit /b 1
)
echo === test unitari (offline)
"%PYEXE%" -m pytest -q
echo.
echo === scansione reale in dry-run (nessun messaggio Telegram)
"%PYEXE%" -m drop_monitor -c config.yaml once --dry-run --delay 3
echo.
echo === stato salvato
"%PYEXE%" -m drop_monitor -c config.yaml status
echo.
echo Per provare Telegram:  "%PYEXE%" -m drop_monitor -c config.yaml test-telegram
pause
