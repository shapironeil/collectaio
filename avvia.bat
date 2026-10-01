@echo off
rem Avvia SOLO la finestra di controllo con il monitor integrato (nessun controllo di setup).
rem Funziona sia dentro la cartella drop-monitor sia accanto a setup-windows.bat.
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%drop_monitor\cli.py" (set "ROOT=%ROOT%") else if exist "%ROOT%drop-monitor\drop_monitor\cli.py" (set "ROOT=%ROOT%drop-monitor\") else (
    echo Installazione non trovata: esegui prima setup-windows.bat
    pause & exit /b 1
)
cd /d "%ROOT%"
set "PYEXE=.venv\Scripts\python.exe"
if exist "portable\python\python.exe" set "PYEXE=portable\python\python.exe"
title collectaio
"%PYEXE%" -m drop_monitor -c config.yaml app --autostart %*
if errorlevel 1 pause
