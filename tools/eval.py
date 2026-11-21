"""Score the selector locally on a set of commits with known answers.

    python3 tools/eval.py <set dir> [--verbose]

<set dir> holds commits/*.patch, tests.json and answers.json (the sample kit's
layout; tools/mutate/collect.py builds the same for generated commits). Runs
selector.py on the host with BLAST_* paths, then scores the output with the
kit's score.py and prints the cost against the oracle, per kind of change.
"""

import json
import os
import resource
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import score  # noqa: E402

DEFAULT_SQLGLOT = os.environ.get("BLAST_SQLGLOT")


def oracle_cost(failing, ms):
    if not failing:
        return 0
    d = sorted((ms[n] for n in failing), reverse=True)
    best = None
    for k in range(1, min(8, len(d)) + 1):
        loads = [0] * k
        for x in d:
            loads[loads.index(min(loads))] += x
        c = max(loads) + 2000 * k
        best = c if best is None else min(best, c)
    return best


def main():
    set_dir = Path(sys.argv[1])
    verbose = "--verbose" in sys.argv
    tests = json.loads((set_dir / "tests.json").read_text())["tests"]
    answers = json.loads((set_dir / "answers.json").read_text())["commits"]
    env = dict(os.environ, BLAST_DATA=str(set_dir))
    if DEFAULT_SQLGLOT:
        env["BLAST_SQLGLOT"] = DEFAULT_SQLGLOT
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    proc = subprocess.run([sys.executable, str(ROOT / "selector.py")], env=env, capture_output=True)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if proc.returncode:
        print(proc.stderr.decode()[-3000:])
        sys.exit(1)
    ev = score.evaluate_run(proc.stdout, tests, answers)
    if not ev["ok"]:
        print("REJECTED", ev)
        sys.exit(1)
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    out = {}
    for line in proc.stdout.decode().splitlines():
        rec = json.loads(line)
        out[rec["commit"]] = rec["workers"]
    notes = {}
    for line in proc.stderr.decode().splitlines():
        if ": " in line and line.split(":")[0] in answers:
            notes[line.split(":")[0]] = line.split(": ", 1)[1]
    missed = set(ev["missed_commits"])
    kinds = {}
    for cid, a in answers.items():
        k = a.get("kind", "?")
        cost = score.evaluate_run((json.dumps({"commit": cid, "workers": out[cid]}) + "\n").encode(), tests,
                                  {cid: a})["simulated_milliseconds"]
        orc = oracle_cost(a["failing"], ms)
        row = kinds.setdefault(k, [0, 0, 0, 0])
        row[0] += 1
        row[1] += cost
        row[2] += orc
        row[3] += cid in missed
        if verbose or cid in missed:
            fail_missing = [n for n in a["failing"] if n not in {x for w in out[cid] for x in w}]
            print(f"{cid:10s} {k:14s} cost {cost/1000:7.2f} oracle {orc/1000:6.2f} "
                  f"{'MISS ' + str(fail_missing[:5]) if cid in missed else ''} | {notes.get(cid, '')[:160]}")
    print(f"{'kind':14s} {'n':>4s} {'cost':>9s} {'oracle':>8s} misses")
    for k, (n, c, o, m) in sorted(kinds.items()):
        print(f"{k:14s} {n:4d} {c/1000:9.2f} {o/1000:8.2f} {m}")
    sim = ev["simulated_milliseconds"] / 1000
    print(f"TOTAL simulated {sim:.2f} s  misses {len(missed)}  cpu {cpu:.2f} s  "
          f"total {sim + cpu:.2f}  oracle {sum(v[2] for v in kinds.values())/1000:.2f}  commits {len(answers)}")


if __name__ == "__main__":
    main()
