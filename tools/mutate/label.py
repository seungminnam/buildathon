"""Label patches with the tests that fail on them (ground truth for validation).

Runs inside the tools/env image:

    docker run --rm -v <sqlglot>:/ro:ro -v <repo>/tools/mutate:/plug:ro \
        -v <patch dir>:/patches:ro -v <out dir>:/out br-env \
        python /plug/label.py /patches /out/labels.jsonl /out/tests.json

For each <id>.patch: fresh copy of the pinned checkout, apply the patch, run
the whole suite (pytest-xdist, SKIP_INTEGRATION=0, 300 s timeout per test),
append {"commit": id, "failing": [numbers], ...} to the output. Patches
already present in the output are skipped, so the run can be resumed.
"""

import json
import os
import shutil
import subprocess
import sys
import time

PATCHES, OUT, TESTS = sys.argv[1], sys.argv[2], sys.argv[3]
WORKERS = os.environ.get("LABEL_WORKERS", "4")
BASE = "/tmp/base"
WORK = "/tmp/w"
LABEL = "/tmp/label.json"
# Files holding the slowest tests go first so a worker starts them early.
ORDER = ["tests/test_parser.py", "tests/test_executor.py", "tests/test_generator.py",
         "tests/test_optimizer.py"]


def main():
    tests = json.load(open(TESTS))["tests"]
    number = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    done = set()
    if os.path.exists(OUT):
        done = {json.loads(line)["commit"] for line in open(OUT)}
    if not os.path.exists(BASE):
        shutil.copytree("/ro", BASE, symlinks=True)
        subprocess.run([sys.executable, "-m", "compileall", "-q", "sqlglot", "tests"], cwd=BASE,
                       stdout=subprocess.DEVNULL)
    ids = sorted(p[:-6] for p in os.listdir(PATCHES) if p.endswith(".patch"))
    shard, shards = (int(x) for x in os.environ.get("LABEL_SHARD", "0/1").split("/"))
    ids = [c for i, c in enumerate(ids) if i % shards == shard]
    for cid in ids:
        if cid in done:
            continue
        start = time.time()
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.copytree(BASE, WORK, symlinks=True)
        applied = subprocess.run(["patch", "-p1", "-s", "-i", f"{PATCHES}/{cid}.patch"], cwd=WORK,
                                 capture_output=True, text=True)
        rec = {"commit": cid}
        if applied.returncode != 0:
            rec["error"] = "patch: " + applied.stdout[-300:] + applied.stderr[-300:]
        else:
            env = dict(os.environ, PYTHONPATH=f"{WORK}:/plug", LABEL_OUT=LABEL,
                       SKIP_INTEGRATION="0")
            subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "label_plugin", "-p",
                            "no:cacheprovider", "-n", WORKERS, "--dist", "worksteal", "--timeout", "300", *ORDER, "tests"],
                           cwd=WORK, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            result = json.load(open(LABEL))
            failing, unknown = set(), []
            for nodeid in result["failed"]:
                if nodeid.startswith("<collect>:"):
                    path = nodeid.split(":", 1)[1]
                    hit = [n for f, ns in by_file.items() if f == path or path in ("", "tests")
                           for n in ns]
                    failing.update(hit)
                    unknown.append(nodeid)
                elif nodeid in number:
                    failing.add(number[nodeid])
                else:
                    unknown.append(nodeid)
            if failing:
                # Re-run what failed in one process, without xdist: tests such as
                # test_generator_memory fail spuriously depending on what ran
                # before them on the same worker.
                ids = [t["id"] for t in tests if t["number"] in failing]
                confirm = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "label_plugin", "-p", "no:cacheprovider",
                     "-p", "no:xdist", "--timeout", "300", *ids],
                    cwd=WORK, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                again = json.load(open(LABEL))["failed"]
                if any(n.startswith("<collect>") for n in again):
                    pass
                else:
                    again_n = {number[n] for n in again if n in number}
                    rec["flaky"] = sorted(failing - again_n)
                    failing &= again_n
            rec["failing"] = sorted(failing)
            rec["unknown"] = unknown
            rec["exit"] = result["exit"]
        rec["secs"] = round(time.time() - start, 1)
        with open(OUT, "a") as out:
            out.write(json.dumps(rec) + "\n")
        print(cid, rec.get("failing", rec.get("error")), rec["secs"], flush=True)


if __name__ == "__main__":
    main()
