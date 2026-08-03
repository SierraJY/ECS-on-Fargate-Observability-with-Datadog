#!/usr/bin/env bash
# 좌석 전체 초기화 스크립트 (PHASE4 트랙 A 보조)
#
# 부하 테스트/데모 리허설 후 BOOKED 상태로 남은 좌석을 전부 AVAILABLE로 되돌린다.
# 취소는 Gateway의 POST /reservations/{seat_id}/cancel(공개 API)만 사용 —
# LOCKED 상태는 Inventory의 내부 /seats/{seat_id}/release가 Gateway에 노출되어 있지 않아
# 이 스크립트로는 되돌릴 수 없다(정상적인 흐름에서는 LOCKED가 순간적으로만 존재하므로
# 실사용에선 거의 발생하지 않음 — 발생 시 별도로 확인 필요).
#
# 사용법: ./scripts/reset-seats.sh [--host URL]

set -uo pipefail

HOST="http://jy-project-alb-972275247.ap-northeast-2.elb.amazonaws.com"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

RESPONSE=$(curl -s "$HOST/seats")

BOOKED=$(echo "$RESPONSE" | grep -o '"seat_id":"[A-Z0-9]*","status":"BOOKED"' | sed -E 's/"seat_id":"([A-Z0-9]+)".*/\1/')
LOCKED=$(echo "$RESPONSE" | grep -o '"seat_id":"[A-Z0-9]*","status":"LOCKED"' | sed -E 's/"seat_id":"([A-Z0-9]+)".*/\1/')

if [[ -z "$BOOKED" ]]; then
  echo "BOOKED 상태 좌석 없음 — 초기화할 것 없음"
else
  reset_count=0
  fail_count=0
  for seat in $BOOKED; do
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOST/reservations/$seat/cancel" \
      -H "Content-Type: application/json" \
      -d '{"user_id":"reset-script"}')
    if [[ "$code" == "200" ]]; then
      reset_count=$((reset_count + 1))
    else
      fail_count=$((fail_count + 1))
      echo "  실패: $seat (HTTP $code)" >&2
    fi
  done
  echo "초기화 완료: 성공 $reset_count, 실패 $fail_count"
fi

if [[ -n "$LOCKED" ]]; then
  echo "경고: LOCKED 상태로 남은 좌석 있음 (Gateway에 release 엔드포인트가 없어 이 스크립트로 처리 불가): $LOCKED" >&2
fi
