-- 따라가기 기능 구현
-- SeenObjectsClientOnly 기반 대상 추적 및 이동

local ObjectTracker = require("ObjectTracker")
local PositionSynchronizer = require("PositionSynchronizer")

local FollowTarget = {}

-- 현재 따라가기 상태
local followState = {
    isFollowing = false,
    targetId = nil,
    targetObject = nil,
    followSpeed = 1.0,
    stopDistance = 2,  -- 거리 2칸 이내면 멈추기
    updateInterval = 0.2,
    lastUpdateTime = 0
}

-- 따라가기 설정
function FollowTarget:Configure(options)
    if options.followSpeed then
        followState.followSpeed = options.followSpeed
    end
    if options.stopDistance then
        followState.stopDistance = options.stopDistance
    end
    if options.updateInterval then
        followState.updateInterval = options.updateInterval
    end
end

-- 가장 가까운 몬스터 찾기
function FollowTarget:FindNearestMonster()
    local monsters = ObjectTracker:GetObjectsByType(2)  -- OBJECT_MONSTER = 2
    local myPos = ObjectTracker:GetMyPosition()

    if not myPos or #monsters == 0 then
        return nil
    end

    local nearest = nil
    local nearestDist = math.huge

    for _, monster in ipairs(monsters) do
        if ObjectTracker:CanTarget(monster) then
            local monsterPos = ObjectTracker:GetPosition(monster)
            if monsterPos then
                local dist = ObjectTracker:CalculateDistance(myPos, monsterPos)
                if dist < nearestDist then
                    nearestDist = dist
                    nearest = monster
                end
            end
        end
    end

    return nearest
end

-- 지정된 대상 따라가기 시작
function FollowTarget:StartFollowing(targetId)
    local target = ObjectTracker:GetObjectById(targetId)

    if not target then
        print("[FollowTarget] 대상을 찾을 수 없음: " .. tostring(targetId))
        return false
    end

    if not ObjectTracker:CanTarget(target) then
        print("[FollowTarget] 대상을 선택할 수 없음: " .. tostring(targetId))
        return false
    end

    followState.isFollowing = true
    followState.targetId = targetId
    followState.targetObject = target
    followState.lastUpdateTime = os.clock()

    -- 위치 업데이트 콜백 등록
    PositionSynchronizer:RegisterCallback(targetId, function(objId, newPos)
        if followState.isFollowing and followState.targetId == objId then
            self:UpdateFollowPosition()
        end
    end)

    print("[FollowTarget] 따라가기 시작: " .. tostring(targetId))
    return true
end

-- 자동으로 몬스터 찾아서 따라가기
function FollowTarget:StartAutoFollow()
    local monster = self:FindNearestMonster()

    if not monster then
        print("[FollowTarget] 주변에 몬스터가 없음")
        return false
    end

    return self:StartFollowing(monster.id)
end

-- 따라가기 멈추기
function FollowTarget:StopFollowing()
    if followState.targetId then
        PositionSynchronizer:UnregisterCallback(followState.targetId, nil)
    end

    followState.isFollowing = false
    followState.targetId = nil
    followState.targetObject = nil

    print("[FollowTarget] 따라가기 중지")
    return true
end

-- 따라가기 상태 확인
function FollowTarget:IsFollowing()
    return followState.isFollowing
end

-- 따라가기 위치 업데이트 (메인 루프에서 계속 호출해야 함)
function FollowTarget:UpdateFollowPosition()
    if not followState.isFollowing then
        return false
    end

    local now = os.clock()
    if now - followState.lastUpdateTime < followState.updateInterval then
        return false
    end

    -- 1. 내 위치 확인
    local myPos = ObjectTracker:GetMyPosition()
    if not myPos then
        return false
    end

    -- 2. 대상 확인
    if not followState.targetId then
        self:StopFollowing()
        return false
    end

    local target = ObjectTracker:GetObjectById(followState.targetId)
    if not target or not ObjectTracker:CanTarget(target) then
        print("[FollowTarget] 대상을 잃음")
        self:StopFollowing()
        return false
    end

    -- 3. 대상 위치 확인
    local targetPos = ObjectTracker:GetPosition(target)
    if not targetPos then
        return false
    end

    -- 4. 거리 계산
    local distance = ObjectTracker:CalculateDistance(myPos, targetPos)

    -- 5. 거리가 너무 가까우면 멈추기
    if distance <= followState.stopDistance then
        -- 이미 가까우면 이동하지 않음
        followState.lastUpdateTime = now
        return true
    end

    -- 6. 이동 명령 생성
    if self:SendMoveCommand(targetPos, myPos) then
        followState.lastUpdateTime = now
        return true
    end

    return false
end

-- 이동 명령 전송
function FollowTarget:SendMoveCommand(targetPos, myPos)
    if not targetPos or not myPos then
        return false
    end

    -- 방향 계산 (dx, dy)
    local dx = targetPos.x - myPos.x
    local dy = targetPos.y - myPos.y

    -- 방향을 8개 중 하나로 정규화
    local direction = self:GetDirection(dx, dy)

    -- MeramNetworkService를 통해 이동 명령 전송
    if MeramNetworkService then
        -- ClientMoveCommand 또는 유사한 함수 호출
        -- (실제 게임의 네트워크 프로토콜에 맞게 조정 필요)
        local success = MeramNetworkService:SendMoveCommand(direction)

        if success then
            return true
        end
    end

    return false
end

-- 좌표 차이를 방향값으로 변환
function FollowTarget:GetDirection(dx, dy)
    -- 8방향 결정
    -- 0 = 상, 1 = 우상, 2 = 우, 3 = 우하, 4 = 하, 5 = 좌하, 6 = 좌, 7 = 좌상

    if math.abs(dx) < 0.1 and math.abs(dy) < 0.1 then
        return 0  -- 정지
    end

    local angle = math.atan2(dy, dx)
    local degress = math.deg(angle)

    -- -45 ~ 45: 우 (2)
    -- 45 ~ 135: 하 (4)
    -- -135 ~ -45: 상 (0)
    -- 135 ~ 180 또는 -180 ~ -135: 좌 (6)

    if degress >= -45 and degress < 45 then
        return 2  -- 우측
    elseif degress >= 45 and degress < 135 then
        return 4  -- 하단
    elseif degress >= 135 or degress < -135 then
        return 6  -- 좌측
    else  -- -135 ~ -45
        return 0  -- 상단
    end
end

-- 메인 루프용 틱 업데이트
function FollowTarget:Tick()
    if followState.isFollowing then
        self:UpdateFollowPosition()
    end
end

-- 현재 따라가기 대상 정보
function FollowTarget:GetTargetInfo()
    if not followState.isFollowing or not followState.targetObject then
        return nil
    end

    local targetPos = ObjectTracker:GetPosition(followState.targetObject)
    local myPos = ObjectTracker:GetMyPosition()

    return {
        targetId = followState.targetId,
        targetPos = targetPos,
        myPos = myPos,
        distance = ObjectTracker:CalculateDistance(myPos, targetPos),
        isFollowing = followState.isFollowing
    }
end

return FollowTarget
