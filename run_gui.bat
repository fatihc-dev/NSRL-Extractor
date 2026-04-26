@echo off
title NSRL Extractor Web UI
set "PYTHON=%~dp0python_embed\python.exe"
set "SCRIPT=%~dp0gui_app.py"

if not exist "%PYTHON%" (
    echo [HATA] Portable Python bulunamadi!
    echo        Once setup.bat dosyasini calistirin.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo   NSRL Arayuzu Baslatiliyor...
echo   Tarayiciniz otomatik olarak acilacaktir.
echo ===================================================
echo.

:: Arka planda tarayiciyi ac (1 saniye gecikmeli)
start "" cmd /c "timeout /t 1 >nul && start http://localhost:8080"

:: Sunucuyu baslat
"%PYTHON%" "%SCRIPT%"
