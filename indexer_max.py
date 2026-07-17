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
import unicodedata
import gc
import psutil  # [필수] pip install psutil
from datetime import datetime
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

# ==============================================================================
# 0. 환경 설정
# ==============================================================================
# [한글 및 이모지 깨짐/에러 방지]
try:
    if sys.stdout and sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr and sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

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
# [PERF] 정규식 사전 컴파일 (80만건 처리 시 컴파일 오버헤드 제거)
# ==============================================================================
RE_PARAGRAPH_BREAK = re.compile(r'\n\s*\n')
RE_JP_NEWLINE = re.compile(r'([ぁ-んァ-ン一-龥])\n')
RE_KR_NEWLINE = re.compile(r'([^.?!~"\'」』ぁ-んァ-ン一-龥])\n')
RE_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')
RE_HTML_TAG = re.compile(r'<[^>]+>')
RE_HTML_ATTR = re.compile(r'[a-zA-Z]+="[^"]*"')
RE_HTML_ENTITY = re.compile(r'&[a-z]+;')
RE_URL = re.compile(r'http[s]?://\S+')
RE_EMAIL = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
RE_JP_BRACKETS = re.compile(r'[「」『』]')
RE_TSUZUKI = re.compile(r'(続き|つづき)[.．…。]*$')
RE_KR_NUM = re.compile(r'([a-zA-Z가-힣])(\d+\.)')
RE_KR_EN1 = re.compile(r'([가-힣])([a-zA-Z0-9])')
RE_KR_EN2 = re.compile(r'([a-zA-Z0-9])([가-힣])')
RE_EN_NUM1 = re.compile(r'([a-zA-Z])(\d)')
RE_EN_NUM2 = re.compile(r'(\d)([a-zA-Z])')
RE_CAMEL1 = re.compile(r'([a-z])([A-Z])')
RE_CAMEL2 = re.compile(r'([A-Z]+)([A-Z][a-z])')
RE_PUNCT_SPACE = re.compile(r'([!?.])(\\S)')
RE_TOCI_NUM = re.compile(r'(^|\s)\d+\.')
RE_COMMA_DUP = re.compile(r'(,\s*)+')
RE_REPEAT_SENT = re.compile(r'(\S+[!?.~]\s?)\1{2,}')
RE_TILDE = re.compile(r'[~〜]{2,}')
RE_DASH = re.compile(r'[ー−]{2,}')
RE_DOTS = re.compile(r'(・\s*){2,}')
RE_EXCL = re.compile(r'[!！]{2,}')
RE_QUEST = re.compile(r'[?？]{2,}')
RE_PERIOD_JP = re.compile(r'。{2,}')
RE_CHAR_REPEAT = re.compile(r'(\D)\1{5,}')
RE_LONG_STR = re.compile(r'\S{100,}')
RE_ELLIPSIS = re.compile(r'(\.\s*){2,}')
RE_MULTI_SPACE = re.compile(r'\s+')
# 청크용
RE_SENT_BOUNDARY = re.compile(r'[.?!。！？]\s')
RE_JP_SENT_END = re.compile(r'[。！？」』)\n]')
RE_KR_SENT_END = re.compile(r'[.?!]\s')
RE_ANY_SENT_END = re.compile(r'(?:[.?!。！？]["\s」』])|(?:\n)')

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

    async def get_dual_gpu_usage(self):
        """
        [핵심] 듀얼 GPU 사용률 체크
        nvidia-smi를 호출하여 GPU 중 가장 높은 메모리 사용률을 반환합니다.
        (보틀넥 방지를 위해 가장 힘든 자원을 기준으로 함)
        """
        def _query_gpu():
            """동기 함수: subprocess.run으로 nvidia-smi 호출 (파이프 hang 방지)"""
            try:
                result = subprocess.run(
                    ['nvidia-smi', '--query-gpu=memory.used,memory.total', '--format=csv,nounits,noheader'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode != 0:
                    return 0.0

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
                            max_gpu_usage = max(max_gpu_usage, usage)
                return max_gpu_usage
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return 0.0
            except Exception:
                return 0.0

        try:
            # [수정] run_in_executor로 동기 subprocess 호출 → ProactorEventLoop 파이프 hang 완전 우회
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, _query_gpu)
        except Exception:
            return 0.0

    async def tune(self):
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
        gpu = await self.get_dual_gpu_usage()
        
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
    
    # 1. 유니코드 정규화
    content = unicodedata.normalize('NFKC', str(content))

    # 2. HTML 태그 제거
    content = html.unescape(content)
    content = content.replace('<br>', ' ').replace('<br/>', ' ').replace('</p>', ' ').replace('</div>', ' ')
    content = html.unescape(content)

    try:
        soup = BeautifulSoup(content, "lxml")
        text = soup.get_text(separator="  ") 
    except Exception:
        text = str(content)

    # [강제 줄바꿈 해결]
    text = RE_PARAGRAPH_BREAK.sub('<<PARAGRAPH_BREAK>>', text)
    text = RE_JP_NEWLINE.sub(r'\1', text)
    text = RE_KR_NEWLINE.sub(r'\1 ', text)
    text = text.replace('<<PARAGRAPH_BREAK>>', ' ')
    text = text.replace('\n', ' ')

    # 3. 제어 문자 삭제 (최적화: regex로 한번에)
    text = RE_CONTROL_CHARS.sub('', text)

    # 4. 찌꺼기 제거
    text = RE_HTML_TAG.sub('', text)
    text = RE_HTML_ATTR.sub('', text)
    text = RE_HTML_ENTITY.sub('', text)
    text = RE_URL.sub('', text)
    text = RE_EMAIL.sub('', text)
    
    # 5. 괄호 변환
    text = text.replace('[', '(').replace(']', ')')
    text = text.replace('(', ' ( ').replace(')', ' ) ') 

    # 일본어 괄호 -> 일반 따옴표
    text = RE_JP_BRACKETS.sub(' " ', text)
    
    # "続き..." 삭제
    text = RE_TSUZUKI.sub('', text)

    # 6. 텍스트 분리
    text = RE_KR_NUM.sub(r'\1 \2', text)
    text = RE_KR_EN1.sub(r'\1 \2', text)
    text = RE_KR_EN2.sub(r'\1 \2', text)
    text = RE_EN_NUM1.sub(r'\1 \2', text)
    text = RE_EN_NUM2.sub(r'\1 \2', text)
    text = RE_CAMEL1.sub(r'\1 \2', text)
    text = RE_CAMEL2.sub(r'\1 \2', text)
    text = RE_PUNCT_SPACE.sub(r'\1 \2', text)

    # 7. 목차 번호 삭제
    text = RE_TOCI_NUM.sub(' ', text)
    
    # 8. 기호 및 반복 정리
    text = text.replace('、', ',') 
    text = RE_COMMA_DUP.sub(', ', text)
    text = RE_REPEAT_SENT.sub(r'\1\1', text)
    text = RE_TILDE.sub('~', text)
    text = RE_DASH.sub('ー', text)
    text = RE_DOTS.sub('...', text)
    text = RE_EXCL.sub('!', text)
    text = RE_QUEST.sub('?', text)
    text = RE_PERIOD_JP.sub('。', text)
    text = RE_CHAR_REPEAT.sub(r'\1\1\1', text)
    text = RE_LONG_STR.sub('', text) 

    # 공백이 들어간 상태에서도 ... 압축
    text = RE_ELLIPSIS.sub(' ... ', text)
    
    # 공백 정리
    text = RE_MULTI_SPACE.sub(' ', text)
    text = text.strip()

    # 문장 앞뒤 기호 찌꺼기 강제 제거
    text = text.strip(' !?.,~ー。";')
    
    return text

def chunk_text(text):
    # [BGE-M3 최적화 청크]
    # BGE-M3 max tokens = 8192, 한국어/일본어 1문자 ≈ 2~3 tokens
    # 안전 한도: 2000자 (≈ 5000 tokens, Manticore 메모리와 균형)
    MAX_LENGTH = min(CHUNK_SIZE, 2000)
    MIN_LENGTH = 300    # 너무 짧은 청크 방지 (검색 품질 저하)
    LOOKBACK_RANGE = 500
    OVERLAP_SIZE = max(OVERLAP, 200)  # 의미 연속성 보장을 위해 최소 200

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
        
        # 1순위: 문단 경계 (줄바꿈)
        newline_match = list(re.finditer(r'\n', search_text))
        if newline_match:
            cut_point = search_start + newline_match[-1].end()
        
        # 2순위: 문장 경계 (한/일/영 모두)
        if cut_point == -1:
            sent_match = list(RE_ANY_SENT_END.finditer(search_text))
            if sent_match:
                cut_point = search_start + sent_match[-1].end()
        
        # 3순위: 쉼표/반점 경계 (최후 수단)
        if cut_point == -1:
            comma_match = list(re.finditer(r'[,、]\s', search_text))
            if comma_match:
                cut_point = search_start + comma_match[-1].end()
        
        if cut_point == -1: cut_point = end_pos

        real_chunk = text[offset:cut_point]
        if real_chunk.strip(): chunks.append(real_chunk.strip())
        
        next_offset = cut_point - OVERLAP_SIZE
        # offset이 반드시 전진하도록 보장 (무한 루프 방지)
        if next_offset <= offset:
            offset = cut_point
        else:
            offset = next_offset

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

def validate_date(date_str, is_end_date=False):
    if not date_str or not date_str.strip():
        return None
    date_str = date_str.strip()
    # 1. YYYY-MM-DD HH:MM:SS
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
        
    # 2. YYYY-MM-DD
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if is_end_date:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
        
    # 3. YYYYMMDD
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        if is_end_date:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
        
    raise ValueError(f"지원하지 않는 날짜 형식입니다: '{date_str}'. (지원 형식: YYYY-MM-DD, YYYY-MM-DD HH:MM:SS, YYYYMMDD)")

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

# ==============================================================================
# [NEW] GPU 헬스 체커 - Ollama 다운 감지 & 자동 페일오버
# ==============================================================================
class OllamaHealthTracker:
    def __init__(self, hosts):
        self.hosts = hosts
        self.host_alive = {i: True for i in range(len(hosts))}
        self.host_fail_count = {i: 0 for i in range(len(hosts))}
        self.last_health_check = 0
        self.health_check_interval = 30  # 30초마다 다운된 호스트 재확인
        self._round_robin = 0
    
    def get_alive_hosts(self):
        """살아있는 호스트 인덱스 목록 반환"""
        alive = [i for i, v in self.host_alive.items() if v]
        return alive if alive else list(range(len(self.hosts)))  # 전부 죽었으면 전부 시도
    
    def get_next_host(self):
        """라운드로빈으로 다음 살아있는 호스트 반환"""
        alive = self.get_alive_hosts()
        idx = alive[self._round_robin % len(alive)]
        self._round_robin += 1
        return idx
    
    def report_failure(self, host_idx):
        """실패 보고 - 3회 연속 실패 시 다운 판정"""
        self.host_fail_count[host_idx] += 1
        if self.host_fail_count[host_idx] >= 3:
            if self.host_alive[host_idx]:
                self.host_alive[host_idx] = False
                alive_count = sum(1 for v in self.host_alive.values() if v)
                print(f"\n🔴 [GPU {host_idx} DOWN] Ollama at {self.hosts[host_idx]} 응답 없음! (남은 GPU: {alive_count}개)")
    
    def report_success(self, host_idx):
        """성공 보고 - 실패 카운터 리셋"""
        self.host_fail_count[host_idx] = 0
        if not self.host_alive[host_idx]:
            self.host_alive[host_idx] = True
            print(f"\n🟢 [GPU {host_idx} RECOVERED] Ollama at {self.hosts[host_idx]} 복구됨!")
    
    async def check_dead_hosts(self, session):
        """다운된 호스트를 주기적으로 ping하여 복구 감지"""
        now = time.time()
        if now - self.last_health_check < self.health_check_interval:
            return
        self.last_health_check = now
        
        for idx, alive in self.host_alive.items():
            if alive:
                continue
            try:
                async with session.get(f"{self.hosts[idx]}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        self.report_success(idx)
            except Exception:
                pass

# 전역 헬스 트래커
health_tracker = OllamaHealthTracker(OLLAMA_HOSTS)

async def get_embedding(session, text, host_idx, sem, bo_table):
    if not text or len(text.strip()) < 2: return None
    max_retries = 3
    current_text = text
    
    async with sem:
        for attempt in range(max_retries):
            # 장애 GPU 회피: 살아있는 호스트로 자동 전환
            actual_host = host_idx if health_tracker.host_alive.get(host_idx, True) else health_tracker.get_next_host()
            url = f"{OLLAMA_HOSTS[actual_host]}/v1/embeddings"
            
            if attempt > 0: 
                wait_time = min(10, (2 ** attempt)) + random.uniform(0, 0.5)
                await asyncio.sleep(wait_time)
            
            payload = {"model": "bge-m3", "input": [current_text]}
            try:
                timeout = aiohttp.ClientTimeout(total=60) 
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        health_tracker.report_success(actual_host)
                        if 'data' in result and len(result['data']) > 0:
                            return result['data'][0]['embedding']
                        return None
                    elif resp.status == 500:
                        error_msg = await resp.text()
                        if "NaN" in error_msg or "unsupported value" in error_msg:
                            return None
                        if "too large to process" in error_msg or "batch size" in error_msg:
                            current_text = current_text[:int(len(current_text) * 0.8)]
                            continue
                        health_tracker.report_failure(actual_host)
                        if attempt < max_retries - 1: continue
                        return None
                    else:
                        health_tracker.report_failure(actual_host)
                        if attempt < max_retries - 1: continue
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                health_tracker.report_failure(actual_host)
                if attempt < max_retries - 1: continue
                return None
            except Exception:
                if attempt < max_retries - 1: continue
                return None
    return None

async def get_batch_embeddings(session, texts, sem):
    """[NEW] 배치 임베딩 - 여러 텍스트를 한 번의 API 호출로 처리 (대폭 속도 향상)"""
    if not texts: return [None] * len(texts)
    
    BATCH_LIMIT = 8  # Ollama는 한 번에 처리할 수 있는 input 수 제한
    results = [None] * len(texts)
    
    for batch_start in range(0, len(texts), BATCH_LIMIT):
        batch_texts = texts[batch_start:batch_start + BATCH_LIMIT]
        host_idx = health_tracker.get_next_host()
        url = f"{OLLAMA_HOSTS[host_idx]}/v1/embeddings"
        
        async with sem:
            payload = {"model": "bge-m3", "input": batch_texts}
            try:
                timeout = aiohttp.ClientTimeout(total=120)
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        health_tracker.report_success(host_idx)
                        if 'data' in result:
                            for item in result['data']:
                                idx = item.get('index', 0)
                                if batch_start + idx < len(results):
                                    results[batch_start + idx] = item['embedding']
                    else:
                        # 배치 실패 시 개별 처리로 폴백
                        health_tracker.report_failure(host_idx)
                        for i, txt in enumerate(batch_texts):
                            emb = await get_embedding(session, txt, health_tracker.get_next_host(), sem, "")
                            if batch_start + i < len(results):
                                results[batch_start + i] = emb
            except Exception:
                health_tracker.report_failure(host_idx)
                for i, txt in enumerate(batch_texts):
                    emb = await get_embedding(session, txt, health_tracker.get_next_host(), sem, "")
                    if batch_start + i < len(results):
                        results[batch_start + i] = emb
    
    return results

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
    valid_chunks = []
    valid_indices = []

    for idx, chunk in enumerate(chunks):
        if not chunk or not chunk.strip(): continue
        if len(chunk) < 5: continue
        valid_chunks.append(chunk)
        valid_indices.append(idx)

    if not valid_chunks: return []

    # [최적화] 배치 임베딩 사용 - 한 문서의 모든 청크를 한 번에 처리
    embeddings = await get_batch_embeddings(session, valid_chunks, sem)

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

async def sync_board(session, pool_gnuboard, pool_manticore, bo_table, target_category="", force_update=False, start_date=None, end_date=None):
    write_table = f"{TABLE_PREFIX}{bo_table}"
    category_msg = f" (Category: {target_category})" if target_category else ""
    date_msg = ""
    if start_date or end_date:
        date_msg = f" (Date: {start_date or 'Min'} ~ {end_date or 'Max'})"
    print(f"\n🔄 [SYNC START] Comparing Table: {write_table}{category_msg}{date_msg}")
    
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
                
                # 시작 날짜 필터 적용
                if start_date:
                    if where_clause.strip():
                        where_clause += " AND wr_datetime >= %s "
                    else:
                        where_clause = " WHERE wr_datetime >= %s "
                    params.append(start_date)

                # 종료 날짜 필터 적용
                if end_date:
                    if where_clause.strip():
                        where_clause += " AND wr_datetime <= %s "
                    else:
                        where_clause = " WHERE wr_datetime <= %s "
                    params.append(end_date)
                        
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
    # 특정 카테고리나 날짜 범위가 지정된 경우, 전체 삭제 대조를 하면 조건에 없는 다른 글들이 날아가는 위험 방지
    if not target_category and not start_date and not end_date:
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
        gc.collect()
        return

    # [수정] 전체 선삭제(Pre-cleaning) 제거 → 배치 단위 삭제로 변경 (전원 차단 시 데이터 보호)
    # 기존: 업데이트 대상 전체를 한꺼번에 삭제 → 전원 나가면 삭제된 상태로 데이터 소실
    # 변경: 각 배치 직전에 해당 배치만 삭제 후 즉시 삽입 → 최대 손실 = 1개 배치분

    to_upsert.sort()
    total_ops = len(to_upsert)
    sem = asyncio.Semaphore(CONCURRENCY)
    parent_cache = {}

    current_idx = 0
    FLUSH_COUNT_INTERVAL = 500   # [전원 보호] N건마다 FLUSH
    FLUSH_TIME_INTERVAL = 60.0   # [전원 보호] N초마다 FLUSH (이중 보호)
    last_flush_idx = 0
    last_flush_time = time.time()
    board_start_time = time.time()
    
    while current_idx < total_ops:
        dynamic_batch, cool_down = await tuner.tune()
        if cool_down > 0: await asyncio.sleep(cool_down)

        # [NEW] 주기적으로 다운된 GPU 복구 확인
        await health_tracker.check_dead_hosts(session)

        end_idx = min(current_idx + dynamic_batch, total_ops)
        batch_ids = to_upsert[current_idx : end_idx]
        ids_str = ",".join(map(str, batch_ids))

        # [전원 보호] 배치 단위 삭제: 이 배치에서 업데이트할 ID만 직전에 삭제
        batch_update_ids = [uid for uid in batch_ids if uid in mc_ids]
        if batch_update_ids:
            await delete_from_manticore(pool_manticore, bo_table, batch_update_ids)
        
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

        # [수정] try/finally로 커넥션 누수 방지
        try:
            for row in rows:
                if row['wr_is_comment'] == 1:
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
        finally:
            if current_conn: pool_gnuboard.release(current_conn)

        processed_batches = await asyncio.gather(*processing_tasks)
        bulk_values = []
        for item_list in processed_batches:
            bulk_values.extend(item_list)
        
        if bulk_values:
            await insert_bulk_manticore(pool_manticore, bulk_values)

        current_idx = end_idx
        now = time.time()

        # [전원 보호] 이중 FLUSH: 건수 OR 시간 기준 중 먼저 도달하면 FLUSH
        need_flush = (current_idx - last_flush_idx >= FLUSH_COUNT_INTERVAL) or (now - last_flush_time >= FLUSH_TIME_INTERVAL)
        if need_flush:
            try:
                async with pool_manticore.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(f"FLUSH RAMCHUNK {MANTICORE_INDEX}")
                last_flush_idx = current_idx
                last_flush_time = now
            except Exception:
                pass

        # [NEW] 속도 표시
        elapsed = now - board_start_time
        speed = current_idx / elapsed if elapsed > 0 else 0
        eta_sec = (total_ops - current_idx) / speed if speed > 0 else 0
        eta_min = int(eta_sec / 60)
        alive_gpus = sum(1 for v in health_tracker.host_alive.values() if v)
        print(f"\r🚀 {bo_table}: {current_idx}/{total_ops} (B:{dynamic_batch} | {speed:.1f}/s | ETA:{eta_min}m | GPU:{alive_gpus}) ", end="")

    print(f"\n✨ {bo_table} Sync Completed.")
    
    del gb_map, mc_map, to_delete, to_upsert
    gc.collect()

# ==============================================================================
# 4. 메인 실행 및 자가점검
# ==============================================================================
async def run_self_check():
    print("\n========================================================")
    print("🔍 [System Self-Check] 서비스 가동 상태 점검")
    print("========================================================")
    
    # 1. MariaDB (GnuBoard DB)
    mariadb_ok = False
    try:
        conn = await aiomysql.connect(
            host=DB_CONFIG['host'],
            port=DB_CONFIG.get('port', 3306),
            user=DB_CONFIG['user'],
            password=DB_CONFIG['password'],
            db=DB_CONFIG['db'],
            connect_timeout=3
        )
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
        await conn.ensure_closed()
        print(f"  [+] MariaDB ({DB_CONFIG['host']}): 🟢 연결 성공 (OK)")
        mariadb_ok = True
    except Exception as e:
        print(f"  [+] MariaDB ({DB_CONFIG['host']}): 🔴 연결 실패 ({e})")
        
    # 2. Manticore Search
    manticore_ok = False
    try:
        conn = await aiomysql.connect(
            host=MANTICORE_CONFIG['host'],
            port=int(MANTICORE_CONFIG['port']),
            user=MANTICORE_CONFIG['user'],
            password=MANTICORE_CONFIG['password'],
            db=MANTICORE_CONFIG.get('db', ''),
            connect_timeout=3
        )
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
        await conn.ensure_closed()
        print(f"  [+] Manticore Search ({MANTICORE_CONFIG['host']}:{MANTICORE_CONFIG['port']}): 🟢 연결 성공 (OK)")
        manticore_ok = True
    except Exception as e:
        print(f"  [+] Manticore Search ({MANTICORE_CONFIG['host']}:{MANTICORE_CONFIG['port']}): 🔴 연결 실패 ({e})")

    # 3. Ollama Hosts
    ollama_ok = False
    active_ollama_hosts = 0
    async with aiohttp.ClientSession() as session:
        for idx, host in enumerate(OLLAMA_HOSTS):
            try:
                # Check status via /api/tags
                async with session.get(f"{host}/api/tags", timeout=3) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = [m.get('name') for m in data.get('models', [])]
                        # Check if bge-m3 is loaded
                        model_found = any('bge-m3' in m.lower() for m in models)
                        model_status = "🟢 'bge-m3' 로드됨" if model_found else "⚠️ 'bge-m3' 모델 미지점 (설치된 모델 목록 확인 필요)"
                        print(f"  [+] Ollama Host {idx+1} ({host}):")
                        print(f"      - 서비스 상태: 🟢 가동 중 (Running)")
                        print(f"      - 모델 상태:   {model_status}")
                        if model_found:
                            active_ollama_hosts += 1
                    else:
                        print(f"  [+] Ollama Host {idx+1} ({host}): 🔴 HTTP 오류 (상태 코드: {resp.status})")
            except Exception as e:
                print(f"  [+] Ollama Host {idx+1} ({host}): 🔴 연결 실패 ({e})")
                
    if active_ollama_hosts == len(OLLAMA_HOSTS) and len(OLLAMA_HOSTS) > 0:
        ollama_ok = True

    print("========================================================")
    
    # Check if critical systems are ready
    if not mariadb_ok or not manticore_ok or not ollama_ok:
        print("❌ [경고] 일부 필수 서비스가 준비되지 않았습니다.")
        if not mariadb_ok:
            print("  - MariaDB 연결 실패 (DB 서버 작동 상태 확인 필요)")
        if not manticore_ok:
            print("  - Manticore Search 연결 실패 (Manticore 서비스 작동 상태 확인 필요)")
        if not ollama_ok:
            if len(OLLAMA_HOSTS) == 0:
                print("  - 설정 파일에 Ollama 호스트 정보가 없습니다.")
            elif active_ollama_hosts < len(OLLAMA_HOSTS):
                print(f"  - 일부 Ollama 호스트가 비활성 상태이거나 'bge-m3' 모델이 없습니다. ({active_ollama_hosts}/{len(OLLAMA_HOSTS)} 정상)")
        print("========================================================")
        
        try:
            print("계속 진행하시겠습니까? (y/n, 기본값: y): ", end="", flush=True)
            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(None, input)
            if answer.strip().lower() in ['n', 'no']:
                print("❌ 사용자가 실행을 중단했습니다.")
                sys.exit(1)
        except (EOFError, OSError):
            print("⚠️ 비대화형 환경이거나 입력을 받을 수 없어 자동으로 계속 진행합니다.")
    else:
        print("🟢 모든 필수 서비스가 정상 작동 중입니다.")
        print("========================================================\n")

async def main():
    print(f"=== Python Indexer V8.0 (Dual-GPU Optimized) ===")
    
    # 자가점검 수행
    await run_self_check()
    
    parser = argparse.ArgumentParser(description="Python Indexer with Manual Sync")
    parser.add_argument('-b', '--board', type=str, default='', help='작업할 게시판명 (bo_table)')
    parser.add_argument('-c', '--category', type=str, default='', help='작업할 카테고리명 (ca_name)')
    parser.add_argument('-f', '--force', action='store_true', help='기존 데이터를 무시하고 강제 재색인')
    parser.add_argument('-s', '--start-date', type=str, default=None, help='시작일 (YYYY-MM-DD, YYYY-MM-DD HH:MM:SS, YYYYMMDD)')
    parser.add_argument('-e', '--end-date', type=str, default=None, help='종료일 (YYYY-MM-DD, YYYY-MM-DD HH:MM:SS, YYYYMMDD)')
    
    args = parser.parse_args()
    
    target_board_input = args.board.strip()
    target_category_input = args.category.strip()
    force_update = args.force

    start_date_input = None
    if args.start_date:
        try:
            start_date_input = validate_date(args.start_date, is_end_date=False)
        except ValueError as e:
            parser.error(str(e))

    end_date_input = None
    if args.end_date:
        try:
            end_date_input = validate_date(args.end_date, is_end_date=True)
        except ValueError as e:
            parser.error(str(e))

    if target_board_input:
        print(f"\n[ 수동 작업 모드 ]")
        print(f" 👉 게시판: {target_board_input}")
        if target_category_input:
            print(f" 👉 카테고리: {target_category_input}")
        if start_date_input:
            print(f" 👉 시작일: {start_date_input}")
        if end_date_input:
            print(f" 👉 종료일: {end_date_input}")
        if force_update:
            print(f" 👉 강제 업데이트(Force) 활성화됨")
        print("=" * 40 + "\n")

    # [최적화] 커넥션 풀 크기 증가 + 더 빈번한 재활용
    pool_gnuboard = await aiomysql.create_pool(
        **DB_CONFIG, autocommit=True, pool_recycle=1800,
        minsize=5, maxsize=20
    )
    pool_manticore = await aiomysql.create_pool(
        host=MANTICORE_CONFIG['host'], port=MANTICORE_CONFIG['port'], 
        user=MANTICORE_CONFIG['user'], password=MANTICORE_CONFIG['password'], 
        db=MANTICORE_CONFIG['db'], autocommit=True,
        pool_recycle=1800, minsize=3, maxsize=10
    )
    print("✅ DB Connected.")

    try:
        await check_and_create_table(pool_manticore)

        # [최적화] GPU별 연결 제한이 있는 커넥터 사용
        connector = aiohttp.TCPConnector(
            limit=64,              # 총 동시 연결 수
            limit_per_host=32,     # 호스트당 최대 연결
            keepalive_timeout=300, # keep-alive 5분 유지
            enable_cleanup_closed=True
        )
        async with aiohttp.ClientSession(connector=connector) as session:
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
                await sync_board(session, pool_gnuboard, pool_manticore, bo_table, target_category_input, force_update, start_date=start_date_input, end_date=end_date_input)
                
                # 게시판 하나 끝날 때마다 인덱스 최적화
                try:
                    print("\n🧹 Flushing RAM to Disk...")
                    async with pool_manticore.acquire() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(f"FLUSH RAMCHUNK {MANTICORE_INDEX}")
                except Exception: pass

    except Exception as e:
        import traceback
        print(f"\n❌ Critical Error: An unexpected error occurred. Details below:")
        traceback.print_exc()
    finally:
        print("\n🔌 Closing DB connections...")
        pool_gnuboard.close()
        await pool_gnuboard.wait_closed()
        pool_manticore.close()
        await pool_manticore.wait_closed()
        print("👋 Finished.")

if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())