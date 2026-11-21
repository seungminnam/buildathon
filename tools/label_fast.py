"""Label patches by running every test through blast/verify.py (one forked
pytest per commit, process pools forked instead of spawned).

    docker run ... br-env python /app/tools/label_fast.py PATCH_DIR OUT.jsonl SHARD/N

Checked against the official sample answers before use. A commit whose run
did not finish cleanly is written with "error" and must not be used.
"""

import json
import os
import resource
import sys
import time

sys.path.insert(0, "/app")
from blast import overlay, patch as patchlib, verify  # noqa: E402

SRC = "/opt/sqlglot"


def main():
    patch_dir, out, shard = sys.argv[1], sys.argv[2], sys.argv[3]
    k, n = (int(x) for x in shard.split("/"))
    tests = json.load(open("/data/tests.json"))["tests"]
    ids = [t["id"] for t in tests]
    num = {t["id"]: t["number"] for t in tests}
    done = set()
    if os.path.exists(out):
        done = {json.loads(line)["commit"] for line in open(out)}
    verify.warm_up()
    cids = sorted(p[:-6] for p in os.listdir(patch_dir) if p.endswith(".patch"))
    for i, cid in enumerate(cids):
        if i % n != k or cid in done:
            continue
        rec = {"commit": cid}
        t0 = time.time()
        try:
            changes = {}
            for fd in patchlib.parse(open(os.path.join(patch_dir, cid + ".patch")).read()):
                old = open(os.path.join(SRC, fd.old_path)).read() if fd.old_path else ""
                if fd.old_path and fd.old_path != fd.new_path:
                    changes[fd.old_path] = None
                if fd.new_path:
                    new = patchlib.apply(old, fd)
                    if new is None:
                        raise ValueError(f"does not apply: {fd.new_path}")
                    changes[fd.new_path] = new
            tree = overlay.build(SRC, f"/tmp/w/{cid}", changes)
            failed, finished, broken, ok = verify.run(tree, ids, 900)
            overlay.remove(tree)
            failing = {num[x] for x in failed if x in num}
            for t in tests:
                if t["file"] in broken:
                    failing.add(t["number"])
            missing = [x for x in ids if x not in finished and x not in failed]
            if not ok or (missing and not broken):
                rec["error"] = f"ok={ok} unfinished={len(missing)}"
            rec["failing"] = sorted(failing)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        rec["secs"] = round(time.time() - t0, 1)
        with open(out, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(cid, rec.get("error", ""), len(rec.get("failing", [])), rec["secs"], flush=True)


if __name__ == "__main__":
    main()
