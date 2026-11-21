#!/bin/sh
# Label a directory of patches with ground truth, in 3 parallel containers.
#   tools/mutate/label.sh <sqlglot checkout> <patch dir> <out dir> <tests.json>
set -e
SRC=$(cd "$1" && pwd); PATCHES=$(cd "$2" && pwd); mkdir -p "$3"; OUT=$(cd "$3" && pwd); TESTS=$(cd "$(dirname "$4")" && pwd)/$(basename "$4")
PLUG=$(cd "$(dirname "$0")" && pwd)
N=${SHARDS:-1}
i=0
while [ $i -lt $N ]; do
  docker run -d --rm --name "lab-$(basename "$PATCHES")-$i" -e LABEL_SHARD=$i/$N -e LABEL_WORKERS=${WORKERS:-8} \
    -v "$SRC":/ro:ro -v "$PLUG":/plug:ro -v "$PATCHES":/patches:ro -v "$TESTS":/tests.json:ro -v "$OUT":/out \
    fw-env python /plug/label.py /patches /out/labels.$i.jsonl /tests.json >/dev/null
  i=$((i+1))
done
