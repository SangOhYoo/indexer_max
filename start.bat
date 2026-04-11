@echo off
:: [한글 깨짐 방지 1] 코드 페이지를 UTF-8(65001)로 변경
chcp 65001 >nul

:: [한글 깨짐 방지 2] Python 출력 인코딩을 UTF-8로 강제
set PYTHONIOENCODING=utf-8

title Python Indexer Control Center (Venv)
cd /d %~dp0
cls

:: 1. 가상환경 폴더(venv)가 있는지 확인
IF NOT EXIST "venv" (
    echo ========================================================
    echo  [INIT] 가상환경이 없습니다. 새로 생성합니다...
    echo ========================================================
    python -m venv venv
    
    echo.
    echo  [INSTALL] 필수 라이브러리를 설치합니다...
    :: 가상환경 활성화 후 설치
    call venv\Scripts\activate
    
    :: pip 업그레이드
    python -m pip install --upgrade pip
    
    :: requirements.txt를 사용하여 설치
    if exist requirements.txt (
        pip install -r requirements.txt
    ) else (
        echo [WARNING] requirements.txt 파일이 없습니다. 기본 패키지만 설치합니다.
        pip install aiohttp aiomysql beautifulsoup4 lxml gradio uvicorn fastapi pymysql langchain langchain-text-splitters langchain-community httpx tiktoken
    )
    
    echo.
    echo  [DONE] 설정 완료!
) ELSE (
    echo ========================================================
    echo  [START] 가상환경^(venv^)을 활성화합니다.
    echo ========================================================
    call venv\Scripts\activate
)

:: 2. 파이썬 스크립트 실행
echo.
echo  [RUN] 듀얼 GPU 인덱서를 실행합니다...
echo  --------------------------------------------------------
python indexer_max.py

:: 3. 종료 방지
echo.
echo  [EXIT] 프로그램이 종료되었습니다.
pause