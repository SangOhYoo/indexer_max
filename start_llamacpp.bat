@echo off
:: Set code page to UTF-8
chcp 65001 >nul

:: Force Python output encoding to UTF-8
set PYTHONIOENCODING=utf-8

title Llama.cpp Indexer Control Center
cd /d %~dp0
cls

setlocal enabledelayedexpansion

:: 1. Hardcoded unused ports
set PORT1=8082
set PORT2=8083

echo ========================================================
echo  [INIT] Using statically assigned ports...
echo ========================================================
echo [INFO] Allocated Ports: GPU0 = %PORT1%, GPU1 = %PORT2%
echo.

:: 2. Start llama-server on those ports
echo ========================================================
echo  [START] Starting Llama.cpp BGE-M3 servers in background.
echo ========================================================

:: Note: We must not kill port 8081.
:: We will kill exactly the two servers we just started using PORT1 and PORT2 later.

start "Llama.cpp - BGE-M3 GPU 0 (%PORT1%)" cmd /c "title Llama.cpp - BGE-M3 GPU 0 & set CUDA_VISIBLE_DEVICES=0 & C:\llama-cpp\llama-server.exe -m C:\llama-cpp\models\bge-m3.gguf --host 127.0.0.1 --port %PORT1% -ngl 99 --embedding -c 8192 -b 8192 -ub 8192"

start "Llama.cpp - BGE-M3 GPU 1 (%PORT2%)" cmd /c "title Llama.cpp - BGE-M3 GPU 1 & set CUDA_VISIBLE_DEVICES=1 & C:\llama-cpp\llama-server.exe -m C:\llama-cpp\models\bge-m3.gguf --host 127.0.0.1 --port %PORT2% -ngl 99 --embedding -c 8192 -b 8192 -ub 8192"

echo [INFO] Waiting 5 seconds for servers to start...
ping 127.0.0.1 -n 6 > nul

:: 3. Setup Venv and run Python script
IF NOT EXIST "venv" (
    echo ========================================================
    echo  [INIT] Virtual environment not found. Creating a new one...
    echo ========================================================
    python -m venv venv
    
    echo.
    echo  [INSTALL] Installing required libraries...
    call venv\Scripts\activate
    
    python -m pip install --upgrade pip
    
    if exist requirements.txt (
        pip install -r requirements.txt
    ) else (
        echo [WARNING] requirements.txt not found. Installing default packages...
        pip install aiohttp aiomysql beautifulsoup4 lxml gradio uvicorn fastapi pymysql langchain langchain-text-splitters langchain-community httpx tiktoken
    )
    
    echo.
    echo  [DONE] Setup completed!
) ELSE (
    echo ========================================================
    echo  [START] Activating virtual environment ^(venv^)...
    echo ========================================================
    call venv\Scripts\activate
)

echo.
echo  [RUN] Executing Dual GPU Indexer...
echo  --------------------------------------------------------
python indexer_max.py

:: 4. Cleanup and exit
echo.
echo ========================================================
echo  [EXIT] Indexing completed. Stopping Llama.cpp servers.
echo ========================================================
echo [INFO] Terminating processes using ports %PORT1% and %PORT2%...

powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %PORT1% -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %PORT2% -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }"

:: Close the cmd windows using their titles
taskkill /FI "WINDOWTITLE eq Llama.cpp - BGE-M3 GPU 0*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Llama.cpp - BGE-M3 GPU 1*" /T /F >nul 2>&1

echo.
echo  [DONE] All tasks finished successfully.
pause
