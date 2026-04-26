@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo ============================================================
echo   NSRL Windows Hash Extractor - Portable Python Setup
echo ============================================================
echo.

set "BASE_DIR=%~dp0"
set "PYTHON_DIR=%BASE_DIR%python_embed"
set "PYTHON_ZIP=python-3.12.7-embed-amd64.zip"
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip"
set "GETPIP_URL=https://bootstrap.pypa.io/get-pip.py"

if exist "%PYTHON_DIR%\python.exe" (
    echo [INFO] Portable Python zaten kurulu.
    "%PYTHON_DIR%\python.exe" --version
    goto :check_pip
)

echo [1/5] Dizin olusturuluyor...
if not exist "%PYTHON_DIR%" mkdir "%PYTHON_DIR%"

echo [2/5] Python Embeddable indiriliyor...
powershell -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_DIR%\%PYTHON_ZIP%'"
if errorlevel 1 (
    echo [HATA] Python indirilemedi!
    pause & exit /b 1
)

echo [3/5] Arsiv aciliyor...
powershell -Command "$ProgressPreference='SilentlyContinue'; Expand-Archive -Path '%PYTHON_DIR%\%PYTHON_ZIP%' -DestinationPath '%PYTHON_DIR%' -Force"
del "%PYTHON_DIR%\%PYTHON_ZIP%" 2>nul

echo [4/5] pth dosyasi duzenleniyor...
set "PTH=%PYTHON_DIR%\python312._pth"
if exist "%PTH%" (
    powershell -Command "$c=Get-Content '%PTH%' -Raw; $c=$c-replace'#import site','import site'; Set-Content '%PTH%' $c -NoNewline"
)

:check_pip
if exist "%PYTHON_DIR%\Scripts\pip.exe" (
    echo [INFO] pip zaten kurulu.
    goto :done
)

echo [5/5] pip kuruluyor...
powershell -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%GETPIP_URL%' -OutFile '%PYTHON_DIR%\get-pip.py'"
"%PYTHON_DIR%\python.exe" "%PYTHON_DIR%\get-pip.py" --no-warn-script-location
del "%PYTHON_DIR%\get-pip.py" 2>nul

:done
echo.
echo ============================================================
echo   Kurulum Tamamlandi!
echo   Kullanim: run_extractor.bat [kaynak_db] [cikti_db]
echo ============================================================

endlocal
