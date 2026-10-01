@echo off
rem Avvia il monitor in questa finestra (Ctrl+C per fermarlo). Log anche in data\drop-monitor.log
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Esegui prima setup-windows.bat
    pause & exit /b 1
)
title drop-monitor
.venv\Scripts\python.exe -m drop_monitor -c config.yaml run %*
echo.
echo Monitor terminato.
pause
