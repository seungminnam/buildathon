"""Blast Radius selector: for each patch, the tests it can break, packed onto workers.

Inputs (read-only): /data/tests.json, /data/commits/*.patch, the sqlglot
checkout at the pinned commit (/opt/sqlglot) and the coverage map built from
it (maps/coverage.json). Output: one JSON line per commit on stdout.
"""

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from blast import patch as patchlib  # noqa: E402
from blast.covmap import CoverageMap  # noqa: E402
from blast.impact import Analyzer, Result  # noqa: E402
from blast.pack import pack, cost  # noqa: E402
from blast.runner import run_tests  # noqa: E402

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SQLGLOT = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")
MAP = os.environ.get("BLAST_MAP", str(HERE / "maps" / "coverage.json"))
TMP = os.environ.get("BLAST_TMP", "/tmp")
RUN = os.environ.get("BLAST_RUN", "1") == "1"

# Running a test here costs its CPU time; handing it to a worker costs its
# share of the busiest worker plus 2 s per worker. Only cheap tests from files
# without process pools are run here.
RUN_MAX_MS = 1000
NO_RUN_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")
RUN_OVERHEAD_MS = 300  # fork, import sqlglot and the test modules
RUN_MARGIN_MS = 200


def decide(chosen, ms, ids):
    """Split candidates into (run here, hand to workers); run=[] means no run."""
    runnable = [n for n in chosen if ms[n] <= RUN_MAX_MS and not ids[n].startswith(NO_RUN_FILES)
                and ids[n].count("::") == 2]
    if not runnable:
        return [], chosen
    rest = [n for n in chosen if n not in set(runnable)]
    blind = cost(pack(chosen, ms), ms)
    mods = len({ids[n].split("::")[0] for n in runnable})
    est = RUN_OVERHEAD_MS + 50 * mods + sum(ms[n] for n in runnable) + cost(pack(rest, ms), ms)
    if est + RUN_MARGIN_MS < blind:
        return runnable, rest
    return [], chosen


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def repo_files(root):
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__")]
        rel = os.path.relpath(d, root)
        for f in files:
            out.append(f if rel == "." else f"{rel}/{f}")
    return out


def main():
    t0 = time.process_time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    number = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    ids = {t["number"]: t["id"] for t in tests}
    cov = CoverageMap(MAP)
    local_to_number = [number.get(tid) for tid in cov.tests]
    unmapped = sorted(set(number.values()) - {n for n in local_to_number if n is not None})
    if unmapped:
        log(f"{len(unmapped)} tests not in the coverage map; they run whenever code changes")
    analyzer = Analyzer(cov, SQLGLOT, repo_files(SQLGLOT))
    log(f"loaded in {time.process_time() - t0:.2f}s cpu")

    for path in sorted((DATA / "commits").glob("*.patch")):
        cid = path.stem
        res = Result()
        files = {}
        try:
            for fd in patchlib.parse(path.read_text(errors="replace")):
                analyzer.analyze(fd, res)
                old = analyzer.text(fd.old_path) if fd.old_path else ""
                if fd.old_path and fd.old_path != fd.new_path:
                    files[fd.old_path] = None
                if fd.new_path:
                    files[fd.new_path] = patchlib.apply(old or "", fd)
        except Exception as e:  # never crash on a strange patch: run everything
            log(f"{cid}: analysis failed ({type(e).__name__}: {e}); running all tests")
            res.everything(cov, "error")
        chosen = {local_to_number[i] for i in res.tests if local_to_number[i] is not None}
        if res.tests and unmapped:
            chosen.update(unmapped)
        chosen = sorted(chosen)
        run, rest = decide(chosen, ms, ids) if RUN and not res.fallback else ([], chosen)
        if run:
            limits = {ids[n]: max(3.0, ms[n] / 100) for n in run}
            budget = 5 + sum(ms[n] for n in run) / 250
            failed = run_tests(SQLGLOT, TMP, files, [ids[n] for n in run], limits, budget)
            if failed is None:
                log(f"{cid}: run did not finish; handing all to workers")
                rest = chosen
            else:
                rest = sorted(set(rest) | {number[t] for t in failed if t in number})
                res.notes.insert(0, f"ran {len(run)} failed {len(failed)}")
            chosen = rest
        workers = pack(chosen, ms)
        log(f"{cid}: {len(chosen)} tests, {len(workers)} workers; {' '.join(res.notes)[:300]}")
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
    import resource

    ch = resource.getrusage(resource.RUSAGE_CHILDREN)
    log(f"total {time.process_time() - t0:.2f}s cpu (+{ch.ru_utime + ch.ru_stime:.2f}s in test runs)")


if __name__ == "__main__":
    main()
