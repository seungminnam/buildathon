#!/bin/sh
# Run the selector from the working tree (no image build) under the scorer's
# limits on a commit set, then score it. Usage: tools/dev_sample.sh [SET_DIR]
set -e
K=${KIT:-$HOME/dev/br-kit}
SET=${1:-$K/sample}
OUT=${OUT:-/tmp/br-out.jsonl}
docker run --rm --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 \
  --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges \
  --user 65534:65534 --env HOME=/tmp --env PYTHONDONTWRITEBYTECODE=1 $DOCKER_ENV \
  -v $K/sqlglot:/opt/sqlglot:ro -v $(pwd):/app:ro \
  -v $SET/commits:/data/commits:ro -v $SET/tests.json:/data/tests.json:ro \
  ${IMAGE:-br-env} python /app/selector.py > $OUT
python3 $K/score.py $SET $SET/answers.json $OUT
