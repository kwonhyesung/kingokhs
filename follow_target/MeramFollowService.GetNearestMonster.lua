return function(self)
  local seenObjects = ___MOD._MeramCreatureService and ___MOD._MeramCreatureService.SeenObjectsClientOnly
  if not seenObjects then
    return nil
  end

  local localPlayer = ___MOD._UserService and ___MOD._UserService.LocalPlayer
  if not localPlayer then
    return nil
  end

  local playerEntity = localPlayer:GetChildByName("Player")
  if not playerEntity then
    return nil
  end

  local playerMov = playerEntity.MeramMovementComponent
  if not playerMov then
    return nil
  end

  local myPos = playerMov:GetPosition()
  if not myPos then
    return nil
  end

  local nearestMonster = nil
  local nearestDist = math.huge

  for netObjId, entity in pairs(seenObjects) do
    if ___MOD.isvalid(entity) then
      local CC = entity.MeramCreatureController
      if CC then
        -- 죽지 않았고 공격 가능하며 몬스터 타입인지 확인
        if not CC:IsDead() and CC:CanTarget() then
          local objType = CC:GetObjectType()
          if objType == 2 then  -- OBJECT_MONSTER = 2
            local movComp = entity.MeramMovementComponent
            if movComp then
              local entityPos = movComp:GetPosition()
              if entityPos then
                -- 맨해튼 거리 계산
                local dist = ___MOD.math.abs(myPos.x - entityPos.x) + ___MOD.math.abs(myPos.y - entityPos.y)
                if dist < nearestDist then
                  nearestDist = dist
                  nearestMonster = entity
                end
              end
            end
          end
        end
      end
    end
  end

  return nearestMonster
end
