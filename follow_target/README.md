# 따라가기 시스템 (FollowTarget System)

## 📖 개요

게임 내 `SeenObjectsClientOnly` 구조를 활용한 따라가기 기능 구현.
- 위치 좌표 동기화
- 대상 선택
- 이동 명령 전송

## 📁 파일 구조

### 1. ObjectTracker.lua
**기능**: 현재 맵의 보이는 객체들 추적

```lua
ObjectTracker:GetAllVisibleObjects()    -- 모든 객체 가져오기
ObjectTracker:GetObjectsByType(type)    -- 타입별 필터링 (MONSTER=2, NPC=3, ITEM=4)
ObjectTracker:GetObjectById(id)         -- 특정 ID 객체 가져오기
ObjectTracker:CanTarget(obj)            -- 대상 공격 가능 확인
ObjectTracker:CalculateDistance(p1, p2) -- 거리 계산 (맨해튼)
ObjectTracker:GetMyPosition()            -- 내 위치
```

### 2. PositionSynchronizer.lua
**기능**: 네트워크에서 받은 위치 업데이트 처리

```lua
PositionSynchronizer:UpdatePosition(id, pos)       -- 위치 업데이트
PositionSynchronizer:GetCurrentPosition(id)        -- 현재 위치
PositionSynchronizer:PredictPosition(id, time)     -- 미래 위치 예측
PositionSynchronizer:GetAverageVelocity(id)        -- 평균 이동 속도
PositionSynchronizer:RegisterCallback(id, callback) -- 위치 변화 감지
```

### 3. FollowTarget.lua
**기능**: 따라가기 핵심 로직

```lua
FollowTarget:StartAutoFollow()           -- 자동으로 가장 가까운 몬스터 따라가기
FollowTarget:StartFollowing(targetId)    -- 특정 대상 따라가기
FollowTarget:StopFollowing()             -- 따라가기 중지
FollowTarget:IsFollowing()               -- 따라가기 중인지 확인
FollowTarget:GetTargetInfo()             -- 현재 대상 정보
FollowTarget:Tick()                      -- 메인 루프 업데이트
```

### 4. Main.lua
**기능**: 전체 시스템 통합

```lua
FollowTargetSystem:Initialize()          -- 초기화
FollowTargetSystem:StartAutoFollow()     -- 자동 따라가기
FollowTargetSystem:FollowById(id)        -- ID로 따라가기
FollowTargetSystem:Stop()                -- 중지
FollowTargetSystem:Update()              -- 루프 업데이트
```

## 🚀 사용 방법

### 기본 사용

```lua
local System = require("Main")

-- 시스템 초기화
System:Initialize()

-- 가장 가까운 몬스터 자동 추적
System:StartAutoFollow()

-- 게임 루프에서 계속 호출 (매 프레임)
System:Update()

-- 현재 상태 확인
System:PrintStatus()

-- 따라가기 중지
System:Stop()
```

### 특정 대상 추적

```lua
-- 대상 ID를 알고 있으면
System:FollowById(targetId)

-- 게임 루프에서 계속 업데이트
System:Update()
```

### 몬스터 목록 확인

```lua
System:PrintMonsters()
```

## 🔧 커스터마이징

### 이동 속도, 정지 거리 등 설정

```lua
FollowTarget:Configure({
    followSpeed = 1.0,      -- 이동 속도 배수
    stopDistance = 2,       -- 정지 거리 (타일 단위)
    updateInterval = 0.2    -- 업데이트 간격 (초)
})
```

## 📊 객체 타입 정상

| 값 | 타입 |
|----|----|
| 1 | PLAYER (플레이어) |
| 2 | MONSTER (몬스터) |
| 3 | NPC (NPC) |
| 4 | ITEM (아이템) |
| 5 | OTHER (기타) |

## ⚙️ 핵심 플로우

### 1️⃣ 대상 선택
```
SeenObjectsClientOnly 순회
→ CanTarget 확인 (공격 가능한가?)
→ 거리 계산 (맨해튼)
→ 가장 가까운 대상 선택
```

### 2️⃣ 위치 동기화
```
네트워크: ClientMoveObject_elementwise
→ PositionSynchronizer:UpdatePosition()
→ 콜백 호출 (등록된 리스너들)
```

### 3️⃣ 이동 명령
```
UpdateFollowPosition()
→ 대상 위치 확인
→ 거리 계산
→ 방향 계산 (8방향)
→ SendMoveCommand()
→ 네트워크 패킷 전송
```

## 📝 주의사항

1. **루프 호출 필수**: `System:Update()`를 게임 루프에서 계속 호출해야 함
2. **네트워크 적응**: `MeramNetworkService:SendMoveCommand()` 구현이 게임의 실제 프로토콜과 맞아야 함
3. **메모리 정리**: 따라가기 중지 시 자동으로 콜백 정리됨

## 🎓 학습 목적

이 코드는 다음을 학습하는데 도움이 됩니다:
- 게임 클라이언트-서버 구조 이해
- 위치 동기화 메커니즘
- 네트워크 이벤트 처리
- 객체 상태 관리
- 콜백/리스너 패턴

## 📌 문서 참고

- SeenObjectsClientOnly 객체 흐름: `D:\dllgo\think\SeenObjectsClientOnly_객체흐름_분석.md`
- 게임 분석 결과물 기반 구현
