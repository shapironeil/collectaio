@echo off
rem ============================================================================
rem  drop-monitor - installazione / aggiornamento su Windows
rem
rem  Uso:  setup-windows.bat [cartella_installazione]
rem        setup-windows.bat check     -> solo controllo: versione installata,
rem                                       versione disponibile, residui; non modifica nulla
rem        Senza argomenti installa TUTTO in una sola cartella accanto a questo
rem        file: <cartella del bat>\drop-monitor  (codice, runtime portable,
rem        dipendenze, dati, backup, file temporanei). Se il bat sta gia' dentro
rem        un'installazione, aggiorna quella. Niente viene scritto altrove.
rem
rem  Cosa fa, ogni volta che lo lanci:
rem   0. pulisce i residui di esecuzioni interrotte (temporanei, backup vuoti) e
rem      cerca una vecchia installazione in %USERPROFILE%\drop-monitor: i dati
rem      personali vengono migrati qui, il resto rimosso
rem   1. controlla Python 3.11+ (prova a installarlo con winget se manca)
rem   2. controlla git (opzionale: senza git scarica lo zip da GitHub)
rem   3. fa un backup dei file personali in _backup\<data-ora>\
rem   4. scarica/aggiorna il codice dal branch indicato sotto
rem      PRESERVANDO: config.yaml, .env, data\, personal\, _backup\, .venv\
rem   5. crea/aggiorna l'ambiente Python (.venv) e le dipendenze
rem   6. crea config.yaml / .env / personal\order-profile.yaml dai template se mancano
rem   7. esegue i test offline e un self-test del parser
rem
rem  Runtime PORTABLE (nessuna installazione di sistema, tutto in <cartella>\portable\):
rem    Python 3.12 embeddable  sempre (python.org) + pip        -> portable\python
rem    MinGit                  solo se git non e' nel PATH       -> portable\git
rem    Node.js (LTS)           opzionale, PORTABLE_NODE=1       -> portable\node
rem      (drop-monitor e' solo Python: Node non serve oggi; e' qui pronto per
rem       componenti futuri, con verifica SHA256 ufficiale di nodejs.org)
rem  Ogni download viene controllato (dimensione, eseguibile che risponde,
rem  SHA256 dove il produttore lo pubblica) prima di essere usato.
rem
rem  Variabili opzionali (impostale prima di lanciare, o in setup.local.bat):
rem    DROP_MONITOR_BRANCH   branch da scaricare (default sotto)
rem    GITHUB_TOKEN          token GitHub: OBBLIGATORIO finche' il repository e' privato
rem                          (fine-grained, permesso "Contents: Read" sul repo). Il bat lo
rem                          chiede una volta e lo salva in <cartella>\setup.local.bat
rem    PORTABLE_PYTHON=0     usa il Python di sistema (venv) invece del portable
rem    PORTABLE_NODE=1       scarica anche Node.js portable
rem ============================================================================
rem Rilancia se stesso in una finestra che resta aperta anche in caso di errore (cmd /k).
if not defined DROP_SETUP_INNER (
    set "DROP_SETUP_INNER=1"
    start "drop-monitor setup" cmd /k ""%~f0" %*"
    exit /b 0
)
setlocal EnableExtensions EnableDelayedExpansion
title drop-monitor setup

set "REPO=shapironeil/collectaio"
set "BRANCH=claude/quirky-ramanujan-1kvhs9"
if exist "%~dp0setup.local.bat" call "%~dp0setup.local.bat"
if not "%DROP_MONITOR_BRANCH%"=="" set "BRANCH=%DROP_MONITOR_BRANCH%"
set "PY_MIN_MINOR=11"
if not defined PORTABLE_PYTHON set "PORTABLE_PYTHON=1"
if not defined PORTABLE_NODE set "PORTABLE_NODE=0"
set "PY_VER=3.12.7"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-embed-amd64.zip"
set "PIP_URL=https://bootstrap.pypa.io/get-pip.py"
set "GIT_VER=2.47.1"
set "GIT_URL=https://github.com/git-for-windows/git/releases/download/v%GIT_VER%.windows.1/MinGit-%GIT_VER%-64-bit.zip"
set "NODE_VER=22.11.0"
set "NODE_URL=https://nodejs.org/dist/v%NODE_VER%/node-v%NODE_VER%-win-x64.zip"
set "NODE_SHA_URL=https://nodejs.org/dist/v%NODE_VER%/SHASUMS256.txt"
set "PRESERVE_DIRS=data personal _backup .venv .git portable _tmp"
set "PRESERVE_FILES=config.yaml .env setup.local.bat install-info.txt"

rem --- cartella di installazione -------------------------------------------
set "SELF_DIR=%~dp0"
if "%SELF_DIR:~-1%"=="\" set "SELF_DIR=%SELF_DIR:~0,-1%"
set "CHECK_ONLY=0"
if /i "%~1"=="check" set "CHECK_ONLY=1"
if "%CHECK_ONLY%"=="1" (
    set "INSTALL_DIR=%SELF_DIR%\drop-monitor"
    if exist "%SELF_DIR%\drop_monitor\cli.py" set "INSTALL_DIR=%SELF_DIR%"
) else if not "%~1"=="" (
    set "INSTALL_DIR=%~f1"
) else if exist "%SELF_DIR%\drop_monitor\cli.py" (
    set "INSTALL_DIR=%SELF_DIR%"
) else (
    set "INSTALL_DIR=%SELF_DIR%\drop-monitor"
)
if "%INSTALL_DIR:~-1%"=="\" set "INSTALL_DIR=%INSTALL_DIR:~0,-1%"
set "TOKEN_SRC=nessuno"
if exist "%SELF_DIR%\setup.local.bat" set "TOKEN_SRC=%SELF_DIR%\setup.local.bat"
if exist "%INSTALL_DIR%\setup.local.bat" (call "%INSTALL_DIR%\setup.local.bat" & set "TOKEN_SRC=%INSTALL_DIR%\setup.local.bat")
if not "%DROP_MONITOR_BRANCH%"=="" set "BRANCH=%DROP_MONITOR_BRANCH%"
if not defined GITHUB_TOKEN set "TOKEN_SRC=nessuno"
set "GIT_TERMINAL_PROMPT=0"
set "GCM_INTERACTIVE=never"
rem Tutto resta dentro la cartella: temporanei e cache pip compresi.
set "SYS_TEMP=%TEMP%"
set "TEMP=%INSTALL_DIR%\_tmp"
set "TMP=%TEMP%"
set "PIP_CACHE_DIR=%INSTALL_DIR%\portable\pip-cache"
set "STAMP="
for /f %%s in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%s"
if not defined STAMP set "STAMP=%RANDOM%%RANDOM%"
set "BACKUP_DIR=%INSTALL_DIR%\_backup\%STAMP%"

echo.
echo  drop-monitor setup
echo  repo    : https://github.com/%REPO%  (branch %BRANCH%)
echo  cartella: %INSTALL_DIR%
echo.
set "OLD_DIR=%USERPROFILE%\drop-monitor"

rem ============================ 0/7 VERSIONE E PULIZIA =======================
echo [0/7] Stato installazione e pulizia residui ...
if defined GITHUB_TOKEN (echo       token GitHub: caricato da %TOKEN_SRC%) else (echo       token GitHub: nessuno ^(cercato in %SELF_DIR%\setup.local.bat e %INSTALL_DIR%\setup.local.bat^))
set "INSTALLED_VER=-"
set "INSTALLED_SHA=-"
if exist "%INSTALL_DIR%\drop_monitor\__init__.py" (
    for /f "tokens=2 delims== " %%v in ('findstr /c:"__version__" "%INSTALL_DIR%\drop_monitor\__init__.py"') do set "INSTALLED_VER=%%~v"
    if exist "%INSTALL_DIR%\install-info.txt" for /f "tokens=2 delims==" %%v in ('findstr /b "commit=" "%INSTALL_DIR%\install-info.txt"') do set "INSTALLED_SHA=%%v"
    echo       installata: versione !INSTALLED_VER! ^(commit !INSTALLED_SHA!^)
) else (
    echo       nessuna installazione in %INSTALL_DIR%: prima installazione
)
set "REMOTE_VER=-"
set "VER_TMP=%SYS_TEMP%\drop-monitor-version-%STAMP%.py"
call :download "https://api.github.com/repos/%REPO%/contents/drop_monitor/__init__.py?ref=%BRANCH%" "%VER_TMP%" 10 "application/vnd.github.raw" >nul 2>&1
if exist "%VER_TMP%" (
    for /f "tokens=2 delims== " %%v in ('findstr /c:"__version__" "%VER_TMP%"') do set "REMOTE_VER=%%~v"
    del /q "%VER_TMP%" >nul 2>&1
)
if "%CHECK_ONLY%"=="0" call :ensure_token
if "%REMOTE_VER%"=="-" (
    echo       versione disponibile: non verificabile ^(GitHub non raggiungibile o token mancante^)
) else if "%REMOTE_VER%"=="%INSTALLED_VER%" (
    echo       disponibile sul branch %BRANCH%: %REMOTE_VER% ^(stessa versione, verifico comunque il commit^)
) else (
    echo       disponibile sul branch %BRANCH%: %REMOTE_VER%  ^<-- aggiornamento
)
call :cleanup_leftovers
call :migrate_old_install
if "%CHECK_ONLY%"=="1" (
    echo.
    echo  Modalita' check: nessuna modifica eseguita.
    goto :end
)
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%" || goto :fail_mkdir
if not exist "%TEMP%" mkdir "%TEMP%"

rem ============================ 1/7 PYTHON ===================================
set "PORTABLE=%INSTALL_DIR%\portable"
if not exist "%PORTABLE%" mkdir "%PORTABLE%"
set "PY="
set "PY_IS_PORTABLE=0"
if "%PORTABLE_PYTHON%"=="1" (
    echo [1/7] Python portable %PY_VER% in portable\python ...
    call :ensure_portable_python
    if defined PY goto :python_ok
    echo       portable non disponibile: provo il Python di sistema
)
echo [1/7] Controllo Python 3.%PY_MIN_MINOR%+ di sistema ...
call :find_python
if defined PY goto :python_ok
echo       Python 3.%PY_MIN_MINOR%+ non trovato. Provo a installarlo con winget...
where winget >nul 2>&1
if errorlevel 1 goto :python_manual
winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
call :find_python
if defined PY goto :python_ok
:python_manual
echo       Installa Python da https://www.python.org/downloads/windows/
echo       (spunta "Add python.exe to PATH"), poi rilancia questo file.
start "" "https://www.python.org/downloads/windows/"
goto :fail
:python_ok
for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo       OK: %PY% (%PYVER%)

rem ============================ 2/7 GIT / NODE ===============================
echo [2/7] Controllo git ...
set "HAVE_GIT=0"
set "GIT=git"
where git >nul 2>&1 && set "HAVE_GIT=1"
if "%HAVE_GIT%"=="1" goto :git_ok
if exist "%PORTABLE%\git\cmd\git.exe" (set "HAVE_GIT=1" & set "GIT=%PORTABLE%\git\cmd\git.exe" & goto :git_ok)
echo       git assente: scarico MinGit %GIT_VER% portable ...
call :download "%GIT_URL%" "%TEMP%\mingit-%STAMP%.zip" 20000000
if errorlevel 1 goto :git_zip
call :unzip "%TEMP%\mingit-%STAMP%.zip" "%PORTABLE%\git"
del /q "%TEMP%\mingit-%STAMP%.zip" >nul 2>&1
if exist "%PORTABLE%\git\cmd\git.exe" (
    "%PORTABLE%\git\cmd\git.exe" --version >nul 2>&1 && (set "HAVE_GIT=1" & set "GIT=%PORTABLE%\git\cmd\git.exe")
)
:git_zip
if "%HAVE_GIT%"=="1" goto :git_ok
echo       nessun git: usero' lo zip di GitHub
goto :git_done
:git_ok
for /f "tokens=3" %%v in ('call "%GIT%" --version 2^>nul') do echo       OK: git %%v - %GIT%
if "%GIT%"=="git" goto :git_done
set "PATH=%PORTABLE%\git\cmd;%PATH%"
:git_done
if not "%PORTABLE_NODE%"=="1" goto :node_done
echo       Node.js portable %NODE_VER% (PORTABLE_NODE=1) ...
if exist "%PORTABLE%\node\node.exe" (
    for /f %%v in ('"%PORTABLE%\node\node.exe" --version 2^>nul') do echo       OK: node %%v gia' presente
    goto :node_done
)
call :download "%NODE_URL%" "%TEMP%\node-%STAMP%.zip" 20000000
if errorlevel 1 (echo       ATTENZIONE: download Node fallito, continuo senza & goto :node_done)
call :download "%NODE_SHA_URL%" "%TEMP%\node-%STAMP%.sha" 100
if errorlevel 1 (echo       ATTENZIONE: SHASUMS256 non scaricato, Node ignorato & goto :node_done)
call :verify_sha "%TEMP%\node-%STAMP%.zip" "%TEMP%\node-%STAMP%.sha" "node-v%NODE_VER%-win-x64.zip"
if errorlevel 1 (echo       ERRORE: SHA256 di Node non corrisponde, file scartato & del /q "%TEMP%\node-%STAMP%.zip" & goto :node_done)
call :unzip "%TEMP%\node-%STAMP%.zip" "%TEMP%\node-unz-%STAMP%"
for /d %%d in ("%TEMP%\node-unz-%STAMP%\node-v*") do robocopy "%%d" "%PORTABLE%\node" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul
rmdir /s /q "%TEMP%\node-unz-%STAMP%" >nul 2>&1
del /q "%TEMP%\node-%STAMP%.zip" "%TEMP%\node-%STAMP%.sha" >nul 2>&1
for /f %%v in ('"%PORTABLE%\node\node.exe" --version 2^>nul') do echo       OK: node %%v verificato SHA256
:node_done

rem ============================ 3/7 BACKUP ===================================
echo [3/7] Backup dei file personali in _backup\%STAMP% ...
set "BACKED=0"
for %%f in (config.yaml .env setup.local.bat) do (
    if exist "%INSTALL_DIR%\%%f" (
        if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
        copy /y "%INSTALL_DIR%\%%f" "%BACKUP_DIR%\" >nul && set "BACKED=1"
    )
)
for %%d in (data personal) do (
    if exist "%INSTALL_DIR%\%%d" (
        robocopy "%INSTALL_DIR%\%%d" "%BACKUP_DIR%\%%d" /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul
        set "BACKED=1"
    )
)
if "%BACKED%"=="1" (echo       OK: backup salvato) else (echo       niente da salvare: prima installazione)
call :prune_backups

rem ============================ 4/7 CODICE ===================================
echo [4/7] Scarico/aggiorno il codice ...
set "OLD_SHA=-"
set "NEW_SHA=-"
call :ensure_token
set "GIT_AUTH="
if defined GITHUB_TOKEN set GIT_AUTH=-c "http.extraheader=AUTHORIZATION: bearer %GITHUB_TOKEN%"
if not "!TOKEN_STATUS!"=="ok" echo       attenzione: nessun accesso verificato al repository, il download potrebbe fallire
if "%HAVE_GIT%"=="1" if exist "%INSTALL_DIR%\.git" goto :update_git
if "%HAVE_GIT%"=="1" goto :clone_git
goto :download_zip

:update_git
pushd "%INSTALL_DIR%"
for /f %%s in ('call "%GIT%" rev-parse --short HEAD 2^>nul') do set "OLD_SHA=%%s"
"%GIT%" %GIT_AUTH% fetch origin "%BRANCH%" || (popd & echo       git fetch fallito: passo allo zip & goto :download_zip)
rem I file personali non sono tracciati: reset --hard non li tocca.
"%GIT%" checkout --quiet -B "%BRANCH%" "origin/%BRANCH%" || (popd & goto :fail_download)
"%GIT%" reset --quiet --hard "origin/%BRANCH%" || (popd & goto :fail_download)
for /f %%s in ('call "%GIT%" rev-parse --short HEAD 2^>nul') do set "NEW_SHA=%%s"
popd
if "%OLD_SHA%"=="%NEW_SHA%" (echo       gia' aggiornato: commit %NEW_SHA%) else (echo       aggiornato: commit %OLD_SHA% -^> %NEW_SHA%)
goto :code_ok

:clone_git
rem Cartella esistente senza .git (es. installata da zip): clono a parte e sincronizzo.
set "TMP_CLONE=%TEMP%\drop-monitor-clone-%STAMP%"
"%GIT%" %GIT_AUTH% clone --depth 1 --branch "%BRANCH%" "https://github.com/%REPO%.git" "%TMP_CLONE%" || (echo       git clone fallito: passo allo zip & goto :download_zip)
call :sync_from "%TMP_CLONE%"
for /f %%s in ('call "%GIT%" -C "%TMP_CLONE%" rev-parse --short HEAD 2^>nul') do set "NEW_SHA=%%s"
rmdir /s /q "%TMP_CLONE%" >nul 2>&1
echo       installato: %NEW_SHA%
goto :code_ok

:download_zip
set "ZIP=%TEMP%\drop-monitor-%STAMP%.zip"
set "UNZ=%TEMP%\drop-monitor-unzip-%STAMP%"
set "ZIP_URL=https://api.github.com/repos/%REPO%/zipball/%BRANCH%"
echo       scarico %ZIP_URL%
call :download "%ZIP_URL%" "%ZIP%" 10000
if errorlevel 1 goto :fail_download
call :unzip "%ZIP%" "%UNZ%"
if errorlevel 1 goto :fail_download
set "SRC="
for /d %%d in ("%UNZ%\*") do set "SRC=%%~fd"
if not defined SRC goto :fail_download
call :sync_from "%SRC%"
set "NEW_SHA=zip-%STAMP%"
rmdir /s /q "%UNZ%" >nul 2>&1
del /q "%ZIP%" >nul 2>&1
echo       installato da zip
goto :code_ok

:code_ok
if not exist "%INSTALL_DIR%\drop_monitor\cli.py" goto :fail_verify
if not exist "%INSTALL_DIR%\requirements.txt" goto :fail_verify
for /f "tokens=2 delims== " %%v in ('findstr /c:"__version__" "%INSTALL_DIR%\drop_monitor\__init__.py"') do set "APP_VER=%%~v"
if "%INSTALLED_VER%"=="-" (echo       versione app: %APP_VER%) else if "%INSTALLED_VER%"=="%APP_VER%" (echo       versione app: %APP_VER% ^(invariata^)) else (echo       versione app: %INSTALLED_VER% -^> %APP_VER%)

rem ============================ 5/7 DIPENDENZE ===============================
pushd "%INSTALL_DIR%"
if "%PY_IS_PORTABLE%"=="1" (
    echo [5/7] Dipendenze nel Python portable ...
    set "VPY=%PORTABLE%\python\python.exe"
) else (
    echo [5/7] Ambiente Python ^(.venv^) e dipendenze ...
    if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || (popd & goto :fail_venv)
    set "VPY=%INSTALL_DIR%\.venv\Scripts\python.exe"
)
"%VPY%" -m pip install --quiet --upgrade pip >nul 2>&1
echo       installo le dipendenze ^(puo' richiedere 1-2 minuti^) ...
"%VPY%" -m pip install --quiet --no-warn-script-location -r requirements.txt -r requirements-dev.txt || (popd & goto :fail_venv)
rem Il Python portable importa drop_monitor direttamente dalla cartella (vedi ._pth): niente pip install -e.
if not "%PY_IS_PORTABLE%"=="1" "%VPY%" -m pip install --quiet --no-warn-script-location -e . || (popd & goto :fail_venv)
"%VPY%" -c "import drop_monitor, httpx, yaml, bs4, lxml; print('      moduli OK: drop_monitor', drop_monitor.__version__)" || (popd & goto :fail_venv)
popd
echo       OK

rem ============================ 6/7 FILE PERSONALI ===========================
echo [6/7] File personali (creati solo se mancano, mai sovrascritti) ...
if not exist "%INSTALL_DIR%\data" mkdir "%INSTALL_DIR%\data"
if not exist "%INSTALL_DIR%\personal" mkdir "%INSTALL_DIR%\personal"
call :ensure_file "config.yaml" "config.example.yaml"
call :ensure_file ".env" ".env.example"
call :ensure_file "personal\order-profile.yaml" "personal\order-profile.example.yaml"
rem Ripristino di sicurezza dal backup di questa esecuzione (se qualcosa fosse sparito).
if exist "%BACKUP_DIR%" (
    for %%f in (config.yaml .env setup.local.bat) do (
        if exist "%BACKUP_DIR%\%%f" if not exist "%INSTALL_DIR%\%%f" copy /y "%BACKUP_DIR%\%%f" "%INSTALL_DIR%\" >nul
    )
    for %%d in (data personal) do (
        if exist "%BACKUP_DIR%\%%d" robocopy "%BACKUP_DIR%\%%d" "%INSTALL_DIR%\%%d" /E /XC /XN /XO /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul
    )
)
> "%INSTALL_DIR%\install-info.txt" (
    echo installed_at=%DATE% %TIME%
    echo branch=%BRANCH%
    echo commit=%NEW_SHA%
    echo app_version=%APP_VER%
    echo python=%PYVER%
    echo python_exe=%VPY%
    echo git=%GIT%
    echo node=%PORTABLE%\node\node.exe
)

rem ============================ 7/7 SELF-TEST ================================
echo [7/7] Self-test offline ...
pushd "%INSTALL_DIR%"
"%VPY%" -m pytest -q >"%TEMP%\drop-monitor-pytest.log" 2>&1
if errorlevel 1 (
    echo       ATTENZIONE: alcuni test falliscono, vedi %TEMP%\drop-monitor-pytest.log
) else (
    for /f "tokens=*" %%l in ('findstr /c:"passed" "%TEMP%\drop-monitor-pytest.log"') do echo       test: %%l
)
"%VPY%" -m drop_monitor -c config.yaml probe --file tests\fixtures\nop_product_oos.html https://www.gemcardinfinitycollection.it/it/pokemon-30-anniversario-mini-tin-case-sealed-ita >"%TEMP%\drop-monitor-probe.log" 2>&1
if errorlevel 1 (
    echo       ATTENZIONE: config.yaml o .env non validi. Dettagli:
    type "%TEMP%\drop-monitor-probe.log"
) else (
    echo       parser + config: OK
)
popd

rmdir /s /q "%TEMP%" >nul 2>&1
echo.
echo ============================================================================
echo  Installazione completata in: %INSTALL_DIR%
echo  Tutto e' in questa cartella: codice, portable\ (python, git, pip-cache),
echo  data\ (db, log), personal\ (profili), _backup\, config.yaml, .env
echo.
echo  Prossimi passi:
echo   1. apri .env e inserisci TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID
echo   2. (opzionale) adatta config.yaml: prodotti, intervallo, sorgenti
echo   3. compila personal\order-profile.yaml con i dati per gli ordini (fase 2)
echo   4. windows\test.bat    - prova senza inviare notifiche
echo   5. windows\start.bat   - avvia il monitor
echo   6. windows\app.bat     - finestra di controllo con monitor integrato: da li' imposti
echo                           intervalli, proxy, Telegram/Discord, prodotti, task, profili
echo.
echo  Per aggiornare in futuro: rilancia questo stesso file. I file personali
echo  vengono preservati e copiati anche in _backup\ (ultimi 5 backup).
echo ============================================================================
echo.
choice /c SN /n /t 20 /d S /m " Aprire adesso la finestra di controllo (windows\app.bat)? [S/N, S fra 20s] "
if not errorlevel 2 start "" "%INSTALL_DIR%\windows\app.bat"
goto :end

rem ============================ FUNZIONI =====================================
:cleanup_leftovers
rem Residui di esecuzioni precedenti/interrotte, nel TEMP di sistema e nella cartella.
set "N=0"
for %%p in ("drop-monitor-*.zip" "drop-monitor-unzip-*" "drop-monitor-clone-*" "drop-monitor-*.log" "drop-monitor-version-*.py" "mingit-*.zip" "node-*.zip" "node-*.sha" "node-unz-*" "python-embed-*.zip" "get-pip-*.py") do (
    for /f "delims=" %%f in ('dir /b /a-d "%SYS_TEMP%\%%~p" 2^>nul') do (
        if "%CHECK_ONLY%"=="1" (echo       residuo: %SYS_TEMP%\%%f) else (del /q "%SYS_TEMP%\%%f" >nul 2>&1 && echo       rimosso: %SYS_TEMP%\%%f)
        set /a N+=1
    )
    for /f "delims=" %%d in ('dir /b /ad "%SYS_TEMP%\%%~p" 2^>nul') do (
        if "%CHECK_ONLY%"=="1" (echo       residuo: %SYS_TEMP%\%%d\) else (rmdir /s /q "%SYS_TEMP%\%%d" >nul 2>&1 && echo       rimosso: %SYS_TEMP%\%%d\)
        set /a N+=1
    )
)
if exist "%INSTALL_DIR%\_tmp" (
    if "%CHECK_ONLY%"=="1" (echo       residuo: %INSTALL_DIR%\_tmp\) else (rmdir /s /q "%INSTALL_DIR%\_tmp" >nul 2>&1 && echo       rimosso: %INSTALL_DIR%\_tmp\)
    set /a N+=1
)
rem backup vuoti (creati da un setup interrotto al passo 3)
for /f "delims=" %%d in ('dir /b /ad "%INSTALL_DIR%\_backup" 2^>nul') do (
    dir /b /s /a-d "%INSTALL_DIR%\_backup\%%d" 2>nul | findstr . >nul || (
        if "%CHECK_ONLY%"=="1" (echo       backup vuoto: _backup\%%d) else (rmdir /s /q "%INSTALL_DIR%\_backup\%%d" >nul 2>&1 && echo       rimosso backup vuoto: _backup\%%d)
        set /a N+=1
    )
)
if "!N!"=="0" echo       nessun residuo trovato
exit /b 0

:migrate_old_install
rem La prima versione del setup installava in %USERPROFILE%\drop-monitor.
if /i "%OLD_DIR%"=="%INSTALL_DIR%" exit /b 0
if not exist "%OLD_DIR%" exit /b 0
echo       trovata vecchia posizione: %OLD_DIR%
set "OLD_HAS_DATA=0"
for %%f in (config.yaml .env setup.local.bat) do if exist "%OLD_DIR%\%%f" set "OLD_HAS_DATA=1"
for %%d in (data personal) do if exist "%OLD_DIR%\%%d" (dir /b /s /a-d "%OLD_DIR%\%%d" 2>nul | findstr . >nul && set "OLD_HAS_DATA=1")
if "%CHECK_ONLY%"=="1" (
    if "!OLD_HAS_DATA!"=="1" (echo       contiene dati personali: verranno migrati qui al prossimo setup) else (echo       senza dati personali: verra' rimossa al prossimo setup)
    exit /b 0
)
if "!OLD_HAS_DATA!"=="1" (
    echo       migro i dati personali ^(solo se qui mancano^) ...
    if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
    for %%f in (config.yaml .env setup.local.bat) do (
        if exist "%OLD_DIR%\%%f" if not exist "%INSTALL_DIR%\%%f" copy /y "%OLD_DIR%\%%f" "%INSTALL_DIR%\" >nul && echo       migrato: %%f
    )
    for %%d in (data personal) do (
        if exist "%OLD_DIR%\%%d" robocopy "%OLD_DIR%\%%d" "%INSTALL_DIR%\%%d" /E /XC /XN /XO /R:1 /W:1 /NFL /NDL /NJH /NJS /NP >nul && echo       migrato: %%d\
    )
    choice /c SN /n /m "      Eliminare la vecchia cartella %OLD_DIR% ? [S/N] "
    if errorlevel 2 (echo       vecchia cartella mantenuta & exit /b 0)
)
rmdir /s /q "%OLD_DIR%" >nul 2>&1
if exist "%OLD_DIR%" (echo       ATTENZIONE: impossibile rimuovere %OLD_DIR%) else (echo       rimossa vecchia cartella: %OLD_DIR%)
exit /b 0

:ensure_portable_python
set "PP=%PORTABLE%\python"
if exist "%PP%\python.exe" goto :pp_check
call :download "%PY_URL%" "%TEMP%\python-embed-%STAMP%.zip" 9000000
if errorlevel 1 exit /b 1
call :unzip "%TEMP%\python-embed-%STAMP%.zip" "%PP%"
del /q "%TEMP%\python-embed-%STAMP%.zip" >nul 2>&1
if not exist "%PP%\python.exe" exit /b 1
rem Abilita site-packages (il file ._pth lo disattiva di default) e aggiungi la cartella app.
for %%f in ("%PP%\python*._pth") do (
    > "%%~ff" (
        echo python312.zip
        echo .
        echo ..\..
        echo import site
    )
)
if not exist "%PP%\Lib\site-packages" mkdir "%PP%\Lib\site-packages"
:pp_check
"%PP%\python.exe" -c "import sys; assert sys.version_info >= (3, %PY_MIN_MINOR%)" >nul 2>&1 || exit /b 1
"%PP%\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo       installo pip nel Python portable ...
    call :download "%PIP_URL%" "%TEMP%\get-pip-%STAMP%.py" 100000
    if errorlevel 1 exit /b 1
    "%PP%\python.exe" "%TEMP%\get-pip-%STAMP%.py" --quiet --no-warn-script-location || exit /b 1
    del /q "%TEMP%\get-pip-%STAMP%.py" >nul 2>&1
)
set "PY="%PP%\python.exe""
set "PY_IS_PORTABLE=1"
exit /b 0

:download
rem :download URL DEST MIN_BYTES [ACCEPT]  -> errorlevel 1 se fallisce o file troppo piccolo
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "$h=@{'User-Agent'='drop-monitor-setup'}; if ('%~4') { $h['Accept']='%~4' }; if ($env:GITHUB_TOKEN -and '%~1'.Contains('github')) { $h['Authorization']='Bearer '+$env:GITHUB_TOKEN };" ^
  "Invoke-WebRequest -Uri '%~1' -Headers $h -OutFile '%~2' -UseBasicParsing;" ^
  "if ((Get-Item '%~2').Length -lt %~3) { throw 'file troppo piccolo: download incompleto' }"
if errorlevel 1 (echo       download fallito: %~1 & exit /b 1)
exit /b 0

:ensure_token
rem Verifica che il token acceda al repository; altrimenti lo chiede (max 3 tentativi) e lo salva.
set "TRIES=0"
:ensure_token_loop
call :check_token
if "!TOKEN_STATUS!"=="ok" (
    if "!TOKEN_SAVED!"=="0" if defined GITHUB_TOKEN call :save_token
    if "%REMOTE_VER%"=="-" call :fetch_remote_version
    exit /b 0
)
if "!TOKEN_STATUS!"=="norepo" echo       token valido per l'utente !TOKEN_USER! ma SENZA accesso a %REPO%:
if "!TOKEN_STATUS!"=="norepo" echo       nel token metti "Repository access: Only select repositories" -^> %REPO% e "Contents: Read-only"
if "!TOKEN_STATUS!"=="invalid" echo       token non valido o scaduto ^(GitHub risponde 401^)
if "!TOKEN_STATUS!"=="none" echo       il repository %REPO% e' privato: serve un token GitHub
if "!TOKEN_STATUS!"=="offline" (echo       GitHub non raggiungibile: impossibile verificare il token & exit /b 1)
set /a TRIES+=1
if !TRIES! GTR 3 (echo       troppi tentativi: continuo senza token & exit /b 1)
echo       Crealo/modificalo su https://github.com/settings/personal-access-tokens
echo       ^(Fine-grained, Repository access: Only select repositories -^> %REPO%, Permissions: Contents Read-only^)
set "NEW_TOKEN="
set /p "NEW_TOKEN=      Incolla il token e premi INVIO (INVIO vuoto per continuare senza): "
if "!NEW_TOKEN!"=="" exit /b 1
set "GITHUB_TOKEN=!NEW_TOKEN!"
set "TOKEN_SAVED=0"
goto :ensure_token_loop

:check_token
rem Esito in TOKEN_STATUS: ok | norepo | invalid | none | offline ; TOKEN_USER = login GitHub
set "TOKEN_STATUS=none"
set "TOKEN_USER=-"
set "CODES="
if not defined TOKEN_SAVED set "TOKEN_SAVED=1"
if defined GITHUB_TOKEN goto :check_token_auth
rem Senza token: se il repository e' pubblico non serve nulla.
for /f %%c in ('powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "try { $r=Invoke-WebRequest -Uri 'https://api.github.com/repos/%REPO%' -Headers @{'User-Agent'='drop-monitor-setup'} -UseBasicParsing; [int]$r.StatusCode } catch { if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 } }"') do set "PUBCODE=%%c"
if "%PUBCODE%"=="200" (set "TOKEN_STATUS=ok" & set "TOKEN_USER=anonimo" & echo       repository pubblico: nessun token necessario)
if "%PUBCODE%"=="0" set "TOKEN_STATUS=offline"
exit /b 0
:check_token_auth
set "TOKEN_STATUS=offline"
for /f "tokens=1,2" %%a in ('powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
  "$h=@{'User-Agent'='drop-monitor-setup';'Authorization'='Bearer '+$env:GITHUB_TOKEN};" ^
  "function code($u){ try { $r=Invoke-WebRequest -Uri $u -Headers $h -UseBasicParsing; return [int]$r.StatusCode } catch { if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode } else { return 0 } } };" ^
  "$u=code('https://api.github.com/user'); $login='-'; if ($u -eq 200) { $login=(Invoke-WebRequest -Uri 'https://api.github.com/user' -Headers $h -UseBasicParsing | ConvertFrom-Json).login };" ^
  "$rp=code('https://api.github.com/repos/%REPO%'); Write-Output ($u.ToString()+'/'+$rp.ToString()+' '+$login)"') do (
    set "CODES=%%a"
    set "TOKEN_USER=%%b"
)
if not defined CODES exit /b 0
for /f "tokens=1,2 delims=/" %%u in ("%CODES%") do (
    if "%%u"=="0" set "TOKEN_STATUS=offline"
    if "%%u"=="401" set "TOKEN_STATUS=invalid"
    if "%%u"=="200" if "%%v"=="200" set "TOKEN_STATUS=ok"
    if "%%u"=="200" if not "%%v"=="200" set "TOKEN_STATUS=norepo"
)
if "%TOKEN_STATUS%"=="ok" echo       token GitHub OK: utente %TOKEN_USER%, accesso a %REPO% confermato
exit /b 0

:save_token
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
> "%INSTALL_DIR%\setup.local.bat" (
    echo @echo off
    echo rem Impostazioni locali di setup-windows.bat ^(file personale, NON condividere, preservato dagli aggiornamenti^)
    echo set "GITHUB_TOKEN=%GITHUB_TOKEN%"
)
set "TOKEN_SAVED=1"
echo       token salvato in %INSTALL_DIR%\setup.local.bat
exit /b 0

:fetch_remote_version
set "VER_TMP=%SYS_TEMP%\drop-monitor-version-%STAMP%.py"
call :download "https://api.github.com/repos/%REPO%/contents/drop_monitor/__init__.py?ref=%BRANCH%" "%VER_TMP%" 10 "application/vnd.github.raw" >nul 2>&1
if exist "%VER_TMP%" (
    for /f "tokens=2 delims== " %%v in ('findstr /c:"__version__" "%VER_TMP%"') do set "REMOTE_VER=%%~v"
    del /q "%VER_TMP%" >nul 2>&1
)
exit /b 0

:unzip
rem :unzip ZIP DEST
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Expand-Archive -Path '%~1' -DestinationPath '%~2' -Force"
if errorlevel 1 (echo       estrazione fallita: %~1 & exit /b 1)
exit /b 0

:verify_sha
rem :verify_sha FILE SHASUMS_TXT NAME_IN_LIST
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $want=(Get-Content '%~2' | Where-Object { $_ -match '\s%~3$' }) -split '\s+' | Select-Object -First 1;" ^
  "if (-not $want) { throw 'hash non trovato nella lista' }; $got=(Get-FileHash '%~1' -Algorithm SHA256).Hash.ToLower();" ^
  "if ($got -ne $want.ToLower()) { throw ('SHA256 diverso: ' + $got) }"
if errorlevel 1 exit /b 1
exit /b 0

:find_python
for %%c in ("py -3.13" "py -3.12" "py -3.11" "py -3" "python" "python3") do (
    if not defined PY call :try_python %%~c
)
exit /b 0

:try_python
set "CAND=%*"
for /f "tokens=2 delims= " %%v in ('%CAND% --version 2^>nul') do (
    for /f "tokens=1,2 delims=." %%a in ("%%v") do (
        if "%%a"=="3" if %%b GEQ %PY_MIN_MINOR% set "PY=%CAND%"
    )
)
exit /b 0

:sync_from
rem Copia il codice da %1 nella cartella di installazione, senza toccare i file preservati.
robocopy "%~1" "%INSTALL_DIR%" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP ^
    /XD "%~1\.git" "%INSTALL_DIR%\.git" "%INSTALL_DIR%\.venv" "%INSTALL_DIR%\data" "%INSTALL_DIR%\personal" "%INSTALL_DIR%\_backup" "%INSTALL_DIR%\portable" "%INSTALL_DIR%\_tmp" ^
    /XF config.yaml .env setup.local.bat install-info.txt >nul
if errorlevel 8 goto :fail_download
exit /b 0

:ensure_file
if exist "%INSTALL_DIR%\%~1" (
    echo       %~1: presente, mantenuto
) else (
    copy /y "%INSTALL_DIR%\%~2" "%INSTALL_DIR%\%~1" >nul && echo       %~1: creato da %~2 ^(da compilare^)
)
exit /b 0

:prune_backups
rem Tiene gli ultimi 5 backup.
set "N=0"
for /f "delims=" %%d in ('dir /b /ad /o-n "%INSTALL_DIR%\_backup" 2^>nul') do (
    set /a N+=1
    if !N! GTR 5 rmdir /s /q "%INSTALL_DIR%\_backup\%%d" >nul 2>&1
)
exit /b 0

rem ============================ ERRORI =======================================
:fail_mkdir
echo ERRORE: impossibile creare %INSTALL_DIR%
goto :fail
:fail_download
echo ERRORE: download/aggiornamento del codice fallito.
echo  - il repository e' privato: serve un token con accesso a %REPO% ^(Contents: Read-only^).
echo    Rilancia il setup: verifica il token e te lo chiede se non funziona.
echo  - branch inesistente? cambia BRANCH in testa a questo file o DROP_MONITOR_BRANCH
goto :fail
:fail_verify
echo ERRORE: i file scaricati sono incompleti (manca drop_monitor\cli.py). Nessun file personale e' stato toccato.
goto :fail
:fail_venv
echo ERRORE: installazione dipendenze Python fallita. Controlla la connessione e rilancia.
goto :fail
:fail
echo.
if exist "%BACKUP_DIR%" echo  Backup dei file personali: %BACKUP_DIR%
echo  Setup interrotto. Copia il testo di questa finestra per la diagnosi.
exit /b 1
:end
echo.
echo  (puoi chiudere questa finestra)
exit /b 0
