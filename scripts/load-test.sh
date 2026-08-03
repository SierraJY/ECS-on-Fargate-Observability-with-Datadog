#!/usr/bin/env bash
# 배경 트래픽 생성 스크립트 (PHASE4 트랙 A)
#
# GET /seats, POST /reservations, POST /reservations/{seat_id}/cancel을 섞어서
# Gateway/Reservation/Inventory/Payment/Notification 5개 서비스에 골고루 트래픽을 발생시킨다.
# POST /reservations는 성공/409 충돌 여부와 무관하게 Payment charge까지 항상 호출되므로
# (reservation/main.py의 lock/charge 병렬 호출 구조) 별도 가중치 없이도 5개 서비스가 고르게 걸린다.
#
# 사용법:
#   ./scripts/load-test.sh [--host URL] [--rps N] [--duration SECONDS]
#
# --duration 0(기본값)이면 Ctrl+C로 중단할 때까지 무한 실행.

set -uo pipefail

HOST="http://jy-project-alb-972275247.ap-northeast-2.elb.amazonaws.com"
RPS=5
DURATION=0
SEATS=()
for i in $(seq 1 20); do SEATS+=("A$i"); done

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --rps) RPS="$2"; shift 2 ;;
    --duration) DURATION="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

LOG_FILE="$(mktemp /tmp/jy-project-load-test.XXXXXX.log)"
BOOKED_CACHE="$(mktemp /tmp/jy-project-load-test-booked.XXXXXX.txt)"
START_TS=$(date +%s)

cleanup() {
  local end_ts elapsed
  end_ts=$(date +%s)
  elapsed=$(( end_ts - START_TS ))
  echo
  echo "=== 트래픽 생성 종료 (${elapsed}s 실행) ==="
  if [[ -s "$LOG_FILE" ]]; then
    echo "엔드포인트별 상태 코드 집계:"
    sort "$LOG_FILE" | uniq -c | sort -rn
    echo "총 요청 수: $(wc -l < "$LOG_FILE")"
  fi
  rm -f "$LOG_FILE" "$BOOKED_CACHE"
  exit 0
}
trap cleanup INT TERM

refresh_booked_cache() {
  # python 없이 grep/sed만으로 응답 JSON에서 BOOKED 좌석의 seat_id만 추출
  curl -s "$HOST/seats" 2>/dev/null \
    | grep -o '"seat_id":"[A-Z0-9]*","status":"BOOKED"' \
    | sed -E 's/"seat_id":"([A-Z0-9]+)".*/\1/' \
    > "$BOOKED_CACHE" || true
}

pick_random() {
  local arr=("$@")
  local n=${#arr[@]}
  [[ $n -eq 0 ]] && return 1
  echo "${arr[$((RANDOM % n))]}"
}

fire_request() {
  local action="$1"
  case "$action" in
    seats)
      code=$(curl -s -o /dev/null -w "%{http_code}" "$HOST/seats")
      echo "GET /seats $code" >> "$LOG_FILE"
      ;;
    reserve)
      local seat="${SEATS[$((RANDOM % ${#SEATS[@]}))]}"
      code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOST/reservations" \
        -H "Content-Type: application/json" \
        -d "{\"seat_id\":\"$seat\",\"user_id\":\"load-test\"}")
      echo "POST /reservations $code" >> "$LOG_FILE"
      ;;
    cancel)
      mapfile -t booked < "$BOOKED_CACHE" 2>/dev/null || booked=()
      local seat
      seat=$(pick_random "${booked[@]}") || return 0
      code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$HOST/reservations/$seat/cancel" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\":\"load-test\"}")
      echo "POST /reservations/{seat}/cancel $code" >> "$LOG_FILE"
      ;;
  esac
}

if [[ "$DURATION" -eq 0 ]]; then
  echo "트래픽 생성 시작: $HOST (rps=$RPS, duration=무한)"
else
  echo "트래픽 생성 시작: $HOST (rps=$RPS, duration=${DURATION}s)"
fi
echo "중단하려면 Ctrl+C"

TICK=0
while true; do
  if [[ "$DURATION" -gt 0 ]]; then
    now=$(date +%s)
    (( now - START_TS >= DURATION )) && break
  fi

  # 5틱(약 5초)마다 BOOKED 좌석 캐시 갱신 — 취소 대상 확보
  if (( TICK % 5 == 0 )); then
    refresh_booked_cache
  fi

  for _ in $(seq 1 "$RPS"); do
    r=$((RANDOM % 100))
    if (( r < 70 )); then
      fire_request reserve &
    elif (( r < 90 )); then
      fire_request seats &
    else
      fire_request cancel &
    fi
  done

  TICK=$((TICK + 1))
  sleep 1
done

wait
cleanup
