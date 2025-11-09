@echo off
setlocal

REM Launches the loopback RMS monitor inside a virtual environment.

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%.venv"
set "PY_CMD=py -3"

%PY_CMD% --version >nul 2>&1
if errorlevel 1 (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python yorumlayicisi bulunamadi. Lutfen Python yukleyin ve yeniden deneyin.
        pause
        exit /b 1
    )
    set "PY_CMD=python"
)

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo [INFO] Sanal ortam olusturuluyor...
    %PY_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Sanal ortam olusturulamadi.
        pause
        exit /b 1
    )
)

call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Sanal ortam etkinlestirilemedi.
    pause
    exit /b 1
)

python -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] pip guncellenemedi.
    call "%VENV_DIR%\Scripts\deactivate.bat"
    pause
    exit /b 1
)

python -m pip install -r "%PROJECT_DIR%requirements.txt"
if errorlevel 1 (
    echo [ERROR] Gerekli paketler yuklenemedi.
    call "%VENV_DIR%\Scripts\deactivate.bat"
    pause
    exit /b 1
)

python "%PROJECT_DIR%loopback_monitor.py"
set "RESULT=%ERRORLEVEL%"

call "%VENV_DIR%\Scripts\deactivate.bat"

if %RESULT% neq 0 (
    echo [ERROR] Uygulama hata kodu %RESULT% ile kapandi.
    pause
    exit /b %RESULT%
)

echo [INFO] Uygulama kapanmistir. Bu pencereyi kapatmak icin bir tusa basin.
pause
exit /b 0
