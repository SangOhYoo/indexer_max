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

def run_backup():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Manticore 백업 시작 (비압축)...")
    
    config = load_config()
    m_config = config.get('manticore', {})
    
    # 1. Manticore RAM 청크 플러시
    try:
        print("🔍 Manticore RAM 데이터를 디스크로 플러시 중...")
        conn = pymysql.connect(
            host=m_config.get('host', '127.0.0.1'),
            port=m_config.get('port', 9306),
            user=m_config.get('user', 'root'),
            password=m_config.get('password', ''),
            autocommit=True
        )
        with conn.cursor() as cur:
            cur.execute("FLUSH RAMCHUNK idx_novel")
        conn.close()
        print("✅ RAM 플러시 완료.")
    except Exception as e:
        print(f"⚠️ Manticore 접속/플러시 실패 (무시하고 진행): {e}")

    # 2. 백업 대상 폴더 준비
    today_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    target_backup_dir = os.path.join(BACKUP_ROOT, today_str)
    
    if not os.path.exists(target_backup_dir):
        os.makedirs(target_backup_dir, exist_ok=True)
    
    # 3. WSL에서 윈도우로 직접 복사 (권한 해결을 위해 root 사용)
    # 윈도우의 d:\indexer_max\backup... 경로는 WSL에서 /mnt/d/indexer_max/backup/... 로 인식됨
    wsl_dest = os.path.abspath(target_backup_dir).replace('\\', '/').replace('D:', '/mnt/d').replace('d:', '/mnt/d')
    src_data_wsl = "/var/lib/manticore/data"
    
    print(f"📂 WSL 데이터 복사 중 (약 20GB+)... 잠시만 기다려 주세요.")
    
    try:
        # 목적지 폴더 생성
        subprocess.run(['wsl', '-u', 'root', 'mkdir', '-p', f"{wsl_dest}/data"], check=True)
        
        # 데이터 폴더 복사
        cp_cmd = f"cp -r {src_data_wsl}/* {wsl_dest}/data/"
        subprocess.run(['wsl', '-u', 'root', 'bash', '-c', cp_cmd], check=True)
        
        # 기타 설정 파일 복사
        for filename in ['manticore.json', 'state.sql']:
            cp_cfg_cmd = f"cp /var/lib/manticore/{filename} {wsl_dest}/"
            subprocess.run(['wsl', '-u', 'root', 'bash', '-c', cp_cfg_cmd], check=False)

        print(f"✅ WSL 데이터 복사 완료.")
        print(f"\n🎉 백업이 성공적으로 완료되었습니다!")
        print(f"📍 백업 위치: {os.path.abspath(target_backup_dir)}")

    except Exception as e:
        print(f"❌ 백업 중 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_backup()
