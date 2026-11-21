#!/bin/bash
# Build per-test coverage at the pinned commit, in 4 parallel shards.
#   tools/covmap/run_coverage.sh <sqlglot checkout> <out dir> [image]
# then: cat <out>/cov.*.jsonl > all.jsonl; python3 tools/covmap/build_map.py all.jsonl maps/coverage.json
set -e
SRC=$(cd "$1" && pwd); mkdir -p "$2"; OUT=$(cd "$2" && pwd); IMG=${3:-fw-env}
PLUG=$(cd "$(dirname "$0")" && pwd)
FILES=($(cd "$SRC" && ls tests/test_*.py tests/dialects/test_*.py))
for k in $(seq 0 $((${SHARDS:-4}-1))); do
  SHARD=()
  for i in "${!FILES[@]}"; do [ $((i % ${SHARDS:-4})) -eq $k ] && SHARD+=("${FILES[$i]}"); done
  docker run -d --rm --name fw-cov-$k -v "$SRC":/src:ro -v "$PLUG":/plug:ro -v "$OUT":/out -w /src \
    -e PYTHONPATH=/src:/plug -e TRACE_ROOT=/src -e TRACE_OUT=/out/cov.$k.jsonl -e SKIP_INTEGRATION=0 \
    "$IMG" python -m pytest -q -p trace_plugin -p no:cacheprovider "${SHARD[@]}" >/dev/null
done
