@echo off
title Ollama GPU 1 (Port 11435) - HIGH PERF
set OLLAMA_HOST=127.0.0.1:11435
set CUDA_VISIBLE_DEVICES=1
REM ★ 핵심: BGE-M3 안정 병렬 처리 (16이 최적, 64는 OOM 위험)
set OLLAMA_NUM_PARALLEL=16
set OLLAMA_KEEP_ALIVE=5m
echo [GPU 1] Parallel Service Started...
ollama serve
pause