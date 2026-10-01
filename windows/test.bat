@echo off
rem Test completo senza inviare notifiche: test unitari + una scansione reale del sito in dry-run.
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Esegui prima setup-windows.bat
    pause & exit /b 1
)
echo === test unitari (offline)
.venv\Scripts\python.exe -m pytest -q
echo.
echo === scansione reale in dry-run (nessun messaggio Telegram)
.venv\Scripts\python.exe -m drop_monitor -c config.yaml once --dry-run --delay 3
echo.
echo === stato salvato
.venv\Scripts\python.exe -m drop_monitor -c config.yaml status
echo.
echo Per provare Telegram:  .venv\Scripts\python.exe -m drop_monitor -c config.yaml test-telegram
pause
