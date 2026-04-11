@echo off
title Ollama GPU 1 (Port 11435) - HIGH PERF
set OLLAMA_HOST=127.0.0.1:11435
set CUDA_VISIBLE_DEVICES=1
REM ★ 핵심: 여기도 8개 병렬 처리
set OLLAMA_NUM_PARALLEL=64
set OLLAMA_KEEP_ALIVE=24h
echo [GPU 1] Parallel Service Started...
ollama serve
pause