#!/usr/bin/env bash
# CI quality gate: run retrieval eval and FAIL the build if recall@k drops below threshold.
# Used by the CodePipeline eval stage (buildspecs/eval_gate.yml). Requires AWS creds.
set -euo pipefail

THRESHOLD="${RECALL_THRESHOLD:-0.85}"
STAMP="${GIT_SHA:-ci}"

echo "== HappyFeet retrieval quality gate (threshold recall@k >= ${THRESHOLD}) =="
python eval/run_retrieval_eval.py --timestamp "${STAMP}"

python - "$THRESHOLD" "$STAMP" <<'PY'
import json, sys, pathlib
threshold = float(sys.argv[1]); stamp = sys.argv[2]
matches = sorted(pathlib.Path("eval/results").glob(f"retrieval_*_{stamp}.json"))
if not matches:
    print("no eval result written"); sys.exit(2)
data = json.loads(matches[-1].read_text())
recall = data["recall_at_k"]
print(f"recall@k = {recall:.3f} (threshold {threshold})")
if recall < threshold:
    print("QUALITY GATE FAILED"); sys.exit(1)
print("QUALITY GATE PASSED")
PY
