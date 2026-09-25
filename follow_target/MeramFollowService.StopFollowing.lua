return function(self)
  if not self.IsFollowing then
    return false
  end

  self.IsFollowing = false
  self.FollowingEntity = nil

  local gameHud = ___MOD._MeramHudService:GetCurrentGameHud()
  if gameHud then
    gameHud:SystemMessage("[자동따라가기] 중지")
  end

  return true
end
