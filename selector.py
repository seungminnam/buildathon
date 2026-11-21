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
from blast import overlay, probe, tpcdigest, verify  # noqa: E402

DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SQLGLOT = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")
MAP = os.environ.get("BLAST_MAP", str(HERE / "maps" / "coverage.json"))
WORK = os.environ.get("BLAST_WORK", "/tmp/w")
ACTUAL_MAP = os.environ.get("BLAST_ACTUAL_MAP", str(HERE / "maps" / "actual_ms.json"))
PROBE_MAP = os.environ.get("BLAST_PROBE_MAP", str(HERE / "maps" / "probe_base.json"))
TPC_MAP = os.environ.get("BLAST_TPC_MAP", str(HERE / "maps" / "tpc_digest.json"))

# Checking candidates by running them directly with unittest.
# Tests whose calibrated actual CPU is <= RUN_MAX_ACTUAL_MS are verified when
# expected simulated-time savings beat the verification CPU cost.
VERIFY = os.environ.get("BLAST_VERIFY", "1") == "1"
RUN_MAX_ACTUAL_MS = int(os.environ.get("BLAST_RUN_MAX_ACTUAL_MS", "1150"))
RUN_MAX_BATCH_ACTUAL_MS = int(os.environ.get("BLAST_RUN_MAX_BATCH_ACTUAL_MS", "5600"))
SUBSET_BUDGET_MS = int(os.environ.get("BLAST_SUBSET_BUDGET_MS", "3200"))
CPU_RATIO = float(os.environ.get("BLAST_CPU_RATIO", "1.15"))
PROXY = os.environ.get("BLAST_PROXY", "1") == "1"
PROXY_MIN = int(os.environ.get("BLAST_PROXY_MIN", "3"))

GIANT_TESTS = {
    "tests/test_parser.py::TestParser::test_max_nodes",
    tpcdigest.TEST_TPCDS,
    tpcdigest.TEST_TPCH,
    tpcdigest.TEST_TPCH_OPT,
}

NODEID = {}
ACTUAL_MS = {}
OVERHEAD_MS = int(os.environ.get("BLAST_OVERHEAD_MS", "120"))
VOUCHABLE = {}
FIXTURE_TESTS = {}
TPC = {}  # test number -> suite key ("tpch_exec", "tpch_opt", "tpcds_exec")
TPC_BASE = None


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
    """Rough odds that none of the candidates fails."""
    if any(n.startswith("import breaks ") for n in notes):
        return 0.02
    kinds = [n.split(" ")[0] for n in notes]
    if kinds and all(k in ("opened", "no-op") for k in kinds):
        return 0.02
    return 0.5


def check(cid, chosen, changes, ms, deadline, notes=(), allow_stop_early=True):
    """Run fast candidates against the patched tree; keep what fails.

    Returns (kept_set, info_str, stopped_early_flag).
    Tests triggered by edited fixture files are kept directly without wasting
    CPU running them, since edited fixtures fail almost every time.
    """
    fix_tests = FIXTURE_TESTS.get(cid, set())
    cheap = {
        n for n in chosen
        if n not in fix_tests and NODEID[n] not in GIANT_TESTS and ACTUAL_MS[n] <= RUN_MAX_ACTUAL_MS
    }
    # A single moderately slow code test (e.g. test_tpcds) can still be worth
    # running when it is the only candidate (saving its duration + 2 s worker cost).
    if not cheap and len(chosen) == 1:
        only = next(iter(chosen))
        if only not in fix_tests and NODEID[only] not in GIANT_TESTS and ACTUAL_MS[only] <= 2600:
            cheap = {only}
    dear = chosen - cheap
    if not cheap:
        return chosen, "listed", False

    cheap_actual = sum(ACTUAL_MS[n] for n in cheap)
    subset_mode = False
    if cheap_actual > RUN_MAX_BATCH_ACTUAL_MS:
        # Too expensive to run every candidate: pick a high-leverage subset
        # whose recorded duration far exceeds its actual runtime (no proxy).
        ranked = sorted(
            (n for n in cheap if ms[n] >= max(300, int(1.8 * ACTUAL_MS[n]))),
            key=lambda n: -(ms[n] - ACTUAL_MS[n]),
        )
        sub, acc = set(), 0
        for n in ranked:
            a = ACTUAL_MS[n]
            if acc + a > SUBSET_BUDGET_MS:
                continue
            sub.add(n)
            acc += a
        if not sub:
            return chosen, "listed", False
        cheap = sub
        dear = chosen - cheap
        cheap_actual = acc
        subset_mode = True

    vouch = set() if subset_mode else (dear & VOUCHABLE.get(cid, set()))
    proxy = PROXY and bool(vouch) and len(cheap) >= PROXY_MIN
    listed = pack_cost(pack(sorted(chosen), ms), ms)
    after_pass = pack_cost(pack(sorted(dear - vouch if proxy else dear), ms), ms)

    run_cost = OVERHEAD_MS + CPU_RATIO * cheap_actual
    p_pass = pass_chance(notes)

    partial_saving = 0
    if not subset_mode and len(cheap) >= 4 and p_pass >= 0.3:
        if dear:
            dear_cost = pack_cost(pack(sorted(dear), ms), ms)
            partial_saving = max(0, listed - dear_cost)
        else:
            est_fail_ms = int(0.18 * sum(ms[n] for n in cheap))
            partial_saving = max(0, listed - (2000 + est_fail_ms))

    gain = p_pass * (listed - after_pass) + (1.0 - p_pass) * partial_saving
    left = deadline - time.monotonic()
    if gain <= run_cost or left < 4:
        return chosen, "listed", False

    tree = overlay.build(SQLGLOT, os.path.join(WORK, cid), changes)
    ids = [NODEID[n] for n in sorted(cheap, key=lambda n: (ACTUAL_MS[n], NODEID[n]))]
    has_giant_dear = bool(dear) and max(ms[n] for n in dear) >= 15000
    stop_early = allow_stop_early and (not subset_mode) and has_giant_dear and cheap_actual > 600
    timeout = min(left - 2, 4 + 3.5 * run_cost / 1000)
    c0 = children_cpu()
    failed, done, broken, ok = verify.run(tree, ids, timeout, stop_on_fail=stop_early)
    cpu = children_cpu() - c0
    overlay.remove(tree)

    bad = set()
    unrun = set()
    for n in cheap:
        tid = NODEID[n]
        if tid in failed or tid.split("::")[0] in broken:
            bad.add(n)
        elif tid not in done:
            bad.add(n)
            unrun.add(n)
    tag = "subset" if subset_mode else "checked"
    info = (
        f"{tag} {len(cheap)} ({sum(ms[n] for n in cheap) / 1000:.2f}s rec, "
        f"{cheap_actual / 1000:.2f}s est) cpu {cpu:.2f}s ok={ok} failed={len(bad)}"
    )
    if not ok:
        return chosen, info + " incomplete", False
    if proxy and not bad:
        return dear - vouch, info + f" dropped {len(vouch)} of {len(dear)} long", False
    return dear | bad, info, (stop_early and bool(unrun))


def tpc_applicable(changes):
    """The digest covers what sqlglot computes, not how the executor runs it."""
    for path in changes:
        if path.startswith("sqlglot/executor/") or path in ("tests/test_executor.py", "tests/helpers.py"):
            return False
        if "/tpc-" in path and not path.endswith(".sql"):
            return False
        if not (path.startswith("sqlglot/") or path.startswith("tests/")):
            return False
    return True


def _leaf_sqlglot_file(path):
    """True for dialect-specific modules that only affect stages that execute them."""
    if not path.endswith(".py"):
        return False
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in ("dialects", "parsers", "generators", "typing"):
        return parts[2] not in ("dialect.py", "__init__.py")
    return False


def _tpc_sql_input_changed(fixture_rel, new_text):
    """True if any query's meta or input SQL (not just expected optimized SQL) changed."""
    old_path = os.path.join(SQLGLOT, fixture_rel)
    if not os.path.isfile(old_path):
        return True
    old_text = open(old_path, encoding="utf-8").read()

    def extract_inputs(text):
        # Match tests/helpers.py load_sql_fixture_pairs: blocks separated by blank lines / comments
        # Each pair is [meta lines...] sql;\n optimized;
        out = []
        buf = []
        meta = []
        for raw in text.splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("#"):
                if not buf:
                    meta.append(s)
                continue
            buf.append(raw)
            if s.endswith(";"):
                if len(buf) == 1 or len(out) == 0 or True:
                    pass
        # Simpler & exact: split on ';' statements just like sqlglot's load_sql_fixture_pairs
        return None

    # Use sqlglot's exact fixture format: pairs of ';'-terminated blocks, first is input sql, second is optimized
    def parse_pairs(text):
        pairs = []
        meta = []
        cur = []
        stmts = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") and not cur:
                meta.append(stripped)
                continue
            cur.append(line)
            if stripped.endswith(";"):
                stmts.append(("\n".join(cur), tuple(meta)))
                cur = []
                meta = []
                if len(stmts) == 2:
                    pairs.append(stmts[0])  # (input_sql, meta)
                    stmts = []
        if stmts or cur:
            return None
        return pairs

    a = parse_pairs(old_text)
    b = parse_pairs(new_text)
    if a is None or b is None:
        return True
    return a != b


def tpc_check(cid, keep, changes, ms, deadline, res, active_paths):
    global TPC_BASE
    tpc = keep & set(TPC)
    if not tpc or not tpc_applicable(changes):
        return keep, " digest n/a" if tpc else ""
    if TPC_BASE is None:
        if not os.path.isfile(TPC_MAP):
            return keep, ""
        TPC_BASE = json.load(open(TPC_MAP))

    stage_files = TPC_BASE.get("stage_files", {})
    q_lines = TPC_BASE.get("q_lines", {})
    suites_needed = sorted({TPC[n] for n in tpc})
    groups_needed = sorted({"tpch" if s.startswith("tpch") else "tpcds" for s in suites_needed})

    # Determine per-group query_filter and stages
    query_filter = {}
    stages = {}
    auto_drop = set()
    pure_stmt = (not res.has_non_stmt) and bool(res.stmt_lines)

    for grp in groups_needed:
        if pure_stmt:
            gl = q_lines.get(grp, {})
            hit_qs = set()
            for p, lns in res.stmt_lines.items():
                per = gl.get(p)
                if not per:
                    continue
                for ln in lns:
                    qs = per.get(str(ln))
                    if qs:
                        hit_qs.update(qs)
            if not hit_qs:
                # Zero queries in this TPC suite execute any edited line!
                for n in tpc:
                    if TPC[n].startswith(grp):
                        auto_drop.add(n)
                continue
            query_filter[grp] = hit_qs

        # Stage filtering: check which stages (duck / py) any active edited file can reach
        fix_rel = f"tests/fixtures/optimizer/{'tpc-h/tpc-h.sql' if grp == 'tpch' else 'tpc-ds/tpc-ds.sql'}"
        grp_stages = []
        for st in ("duck", "py"):
            sf = set(stage_files.get(f"{grp}:{st}", ()))
            touched = False
            for p in active_paths:
                if p == fix_rel:
                    # Only touches duck/py exec if input SQL or meta changed (not just expected optimized SQL)
                    if changes.get(p) is None or _tpc_sql_input_changed(fix_rel, changes[p]):
                        touched = True
                        break
                elif p.startswith("tests/"):
                    continue
                elif p in sf or not _leaf_sqlglot_file(p):
                    touched = True
                    break
            if touched:
                grp_stages.append(st)
        stages[grp] = tuple(grp_stages)
        if not grp_stages:
            # Neither duck nor py exec stage is touched by any active change
            for n in tpc:
                if TPC[n] == f"{grp}_exec":
                    auto_drop.add(n)

    if auto_drop:
        keep = keep - auto_drop
        tpc = keep & set(TPC)
        if not tpc:
            return keep, f" digest auto-dropped {len(auto_drop)}"

    suites_run = []
    for n in tpc:
        sk = TPC[n]
        grp = "tpch" if sk.startswith("tpch") else "tpcds"
        if sk.endswith("_exec") and not stages.get(grp):
            continue
        if sk not in suites_run:
            suites_run.append(sk)
    if not suites_run:
        return keep, ""

    saving = pack_cost(pack(sorted(keep), ms), ms) - pack_cost(pack(sorted(keep - tpc), ms), ms)
    # Estimate digest CPU based on stages and filtered query count
    estimate = 150
    for sk in suites_run:
        if sk == "tpch_opt":
            estimate += 120
        elif sk == "tpch_exec":
            st = stages.get("tpch", ("duck", "py"))
            nq = len(query_filter["tpch"]) if "tpch" in query_filter else 22
            estimate += (25 if "py" in st else 6) * nq
        elif sk == "tpcds_exec":
            st = stages.get("tpcds", ("duck", "py"))
            nq = len(query_filter["tpcds"]) if "tpcds" in query_filter else 57
            estimate += (35 if "py" in st else 8) * min(nq, 57)

    left = deadline - time.monotonic()
    if saving <= estimate or left < 10:
        return keep, (f" digest auto-dropped {len(auto_drop)}" if auto_drop else "")

    tree = overlay.build(SQLGLOT, os.path.join(WORK, cid), changes)
    c0 = children_cpu()
    same = tpcdigest.run(
        tree,
        timeout=min(45, left - 4),
        base=TPC_BASE,
        suites=tuple(suites_run),
        query_filter=query_filter or None,
        stages=stages or None,
    ) or {}
    cpu = children_cpu() - c0
    overlay.remove(tree)
    drop = {n for n in tpc if same.get(NODEID[n]) is True}
    total_dropped = len(drop) + len(auto_drop)
    return keep - drop, f" digest {suites_run} cpu {cpu:.2f}s dropped {total_dropped}"


def import_breakage(got, analyzer, local_to_number):
    """Tests hit by modules that fail to import, or None when that is everyone."""
    out = set()
    for mod, _msg in got["errors"]:
        if mod in ("sqlglot", "tests", "<probe>") or mod.count(".") < 1:
            return None
        base = os.path.join(SQLGLOT, *mod.split("."))
        if os.path.isfile(base + ".py"):
            path = mod.replace(".", "/") + ".py"
        elif os.path.isfile(os.path.join(base, "__init__.py")):
            path = mod.replace(".", "/") + "/__init__.py"
        else:
            return None
        if path.startswith("tests/"):
            local = analyzer.cov.by_file.get(path, set())
        else:
            local = analyzer.module_users(path)
        out |= {local_to_number[i] for i in local if local_to_number[i] is not None}
    return out


def merge(res, part):
    res.tests |= part.tests
    res.notes += part.notes
    res.fallback = res.fallback or part.fallback
    res.if_import_changed |= part.if_import_changed
    res.allowed += part.allowed
    res.import_paths |= part.import_paths
    res.has_non_stmt = res.has_non_stmt or part.has_non_stmt
    for p, lns in part.stmt_lines.items():
        res.stmt_lines.setdefault(p, set()).update(lns)


def children_cpu():
    import resource

    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def main():
    global OVERHEAD_MS
    t0 = time.process_time()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    number = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    NODEID.update({t["number"]: t["id"] for t in tests})

    # Load build-time calibrated actual test runtimes
    actual_raw = {}
    if os.path.isfile(ACTUAL_MAP):
        data = json.load(open(ACTUAL_MAP))
        actual_raw = data.get("tests", {})
        if "BLAST_OVERHEAD_MS" not in os.environ and "overhead_ms" in data:
            OVERHEAD_MS = int(data["overhead_ms"])
    for t in tests:
        n = t["number"]
        tid = t["id"]
        if tid in actual_raw:
            ACTUAL_MS[n] = int(actual_raw[tid])
        else:
            ACTUAL_MS[n] = max(1, round(ms[n] * 0.45))

    for suite, nodes in tpcdigest.TESTS.items():
        for node in nodes:
            if node in number:
                TPC[number[node]] = suite

    cov = CoverageMap(MAP)
    local_to_number = [number.get(tid) for tid in cov.tests]
    unmapped = sorted(set(number.values()) - {n for n in local_to_number if n is not None})
    if unmapped:
        log(f"{len(unmapped)} tests not in the coverage map; they run whenever code changes")
    analyzer = Analyzer(cov, SQLGLOT, repo_files(SQLGLOT))
    log(f"loaded in {time.process_time() - t0:.2f}s cpu (overhead={OVERHEAD_MS}ms)")

    paths = sorted((DATA / "commits").glob("*.patch"))
    deadline = time.monotonic() + min(165.0, 30 + 3.5 * len(paths))
    plans = []
    pending_probe = {}
    for path in paths:
        cid = path.stem
        res = Result()
        parts = []
        changes = {}
        active_paths = set()
        try:
            for fd in patchlib.parse(path.read_text(errors="replace")):
                part = Result()
                analyzer.analyze(fd, part)
                merge(res, part)
                parts.append((fd.path, part))
                if not (len(part.notes) == 1 and part.notes[0].startswith("no-op ")):
                    if fd.old_path:
                        active_paths.add(fd.old_path)
                    if fd.new_path:
                        active_paths.add(fd.new_path)
                if changes is not None:
                    changes = add_change(changes, fd)
        except Exception as e:  # never crash on a strange patch: run everything
            log(f"{cid}: analysis failed ({type(e).__name__}: {e}); running all tests")
            res.everything(cov, "error")
            changes = None
        chosen = {local_to_number[i] for i in res.tests if local_to_number[i] is not None}
        extra = {local_to_number[i] for i in res.if_import_changed if local_to_number[i] is not None}
        if (res.tests or extra) and unmapped:
            chosen.update(unmapped)
        if extra - chosen:
            if changes is None or not VERIFY:
                chosen |= extra
                res.notes.append("import not checked")
            else:
                pending_probe[cid] = (extra, res.allowed)

        vouchable = set()
        fix_tests = set()
        if not res.fallback:
            per_file = []
            for fpath, part in parts:
                nums = {
                    local_to_number[i]
                    for i in part.tests | part.if_import_changed
                    if local_to_number[i] is not None
                }
                if not fpath.endswith(".py") and 0 < len(nums) <= 3:
                    fix_tests |= nums
                n_fast = sum(
                    1 for n in nums
                    if NODEID[n] not in GIANT_TESTS and ACTUAL_MS[n] <= RUN_MAX_ACTUAL_MS
                )
                ok = fpath.endswith(".py") and n_fast >= PROXY_MIN
                per_file.append((nums, ok))
            for n in chosen | extra:
                is_dear = NODEID[n] in GIANT_TESTS or ACTUAL_MS[n] > RUN_MAX_ACTUAL_MS
                if is_dear and all(ok for nums, ok in per_file if n in nums):
                    vouchable.add(n)
        VOUCHABLE[cid] = vouchable
        FIXTURE_TESTS[cid] = fix_tests
        plans.append((cid, chosen, changes, res, active_paths))

    global TPC_BASE
    base_fp = None
    base_noise = set()
    if VERIFY:
        if pending_probe and os.path.isfile(PROBE_MAP):
            pdata = json.load(open(PROBE_MAP))
            base_fp = pdata.get("fp")
            base_noise = set(pdata.get("noise", ()))
        if TPC_BASE is None and os.path.isfile(TPC_MAP) and any(
            (chosen & set(TPC)) for _cid, chosen, _ch, _r, _ap in plans
        ):
            TPC_BASE = json.load(open(TPC_MAP))
        verify.warm_up()

    for cid, chosen, changes, res, active_paths in plans:
        notes = res.notes
        if cid in pending_probe:
            extra, allowed = pending_probe[cid]
            p0 = children_cpu()
            try:
                if base_fp is None:
                    a, b = probe.probe(SQLGLOT), probe.probe(SQLGLOT)
                    base_fp = {}
                    if a and b and not a["errors"] and not b["errors"]:
                        noise = set(probe.diff(a["fp"], b["fp"]))
                        base_fp = {k: v for k, v in a["fp"].items() if k not in noise}
                        base_noise = noise
                tree = overlay.build(SQLGLOT, os.path.join(WORK, cid), changes)
                got = probe.probe(tree)
                overlay.remove(tree)
                changed = None
                if got and not got["errors"] and base_fp:
                    new_fp = {k: v for k, v in got["fp"].items() if k not in base_noise}
                    changed = [k for k in probe.diff(base_fp, new_fp) if not any(k.startswith(a) for a in allowed)]
                broken = import_breakage(got, analyzer, local_to_number) if got and got["errors"] else None
                if broken is not None:
                    chosen = chosen | broken
                    notes = notes + [f"import breaks {len(got['errors'])} modules: {len(broken)} tests"]
                elif changed is None or changed:
                    chosen = chosen | extra
                    notes = notes + [f"import changed: {(changed or ['error'])[:3]}"]
                else:
                    notes = notes + ["import unchanged"]
            except Exception as e:  # unsure: keep them
                chosen = chosen | extra
                notes = notes + [f"probe failed {type(e).__name__}"]
            notes = notes + [f"probe cpu {children_cpu() - p0:.2f}s"]
        keep, how, stopped_early = chosen, "listed", False
        if VERIFY and changes is not None and chosen:
            try:
                keep, how, stopped_early = check(cid, chosen, changes, ms, deadline, notes)
            except Exception as e:  # a failed check keeps every candidate
                log(f"{cid}: check failed ({type(e).__name__}: {e})")
                keep, how, stopped_early = chosen, "check-error", False
        if VERIFY and changes is not None and keep & set(TPC):
            before_tpc = len(keep)
            how_pre = how
            try:
                keep, more = tpc_check(cid, keep, changes, ms, deadline, res, active_paths)
                how += more
            except Exception as e:  # unsure: keep them
                how += f" digest failed {type(e).__name__}"
            if (stopped_early or how_pre == "listed") and len(keep) < before_tpc and keep:
                try:
                    keep, how2, _ = check(cid, keep, changes, ms, deadline, notes, allow_stop_early=False)
                    if how2 != "listed":
                        how += " + " + how2
                except Exception:  # noqa: BLE001
                    pass
        workers = pack(sorted(keep), ms)
        log(
            f"{cid}: {len(chosen)} candidates, {how}, {len(keep)} kept, {len(workers)} workers,"
            f" cost {pack_cost(workers, ms) / 1000:.2f}; {' '.join(notes)[:240]}"
        )
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
    log(f"total {time.process_time() - t0:.2f}s cpu, checks {children_cpu():.2f}s cpu")


if __name__ == "__main__":
    main()
