return function(self)
  -- 가장 가까운 몬스터 찾기
  local monster = self:GetNearestMonster()
  if not monster then
    local gameHud = ___MOD._MeramHudService:GetCurrentGameHud()
    if gameHud then
      gameHud:SystemMessage("[자동따라가기] 주변에 몬스터가 없습니다.")
    end
    return false
  end

  -- 몬스터 정보 저장
  self.FollowingEntity = monster
  self.IsFollowing = true

  local monsterName = monster.Name or "몬스터"
  local gameHud = ___MOD._MeramHudService:GetCurrentGameHud()
  if gameHud then
    gameHud:SystemMessage(___MOD.string.format("[자동따라가기] [%s] 님을 따라갑니다.", monsterName))
  end

  return true
end
