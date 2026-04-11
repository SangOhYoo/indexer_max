@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

title Manticore Backup (WSL to Windows)
cd /d %~dp0

echo ========================================================
echo  Manticore Backup System (WSL -^> Windows)
echo ========================================================

:: 1. Check virtual environment
if exist "venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment [venv]...
    call venv\Scripts\activate
) else (
    echo [WARNING] venv not found. Using global python.
)

:: 2. Run backup script
echo [RUN] Running backup_manticore.py...
echo --------------------------------------------------------
python backup_manticore.py

echo --------------------------------------------------------
echo [EXIT] Backup process finished.
pause
