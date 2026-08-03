#!/usr/bin/env bash
# Payment 장애 주입/해제 스크립트 (PHASE4 트랙 D)
#
# Gateway가 Payment의 POST /admin/chaos를 그대로 프록시하므로 ALB 통해 호출한다.
# mode=off가 곧 장애 해제다.
#
# 사용법:
#   ./scripts/chaos.sh error --error-rate 1.0
#   ./scripts/chaos.sh latency --delay-ms 3000
#   ./scripts/chaos.sh off

set -uo pipefail

HOST="http://jy-project-alb-972275247.ap-northeast-2.elb.amazonaws.com"
MODE="${1:-}"
shift || true

DELAY_MS=0
ERROR_RATE=0.0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --delay-ms) DELAY_MS="$2"; shift 2 ;;
    --error-rate) ERROR_RATE="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1 ;;
  esac
done

case "$MODE" in
  latency|error|off) ;;
  *)
    echo "사용법: $0 <latency|error|off> [--delay-ms N] [--error-rate R] [--host URL]" >&2
    exit 1
    ;;
esac

curl -s -X POST "$HOST/admin/chaos" \
  -H "Content-Type: application/json" \
  -d "{\"mode\":\"$MODE\",\"delay_ms\":$DELAY_MS,\"error_rate\":$ERROR_RATE}"
echo
