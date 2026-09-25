-- 현재 맵의 보이는 객체들을 추적하는 시스템
-- SeenObjectsClientOnly를 기반으로 함

local ObjectTracker = {}

-- 객체 타입 정의
local OBJECT_TYPE = {
    PLAYER = 1,
    MONSTER = 2,
    NPC = 3,
    ITEM = 4,
    OTHER = 5
}

-- 객체 정보 캐시
local objectCache = {}
local cacheUpdateTime = 0
local CACHE_UPDATE_INTERVAL = 0.1  -- 100ms마다 업데이트

-- SeenObjectsClientOnly에서 모든 객체 가져오기
function ObjectTracker:GetAllVisibleObjects()
    if not MeramCreatureService then
        return {}
    end

    local objects = {}
    local seenObjects = MeramCreatureService.SeenObjectsClientOnly

    if not seenObjects then
        return objects
    end

    for netObjId, entity in pairs(seenObjects) do
        if entity and entity:IsValid() then
            table.insert(objects, {
                id = netObjId,
                entity = entity,
                controller = entity:GetComponent("MeramCreatureController")
            })
        end
    end

    return objects
end

-- 특정 타입의 객체만 필터링
function ObjectTracker:GetObjectsByType(objectType)
    local allObjects = self:GetAllVisibleObjects()
    local filtered = {}

    for _, obj in ipairs(allObjects) do
        if obj.controller then
            local type = obj.controller:GetObjectType()
            if type == objectType then
                table.insert(filtered, obj)
            end
        end
    end

    return filtered
end

-- 특정 ID의 객체 가져오기
function ObjectTracker:GetObjectById(objectId)
    if not MeramCreatureService then
        return nil
    end

    local seenObjects = MeramCreatureService.SeenObjectsClientOnly
    if not seenObjects then
        return nil
    end

    local entity = seenObjects[objectId]
    if entity and entity:IsValid() then
        return {
            id = objectId,
            entity = entity,
            controller = entity:GetComponent("MeramCreatureController")
        }
    end

    return nil
end

-- 객체의 위치 가져오기
function ObjectTracker:GetPosition(obj)
    if not obj or not obj.entity then
        return nil
    end

    local movementComp = obj.entity:GetComponent("MeramMovementComponent")
    if movementComp then
        return movementComp:GetPosition()
    end

    return nil
end

-- 객체가 공격 가능한지 확인
function ObjectTracker:CanTarget(obj)
    if not obj or not obj.controller then
        return false
    end

    -- 죽은 객체는 불가
    if obj.controller:IsDead() then
        return false
    end

    -- CanTarget 플래그 확인
    if not obj.controller:CanTarget() then
        return false
    end

    return true
end

-- 두 위치 사이의 맨해튼 거리 계산
function ObjectTracker:CalculateDistance(pos1, pos2)
    if not pos1 or not pos2 then
        return math.huge
    end

    local dx = math.abs(pos1.x - pos2.x)
    local dy = math.abs(pos1.y - pos2.y)

    return dx + dy
end

-- 내 위치 가져오기
function ObjectTracker:GetMyPosition()
    if not MeramCharacterManager then
        return nil
    end

    local localPlayer = MeramCharacterManager:GetLocalPlayer()
    if not localPlayer then
        return nil
    end

    local movementComp = localPlayer:GetComponent("MeramMovementComponent")
    if movementComp then
        return movementComp:GetPosition()
    end

    return nil
end

-- 캐시된 객체 목록 업데이트
function ObjectTracker:UpdateCache()
    local now = os.clock()
    if now - cacheUpdateTime < CACHE_UPDATE_INTERVAL then
        return
    end

    objectCache = self:GetAllVisibleObjects()
    cacheUpdateTime = now
end

-- 캐시된 객체 가져오기
function ObjectTracker:GetCachedObjects()
    self:UpdateCache()
    return objectCache
end

return ObjectTracker
