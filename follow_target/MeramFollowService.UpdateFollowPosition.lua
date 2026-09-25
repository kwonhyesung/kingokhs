return function(self)
  if not self.IsFollowing then
    return false
  end

  local entity = self.FollowingEntity
  if not entity or not ___MOD.isvalid(entity) then
    self.IsFollowing = false
    return false
  end

  -- 대상이 여전히 공격 가능한지 확인
  local CC = entity.MeramCreatureController
  if not CC or CC:IsDead() or not CC:CanTarget() then
    self:StopFollowing()
    return false
  end

  -- 현재 내 위치
  local localPlayer = ___MOD._UserService and ___MOD._UserService.LocalPlayer
  if not localPlayer then
    self:StopFollowing()
    return false
  end

  local playerEntity = localPlayer:GetChildByName("Player")
  if not playerEntity then
    self:StopFollowing()
    return false
  end

  local myMov = playerEntity.MeramMovementComponent
  if not myMov then
    self:StopFollowing()
    return false
  end

  local myPos = myMov:GetPosition()
  if not myPos then
    self:StopFollowing()
    return false
  end

  -- 대상 위치
  local targetMov = entity.MeramMovementComponent
  if not targetMov then
    self:StopFollowing()
    return false
  end

  local targetPos = targetMov:GetPosition()
  if not targetPos then
    self:StopFollowing()
    return false
  end

  -- 거리 계산 (맨해튼)
  local distance = ___MOD.math.abs(myPos.x - targetPos.x) + ___MOD.math.abs(myPos.y - targetPos.y)

  -- 거리가 1칸 이내면 정지
  if distance <= 1 then
    return true
  end

  -- 방향 계산
  local dx = targetPos.x - myPos.x
  local dy = targetPos.y - myPos.y

  local angle = ___MOD.math.atan2(dy, dx)
  local degrees = ___MOD.math.deg(angle)

  -- 8방향 결정
  local direction = 0
  if degrees >= -45 and degrees < 45 then
    direction = 2  -- 우측
  elseif degrees >= 45 and degrees < 135 then
    direction = 4  -- 하단
  elseif degrees >= 135 or degrees < -135 then
    direction = 6  -- 좌측
  else
    direction = 0  -- 상단
  end

  -- 이동 명령 전송
  if ___MOD._MeramNetworkService then
    ___MOD._MeramNetworkService:ClientInputMovement(direction)
  end

  return true
end
