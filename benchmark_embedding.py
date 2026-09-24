"""
===============================================================================
 Embedding Benchmark Script (llama.cpp focused)
 - 단건/배치 임베딩 레이턴시 및 처리량 측정
 - llama.cpp 서버가 실행 중이어야 합니다 (기본: http://127.0.0.1:8082)
===============================================================================
"""
import asyncio
import aiohttp
import time
import json
import sys
import os
import statistics

# 설정
CONFIG_FILE = 'config.json'

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Error: '{CONFIG_FILE}' Not Found.")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()
HOSTS = config['ollama']['hosts']

# ============================================================================
# 테스트 텍스트 (한국어/일본어/영어 혼합 - 실제 인덱싱 데이터와 유사한 길이)
# ============================================================================
SHORT_TEXT = "이것은 짧은 테스트 문장입니다. This is a short test sentence."

MEDIUM_TEXT = """소설 번역에서 가장 중요한 것은 원문의 뉘앙스를 살리면서도 자연스러운 한국어로 
전달하는 것입니다. 특히 일본 소설의 경우 존경어와 반말의 미묘한 차이, 문화적 맥락, 
그리고 작가 특유의 문체를 유지하는 것이 핵심입니다. BGE-M3 모델은 다국어 임베딩을 
지원하여 한국어, 일본어, 영어 텍스트를 동일한 벡터 공간에 매핑할 수 있습니다. 
이를 통해 교차 언어 검색이 가능해지며, 번역 품질 향상에도 기여합니다.
The BGE-M3 model supports multilingual embeddings, enabling cross-language search 
across Korean, Japanese, and English texts. This capability is essential for 
novel translation workflows where maintaining semantic similarity is critical."""

LONG_TEXT = MEDIUM_TEXT * 3  # ~1500자 (실제 청크 크기와 유사)

# ============================================================================
# 벤치마크 함수
# ============================================================================
async def single_embed(session, host, text, timeout_sec=30):
    """단건 임베딩 요청"""
    url = f"{host}/v1/embeddings"
    payload = {"model": "bge-m3", "input": [text]}
    
    start = time.perf_counter()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with session.post(url, json=payload, timeout=timeout) as resp:
            if resp.status == 200:
                result = await resp.json()
                elapsed = time.perf_counter() - start
                if 'data' in result and len(result['data']) > 0:
                    dim = len(result['data'][0]['embedding'])
                    return elapsed, dim
            else:
                error = await resp.text()
                print(f"  ❌ Status {resp.status}: {error[:200]}")
                return None, 0
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return None, 0

async def batch_embed(session, host, texts, timeout_sec=120):
    """배치 임베딩 요청"""
    url = f"{host}/v1/embeddings"
    payload = {"model": "bge-m3", "input": texts}
    
    start = time.perf_counter()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with session.post(url, json=payload, timeout=timeout) as resp:
            if resp.status == 200:
                result = await resp.json()
                elapsed = time.perf_counter() - start
                count = len(result.get('data', []))
                return elapsed, count
            else:
                error = await resp.text()
                print(f"  ❌ Status {resp.status}: {error[:200]}")
                return None, 0
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return None, 0

async def warmup(session, host):
    """워밍업 요청 (첫 요청은 느릴 수 있으므로)"""
    print(f"  🔄 Warming up {host}...")
    await single_embed(session, host, "warmup test", timeout_sec=60)
    await single_embed(session, host, "warmup test 2", timeout_sec=60)

async def run_benchmark():
    print("=" * 70)
    print("  📊 Embedding Benchmark (llama.cpp)")
    print("=" * 70)
    
    # 서버 가용성 확인
    active_hosts = []
    async with aiohttp.ClientSession() as session:
        for host in HOSTS:
            try:
                # llama.cpp 체크
                async with session.get(f"{host}/v1/models", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        active_hosts.append(('llamacpp', host))
                        print(f"  ✅ llama.cpp detected at {host}")
                        continue
            except:
                pass
            try:
                # Ollama 체크
                async with session.get(f"{host}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        active_hosts.append(('ollama', host))
                        print(f"  ✅ Ollama detected at {host}")
                        continue
            except:
                pass
            print(f"  ❌ No server at {host}")
    
    if not active_hosts:
        print("\n❌ 활성 서버가 없습니다. llama.cpp 또는 Ollama를 먼저 실행하세요.")
        return
    
    print(f"\n  📡 Active servers: {len(active_hosts)}")
    print("-" * 70)
    
    # 첫 번째 활성 서버로 벤치마크
    backend, host = active_hosts[0]
    
    connector = aiohttp.TCPConnector(limit=64, limit_per_host=32)
    async with aiohttp.ClientSession(connector=connector) as session:
        # 워밍업
        await warmup(session, host)
        
        REPEAT = 5  # 각 테스트 반복 횟수
        
        # ====================================================================
        # Test 1: 단건 레이턴시 (짧은 텍스트)
        # ====================================================================
        print(f"\n📌 Test 1: 단건 레이턴시 (짧은 텍스트, {len(SHORT_TEXT)}자)")
        times = []
        for i in range(REPEAT):
            elapsed, dim = await single_embed(session, host, SHORT_TEXT)
            if elapsed is not None:
                times.append(elapsed)
                print(f"   Run {i+1}: {elapsed*1000:.1f}ms (dim={dim})")
        if times:
            print(f"   → 평균: {statistics.mean(times)*1000:.1f}ms | 중앙값: {statistics.median(times)*1000:.1f}ms")
        
        # ====================================================================
        # Test 2: 단건 레이턴시 (중간 텍스트)
        # ====================================================================
        print(f"\n📌 Test 2: 단건 레이턴시 (중간 텍스트, {len(MEDIUM_TEXT)}자)")
        times = []
        for i in range(REPEAT):
            elapsed, dim = await single_embed(session, host, MEDIUM_TEXT)
            if elapsed is not None:
                times.append(elapsed)
                print(f"   Run {i+1}: {elapsed*1000:.1f}ms")
        if times:
            print(f"   → 평균: {statistics.mean(times)*1000:.1f}ms | 중앙값: {statistics.median(times)*1000:.1f}ms")
        
        # ====================================================================
        # Test 3: 단건 레이턴시 (긴 텍스트 ~1500자)
        # ====================================================================
        print(f"\n📌 Test 3: 단건 레이턴시 (긴 텍스트, {len(LONG_TEXT)}자)")
        times = []
        for i in range(REPEAT):
            elapsed, dim = await single_embed(session, host, LONG_TEXT)
            if elapsed is not None:
                times.append(elapsed)
                print(f"   Run {i+1}: {elapsed*1000:.1f}ms")
        if times:
            print(f"   → 평균: {statistics.mean(times)*1000:.1f}ms | 중앙값: {statistics.median(times)*1000:.1f}ms")
        
        # ====================================================================
        # Test 4: 배치 처리 - 크기별 비교
        # ====================================================================
        batch_sizes = [1, 8, 16, 32, 64]
        if backend == 'ollama':
            batch_sizes = [1, 4, 8]  # Ollama는 큰 배치 지원 안 함
        
        print(f"\n📌 Test 4: 배치 크기별 처리량 비교 (중간 텍스트 사용)")
        print(f"   {'배치 크기':>8} | {'총 시간':>10} | {'건당 시간':>10} | {'처리량':>12}")
        print(f"   {'-'*8} | {'-'*10} | {'-'*10} | {'-'*12}")
        
        for bs in batch_sizes:
            texts = [MEDIUM_TEXT] * bs
            batch_times = []
            for _ in range(3):  # 3회 반복
                elapsed, count = await batch_embed(session, host, texts)
                if elapsed is not None and count == bs:
                    batch_times.append(elapsed)
            
            if batch_times:
                avg = statistics.mean(batch_times)
                per_item = avg / bs
                throughput = bs / avg
                print(f"   {bs:>8} | {avg*1000:>8.1f}ms | {per_item*1000:>8.1f}ms | {throughput:>8.1f} items/s")
            else:
                print(f"   {bs:>8} | {'FAILED':>10} | {'N/A':>10} | {'N/A':>12}")
        
        # ====================================================================
        # Test 5: 듀얼 GPU 동시 처리량 (서버 2개 이상일 때)
        # ====================================================================
        if len(active_hosts) >= 2:
            print(f"\n📌 Test 5: 듀얼 GPU 동시 처리량")
            _, host1 = active_hosts[0]
            _, host2 = active_hosts[1]
            
            test_batch = [MEDIUM_TEXT] * 32
            
            # 순차 (GPU 하나씩)
            start = time.perf_counter()
            await batch_embed(session, host1, test_batch)
            await batch_embed(session, host2, test_batch)
            seq_time = time.perf_counter() - start
            
            # 병렬 (양쪽 동시)
            start = time.perf_counter()
            await asyncio.gather(
                batch_embed(session, host1, test_batch),
                batch_embed(session, host2, test_batch)
            )
            par_time = time.perf_counter() - start
            
            speedup = seq_time / par_time if par_time > 0 else 0
            print(f"   순차 (GPU0 → GPU1): {seq_time*1000:.0f}ms")
            print(f"   병렬 (GPU0 + GPU1): {par_time*1000:.0f}ms")
            print(f"   → 병렬 가속비: {speedup:.2f}x")
    
    print("\n" + "=" * 70)
    print("  ✅ 벤치마크 완료")
    print("=" * 70)

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run_benchmark())
