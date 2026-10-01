@echo off
rem Apre la finestra di controllo (stile glass) nel browser: http://127.0.0.1:8765
setlocal
cd /d "%~dp0.."
set "PYEXE=.venv\Scripts\python.exe"
if exist "portable\python\python.exe" set "PYEXE=portable\python\python.exe"
if not exist "%PYEXE%" (
    echo Esegui prima setup-windows.bat
    pause & exit /b 1
)
title drop-monitor ui
"%PYEXE%" -m drop_monitor -c config.yaml ui %*
pause
