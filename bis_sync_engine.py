import os
import time
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 설정
SCOPES = ['https://www.googleapis.com/auth/drive']
WATCH_EXTENSIONS = ('.py', '.csv', '.txt') # 감시할 확장자

class BISSyncHandler(FileSystemEventHandler):
    def __init__(self, service):
        self.service = service

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(WATCH_EXTENSIONS):
            filename = os.path.basename(event.src_path)
            # 임시 파일이나 자기 자신은 제외
            if filename.startswith('.') or filename == 'bis_sync_engine.py':
                return
            
            print(f"🔄 변경 감지: {filename} -> 드라이브 업데이트 중@...")
            self.smart_upload(event.src_path, filename)

    def smart_upload(self, file_path, filename):
        try:
            # 1. 드라이브에서 동일한 이름의 파일이 있는지 검색
            query = f"name = '{filename}' and trashed = false"
            results = self.service.files().list(q=query, fields="files(id)").execute()
            files = results.get('files', [])

            media = MediaFileUpload(file_path, resumable=True)

            if files:
                # 기존 파일이 있으면 업데이트 (덮어쓰기)
                file_id = files[0]['id']
                self.service.files().update(fileId=file_id, media_body=media).execute()
                print(f"✅ 업데이트 완료: {filename}")
            else:
                # 없으면 새로 생성
                file_metadata = {'name': filename}
                self.service.files().create(body=file_metadata, media_body=media).execute()
                print(f"🆕 새 파일 업로드 완료: {filename}")
        except Exception as e:
            print(f"❌ 오류 발생: {e}")

def get_service():
    creds = None
    # 이전에 인증한 토큰이 있으면 로드 (매번 로그인할 필요 없음)
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    
    return build('drive', 'v3', credentials=creds)

if __name__ == "__main__":
    service = get_service()
    path = "." # 현재 폴der 감시
    event_handler = BISSyncHandler(service)
    observer = Observer()
    observer.schedule(event_handler, path, recursive=False)
    
    print("🚀 BIS 실시간 싱크 엔진 가동 시작! (Ctrl+C로 종료)")
    print(f"📂 감시 중인 확장자: {WATCH_EXTENSIONS}")
    
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()