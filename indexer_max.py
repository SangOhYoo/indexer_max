import asyncio
import aiohttp
import aiomysql
import json
import re
import html
import os
import time
import zlib
import sys
import argparse
import random
import warnings
import subprocess
import psutil  # [필수] pip install psutil
from datetime import datetime
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

# ==============================================================================
# 0. 환경 설정
# ==============================================================================
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)

CONFIG_FILE = 'config.json'

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Error: '{CONFIG_FILE}' Not Found.")
        sys.exit(1)
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Config Load Error: {e}")
        sys.exit(1)

config = load_config()
DB_CONFIG = config['database']
MANTICORE_CONFIG = config['manticore']
MANTICORE_INDEX = 'idx_novel'

TABLE_PREFIX = config['settings']['table_prefix']
OLLAMA_HOSTS = config['ollama']['hosts']
CHUNK_SIZE = config['settings']['chunk_size']
OVERLAP = config['settings']['overlap']
# BATCH_SIZE는 AutoTuner에 의해 동적으로 조절되므로 초기값으로만 사용됩니다.
INITIAL_BATCH_SIZE = config['settings']['batch_size'] 
CONCURRENCY = config['settings']['concurrency']

FETCH_LIMIT = 5000000

# [설정] 인덱싱 대상 필터 조건 (wr_good이 0보다 큰 경우, 즉 1부터 인덱싱)
# TARGET_CONDITION = " WHERE wr_good > 0 "
TARGET_CONDITION = ""

# ==============================================================================
# 1. [NEW] 오토 튜너 (AutoTuner) & 리소스 모니터링
# ==============================================================================
class AutoTuner:
    def __init__(self):
        # [초기 설정]
        self.current_batch = INITIAL_BATCH_SIZE    # 초기 배치 사이즈 (안전하게 시작)
        self.min_batch = 5         # 최소 배치 (너무 작으면 느림)
        self.max_batch = 200       # 최대 배치 (메모리 보호 상한선)
        
        self.cool_down = 0.0       # 과부하 시 휴식 시간
        self.check_interval = 1.0  # 1초마다 상태 체크
        self.last_check = 0
        
        # [임계값 설정]
        self.TARGET_LOAD = 85.0    # 목표 부하율 (이 수치 근처 유지 노력)
        self.CRITICAL_LIMIT = 95.0 # 위험 한계선 (즉시 감속)

    def get_dual_gpu_usage(self):
        """
        [핵심] 듀얼 GPU 사용률 체크
        nvidia-smi를 호출하여 GPU 중 가장 높은 메모리 사용률을 반환합니다.
        (보틀넥 방지를 위해 가장 힘든 자원을 기준으로 함)
        """
        try:
            # GPU별 메모리 사용량 쿼리
            result = subprocess.run(
                ['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,nounits,noheader'],
                capture_output=True, text=True
            )
            if result.returncode != 0: return 0.0
            
            lines = result.stdout.strip().split('\n')
            max_gpu_usage = 0.0
            
            for line in lines:
                if not line: continue
                parts = line.split(',')
                if len(parts) == 2:
                    used = float(parts[0].strip())
                    total = float(parts[1].strip())
                    if total > 0:
                        usage = (used / total) * 100
                        if usage > max_gpu_usage:
                            max_gpu_usage = usage
            return max_gpu_usage
        except:
            return 0.0

    def tune(self):
        """
        [보틀넥 방지 알고리즘]
        CPU, RAM, GPU 중 가장 바쁜 자원(Max Load)을 기준으로 배치를 조절
        """
        now = time.time()
        # 너무 자주 체크하면 오버헤드 발생하므로 인터벌 체크
        if now - self.last_check < self.check_interval:
            return self.current_batch, self.cool_down
        
        self.last_check = now
        
        # 1. 모든 자원 상태 측정
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        gpu = self.get_dual_gpu_usage()
        
        # 2. 보틀넥(Limiting Factor) 식별
        # 시스템의 속도는 가장 느린(부하가 높은) 자원에 의해 결정됨
        current_load = max(cpu, ram, gpu)
        
        # 3. 유기적 튜닝 로직
        if current_load > self.CRITICAL_LIMIT:
            # [위험] 95% 초과: 터지기 일보 직전 -> 배치를 절반으로 뚝 자름
            old_batch = self.current_batch
            self.current_batch = max(self.min_batch, int(self.current_batch * 0.5))
            self.cool_down = min(5.0, self.cool_down + 1.0) # 열 식힐 시간 부여
            print(f"   🔥 [CRITICAL] Load {current_load:.1f}% (Bottleneck) -> Batch Cut: {old_batch} -> {self.current_batch}")
            
        elif current_load > self.TARGET_LOAD:
            # [경고] 85% ~ 95%: 조금 버거움 -> 배치를 살짝 줄여서 안정화
            self.current_batch = max(self.min_batch, int(self.current_batch * 0.9))
            self.cool_down = min(1.0, self.cool_down + 0.1)
            
        elif current_load < 50.0:
            # [유휴] 50% 미만: 자원이 펑펑 놂 -> 배치를 과감하게 늘림 (가속)
            increase = int(self.current_batch * 0.2) + 1
            self.current_batch = min(self.max_batch, self.current_batch + increase)
            self.cool_down = max(0.0, self.cool_down - 0.5) # 쿨다운 해제
            # print(f"   🚀 [Boost] System Idle ({current_load:.1f}%) -> Batch Up: {self.current_batch}")
            
        else:
            # [안정] 50% ~ 85%: 아주 좋음 -> 미세하게 늘려서 한계까지 밀어봄 (탐색)
            self.current_batch = min(self.max_batch, self.current_batch + 1)
            self.cool_down = max(0.0, self.cool_down - 0.1)
            
        return self.current_batch, self.cool_down

# 전역 튜너 인스턴스
tuner = AutoTuner()

# ==============================================================================
# 2. 유틸리티 함수
# ==============================================================================
def clean_text(content):
    if not content: return ""
    
    import unicodedata
    
    # 1. 유니코드 정규화
    content = unicodedata.normalize('NFKC', str(content))

    # 2. HTML 태그 제거
    content = html.unescape(content)
    content = content.replace('<br>', ' ').replace('<br/>', ' ').replace('</p>', ' ').replace('</div>', ' ')
    content = html.unescape(content)

    try:
        soup = BeautifulSoup(content, "lxml")
        text = soup.get_text(separator="  ") 
    except:
        text = str(content)

    # ==============================================================
    # [NEW] 강제 줄바꿈(Hard Wrap) 해결 로직 (Step 3 이전에 수행 필수)
    # ==============================================================
    
    # 2-1. 명확한 문단 구분(\n\n)은 보호 (임시 마커로 치환)
    text = re.sub(r'\n\s*\n', '<<PARAGRAPH_BREAK>>', text)
    
    # 2-2. [일본어 전용] 일본어 문자 뒤의 줄바꿈은 "공백 없이" 잇는다.
    # 범위: 히라가나(ぁ-ん), 가타카나(ァ-ン), 한자(一-龥)
    text = re.sub(r'([ぁ-んァ-ン一-龥])\n', r'\1', text)
    
    # 2-3. [한국어/기타] 문장 부호가 아닌 글자 뒤의 줄바꿈은 "공백을 넣고" 잇는다.
    # 조건: 마침표, 물음표, 느낌표, 따옴표, 괄호, 그리고 일본어 문자가 '아닌' 경우
    text = re.sub(r'([^.?!~"\'」』ぁ-んァ-ン一-龥])\n', r'\1 ', text)
    
    # 2-4. 보호했던 문단 구분을 공백으로 복원 (나중에 whitespace 정리됨)
    text = text.replace('<<PARAGRAPH_BREAK>>', ' ')
    
    # 2-5. 남은 줄바꿈 문자 제거 (Step 3에서 삭제되겠지만 명시적으로 공백 처리)
    text = text.replace('\n', ' ')
    # ==============================================================

    # 3. 유령 문자(제어 문자) 삭제
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")

    # 4. 찌꺼기 제거
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[a-zA-Z]+="[^"]*"', '', text)
    text = re.sub(r'&[a-z]+;', '', text)
    text = re.sub(r'http[s]?://\S+', '', text)
    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', text)
    
    # 5. 괄호 변환
    text = text.replace('[', '(').replace(']', ')')
    text = text.replace('(', ' ( ').replace(')', ' ) ') 

    # [NEW] 일본어 괄호 -> 일반 따옴표
    text = re.sub(r'[「」『』]', ' " ', text)
    
    # [NEW] "続き..." 삭제
    text = re.sub(r'(続き|つづき)[.．…。]*$', '', text)

    # 6. 텍스트 분리
    text = re.sub(r'([a-zA-Z가-힣])(\d+\.)', r'\1 \2', text)
    text = re.sub(r'([가-힣])([a-zA-Z0-9])', r'\1 \2', text)
    text = re.sub(r'([a-zA-Z0-9])([가-힣])', r'\1 \2', text)
    text = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', text)
    text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', text)
    text = re.sub(r'([!?.])(\S)', r'\1 \2', text)

    # 7. 목차 번호 삭제
    text = re.sub(r'(^|\s)\d+\.', ' ', text)
    
    # 8. 기호 및 반복 정리
    text = text.replace('、', ',') 
    text = re.sub(r'(,\s*)+', ', ', text)
    text = re.sub(r'(\S+[!?.~]\s?)\1{2,}', r'\1\1', text)

    text = re.sub(r'[~〜]{2,}', '~', text)
    text = re.sub(r'[ー−]{2,}', 'ー', text)
    text = re.sub(r'(・\s*){2,}', '...', text)
    text = re.sub(r'(\.\s*){2,}', '...', text)
    text = re.sub(r'[!！]{2,}', '!', text)
    text = re.sub(r'[?？]{2,}', '?', text)
    text = re.sub(r'。{2,}', '。', text)

    text = re.sub(r'(\D)\1{5,}', r'\1\1\1', text)
    text = re.sub(r'\S{100,}', '', text) 

    # 공백이 들어간 상태에서도 ... 압축이 잘 되도록 수정
    text = re.sub(r'(\.\s*){2,}', ' ... ', text)
    
    # 공백 정리
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()

    # [NEW] 문장 앞뒤 기호 찌꺼기 강제 제거
    text = text.strip(' !?.,~ー。";')
    
    return text

def chunk_text(text):
    # [스마트 가변 청크 적용]
    # MAX_LENGTH를 2000으로 제한: CJK 문자는 ~1토큰/글자이므로 llama.cpp batch_size(2048) 이내 유지
    MAX_LENGTH = 2000
    MIN_LENGTH = 1000
    LOOKBACK_RANGE = 500
    OVERLAP_SIZE = OVERLAP

    length = len(text)
    chunks = []
    
    if length <= MAX_LENGTH:
        if text.strip(): chunks.append(text.strip())
        return chunks
    
    offset = 0
    while offset < length:
        end_pos = min(offset + MAX_LENGTH, length)
        
        if end_pos == length:
            chunk = text[offset:end_pos]
            if chunk.strip(): chunks.append(chunk.strip())
            break
            
        search_start = max(offset + MIN_LENGTH, end_pos - LOOKBACK_RANGE)
        search_text = text[search_start:end_pos]
        
        cut_point = -1
        newline_match = list(re.finditer(r'\n', search_text))
        if newline_match:
            cut_point = search_start + newline_match[-1].end()
            
        if cut_point == -1:
            sent_match = list(re.finditer(r'[.?!。！？][\s\n]', search_text))
            if sent_match:
                cut_point = search_start + sent_match[-1].end()
        
        if cut_point == -1: cut_point = end_pos

        real_chunk = text[offset:cut_point]
        if real_chunk.strip(): chunks.append(real_chunk.strip())
        
        offset = cut_point - OVERLAP_SIZE
        if not (offset <= offset + OVERLAP_SIZE and offset < cut_point):
             offset = cut_point

    return chunks

def generate_manticore_id(bo_table, wr_id, seq):
    board_hash = zlib.crc32(bo_table.encode('utf-8')) & 0x1FF 
    m_id = (board_hash << 54) | (wr_id << 20) | (seq & 0xFFFFF)
    return m_id, board_hash

def parse_timestamp(value):
    if not value: return 0
    if isinstance(value, datetime): return int(value.timestamp())
    if isinstance(value, str):
        try:
            if value == '0000-00-00 00:00:00': return 0
            dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp())
        except ValueError:
            pass
    return 0

# ==============================================================================
# 3. 핵심 로직 (Async)
# ==============================================================================
async def check_and_create_table(pool_manticore):
    print(f"🔍 Checking Manticore table: '{MANTICORE_INDEX}'...")
    async with pool_manticore.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SHOW TABLES LIKE '{MANTICORE_INDEX}'")
            result = await cur.fetchone()
            if result:
                print(f"✅ Table '{MANTICORE_INDEX}' already exists.")
                return

            print(f"⚠️ Table '{MANTICORE_INDEX}' not found. Creating with optimized schema...")
            
            # [Manticore 7.x Syntax] Attribute 키워드 제거 + 8bit 양자화 + 128M 메모리 제한
            create_sql = f"""CREATE TABLE {MANTICORE_INDEX} (
                title       text stored,
                content     text stored,
                author      text stored,
                category    text stored,
                tags        text stored,
                
                bo_table    string,
                wr_id       bigint,
                bn_id       bigint,
                chunk_seq   integer,
                wr_last     timestamp,
                is_comment  integer,
                
                embedding   float_vector knn_type='hnsw' knn_dims='1024' hnsw_similarity='COSINE' hnsw_m='8' hnsw_ef_construction='64' quantization='8bit'
            ) rt_mem_limit='128M' access_blob_attrs='mmap' access_plain_attrs='mmap' min_prefix_len='2' min_infix_len='2' html_strip='1' ngram_len='2' ngram_chars='cjk'"""
            
            try:
                await cur.execute(create_sql)
                print(f"🎉 Table '{MANTICORE_INDEX}' successfully created!")
            except Exception as e:
                err_msg = str(e)
                # [복구 로직] 디렉토리가 비어있지 않다는 오류(1064) 발생 시 IMPORT 시도
                if "directory is not empty" in err_msg:
                    print(f"⚠️ Directory exists but table is not registered. Attempting to RECOVER (IMPORT)...")
                    try:
                        # Manticore 4.2+ 에서 RT 테이블 복구는 IMPORT TABLE 명령 사용
                        # 경로가 /var/lib/manticore/data/idx_novel 라면 Manticore 내부에 이미 존재한다는 의미
                        import_sql = f"IMPORT TABLE {MANTICORE_INDEX} FROM '{MANTICORE_INDEX}'"
                        await cur.execute(import_sql)
                        print(f"✅ Success! Table '{MANTICORE_INDEX}' recovered from existing data.")
                        return
                    except Exception as import_err:
                        print(f"❌ Recovery failed: {import_err}")
                
                print(f"❌ Failed to create or recover table: {e}")
                # [수정] sys.exit(1) 대신 예외를 발생시켜 main에서 처리하게 함
                raise RuntimeError(f"Manticore table setup failed: {e}")

async def get_embedding(session, text, host_idx, sem, bo_table):
    if not text or len(text.strip()) < 2: return None
    url = f"{OLLAMA_HOSTS[host_idx]}/v1/embeddings"
    max_retries = 10
    current_text = text  # 토큰 초과 시 텍스트를 줄여가며 재시도
    
    async with sem:
        for attempt in range(max_retries):
            if attempt > 0: 
                wait_time = (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(wait_time)
            
            payload = {"model": "bge-m3", "input": [current_text]}
            start_time = time.time()
            try:
                timeout = aiohttp.ClientTimeout(total=60) 
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if 'data' in result and len(result['data']) > 0:
                            return result['data'][0]['embedding']
                        return None
                    elif resp.status == 500:
                        error_msg = await resp.text()
                        if "NaN" in error_msg or "unsupported value" in error_msg:
                            print(f"\n🗑️ [Skip-NaN] Toxic Text Detected")
                            return None
                        # [NEW] 토큰 초과 에러 감지 → 텍스트를 80%로 잘라서 재시도
                        if "too large to process" in error_msg or "batch size" in error_msg:
                            old_len = len(current_text)
                            current_text = current_text[:int(len(current_text) * 0.8)]
                            print(f"\n⚠️ [Token Overflow] Text too long ({old_len} chars). Trimmed to {len(current_text)} chars, retrying...")
                            continue
                        if attempt < max_retries - 1:
                            continue 
                        else:
                            return None
                    else:
                        if attempt < max_retries - 1: continue
                        return None
            except Exception as e:
                if attempt < max_retries - 1: continue
                else: return None
    return None

async def get_parent_info(conn, write_table, parent_id, cache):
    if parent_id in cache: return cache[parent_id]
    if len(cache) > 5000: cache.clear()
    async with conn.cursor(aiomysql.DictCursor) as cur:
        await cur.execute(f"SELECT wr_subject, ca_name FROM {write_table} WHERE wr_id = {parent_id}")
        row = await cur.fetchone()
        if row: cache[parent_id] = row
        return row

async def delete_from_manticore(pool_manticore, bo_table, wr_id_list):
    if not wr_id_list: return
    batch_size = 200
    total = len(wr_id_list)
    async with pool_manticore.acquire() as conn:
        async with conn.cursor() as cur:
            for i in range(0, total, batch_size):
                batch = wr_id_list[i:i+batch_size]
                ids_str = ",".join(map(str, batch))
                sql = f"DELETE FROM {MANTICORE_INDEX} WHERE bo_table='{bo_table}' AND wr_id IN ({ids_str})"
                await cur.execute(sql)

async def insert_bulk_manticore(pool_manticore, values_list):
    if not values_list: return
    SAFE_CHUNK_LIMIT = 2000 
    total_count = len(values_list)
    
    async with pool_manticore.acquire() as conn:
        async with conn.cursor() as cur:
            for i in range(0, total_count, SAFE_CHUNK_LIMIT):
                current_batch = values_list[i : i + SAFE_CHUNK_LIMIT]
                placeholders = []
                params = []
                for val in current_batch:
                    placeholders.append("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, " + val[-1] + ")")
                    params.extend(val[:-1]) 
                sql = f"""INSERT INTO {MANTICORE_INDEX} 
                    (id, title, content, author, category, tags, bo_table, wr_id, bn_id, chunk_seq, wr_last, is_comment, embedding)
                    VALUES {", ".join(placeholders)}"""
                max_db_retries = 3
                for db_attempt in range(max_db_retries):
                    try:
                        await cur.execute(sql, tuple(params))
                        break 
                    except Exception as e:
                        if db_attempt < max_db_retries - 1:
                            print(f"\n⚠️ [DB Retry-{db_attempt+1}] Insert Failed. Retrying...")
                            await asyncio.sleep(1)
                        else:
                            print(f"\n❌ [DB Fail] Final Insert Error: {e}")

async def process_item_embedding(session, row, bo_table, parent_cache, sem):
    wr_id = row['wr_id']
    clean_body = clean_text(row['wr_content'])
    if not clean_body or len(clean_body.strip()) < 10: return []

    final_subject = row['wr_subject']
    final_category = row['ca_name'] if row['ca_name'] else ""
    is_comment_val = 0
    wr_last_ts = parse_timestamp(row['wr_last'])
    
    chunks = chunk_text(clean_body)
    results = []
    tasks = []
    valid_chunks = []
    valid_indices = []

    for idx, chunk in enumerate(chunks):
        if not chunk or not chunk.strip(): continue
        if len(chunk) < 5: continue
        valid_chunks.append(chunk)
        valid_indices.append(idx)

    if not valid_chunks: return []

    for i, chunk in enumerate(valid_chunks):
        host_idx = (wr_id + i) % len(OLLAMA_HOSTS)
        tasks.append(get_embedding(session, chunk, host_idx, sem, bo_table))
    
    embeddings = await asyncio.gather(*tasks)

    for i, embedding in enumerate(embeddings):
        if not embedding: continue
        real_idx = valid_indices[i]
        m_id, board_hash = generate_manticore_id(bo_table, wr_id, real_idx)
        vec_str = "(" + ",".join(map(str, embedding)) + ")"
        results.append((
            m_id, final_subject, valid_chunks[i], row['wr_name'], final_category,
            row['wr_1'] if row['wr_1'] else '', bo_table, wr_id, board_hash, 
            real_idx, wr_last_ts, is_comment_val, vec_str
        ))
    return results

async def sync_board(session, pool_gnuboard, pool_manticore, bo_table, target_category="", force_update=False):
    write_table = f"{TABLE_PREFIX}{bo_table}"
    category_msg = f" (Category: {target_category})" if target_category else ""
    print(f"\n🔄 [SYNC START] Comparing Table: {write_table}{category_msg}")
    
    gb_map = {} 
    mc_map = {}

    # 1. 메타데이터 수집 (그누보드)
    try:
        async with pool_gnuboard.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"SHOW TABLES LIKE '{write_table}'")
                if not await cur.fetchone():
                    print(f"⚠️ Table skipped: {write_table}")
                    return
                print(f"   Reading Gnuboard metadata...")
                where_clause = TARGET_CONDITION
                params = []
                if target_category:
                    if where_clause.strip():
                        where_clause += " AND ca_name = %s "
                    else:
                        where_clause = " WHERE ca_name = %s "
                    params.append(target_category)
                        
                query = f"SELECT wr_id, wr_last FROM {write_table} {where_clause}"
                if params:
                    await cur.execute(query, tuple(params))
                else:
                    await cur.execute(query)
                rows = await cur.fetchall()
                for r in rows: gb_map[r[0]] = parse_timestamp(r[1])
    except Exception as e:
        print(f"❌ Error reading Gnuboard: {e}")
        return

    # 2. 메타데이터 수집 (Manticore)
    try:
        async with pool_manticore.acquire() as conn:
            async with conn.cursor() as cur:
                print(f"   Reading Manticore metadata...")
                sql = f"SELECT wr_id, wr_last FROM {MANTICORE_INDEX} WHERE bo_table='{bo_table}' GROUP BY wr_id LIMIT {FETCH_LIMIT} OPTION max_matches={FETCH_LIMIT}"
                await cur.execute(sql)
                rows = await cur.fetchall()
                for r in rows: mc_map[r[0]] = int(r[1])
    except Exception as e:
        print(f"❌ Error reading Manticore: {e}")
        return

    gb_ids = set(gb_map.keys())
    mc_ids = set(mc_map.keys())
    
    to_delete = []
    # 특정 카테고리를 지정한 경우, 전체 삭제 대조를 하면 다른 카테고리가 날아가는 위험 방지
    if not target_category:
        to_delete = list(mc_ids - gb_ids)
        
    to_upsert = [] 
    
    for wr_id in gb_ids:
        if wr_id not in mc_ids:
            to_upsert.append(wr_id)
        else:
            # 강제 업데이트이거나 시간이 다르면 업데이트
            if force_update or gb_map[wr_id] != mc_map[wr_id]:
                to_upsert.append(wr_id)

    print(f"📊 {bo_table} -> Delete: {len(to_delete)}, Upsert: {len(to_upsert)}")

    if to_delete:
        print(f"🗑️ Deleting {len(to_delete)} removed posts...")
        await delete_from_manticore(pool_manticore, bo_table, to_delete)

    if not to_upsert:
        print("✅ No updates needed.")
        del gb_map, mc_map, to_delete, to_upsert
        import gc; gc.collect()
        return

    update_ids = [uid for uid in to_upsert if uid in mc_ids]
    if update_ids:
        print(f"🧹 Pre-cleaning {len(update_ids)} posts for update...")
        await delete_from_manticore(pool_manticore, bo_table, update_ids)

    to_upsert.sort()
    total_ops = len(to_upsert)
    sem = asyncio.Semaphore(CONCURRENCY)
    parent_cache = {}

    current_idx = 0
    
    while current_idx < total_ops:
        dynamic_batch, cool_down = tuner.tune()
        if cool_down > 0: await asyncio.sleep(cool_down)

        end_idx = min(current_idx + dynamic_batch, total_ops)
        batch_ids = to_upsert[current_idx : end_idx]
        ids_str = ",".join(map(str, batch_ids))
        
        rows = []
        # ======================================================================
        # [수정] 데이터 인코딩 오류 방지 로직 (Safe Fetch)
        # ======================================================================
        try:
            async with pool_gnuboard.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    sql = f"""SELECT wr_id, wr_parent, wr_is_comment, wr_subject, wr_content, wr_name, ca_name, wr_1, wr_last 
                        FROM {write_table} WHERE wr_id IN ({ids_str})"""
                    await cur.execute(sql)
                    rows = await cur.fetchall()
        
        except UnicodeDecodeError:
            print(f"\n⚠️ [Encoding Error] Batch fetch failed. Retrying item-by-item to isolate corrupt data...")
            rows = []
            # 배치 전체가 실패했으므로, 하나씩 가져와서 문제 있는 녀석만 건너뜀
            for single_id in batch_ids:
                try:
                    async with pool_gnuboard.acquire() as conn:
                        async with conn.cursor(aiomysql.DictCursor) as cur:
                            s_sql = f"""SELECT wr_id, wr_parent, wr_is_comment, wr_subject, wr_content, wr_name, ca_name, wr_1, wr_last 
                                FROM {write_table} WHERE wr_id = {single_id}"""
                            await cur.execute(s_sql)
                            one_row = await cur.fetchone()
                            if one_row: rows.append(one_row)
                except UnicodeDecodeError:
                    print(f"   ❌ Skipping Corrupt Post ID: {single_id} (Data damaged)")
                except Exception as e:
                    print(f"   ❌ Error on ID {single_id}: {e}")
        # ======================================================================

        processing_tasks = []
        current_conn = None 

        for row in rows:
            if row['wr_is_comment'] == 1:
                # 댓글 부모 정보 조회 시에도 오류 가능성 있으므로 예외 처리
                try:
                    if not current_conn: current_conn = await pool_gnuboard.acquire()
                    p_info = await get_parent_info(current_conn, write_table, row['wr_parent'], parent_cache)
                    if p_info:
                        row['wr_subject'] = f"↳ [Comment] {p_info['wr_subject']}"
                        row['ca_name'] = f"{p_info['ca_name']} (Comment)" if row['ca_name'] else "Comment"
                    else:
                        row['wr_subject'] = "[Comment] (Deleted)"
                except Exception:
                     row['wr_subject'] = "[Comment] (Parent Info Error)"

            processing_tasks.append(process_item_embedding(session, row, bo_table, parent_cache, sem))

        if current_conn: pool_gnuboard.release(current_conn)

        processed_batches = await asyncio.gather(*processing_tasks)
        bulk_values = []
        for item_list in processed_batches:
            bulk_values.extend(item_list)
        
        if bulk_values:
            await insert_bulk_manticore(pool_manticore, bulk_values)

        current_idx = end_idx
        print(f"\r🚀 Processing {bo_table}: {current_idx}/{total_ops} (Batch: {dynamic_batch}) ...", end="")

    print(f"\n✨ {bo_table} Sync Completed.")
    
    del gb_map, mc_map, to_delete, to_upsert, rows, processing_tasks
    import gc; gc.collect()

# ==============================================================================
# 4. 메인 실행
# ==============================================================================
async def main():
    print(f"=== Python Indexer V7.1 (Auto-Tuned) ===")
    
    parser = argparse.ArgumentParser(description="Python Indexer with Manual Sync")
    parser.add_argument('-b', '--board', type=str, default='', help='작업할 게시판명 (bo_table)')
    parser.add_argument('-c', '--category', type=str, default='', help='작업할 카테고리명 (ca_name)')
    parser.add_argument('-f', '--force', action='store_true', help='기존 데이터를 무시하고 강제 재색인')
    
    args = parser.parse_args()
    
    target_board_input = args.board.strip()
    target_category_input = args.category.strip()
    force_update = args.force

    if target_board_input:
        print(f"\n[ 수동 작업 모드 ]")
        print(f" 👉 게시판: {target_board_input}")
        if target_category_input:
            print(f" 👉 카테고리: {target_category_input}")
        if force_update:
            print(f" 👉 강제 업데이트(Force) 활성화됨")
        print("=" * 40 + "\n")

    pool_gnuboard = await aiomysql.create_pool(**DB_CONFIG, autocommit=True)
    pool_manticore = await aiomysql.create_pool(
        host=MANTICORE_CONFIG['host'], port=MANTICORE_CONFIG['port'], 
        user=MANTICORE_CONFIG['user'], password=MANTICORE_CONFIG['password'], 
        db=MANTICORE_CONFIG['db'], autocommit=True
    )
    print("✅ DB Connected.")

    try:
        await check_and_create_table(pool_manticore)

        async with aiohttp.ClientSession() as session:
            target_boards = []
            if target_board_input:
                target_boards.append(target_board_input)
            else:
                async with pool_gnuboard.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(f"SHOW TABLES LIKE '{TABLE_PREFIX}%'")
                        result = await cur.fetchall()
                        for r in result:
                            bo_id = r[0][len(TABLE_PREFIX):]
                            if bo_id: target_boards.append(bo_id)
                
                PRIORITY_LIST = ['noc','jp','wm','private','trs','sora','wolf','yajun',] 
                target_boards.sort(key=lambda x: PRIORITY_LIST.index(x) if x in PRIORITY_LIST else 999)
            
            print(f"📋 Found {len(target_boards)} boards.")
            
            for bo_table in target_boards:
                await sync_board(session, pool_gnuboard, pool_manticore, bo_table, target_category_input, force_update)
                
                # 게시판 하나 끝날 때마다 인덱스 최적화
                try:
                    print("\n🧹 Flushing RAM to Disk...")
                    async with pool_manticore.acquire() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(f"FLUSH RAMCHUNK {MANTICORE_INDEX}")
                except: pass

    except Exception as e:
        print(f"\n❌ Critical Error: {e}")
    finally:
        # [수정] 어떠한 경우에도 커넥션 풀을 닫도록 보장 (RuntimeError 방지)
        print("\n🔌 Closing DB connections...")
        pool_gnuboard.close()
        await pool_gnuboard.wait_closed()
        pool_manticore.close()
        await pool_manticore.wait_closed()
        print("👋 Finished.")

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())