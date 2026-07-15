import os
import json
import pymysql
import sys
import subprocess
from datetime import datetime

# ==============================================================================
# 0. 설정 로드
# ==============================================================================
CONFIG_FILE = 'config.json'
BACKUP_ROOT = 'backup'
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

def wsl_count_files(wsl_path):
    """WSL 경로의 파일 수를 재귀적으로 세기"""
    try:
        result = subprocess.run(
            ['wsl', '-u', 'root', 'bash', '-c', f"find {wsl_path} -type f 2>/dev/null | wc -l"],
            capture_output=True, text=True, timeout=30
        )
        return int(result.stdout.strip()) if result.returncode == 0 else -1
    except Exception:
        return -1

def run_backup():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Manticore 백업 시작...")
    
    config = load_config()
    m_config = config.get('manticore', {})
    
    conn = None
    frozen = False
    
    try:
        # ==================================================================
        # 1. Manticore 접속 + FLUSH RAMCHUNK + FREEZE (데이터 일관성 보장)
        # ==================================================================
        print("🔍 Manticore에 접속하여 데이터를 안전하게 잠금 중...")
        try:
            conn = pymysql.connect(
                host=m_config.get('host', '127.0.0.1'),
                port=m_config.get('port', 9306),
                user=m_config.get('user', 'root'),
                password=m_config.get('password', ''),
                autocommit=True
            )
            with conn.cursor() as cur:
                # RAM에 있는 데이터를 디스크로 먼저 내림
                print("   ▸ FLUSH RAMCHUNK 실행...")
                cur.execute(f"FLUSH RAMCHUNK {TABLE_NAME}")
                
                # FREEZE: 복사 중 파일 변경/삭제/compaction 방지
                print("   ▸ FREEZE 실행 (복사 중 파일 변경 방지)...")
                cur.execute(f"FREEZE {TABLE_NAME}")
                frozen = True
                
            print("✅ 데이터 잠금 완료 (FREEZE 상태)")
        except Exception as e:
            print(f"⚠️ Manticore 접속/잠금 실패: {e}")
            print("   ⚠️ FREEZE 없이 진행합니다. 백업 데이터가 불완전할 수 있습니다.")

        # ==================================================================
        # 2. 백업 대상 폴더 준비
        # ==================================================================
        today_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        target_backup_dir = os.path.join(BACKUP_ROOT, today_str)
        
        if not os.path.exists(target_backup_dir):
            os.makedirs(target_backup_dir, exist_ok=True)
        
        # ==================================================================
        # 3. WSL에서 윈도우로 직접 복사
        # ==================================================================
        wsl_dest = os.path.abspath(target_backup_dir).replace('\\', '/').replace('D:', '/mnt/d').replace('d:', '/mnt/d')
        src_data_wsl = "/var/lib/manticore/data"
        src_binlog_wsl = "/var/lib/manticore/binlog"
        src_conf_path = "/var/lib/manticore"
        
        print(f"📂 WSL 데이터 복사 중... 잠시만 기다려 주세요.")
        
        # 3-1. 목적지 폴더 생성
        subprocess.run(['wsl', '-u', 'root', 'mkdir', '-p', f"{wsl_dest}/data"], check=True)
        subprocess.run(['wsl', '-u', 'root', 'mkdir', '-p', f"{wsl_dest}/binlog"], check=True)
        
        has_error = False
        
        # 3-2. data 폴더 복사
        print("   ▸ data/ 복사 중...")
        cp_data_cmd = f"cp -a {src_data_wsl}/. {wsl_dest}/data/"
        result = subprocess.run(['wsl', '-u', 'root', 'bash', '-c', cp_data_cmd], 
                              capture_output=True, text=True)
        if result.returncode != 0:
            stderr_msg = result.stderr.strip()
            if stderr_msg:
                print(f"   ⚠️ data 복사 경고: {stderr_msg[:200]}")
        
        # 3-3. binlog 폴더 복사 (갑작스러운 종료 후 복구에 필수)
        print("   ▸ binlog/ 복사 중...")
        # binlog 존재 여부 확인
        binlog_check = subprocess.run(
            ['wsl', '-u', 'root', 'bash', '-c', f"test -d {src_binlog_wsl} && echo 'exists'"],
            capture_output=True, text=True
        )
        if 'exists' in binlog_check.stdout:
            cp_binlog_cmd = f"cp -a {src_binlog_wsl}/. {wsl_dest}/binlog/"
            result = subprocess.run(['wsl', '-u', 'root', 'bash', '-c', cp_binlog_cmd],
                                  capture_output=True, text=True)
            if result.returncode != 0:
                stderr_msg = result.stderr.strip()
                if stderr_msg:
                    print(f"   ⚠️ binlog 복사 경고: {stderr_msg[:200]}")
        else:
            print("   ℹ️ binlog 디렉토리가 없습니다 (정상일 수 있음).")
        
        # 3-4. 설정 파일 복사
        print("   ▸ 설정 파일 복사 중...")
        for filename in ['manticore.json', 'state.sql']:
            cp_cfg_cmd = f"cp {src_conf_path}/{filename} {wsl_dest}/"
            subprocess.run(['wsl', '-u', 'root', 'bash', '-c', cp_cfg_cmd], 
                         capture_output=True)

        # ==================================================================
        # 4. 백업 무결성 검증
        # ==================================================================
        print("🔍 백업 무결성 검증 중...")
        src_file_count = wsl_count_files(src_data_wsl)
        dst_file_count = wsl_count_files(f"{wsl_dest}/data")
        
        if src_file_count > 0 and dst_file_count > 0:
            if dst_file_count >= src_file_count:
                print(f"   ✅ 파일 수 검증 통과: 원본 {src_file_count}개 → 백업 {dst_file_count}개")
            else:
                diff = src_file_count - dst_file_count
                print(f"   ⚠️ 파일 수 불일치: 원본 {src_file_count}개, 백업 {dst_file_count}개 (누락: {diff}개)")
                print(f"   ⚠️ 백업이 불완전할 수 있습니다. 디스크 공간을 확인하세요.")
                has_error = True
        elif dst_file_count == 0:
            print("   ❌ 백업된 파일이 없습니다! 백업 실패.")
            has_error = True
        else:
            print("   ⚠️ 파일 수 검증을 수행할 수 없습니다.")

        if has_error:
            print(f"\n⚠️ 백업이 완료되었지만 일부 문제가 감지되었습니다.")
        else:
            print(f"\n🎉 백업이 성공적으로 완료되었습니다!")
        print(f"📍 백업 위치: {os.path.abspath(target_backup_dir)}")

    except Exception as e:
        print(f"❌ 백업 중 오류 발생: {e}")
        sys.exit(1)
    
    finally:
        # ==================================================================
        # 5. UNFREEZE (반드시 실행 - 실패해도 Manticore가 멈추지 않도록)
        # ==================================================================
        if frozen and conn:
            try:
                print("🔓 UNFREEZE 실행 중 (잠금 해제)...")
                with conn.cursor() as cur:
                    cur.execute(f"UNFREEZE {TABLE_NAME}")
                print("✅ 잠금 해제 완료.")
            except Exception as e:
                print(f"⚠️ UNFREEZE 실패: {e}")
                print("   ⚠️ 수동으로 'UNFREEZE idx_novel' 실행이 필요할 수 있습니다.")
        
        if conn:
            try:
                conn.close()
            except Exception:
                pass

if __name__ == "__main__":
    run_backup()
