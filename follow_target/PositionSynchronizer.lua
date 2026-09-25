-- 네트워크에서 받은 위치 업데이트를 처리하는 시스템
-- ClientMoveObject_elementwise 및 ClientSetObjectPosition_elementwise 기반

local PositionSynchronizer = {}

-- 위치 업데이트 이력
local positionHistory = {}
local MAX_HISTORY_SIZE = 100

-- 위치 업데이트 콜백 리스너
local positionUpdateCallbacks = {}

-- 위치 업데이트 콜백 등록
function PositionSynchronizer:RegisterCallback(objectId, callback)
    if not positionUpdateCallbacks[objectId] then
        positionUpdateCallbacks[objectId] = {}
    end
    table.insert(positionUpdateCallbacks[objectId], callback)
end

-- 위치 업데이트 콜백 제거
function PositionSynchronizer:UnregisterCallback(objectId, callback)
    if positionUpdateCallbacks[objectId] then
        for i, cb in ipairs(positionUpdateCallbacks[objectId]) do
            if cb == callback then
                table.remove(positionUpdateCallbacks[objectId], i)
                break
            end
        end
    end
end

-- 객체 위치 업데이트 (네트워크에서 받음)
function PositionSynchronizer:UpdatePosition(objectId, newPosition, timestamp)
    if not objectId or not newPosition then
        return false
    end

    -- 위치 이력 저장
    if not positionHistory[objectId] then
        positionHistory[objectId] = {}
    end

    table.insert(positionHistory[objectId], {
        position = newPosition,
        timestamp = timestamp or os.clock()
    })

    -- 이력 크기 제한
    if #positionHistory[objectId] > MAX_HISTORY_SIZE then
        table.remove(positionHistory[objectId], 1)
    end

    -- 등록된 콜백 호출
    if positionUpdateCallbacks[objectId] then
        for _, callback in ipairs(positionUpdateCallbacks[objectId]) do
            callback(objectId, newPosition)
        end
    end

    return true
end

-- 객체의 현재 위치 가져오기
function PositionSynchronizer:GetCurrentPosition(objectId)
    if not MeramCreatureService then
        return nil
    end

    local seenObjects = MeramCreatureService.SeenObjectsClientOnly
    if not seenObjects then
        return nil
    end

    local entity = seenObjects[objectId]
    if not entity or not entity:IsValid() then
        return nil
    end

    local movementComp = entity:GetComponent("MeramMovementComponent")
    if movementComp then
        return movementComp:GetPosition()
    end

    return nil
end

-- 객체의 위치 이동 (MoveFrom 방식)
function PositionSynchronizer:MoveObject(objectId, fromPos, toPos, direction)
    if not MeramCreatureService then
        return false
    end

    local seenObjects = MeramCreatureService.SeenObjectsClientOnly
    if not seenObjects then
        return false
    end

    local entity = seenObjects[objectId]
    if not entity or not entity:IsValid() then
        return false
    end

    local movementComp = entity:GetComponent("MeramMovementComponent")
    if movementComp then
        movementComp:MoveFrom(fromPos, direction)
        return true
    end

    return false
end

-- 객체의 위치 설정 (InitializePosition 방식)
function PositionSynchronizer:SetObjectPosition(objectId, mapId, newPosition)
    if not MeramCreatureService then
        return false
    end

    local seenObjects = MeramCreatureService.SeenObjectsClientOnly
    if not seenObjects then
        return false
    end

    local entity = seenObjects[objectId]
    if not entity or not entity:IsValid() then
        return false
    end

    local movementComp = entity:GetComponent("MeramMovementComponent")
    if movementComp then
        movementComp:InitializePosition(mapId, newPosition)
        return true
    end

    return false
end

-- 객체의 위치 예측 (선형 보간)
function PositionSynchronizer:PredictPosition(objectId, futureTime)
    if not positionHistory[objectId] or #positionHistory[objectId] < 2 then
        return self:GetCurrentPosition(objectId)
    end

    local history = positionHistory[objectId]
    local lastUpdate = history[#history]
    local prevUpdate = history[#history - 1]

    if not lastUpdate or not prevUpdate then
        return lastUpdate.position
    end

    local timeDelta = lastUpdate.timestamp - prevUpdate.timestamp
    if timeDelta == 0 then
        return lastUpdate.position
    end

    -- 속도 계산
    local velocityX = (lastUpdate.position.x - prevUpdate.position.x) / timeDelta
    local velocityY = (lastUpdate.position.y - prevUpdate.position.y) / timeDelta

    -- 미래 위치 예측
    local predictedX = lastUpdate.position.x + velocityX * futureTime
    local predictedY = lastUpdate.position.y + velocityY * futureTime

    return {
        x = predictedX,
        y = predictedY
    }
end

-- 위치 이력 가져오기
function PositionSynchronizer:GetHistory(objectId)
    return positionHistory[objectId] or {}
end

-- 위치 이력 초기화
function PositionSynchronizer:ClearHistory(objectId)
    if objectId then
        positionHistory[objectId] = nil
    else
        positionHistory = {}
    end
end

-- 객체의 평균 이동 속도 계산
function PositionSynchronizer:GetAverageVelocity(objectId, sampleSize)
    sampleSize = sampleSize or 5

    if not positionHistory[objectId] or #positionHistory[objectId] < 2 then
        return { x = 0, y = 0 }
    end

    local history = positionHistory[objectId]
    local startIdx = math.max(1, #history - sampleSize)
    local firstUpdate = history[startIdx]
    local lastUpdate = history[#history]

    local timeDelta = lastUpdate.timestamp - firstUpdate.timestamp
    if timeDelta == 0 then
        return { x = 0, y = 0 }
    end

    return {
        x = (lastUpdate.position.x - firstUpdate.position.x) / timeDelta,
        y = (lastUpdate.position.y - firstUpdate.position.y) / timeDelta
    }
end

return PositionSynchronizer
