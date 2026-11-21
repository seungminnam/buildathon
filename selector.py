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
from blast.pack import cost as pack_cost  # noqa: E402
from blast.pack import pack  # noqa: E402
from blast import overlay, verify  # noqa: E402

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SQLGLOT = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")
MAP = os.environ.get("BLAST_MAP", str(HERE / "maps" / "coverage.json"))
WORK = os.environ.get("BLAST_WORK", "/tmp/w")

# Checking a candidate by running it. Tests longer than RUN_MAX_MS are never
# run here: listing one costs about what running it costs. A check is done
# only when its expected CPU plus what is still listed is less than listing
# every candidate. OVERHEAD_MS is what one check costs before any test runs.
VERIFY = os.environ.get("BLAST_VERIFY", "1") == "1"
RUN_MAX_MS = int(os.environ.get("BLAST_RUN_MAX_MS", "1500"))
OVERHEAD_MS = int(os.environ.get("BLAST_OVERHEAD_MS", "700"))
CPU_RATIO = float(os.environ.get("BLAST_CPU_RATIO", "1.3"))
PROXY = os.environ.get("BLAST_PROXY", "1") == "1"
PROXY_MIN = int(os.environ.get("BLAST_PROXY_MIN", "3"))


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


def add_change(changes, fd):
    """Record the new text of one file of a patch; None when it does not apply."""
    if fd.binary:
        return None
    old_path, new_path = fd.old_path, fd.new_path
    old_text = ""
    if old_path:
        full = os.path.join(SQLGLOT, old_path)
        if not os.path.isfile(full):
            return None
        with open(full, encoding="utf-8", newline="") as f:
            old_text = f.read()
    if old_path and old_path != new_path:
        changes[old_path] = None
    if new_path:
        new_text = patchlib.apply(old_text, fd)
        if new_text is None:
            return None
        changes[new_path] = new_text
    return changes


def pass_chance(notes):
    """Rough odds that none of the candidates fails.

    An edited fixture almost always changes an expected output, so its
    reader fails; checking it only burns CPU. A code edit that the coverage
    map reaches breaks something about half the time.
    """
    kinds = [n.split(" ")[0] for n in notes]
    if kinds and all(k in ("opened", "no-op") for k in kinds):
        return 0.05
    return 0.5


def check(cid, chosen, changes, ms, deadline, notes=()):
    """Run the cheap candidates against the patched tree; keep what fails.

    With PROXY on, the cheap candidates also speak for the expensive ones:
    they ran the same changed code, so when none of them fails we drop the
    expensive tests as well. When any cheap one fails, every expensive
    candidate stays.
    """
    cheap = {n for n in chosen if ms[n] <= RUN_MAX_MS}
    dear = chosen - cheap
    if not cheap:
        return chosen, "listed"
    proxy = PROXY and bool(dear) and len(cheap) >= PROXY_MIN
    listed = pack_cost(pack(sorted(chosen), ms), ms)
    after = 0 if proxy else pack_cost(pack(sorted(dear), ms), ms)
    run_cost = OVERHEAD_MS + CPU_RATIO * sum(ms[n] for n in cheap)
    gain = pass_chance(notes) * (listed - after)
    left = deadline - time.monotonic()
    if gain <= run_cost or left < 5:
        return chosen, "listed"
    tree = overlay.build(SQLGLOT, os.path.join(WORK, cid), changes)
    ids = sorted(NODEID[n] for n in cheap)
    # a run far past its expected CPU is a hang or a slow failure; stop it
    # and list everything rather than pay for it
    timeout = min(left - 2, 5 + 3 * run_cost / 1000)
    c0 = children_cpu()
    failed, done, broken, ok = verify.run(tree, ids, timeout, stop_on_fail=bool(dear))
    cpu = children_cpu() - c0
    overlay.remove(tree)
    bad = set()
    for n in cheap:
        tid = NODEID[n]
        if tid in failed or tid not in done or tid.split("::")[0] in broken:
            bad.add(n)
    info = f"checked {len(cheap)} ({sum(ms[n] for n in cheap) / 1000:.2f}s rec) cpu {cpu:.2f}s ok={ok} failed={len(bad)}"
    if not ok:
        return chosen, info + " incomplete"
    if proxy and not bad:
        return set(), info + f" dropped {len(dear)} long"
    return dear | bad, info


NODEID = {}


def children_cpu():
    import resource

    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def main():
    t0 = time.process_time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    number = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    NODEID.update({t["number"]: t["id"] for t in tests})
    cov = CoverageMap(MAP)
    local_to_number = [number.get(tid) for tid in cov.tests]
    unmapped = sorted(set(number.values()) - {n for n in local_to_number if n is not None})
    if unmapped:
        log(f"{len(unmapped)} tests not in the coverage map; they run whenever code changes")
    analyzer = Analyzer(cov, SQLGLOT, repo_files(SQLGLOT))
    log(f"loaded in {time.process_time() - t0:.2f}s cpu")

    paths = sorted((DATA / "commits").glob("*.patch"))
    deadline = time.monotonic() + min(160.0, 3.5 * max(len(paths), 1))
    plans = []
    for path in paths:
        cid = path.stem
        res = Result()
        changes = {}
        try:
            for fd in patchlib.parse(path.read_text(errors="replace")):
                analyzer.analyze(fd, res)
                if changes is not None:
                    changes = add_change(changes, fd)
        except Exception as e:  # never crash on a strange patch: run everything
            log(f"{cid}: analysis failed ({type(e).__name__}: {e}); running all tests")
            res.everything(cov, "error")
            changes = None
        chosen = {local_to_number[i] for i in res.tests if local_to_number[i] is not None}
        if res.tests and unmapped:
            chosen.update(unmapped)
        plans.append((cid, chosen, changes, res.notes))

    if VERIFY:
        verify.warm_up()
    for cid, chosen, changes, notes in plans:
        keep, how = chosen, "listed"
        if VERIFY and changes is not None and chosen:
            try:
                keep, how = check(cid, chosen, changes, ms, deadline, notes)
            except Exception as e:  # a failed check keeps every candidate
                log(f"{cid}: check failed ({type(e).__name__}: {e})")
                keep, how = chosen, "check-error"
        workers = pack(sorted(keep), ms)
        log(
            f"{cid}: {len(chosen)} candidates, {how}, {len(keep)} kept, {len(workers)} workers,"
            f" cost {pack_cost(workers, ms) / 1000:.2f}; {' '.join(notes)[:240]}"
        )
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
    log(f"total {time.process_time() - t0:.2f}s cpu, checks {children_cpu():.2f}s cpu")


if __name__ == "__main__":
    main()
