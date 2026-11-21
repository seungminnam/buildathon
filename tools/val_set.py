"""Collect labelled validation commits into a set directory that score.py
and tools/dev_sample.sh understand (commits/, tests.json, answers.json).

    python3 tools/val_set.py PATCH_DIR LABEL_DIR KINDS_JSON TESTS_JSON OUT_DIR

Commits whose label has an "error" are left out.
"""
import glob
import json
import os
import shutil
import sys

patches, labels, kinds_path, tests, out = sys.argv[1:6]
kinds = json.load(open(kinds_path)) if os.path.exists(kinds_path) else {}
answers, skipped = {}, 0
for f in glob.glob(os.path.join(labels, "*.jsonl")):
    for line in open(f):
        d = json.loads(line)
        if "error" in d or "failing" not in d:
            skipped += 1
            continue
        answers[d["commit"]] = {"failing": d["failing"], "kind": kinds.get(d["commit"], "?")}
shutil.rmtree(out, ignore_errors=True)
os.makedirs(os.path.join(out, "commits"))
for c in answers:
    shutil.copy(os.path.join(patches, c + ".patch"), os.path.join(out, "commits", c + ".patch"))
shutil.copy(tests, os.path.join(out, "tests.json"))
json.dump({"commits": answers}, open(os.path.join(out, "answers.json"), "w"))
print(f"{len(answers)} commits, {skipped} skipped", file=sys.stderr)
