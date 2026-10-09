#!/usr/bin/env bash
# One-command evaluation. Usage: ./eval.sh [base-url]
# Does not print secrets. Exits 1 if a required check fails.
set -euo pipefail
BASE="${1:-http://localhost:8090}"
BASE="${BASE%/}"
fail=0
pass() { printf 'PASS  %s\n' "$1"; }
failx() { printf 'FAIL  %s\n' "$1"; fail=1; }

echo "base=$BASE"

if ! health=$(curl -sf -m 20 "$BASE/health"); then
  echo "FAIL  GET /health (unreachable)"; exit 1
fi
echo "$health" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("ok") else 1)' \
  && pass "GET /health ok" || failx "GET /health ok"

if ! evalj=$(curl -sf -m 30 "$BASE/api/eval"); then
  echo "FAIL  GET /api/eval"; exit 1
fi
python3 -c '
import json, sys
d = json.loads(sys.argv[1])
print("inputs:", json.dumps(d.get("inputs"), indent=2)[:900])
print("outputs:", json.dumps(d.get("outputs"), indent=2)[:900])
bad = [c["id"] for c in d.get("checks", []) if not c.get("pass")]
print("failed checks:", bad or "none")
sys.exit(1 if bad else 0)
' "$evalj"
pass "GET /api/eval all checks"

search=$(curl -sf -m 60 "$BASE/api/search?q=truck%20in%20the%20bike%20lane")
n=$(echo "$search" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("results") or []))')
if [ "$n" -gt 0 ]; then pass "search hits=$n"; else failx "search hits=$n"; fi

iv=$(curl -sf -m 120 -X POST "$BASE/api/interview" -H 'content-type: application/json' \
  -d '{"persona":"bike_lane","question":"What blocks you most often?"}')
if echo "$iv" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("answer") and d.get("evidence") else 1)'; then
  pass "interview answer + evidence"
else
  failx "interview answer + evidence"
fi

echo
if [ "$fail" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED"; exit 1; fi
