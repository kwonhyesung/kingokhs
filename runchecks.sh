#!/bin/sh
# 게임 없이 돌릴 수 있는 검사 전부. 로직 수정 후 이것부터 돌린다.
cd "$(dirname "$0")"
fail=0
for f in tests/test_navigation_sim.py tests/test_hunt_cycle.py tests/test_scan_map_filter.py \
         tests/test_move_command_form.py tests/test_patrol_routes.py \
         tests/test_support_runtime_rules.py test_findtext_wrapper.py svc_hunt.py; do
  printf '%-38s' "$f"
  if out=$(python -X utf8 "$f" 2>&1); then
    echo "OK   $(echo "$out" | tail -1)"
  else
    echo "FAIL"; echo "$out" | tail -12; fail=1
  fi
done
[ $fail -eq 0 ] && echo "--- 전부 통과. 게임에서 확인할 것만 남았다." || echo "--- 실패 있음. 게임 켜지 말 것."
exit $fail
