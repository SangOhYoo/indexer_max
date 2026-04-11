import os
import sys
import subprocess
from datetime import datetime

# ==============================================================================
# 0. 설정
# ==============================================================================
BACKUP_ROOT = 'backup'
WSL_DATA_PATH = '/var/lib/manticore/data'
WSL_CONF_PATH = '/var/lib/manticore'

def get_backups():
    if not os.path.exists(BACKUP_ROOT):
        return []
    # 폴더 형태의 백업 목록 가져오기
    backups = [d for d in os.listdir(BACKUP_ROOT) if os.path.isdir(os.path.join(BACKUP_ROOT, d))]
    return sorted(backups, reverse=True)

def run_restore():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Manticore 복구 시작 (비압축)...")
    
    # 1. 백업 목록 가져오기
    backups = get_backups()
    if not backups:
        print("❌ 복구할 백업 폴더가 없습니다.")
        return

    print("\n📦 사용 가능한 백업 목록:")
    for i, b in enumerate(backups):
        print(f"  [{i+1}] {b}")
    
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
    print(f"\n⚠️ 선택된 백업: {selected_backup}")
    confirm = input("정말로 복구를 진행하시겠습니까? WSL의 현재 데이터가 삭제됩니다! (y/n): ")
    if confirm.lower() != 'y':
        print("👋 복구를 중단합니다.")
        return

    try:
        # 2. Manticore 서비스 중지 시도
        print("\n🛑 Manticore 서비스를 중지하는 중...")
        subprocess.run(['wsl', '-u', 'root', 'searchd', '--stop'], capture_output=True)
        subprocess.run(['wsl', '-u', 'root', 'service', 'manticore', 'stop'], capture_output=True)

        # 3. WSL 데이터 폴더 정리
        print("🧹 WSL의 기존 데이터 삭제 중...")
        subprocess.run(['wsl', '-u', 'root', 'mkdir', '-p', WSL_DATA_PATH], check=True)
        subprocess.run(['wsl', '-u', 'root', 'bash', '-c', f"rm -rf {WSL_DATA_PATH}/*"], check=True)

        # 4. 백업 데이터 복원 (WSL -u root 활용)
        wsl_src = backup_path.replace('\\', '/').replace('D:', '/mnt/d').replace('d:', '/mnt/d')
        print(f"📂 백업 복원 중: {selected_backup} -> WSL")
        
        # 데이터 폴더 내 파일 복사
        subprocess.run(['wsl', '-u', 'root', 'bash', '-c', f"cp -r {wsl_src}/data/* {WSL_DATA_PATH}/"], check=True)
        
        # 기타 설정 파일 복사
        for filename in ['manticore.json', 'state.sql']:
            subprocess.run(['wsl', '-u', 'root', 'bash', '-c', f"cp {wsl_src}/{filename} {WSL_CONF_PATH}/"], capture_output=True)

        print("✅ 파일 복원 완료.")

        # 5. Manticore 서비스 시작 안내
        print("\n🚀 Manticore 서비스를 다시 시작해 주세요.")
        print("  예시: wsl searchd")
        print("       (또는) wsl sudo service manticore start")
        
        print(f"\n🎉 복구가 성공적으로 완료되었습니다!")

    except Exception as e:
        print(f"❌ 복구 중 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    run_restore()
