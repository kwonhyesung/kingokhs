# Profile Guide

## 목적
- 파일이 많은 루트 폴더에서 역할별 실행 기준을 분리해 이력 관리를 쉽게 한다.

## 구조
- `manifests/required_common_files.txt`: 공용 필수 파일 목록
- `manifests/required_warrior_files.txt`: 격수 런처 파일 목록
- `profiles/warrior/`: 격수PC 실행 가이드/실행 배치

## 운영 원칙
1. 공용 로직 수정 시 `required_common_files.txt` 기준으로 영향 범위를 본다.
2. 격수 실행 변경 시 `svc_warrior.py`와 `profiles/warrior`만 먼저 검토한다.
3. 배포 전에는 `python -m py_compile svc_warrior.py bis_logic.py gui_app.py`를 수행한다.
