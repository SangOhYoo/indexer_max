@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

title Manticore Restore (Windows to WSL)
cd /d %~dp0

echo ========================================================
echo  Manticore Restore System (Windows -^> WSL)
echo ========================================================

:: 1. Check virtual environment
if exist "venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment [venv]...
    call venv\Scripts\activate
) else (
    echo [WARNING] venv not found. Using global python.
)

:: 2. Run restore script
echo [RUN] Running restore_manticore.py...
echo --------------------------------------------------------
python restore_manticore.py

echo --------------------------------------------------------
echo [EXIT] Restore process finished.
pause
