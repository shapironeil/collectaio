@echo off
rem Registra (o rimuove con "autostart.bat remove") un'attivita' pianificata che avvia
rem il monitor in background a ogni accesso di questo utente. Log in data\drop-monitor.log
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"
set "TASK=drop-monitor"
if /i "%~1"=="remove" (
    schtasks /Delete /TN "%TASK%" /F
    goto :done
)
if not exist "%ROOT%\.venv\Scripts\pythonw.exe" (
    echo Esegui prima setup-windows.bat
    pause & exit /b 1
)
schtasks /Create /TN "%TASK%" /SC ONLOGON /RL LIMITED /F ^
  /TR "\"%ROOT%\.venv\Scripts\pythonw.exe\" -m drop_monitor -c \"%ROOT%\config.yaml\" run"
if errorlevel 1 (echo Creazione attivita' fallita & pause & exit /b 1)
echo Attivita' "%TASK%" creata: parte a ogni accesso. Avvio adesso...
schtasks /Run /TN "%TASK%"
echo Per fermarla: schtasks /End /TN "%TASK%"   -   per rimuoverla: autostart.bat remove
:done
pause
