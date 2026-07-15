import os
import sys
import subprocess
import time
import json
import pymysql
from datetime import datetime

# ==============================================================================
# 0. 설정
# ==============================================================================
CONFIG_FILE = 'config.json'
BACKUP_ROOT = 'backup'
WSL_DATA_PATH = '/var/lib/manticore/data'
WSL_BINLOG_PATH = '/var/lib/manticore/binlog'
WSL_CONF_PATH = '/var/lib/manticore'
TABLE_NAME = 'idx_novel'

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"❌ Error: '{CONFIG_FILE}' 파일을 찾을 수 없습니다.")
        sys.exit(1)
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ 설정 로드 오류: {e}")
        sys.exit(1)

def get_backups():
    if not os.path.exists(BACKUP_ROOT):
        return []
    # 폴더 형태의 백업 목록 가져오기
    backups = [d for d in os.listdir(BACKUP_ROOT) if os.path.isdir(os.path.join(BACKUP_ROOT, d))]
    return sorted(backups, reverse=True)

def wait_for_manticore_stop(timeout=30):
    """Manticore searchd 프로세스가 완전히 종료될 때까지 대기"""
    print(f"   ▸ searchd 프로세스 종료 대기 중 (최대 {timeout}초)...")
    
    for i in range(timeout):
        result = subprocess.run(
            ['wsl', '-u', 'root', 'bash', '-c', 'pgrep -x searchd'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # pgrep이 실패 = 프로세스 없음
            print(f"   ✅ searchd 프로세스 종료 확인 ({i+1}초 소요)")
            return True
        time.sleep(1)
        if (i + 1) % 5 == 0:
            print(f"      ... 아직 실행 중 ({i+1}/{timeout}초)")
    
    return False

def force_kill_searchd():
    """강제로 searchd 프로세스 종료"""
    print("   ⚠️ 강제 종료 시도 (SIGKILL)...")
    subprocess.run(
        ['wsl', '-u', 'root', 'bash', '-c', 'pkill -9 searchd'],
        capture_output=True
    )
    time.sleep(3)
    
    # 종료 확인
    result = subprocess.run(
        ['wsl', '-u', 'root', 'bash', '-c', 'pgrep -x searchd'],
        capture_output=True, text=True
    )
    return result.returncode != 0

def wsl_path_convert(win_path):
    """Windows 절대경로를 WSL 경로로 변환"""
    return win_path.replace('\\', '/').replace('D:', '/mnt/d').replace('d:', '/mnt/d')

def run_restore():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Manticore 복구 시작...")
    
    # 1. 백업 목록 가져오기
    backups = get_backups()
    if not backups:
        print("❌ 복구할 백업 폴더가 없습니다.")
        return

    print("\n📦 사용 가능한 백업 목록:")
    for i, b in enumerate(backups):
        # 백업 폴더의 data 유무 확인
        data_path = os.path.join(BACKUP_ROOT, b, 'data')
        has_data = "✅" if os.path.isdir(data_path) else "⚠️ data 없음"
        binlog_path = os.path.join(BACKUP_ROOT, b, 'binlog')
        has_binlog = "✅" if os.path.isdir(binlog_path) else "⚠️ binlog 없음"
        print(f"  [{i+1}] {b}  (data: {has_data}, binlog: {has_binlog})")
    
    try:
        choice = int(input("\n복구할 백업 번호를 선택하세요 (0: 취소): "))
        if choice == 0:
            print("👋 복구를 취소합니다.")
            return
        selected_backup = backups[choice-1]
    except (ValueError, IndexError):
        print("❌ 잘못된 선택입니다.")
        return

    backup_path = os.path.abspath(os.path.join(BACKUP_ROOT, selected_backup))
    
    # 중첩된 폴더 구조 감지
    nested_path = os.path.join(backup_path, selected_backup)
    if not os.path.exists(os.path.join(backup_path, 'data')) and os.path.exists(os.path.join(nested_path, 'data')):
        print("💡 중첩된 폴더 구조 감지: 하위 폴더 경로로 조정합니다.")
        backup_path = nested_path
    
    # 백업 데이터 존재 확인
    backup_data_path = os.path.join(backup_path, 'data')
    if not os.path.isdir(backup_data_path):
        print(f"❌ 백업 폴더에 'data' 디렉토리가 없습니다: {backup_path}")
        print("   유효하지 않은 백업입니다.")
        return
    
    print(f"\n⚠️ 선택된 백업: {selected_backup}")
    print(f"   경로: {backup_path}")
    confirm = input("정말로 복구를 진행하시겠습니까? WSL의 현재 데이터가 삭제됩니다! (y/n): ")
    if confirm.lower() != 'y':
        print("👋 복구를 중단합니다.")
        return

    try:
        # ==================================================================
        # 2. Manticore 서비스 완전 중지 (확인 포함)
        # ==================================================================
        print("\n🛑 Manticore 서비스를 중지하는 중...")
        
        # 여러 방법으로 중지 시도
        subprocess.run(['wsl', '-u', 'root', 'bash', '-c', 'searchd --stopwait 2>/dev/null'], 
                       capture_output=True)
        subprocess.run(['wsl', '-u', 'root', 'bash', '-c', 'service manticore stop 2>/dev/null'], 
                       capture_output=True)
        
        # 프로세스 종료 대기
        if not wait_for_manticore_stop(timeout=30):
            print("   ⚠️ 정상 종료 실패. 강제 종료를 시도합니다.")
            if not force_kill_searchd():
                print("   ❌ Manticore를 종료할 수 없습니다!")
                print("   수동으로 WSL에서 'sudo killall searchd'를 실행한 후 다시 시도하세요.")
                sys.exit(1)
            print("   ✅ 강제 종료 완료.")
        
        # ==================================================================
        # 3. 기존 데이터 + binlog 완전 삭제
        # ==================================================================
        print("🧹 WSL의 기존 데이터 삭제 중...")
        
        # data 디렉토리 정리
        subprocess.run(['wsl', '-u', 'root', 'bash', '-c', 
                       f"rm -rf {WSL_DATA_PATH}/* 2>/dev/null; mkdir -p {WSL_DATA_PATH}"], 
                       check=True)
        
        # binlog 디렉토리 정리 (오래된 binlog가 남아있으면 복원된 data와 불일치 → 시작 실패)
        subprocess.run(['wsl', '-u', 'root', 'bash', '-c', 
                       f"rm -rf {WSL_BINLOG_PATH}/* 2>/dev/null; mkdir -p {WSL_BINLOG_PATH}"], 
                       check=True)
        
        print("   ✅ 기존 데이터 및 binlog 삭제 완료.")

        # ==================================================================
        # 4. 백업 데이터 복원
        # ==================================================================
        wsl_src = wsl_path_convert(backup_path)
        print(f"📂 백업 복원 중: {selected_backup} → WSL")
        
        # 4-1. data 폴더 복원
        print("   ▸ data/ 복원 중...")
        result = subprocess.run(
            ['wsl', '-u', 'root', 'bash', '-c', f"cp -a {wsl_src}/data/. {WSL_DATA_PATH}/"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"   ❌ data 복원 실패: {result.stderr.strip()[:300]}")
            sys.exit(1)
        
        # 4-2. binlog 폴더 복원 (백업에 있는 경우)
        backup_binlog_path = os.path.join(backup_path, 'binlog')
        if os.path.isdir(backup_binlog_path) and os.listdir(backup_binlog_path):
            print("   ▸ binlog/ 복원 중...")
            result = subprocess.run(
                ['wsl', '-u', 'root', 'bash', '-c', f"cp -a {wsl_src}/binlog/. {WSL_BINLOG_PATH}/"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"   ⚠️ binlog 복원 경고: {result.stderr.strip()[:300]}")
        else:
            print("   ℹ️ 백업에 binlog가 없습니다 (이전 버전 백업일 수 있음).")
        
        # 4-3. 설정 파일 복원
        print("   ▸ 설정 파일 복원 중...")
        for filename in ['manticore.json', 'state.sql']:
            src_file = os.path.join(backup_path, filename)
            if os.path.exists(src_file):
                subprocess.run(
                    ['wsl', '-u', 'root', 'bash', '-c', f"cp {wsl_src}/{filename} {WSL_CONF_PATH}/"],
                    capture_output=True
                )
        
        print("   ✅ 파일 복원 완료.")

        # ==================================================================
        # 5. 소유권 변경 및 권한 설정
        # ==================================================================
        print("🔧 소유권 및 권한 조정 중...")
        subprocess.run(['wsl', '-u', 'root', 'chown', '-R', 'manticore:manticore', WSL_CONF_PATH], check=True)
        subprocess.run(['wsl', '-u', 'root', 'chmod', '-R', '755', WSL_DATA_PATH], check=True)
        subprocess.run(['wsl', '-u', 'root', 'chmod', '-R', '755', WSL_BINLOG_PATH], check=True)
        print("   ✅ 권한 설정 완료.")

        # ==================================================================
        # 6. Manticore 서비스 시작 + 헬스체크
        # ==================================================================
        print("\n🚀 Manticore 서비스를 시작하는 중...")
        
        # searchd 시작 (서비스 또는 직접 실행)
        start_result = subprocess.run(
            ['wsl', '-u', 'root', 'bash', '-c', 'service manticore start 2>&1 || searchd 2>&1'],
            capture_output=True, text=True
        )
        
        if start_result.returncode != 0:
            print(f"   ⚠️ 서비스 시작 명령 결과: {start_result.stdout.strip()}")
        
        # 시작 대기
        print("   ▸ searchd 시작 대기 중...")
        time.sleep(5)
        
        # 헬스체크: 실제로 접속 가능한지 확인
        config = load_config()
        m_config = config.get('manticore', {})
        
        health_ok = False
        for attempt in range(6):
            try:
                conn = pymysql.connect(
                    host=m_config.get('host', '127.0.0.1'),
                    port=m_config.get('port', 9306),
                    user=m_config.get('user', 'root'),
                    password=m_config.get('password', ''),
                    autocommit=True,
                    connect_timeout=5
                )
                with conn.cursor() as cur:
                    cur.execute(f"SHOW TABLES LIKE '{TABLE_NAME}'")
                    result = cur.fetchone()
                    if result:
                        # 테이블이 있으면 문서 수도 확인
                        cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
                        count = cur.fetchone()[0]
                        print(f"   ✅ 헬스체크 통과! 테이블 '{TABLE_NAME}' 확인 (문서 수: {count:,})")
                        health_ok = True
                    else:
                        print(f"   ⚠️ Manticore에 접속 가능하나 '{TABLE_NAME}' 테이블을 찾을 수 없습니다.")
                        print(f"      indexer_max.py 실행 시 자동으로 IMPORT/CREATE됩니다.")
                        health_ok = True
                conn.close()
                break
            except Exception as e:
                if attempt < 5:
                    print(f"      ... 접속 재시도 중 ({attempt+1}/6)")
                    time.sleep(5)
                else:
                    print(f"   ❌ 헬스체크 실패: {e}")
                    print("   수동으로 확인해 주세요:")
                    print("     wsl -u root searchd")
                    print(f"     wsl -u root mysql -h 127.0.0.1 -P {m_config.get('port', 9306)} -e 'SHOW TABLES'")
        
        if health_ok:
            print(f"\n🎉 복구가 성공적으로 완료되었습니다!")
        else:
            print(f"\n⚠️ 파일 복원은 완료되었지만, Manticore 서비스 확인이 필요합니다.")

    except Exception as e:
        print(f"❌ 복구 중 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_restore()
