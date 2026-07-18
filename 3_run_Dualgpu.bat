@echo off
title Ollama DUAL GPU Mode
set OLLAMA_HOST=127.0.0.1:11434
REM ★ 0번과 1번 GPU를 모두 사용하도록 설정
set CUDA_VISIBLE_DEVICES=0,1
REM ★ 병렬 처리를 8~16 정도로 낮춰 요약 1개당 배정될 VRAM 자원을 극대화
set OLLAMA_NUM_PARALLEL=8
set OLLAMA_KEEP_ALIVE=5m
ollama serve
