"""Build an eval set (tools/eval.py layout) from labelled generated commits.

    python3 tools/mutate/collect.py <patch dir> <label dir> <tests.json> <out set dir>

Only labelled commits are included; patches that failed to apply are skipped.
"""

import glob
import json
import os
import shutil
import sys

patch_dir, label_dir, tests_json, out = sys.argv[1:5]
kinds = json.load(open(os.path.join(patch_dir, "kinds.json")))
commits = {}
for path in glob.glob(os.path.join(label_dir, "labels.*.jsonl")):
    for line in open(path):
        r = json.loads(line)
        if "failing" in r:
            commits[r["commit"]] = {"failing": r["failing"], "kind": kinds.get(r["commit"], "?")}
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
for f in glob.glob(os.path.join(out, "commits", "*.patch")):
    os.remove(f)
for cid in commits:
    shutil.copy(os.path.join(patch_dir, cid + ".patch"), os.path.join(out, "commits", cid + ".patch"))
shutil.copy(tests_json, os.path.join(out, "tests.json"))
json.dump({"commits": commits}, open(os.path.join(out, "answers.json"), "w"))
print(f"{len(commits)} commits, {sum(1 for c in commits.values() if c['failing'])} with failures")
