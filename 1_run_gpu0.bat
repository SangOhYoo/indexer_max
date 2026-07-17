@echo off
title Ollama GPU 0 (Port 11434) - HIGH PERF
set OLLAMA_HOST=127.0.0.1:11434
set CUDA_VISIBLE_DEVICES=0
REM ★ 핵심: BGE-M3 안정 병렬 처리 (16이 최적, 64는 OOM 위험)
set OLLAMA_NUM_PARALLEL=16
REM 모델이 메모리에서 내려가지 않도록 유지
set OLLAMA_KEEP_ALIVE=24h
echo [GPU 0] Parallel Service Started...
ollama serve
pause