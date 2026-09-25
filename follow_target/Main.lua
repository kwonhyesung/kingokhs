-- 따라가기 시스템 메인 엔트리 포인트
-- 모든 모듈을 통합하여 사용

local FollowTarget = require("FollowTarget")
local ObjectTracker = require("ObjectTracker")
local PositionSynchronizer = require("PositionSynchronizer")

local FollowTargetSystem = {}

-- 초기화
function FollowTargetSystem:Initialize()
    print("[FollowTargetSystem] 시스템 초기화 중...")

    -- 따라가기 설정
    FollowTarget:Configure({
        followSpeed = 1.0,      -- 이동 속도
        stopDistance = 2,       -- 정지 거리 (2칸)
        updateInterval = 0.2    -- 업데이트 간격 (200ms)
    })

    print("[FollowTargetSystem] 초기화 완료")
end

-- 자동 따라가기 (가장 가까운 몬스터)
function FollowTargetSystem:StartAutoFollow()
    print("[FollowTargetSystem] 자동 따라가기 시작...")
    return FollowTarget:StartAutoFollow()
end

-- 특정 대상 따라가기
function FollowTargetSystem:FollowById(targetId)
    print("[FollowTargetSystem] 대상 " .. targetId .. " 따라가기 시작...")
    return FollowTarget:StartFollowing(targetId)
end

-- 따라가기 중지
function FollowTargetSystem:Stop()
    print("[FollowTargetSystem] 따라가기 중지")
    return FollowTarget:StopFollowing()
end

-- 현재 상태 출력
function FollowTargetSystem:PrintStatus()
    if not FollowTarget:IsFollowing() then
        print("[Status] 따라가기 중지 중")
        return
    end

    local info = FollowTarget:GetTargetInfo()
    if info then
        print(string.format(
            "[Status] 대상: %d, 거리: %.1f칸, 내위치: (%.1f, %.1f), 대상위치: (%.1f, %.1f)",
            info.targetId,
            info.distance,
            info.myPos.x, info.myPos.y,
            info.targetPos.x, info.targetPos.y
        ))
    end
end

-- 현재 보이는 몬스터 목록 출력
function FollowTargetSystem:PrintMonsters()
    local monsters = ObjectTracker:GetObjectsByType(2)  -- OBJECT_MONSTER = 2
    local myPos = ObjectTracker:GetMyPosition()

    print(string.format("[Monsters] 현재 %d마리 보임", #monsters))

    for i, monster in ipairs(monsters) do
        local monsterPos = ObjectTracker:GetPosition(monster)
        local canTarget = ObjectTracker:CanTarget(monster)
        local distance = ObjectTracker:CalculateDistance(myPos, monsterPos)

        print(string.format(
            "  [%d] ID: %d, 위치: (%.1f, %.1f), 거리: %.1f, 공격가능: %s",
            i, monster.id, monsterPos.x, monsterPos.y, distance, tostring(canTarget)
        ))
    end
end

-- 메인 루프 (게임 틱마다 호출)
function FollowTargetSystem:Update()
    FollowTarget:Tick()
end

-- ============================================================
-- 사용 예제
-- ============================================================

--[[
-- 초기화
FollowTargetSystem:Initialize()

-- 방법 1: 자동으로 가장 가까운 몬스터 찾아서 따라가기
FollowTargetSystem:StartAutoFollow()

-- 방법 2: 특정 몬스터 ID로 따라가기
-- FollowTargetSystem:FollowById(12345)

-- 게임 루프에서 계속 호출 (매 프레임)
-- FollowTargetSystem:Update()

-- 현재 상태 확인
-- FollowTargetSystem:PrintStatus()

-- 몬스터 목록 확인
-- FollowTargetSystem:PrintMonsters()

-- 따라가기 중지
-- FollowTargetSystem:Stop()
--]]

return FollowTargetSystem
