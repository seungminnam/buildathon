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
from blast.impact import Analyzer, Result, tpc_sql_changed_indices  # noqa: E402
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
SUBCOV_MAP = os.environ.get("BLAST_SUBCOV_MAP", str(HERE / "maps" / "subcov.json"))

# Checking candidates by running them directly with unittest.
# Tests whose calibrated actual CPU is <= RUN_MAX_ACTUAL_MS are verified when
# expected simulated-time savings beat the verification CPU cost.
VERIFY = os.environ.get("BLAST_VERIFY", "1") == "1"
RUN_MAX_ACTUAL_MS = int(os.environ.get("BLAST_RUN_MAX_ACTUAL_MS", "1500"))
RUN_MAX_BATCH_ACTUAL_MS = int(os.environ.get("BLAST_RUN_MAX_BATCH_ACTUAL_MS", "5600"))
SUBSET_BUDGET_MS = int(os.environ.get("BLAST_SUBSET_BUDGET_MS", "3200"))
CPU_RATIO = float(os.environ.get("BLAST_CPU_RATIO", "1.15"))
SKIP_GATE = os.environ.get("BLAST_SKIP_GATE", "1") == "1"
SKIP_P_FAIL = float(os.environ.get("BLAST_SKIP_P_FAIL", "0.60"))
SKIP_P_CLEAN = float(os.environ.get("BLAST_SKIP_P_CLEAN", "0.85"))
PROXY = os.environ.get("BLAST_PROXY", "1") == "1"
PROXY_MIN = int(os.environ.get("BLAST_PROXY_MIN", "3"))

TEST_MAX_NODES = "tests/test_parser.py::TestParser::test_max_nodes"
GIANT_TESTS = {
    TEST_MAX_NODES,
    tpcdigest.TEST_TPCDS,
    tpcdigest.TEST_TPCH,
    tpcdigest.TEST_TPCH_OPT,
}

NODEID = {}
ACTUAL_MS = {}
OVERHEAD_MS = int(os.environ.get("BLAST_OVERHEAD_MS", "120"))
VOUCHABLE = {}
FIXTURE_TESTS = {}
NO_SUBCOV = {}
TPC = {}  # test number -> suite key ("tpch_exec", "tpch_opt", "tpcds_exec")
TPC_BASE = None
SUBCOV = None
SUBCOV_SETUP = {}  # cls_key -> (lines_dict_of_sets, classes_set)


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


def max_nodes_fast_ok(changes, active_paths, patch_text, edited_lines, res):
    """True when scaled _fast_max_nodes is sound for test_max_nodes."""
    if changes is None or res.fallback:
        return False
    if "tests/test_parser.py" in active_paths:
        return False
    low = patch_text.lower()
    if "max_nodes" in low or "node_count" in low:
        return False
    for ln in edited_lines.get("sqlglot/parser.py", ()):
        if (375 <= ln <= 410) or (2030 <= ln <= 2090) or (2190 <= ln <= 2220):
            return False
    for ln in edited_lines.get("sqlglot/parsers/athena.py", ()):
        if 30 <= ln <= 75:
            return False
    return True


def subcov_filters(cid, chosen, res, active_paths):
    """Compute per-test fixture/dialect item filters and scaled actual runtimes."""
    if SUBCOV is None or res.fallback or res.has_non_class:
        return None, {}
    if not (res.stmt_lines or res.class_quals or res.class_line_pairs or res.class_class_pairs):
        return None, {}
    if any(p.startswith("tests/") and p.endswith(".py") for p in active_paths):
        return None, {}
    # Dialect._try_load and Generator._build_dispatch populate module-level lazy caches;
    # do not sub-filter items when those lines themselves were edited.
    if any(230 <= ln <= 310 for ln in res.stmt_lines.get("sqlglot/dialects/dialect.py", ())):
        return None, {}
    if any(
        (70 <= ln <= 92) or (1003 <= ln <= 1011)
        for ln in res.stmt_lines.get("sqlglot/generator.py", ())
    ):
        return None, {}

    skip_tests = NO_SUBCOV.get(cid, set())
    sets = SUBCOV["sets"]
    tests_map = SUBCOV["tests"]
    setup_touched = {}
    item_filters = {}
    eff_actual = {}

    all_quals = set(res.class_quals)
    for cq, _ in res.class_line_pairs:
        all_quals.add(cq)
    for cq, eqs in res.class_class_pairs:
        all_quals.add(cq)
        all_quals |= eqs

    for n in chosen:
        if n in skip_tests:
            continue
        tid = NODEID[n]
        tinfo = tests_map.get(tid)
        if tinfo is None:
            continue
        cls_key = tinfo["cls"]
        touched = setup_touched.get(cls_key)
        if touched is None:
            s_lines, s_classes = SUBCOV_SETUP.get(cls_key, ({}, set()))
            touched = bool(all_quals & s_classes) or any(
                bool(lns & s_lines.get(p, set())) for p, lns in res.stmt_lines.items()
            )
            setup_touched[cls_key] = touched
        if touched:
            continue
        pre_lines = tinfo.get("pre_lines")
        pre_classes = tinfo.get("pre_classes")
        if pre_lines or pre_classes:
            if (pre_classes and (all_quals & set(pre_classes))) or (
                pre_lines
                and any(bool(lns & set(pre_lines.get(p, ()))) for p, lns in res.stmt_lines.items())
            ):
                continue

        flt = {}
        tot_n = 0
        tot_hit = 0
        for fname, fdata in tinfo["fixtures"].items():
            hit = set()
            flines = fdata["lines"]
            fclasses = fdata["classes"]
            for p, lns in res.stmt_lines.items():
                pmap = flines.get(p)
                if not pmap:
                    continue
                for ln in lns:
                    sid = pmap.get(str(ln))
                    if sid is not None:
                        hit.update(sets[sid])
            for q in res.class_quals:
                sid = fclasses.get(q)
                if sid is not None:
                    hit.update(sets[sid])
            for cq, ldict in res.class_line_pairs:
                csid = fclasses.get(cq)
                if csid is None:
                    continue
                c_items = set(sets[csid])
                l_items = set()
                for p, lns in ldict.items():
                    pmap = flines.get(p)
                    if not pmap:
                        continue
                    for ln in lns:
                        sid = pmap.get(str(ln))
                        if sid is not None:
                            l_items.update(sets[sid])
                hit.update(c_items & l_items)
            for cq, eqs in res.class_class_pairs:
                csid = fclasses.get(cq)
                if csid is None:
                    continue
                c_items = set(sets[csid])
                e_items = set()
                for eq in eqs:
                    esid = fclasses.get(eq)
                    if esid is not None:
                        e_items.update(sets[esid])
                hit.update(c_items & e_items)
            if fname == "__vcalls__":
                skippable = fdata["skippable"]
                skip_lines = set(skippable) - hit
                if skip_lines:
                    flt["__vcalls__"] = skip_lines
                unsafe_n = max(0, fdata["n"] - len(skippable))
                tot_n += fdata["n"]
                tot_hit += (len(skippable) - len(skip_lines)) + unsafe_n
            elif "w" in fdata:
                w = fdata["w"]
                flt[fname] = hit
                tot_n += sum(w)
                tot_hit += sum(w[i] for i in hit if i < len(w))
            else:
                flt[fname] = hit
                tot_n += fdata["n"]
                tot_hit += len(hit)

        if flt and tot_n > 0 and tot_hit < tot_n:
            item_filters[tid] = flt
            if tot_hit == 0:
                floor = 3
            elif "__vcalls__" in flt:
                floor = 1
            elif "__subtests__" in flt or "__blocks__" in flt:
                floor = 3
            elif "__dialects__" in flt:
                floor = 5
            else:
                floor = 25
            eff_actual[n] = max(floor, round(ACTUAL_MS[n] * (tot_hit / tot_n)))

    return (item_filters or None), eff_actual


def check(
    cid,
    chosen,
    changes,
    ms,
    deadline,
    notes=(),
    allow_stop_early=True,
    res=None,
    active_paths=(),
    p_pass_override=None,
    get_tree=None,
    fast_max_nodes=False,
):
    """Run fast candidates against the patched tree; keep what fails.

    Returns (kept_set, info_str, stopped_early_flag).
    Tests triggered by edited fixture files are kept directly without wasting
    CPU running them, since edited fixtures fail almost every time.
    """
    item_filters, eff_actual = (
        subcov_filters(cid, chosen, res, active_paths) if res is not None else (None, {})
    )
    fast_giants = set()
    if fast_max_nodes:
        for n in chosen:
            if NODEID[n] == TEST_MAX_NODES:
                fast_giants.add(n)
                eff_actual[n] = 20
                if item_filters is None:
                    item_filters = {}
                item_filters[TEST_MAX_NODES] = {"__fast_max_nodes__": True}
                break

    def est_ms(n):
        return eff_actual.get(n, ACTUAL_MS[n])

    fix_tests = FIXTURE_TESTS.get(cid, set())
    cheap = {
        n for n in chosen
        if n not in fix_tests
        and (NODEID[n] not in GIANT_TESTS or n in fast_giants)
        and est_ms(n) <= min(3200, max(RUN_MAX_ACTUAL_MS, int(0.80 * ms[n])))
    }
    # A single moderately slow code test (e.g. test_optimizer::test_tpcds) can
    # still be worth running when it is the only candidate (saving its duration + 2 s worker).
    if not cheap and len(chosen) == 1:
        only = next(iter(chosen))
        if (
            only not in fix_tests
            and (NODEID[only] not in GIANT_TESTS or only in fast_giants)
            and est_ms(only) <= min(4200, int(0.85 * (ms[only] + 2000)))
        ):
            cheap = {only}
    dear = chosen - cheap
    if not cheap:
        return chosen, "listed", False

    listed = pack_cost(pack(sorted(chosen), ms), ms)
    cheap_actual = sum(est_ms(n) for n in cheap)
    batch_cap = max(RUN_MAX_BATCH_ACTUAL_MS, min(11000, int(0.75 * listed)))
    subset_mode = False
    if cheap_actual > batch_cap:
        # Too expensive to run every candidate: pick a high-leverage subset
        # whose recorded duration exceeds its actual runtime (no proxy).
        sub_budget = min(5000, max(SUBSET_BUDGET_MS, int(0.30 * listed)))
        ranked = sorted(
            (n for n in cheap if ms[n] >= max(250, int(1.25 * est_ms(n)))),
            key=lambda n: -(ms[n] - est_ms(n)),
        )
        sub, acc = set(), 0
        for n in ranked:
            a = est_ms(n)
            if acc + a > sub_budget:
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
    if vouch and tpc_applicable(changes):
        vouch = vouch - set(TPC)
    proxy = PROXY and bool(vouch) and len(cheap) >= PROXY_MIN
    after_pass = pack_cost(pack(sorted(dear - vouch if proxy else dear), ms), ms)

    run_cost = OVERHEAD_MS + CPU_RATIO * cheap_actual
    p_pass = p_pass_override if p_pass_override is not None else pass_chance(notes)

    partial_saving = 0
    if p_pass >= 0.3:
        if subset_mode:
            partial_saving = int(0.5 * max(0, listed - after_pass))
        elif len(cheap) >= 4:
            if dear:
                dear_cost = pack_cost(pack(sorted(dear), ms), ms)
                partial_saving = max(0, listed - dear_cost)
            else:
                est_fail_ms = int(0.18 * sum(ms[n] for n in cheap))
                partial_saving = max(0, listed - (2000 + est_fail_ms))

    gain = p_pass * (listed - after_pass) + (1.0 - p_pass) * partial_saving
    left = deadline - time.monotonic()
    if gain <= run_cost or left < 4:
        if left >= 4 and fast_giants and fast_giants < cheap:
            fg_actual = sum(est_ms(n) for n in fast_giants)
            fg_cost = OVERHEAD_MS + CPU_RATIO * fg_actual
            fg_saving = listed - pack_cost(pack(sorted(chosen - fast_giants), ms), ms)
            if p_pass * fg_saving > fg_cost:
                cheap = set(fast_giants)
                dear = chosen - cheap
                cheap_actual = fg_actual
                run_cost = fg_cost
                subset_mode = True
                proxy = False
                vouch = set()
            else:
                return chosen, "listed", False
        else:
            return chosen, "listed", False

    own_tree = get_tree is None
    tree = overlay.build(SQLGLOT, os.path.join(WORK, cid), changes) if own_tree else get_tree()
    ids = [NODEID[n] for n in sorted(cheap, key=lambda n: (est_ms(n), NODEID[n]))]
    has_giant_dear = bool(dear) and max(ms[n] for n in dear) >= 15000
    stop_early = allow_stop_early and (not subset_mode) and has_giant_dear and cheap_actual > 600
    timeout = min(left - 2, 4 + 3.5 * run_cost / 1000)
    budget_ctx = None
    if SKIP_GATE and not proxy and not stop_early:
        budget_ctx = {
            "ms": {NODEID[n]: ms[n] for n in cheap},
            "est": {NODEID[n]: est_ms(n) for n in cheap},
            "dear": [ms[n] for n in dear],
            "cpu_ratio": CPU_RATIO,
            "p_fail": SKIP_P_FAIL,
        }
    c0 = children_cpu()
    try:
        failed, done, broken, ok = verify.run(
            tree,
            ids,
            timeout,
            stop_on_fail=stop_early,
            item_filters=item_filters,
            budget_ctx=budget_ctx,
        )
    finally:
        if own_tree:
            overlay.remove(tree)
    cpu = children_cpu() - c0

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
    if not bad and len(dear) == 1 and p_pass_override is None:
        only = next(iter(dear))
        if (
            only not in fix_tests
            and (NODEID[only] not in GIANT_TESTS or only in fast_giants)
            and est_ms(only) <= min(4200, int(0.85 * (ms[only] + 2000)))
        ):
            keep2, info2, _ = check(
                cid,
                dear,
                changes,
                ms,
                deadline,
                notes,
                allow_stop_early=False,
                res=res,
                active_paths=active_paths,
                p_pass_override=0.80,
                get_tree=get_tree,
                fast_max_nodes=fast_max_nodes,
            )
            if info2 != "listed":
                return keep2, info + " + " + info2, False
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


def _tpc_fixture_diff(fixture_rel, changes):
    """Return (exec_changed, opt_changed) sets for an edited TPC fixture, or (None, None)."""
    new_text = changes.get(fixture_rel)
    if new_text is None:
        return None, None
    old_path = os.path.join(SQLGLOT, fixture_rel)
    if not os.path.isfile(old_path):
        return None, None
    old_text = open(old_path, encoding="utf-8").read()
    return tpc_sql_changed_indices(old_text, new_text)


def tpc_check(
    cid, keep, changes, ms, deadline, res, active_paths, get_tree=None, max_estimate=None, checked_suites=None
):
    global TPC_BASE
    tpc = keep & set(TPC)
    if checked_suites:
        tpc = {n for n in tpc if TPC[n] not in checked_suites}
    if not tpc:
        return keep, ""
    if not tpc_applicable(changes):
        return keep, ("" if max_estimate is not None else " digest n/a")
    if TPC_BASE is None:
        if not os.path.isfile(TPC_MAP):
            return keep, ""
        TPC_BASE = json.load(open(TPC_MAP))

    stage_files = TPC_BASE.get("stage_files", {})
    q_lines = TPC_BASE.get("q_lines", {})
    py_core_lines = TPC_BASE.get("py_core_lines", {})
    suites_needed = sorted({TPC[n] for n in tpc})
    groups_needed = sorted({"tpch" if s.startswith("tpch") else "tpcds" for s in suites_needed})

    # Determine per-group query_filter and stages
    query_filter = {}
    stages = {}
    auto_drop = set()
    pure_stmt = (not res.has_non_stmt) and bool(res.stmt_lines)

    for grp in groups_needed:
        fix_rel = f"tests/fixtures/optimizer/{'tpc-h/tpc-h.sql' if grp == 'tpch' else 'tpc-ds/tpc-ds.sql'}"
        fix_diff = _tpc_fixture_diff(fix_rel, changes) if fix_rel in active_paths else None

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
        elif fix_diff is not None and all(
            p == fix_rel
            or p.startswith("tests/")
            or (
                _leaf_sqlglot_file(p)
                and p not in stage_files.get(f"{grp}:duck", ())
                and p not in stage_files.get(f"{grp}:py", ())
            )
            for p in active_paths
        ):
            exec_ch, opt_ch = fix_diff
            need_exec = any(TPC[n] == f"{grp}_exec" for n in tpc)
            need_opt = grp == "tpch" and any(TPC[n] == "tpch_opt" for n in tpc)
            if need_exec and exec_ch is not None and not exec_ch:
                for n in tpc:
                    if TPC[n] == f"{grp}_exec":
                        auto_drop.add(n)
                need_exec = False
            if need_opt and opt_ch is not None and not opt_ch:
                for n in tpc:
                    if TPC[n] == "tpch_opt":
                        auto_drop.add(n)
                need_opt = False
            if not need_exec and not need_opt:
                continue
            qset = set()
            can_filter = True
            if need_exec:
                if exec_ch is None:
                    can_filter = False
                else:
                    qset |= exec_ch
            if need_opt:
                if opt_ch is None:
                    can_filter = False
                else:
                    qset |= opt_ch
            if can_filter:
                query_filter[grp] = qset

        # Stage filtering: check which stages (duck / py / dbg_sql) any active edited file can reach
        grp_stages = []
        for st in ("duck", "py"):
            sf = set(stage_files.get(f"{grp}:{st}", ()))
            touched = False
            for p in active_paths:
                if p == fix_rel:
                    # Only touches duck/py exec if input SQL or non-title meta changed
                    if fix_diff is None or fix_diff[0] is None or bool(fix_diff[0]):
                        touched = True
                        break
                elif p.startswith("tests/"):
                    continue
                elif p in sf or not _leaf_sqlglot_file(p):
                    touched = True
                    break
            if touched:
                if st == "py" and pure_stmt and grp in py_core_lines and fix_rel not in active_paths:
                    core_map = py_core_lines[grp]
                    hits_core = any(
                        bool(lns & set(core_map.get(p, ())))
                        for p, lns in res.stmt_lines.items()
                    )
                    if not hits_core:
                        grp_stages.append("dbg_sql")
                        continue
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
        return keep, (f" digest auto-dropped {len(auto_drop)}" if auto_drop else "")

    # Estimate digest CPU based on stages and filtered query count
    estimate = 150
    for sk in suites_run:
        if sk == "tpch_opt":
            estimate += 120
        elif sk == "tpch_exec":
            st = stages.get("tpch", ("duck", "py"))
            nq = len(query_filter["tpch"]) if "tpch" in query_filter else 22
            if "real" in st:
                estimate += 120 * nq
            elif "py" in st:
                estimate += 25 * nq
            else:
                estimate += 6 * nq
        elif sk == "tpcds_exec":
            st = stages.get("tpcds", ("duck", "py"))
            nq = len(query_filter["tpcds"]) if "tpcds" in query_filter else 57
            if "real" in st:
                estimate += 250 * min(nq, 15)
            elif "py" in st:
                estimate += 35 * min(nq, 57)
            else:
                estimate += 8 * min(nq, 57)

    if max_estimate is not None and estimate > max_estimate:
        return keep, (f" digest auto-dropped {len(auto_drop)}" if auto_drop else "")

    if max_estimate is not None:
        saving = sum(ms[n] for n in tpc) + 2000
    else:
        saving = pack_cost(pack(sorted(keep), ms), ms) - pack_cost(pack(sorted(keep - tpc), ms), ms)

    left = deadline - time.monotonic()
    if saving <= estimate or left < 10:
        return keep, (f" digest auto-dropped {len(auto_drop)}" if auto_drop else "")

    verify.warm_up_heavy()
    own_tree = get_tree is None
    tree = overlay.build(SQLGLOT, os.path.join(WORK, cid), changes) if own_tree else get_tree()
    c0 = children_cpu()
    try:
        same = tpcdigest.run(
            tree,
            timeout=min(45, left - 4),
            base=TPC_BASE,
            suites=tuple(suites_run),
            query_filter=query_filter or None,
            stages=stages or None,
        ) or {}
    finally:
        if own_tree:
            overlay.remove(tree)
    cpu = children_cpu() - c0
    if checked_suites is not None:
        checked_suites.update(suites_run)
    drop = {n for n in tpc if same.get(NODEID[n]) is True}
    total_dropped = len(drop) + len(auto_drop)
    return keep - drop, f" digest {suites_run} cpu {cpu:.2f}s dropped {total_dropped}"


def _mod_to_path(mod):
    base = os.path.join(SQLGLOT, *mod.split("."))
    if os.path.isfile(base + ".py"):
        return mod.replace(".", "/") + ".py"
    if os.path.isfile(os.path.join(base, "__init__.py")):
        return mod.replace(".", "/") + "/__init__.py"
    return None


def import_breakage(got, analyzer, local_to_number):
    """Tests hit by modules that fail to import, or None when that is everyone."""
    out = set()
    for mod, _msg in got["errors"]:
        if mod in ("sqlglot", "tests", "<probe>") or mod.count(".") < 1:
            return None
        path = _mod_to_path(mod)
        if path is None:
            return None
        if path.startswith("tests/"):
            local = analyzer.cov.by_file.get(path, set())
        else:
            local = analyzer.module_users(path)
        out |= {local_to_number[i] for i in local if local_to_number[i] is not None}
    return out


def probe_change_impact(changed, analyzer, cov, local_to_number):
    """Narrow non-empty probe fingerprint changes to the tests using the changed
    classes or globals, or return None to fall back to `extra`."""
    if not changed:
        return None
    local = set()
    for key in changed:
        mod, sep, rest = key.partition(":")
        if not sep:
            return None
        path = _mod_to_path(mod)
        if path is None:
            return None
        if "." in rest:
            cls_part, _attr = rest.rsplit(".", 1)
            qual = f"{mod}.{cls_part}"
            inst = cov.instantiated(qual)
            if inst is None:
                return None
            local |= inst
            local |= cov.opened(path)
        else:
            name = rest
            if path.startswith("sqlglot/typing/") and name == "EXPRESSION_METADATA":
                local |= cov.opened(path)
                local |= cov.by_file.get("tests/test_typing.py", set())
                local |= analyzer.module_users(path)
            else:
                tests, at_import = analyzer.refs(name)
                if at_import:
                    return None
                local |= tests
                local |= cov.opened(path)
    return {local_to_number[i] for i in local if local_to_number[i] is not None}


def merge(res, part):
    res.tests |= part.tests
    res.notes += part.notes
    res.fallback = res.fallback or part.fallback
    res.no_verify = res.no_verify or part.no_verify
    res.if_import_changed |= part.if_import_changed
    res.allowed += part.allowed
    res.import_paths |= part.import_paths
    res.has_non_stmt = res.has_non_stmt or part.has_non_stmt
    for p, lns in part.stmt_lines.items():
        res.stmt_lines.setdefault(p, set()).update(lns)
    for p, lns in part.import_stmt_lines.items():
        res.import_stmt_lines.setdefault(p, set()).update(lns)
    res.class_quals |= part.class_quals
    res.class_line_pairs.extend(part.class_line_pairs)
    res.class_class_pairs.extend(part.class_class_pairs)
    res.has_non_class = res.has_non_class or part.has_non_class


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
        edited_lines = {}
        patch_text = path.read_text(encoding="utf-8", errors="surrogateescape")
        try:
            for fd in patchlib.parse(patch_text):
                part = Result()
                analyzer.analyze(fd, part)
                merge(res, part)
                parts.append((fd.path, part))
                if not (len(part.notes) == 1 and part.notes[0].startswith("no-op ")):
                    if fd.old_path:
                        active_paths.add(fd.old_path)
                        el = edited_lines.setdefault(fd.old_path, set())
                        for ch in patchlib.changes(fd):
                            el.add(ch.old_line)
                            if ch.kind == "ins" and ch.old_line > 1:
                                el.add(ch.old_line - 1)
                    if fd.new_path:
                        active_paths.add(fd.new_path)
                if changes is not None:
                    changes = add_change(changes, fd)
        except Exception as e:  # never crash on a strange patch: run everything
            log(f"{cid}: analysis failed ({type(e).__name__}: {e}); running all tests")
            res.everything(cov, "error")
            changes = None
        if res.no_verify:
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
        no_subcov = set()
        if not res.fallback:
            per_file = []
            for fpath, part in parts:
                nums = {
                    local_to_number[i]
                    for i in part.tests | part.if_import_changed
                    if local_to_number[i] is not None
                }
                if not fpath.endswith(".py"):
                    no_subcov |= nums
                    if 0 < len(nums) <= 3:
                        fix_tests |= nums
                n_fast = sum(
                    1 for n in nums
                    if NODEID[n] not in GIANT_TESTS and ACTUAL_MS[n] <= RUN_MAX_ACTUAL_MS
                )
                ok = fpath.endswith(".py") and n_fast >= PROXY_MIN
                per_file.append((nums, ok))
            for n in chosen | extra:
                if NODEID[n] in GIANT_TESTS and all(ok for nums, ok in per_file if n in nums):
                    vouchable.add(n)
        VOUCHABLE[cid] = vouchable
        FIXTURE_TESTS[cid] = fix_tests
        NO_SUBCOV[cid] = no_subcov
        plans.append((cid, chosen, changes, res, active_paths, patch_text, edited_lines))

    global TPC_BASE, SUBCOV
    base_fp = None
    base_noise = set()
    if VERIFY:
        if pending_probe and os.path.isfile(PROBE_MAP):
            pdata = json.load(open(PROBE_MAP))
            base_fp = pdata.get("fp")
            base_noise = set(pdata.get("noise", ()))
        if TPC_BASE is None and os.path.isfile(TPC_MAP) and any(
            (chosen & set(TPC)) for _cid, chosen, _ch, _r, _ap, _pt, _el in plans
        ):
            TPC_BASE = json.load(open(TPC_MAP))
        if SUBCOV is None and os.path.isfile(SUBCOV_MAP):
            SUBCOV = json.load(open(SUBCOV_MAP))
            for cls_key, sd in SUBCOV.get("setup", {}).items():
                SUBCOV_SETUP[cls_key] = (
                    {p: set(lns) for p, lns in sd.get("lines", {}).items()},
                    set(sd.get("classes", ())),
                )
        verify.warm_up()

    for cid, chosen, changes, res, active_paths, patch_text, edited_lines in plans:
        notes = res.notes
        tree_holder = [None]

        def get_tree():
            if tree_holder[0] is None:
                tree_holder[0] = overlay.build(SQLGLOT, os.path.join(WORK, cid), changes)
            return tree_holder[0]

        try:
            if cid in pending_probe:
                verify.warm_up_probe()
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
                    got = probe.probe(get_tree())
                    changed = None
                    if got and not got["errors"] and base_fp:
                        new_fp = {k: v for k, v in got["fp"].items() if k not in base_noise}
                        changed = [k for k in probe.diff(base_fp, new_fp) if not any(k.startswith(a) for a in allowed)]
                    broken = import_breakage(got, analyzer, local_to_number) if got and got["errors"] else None
                    if broken is not None:
                        chosen = chosen | broken
                        res.has_non_class = True
                        if res.import_stmt_lines:
                            res.has_non_stmt = True
                        notes = notes + [f"import breaks {len(got['errors'])} modules: {len(broken)} tests"]
                    elif changed is None or changed:
                        narrowed = (
                            probe_change_impact(changed, analyzer, cov, local_to_number)
                            if changed
                            else None
                        )
                        if narrowed is not None:
                            chosen = chosen | narrowed
                            notes = notes + [f"import changed (narrowed {len(narrowed)}): {changed[:3]}"]
                        else:
                            chosen = chosen | extra
                            notes = notes + [f"import changed: {(changed or ['error'])[:3]}"]
                        res.has_non_class = True
                        if res.import_stmt_lines:
                            res.has_non_stmt = True
                    else:
                        for p, lns in res.import_stmt_lines.items():
                            res.stmt_lines.setdefault(p, set()).update(lns)
                        notes = notes + ["import unchanged"]
                except Exception as e:  # unsure: keep them
                    chosen = chosen | extra
                    res.has_non_class = True
                    if res.import_stmt_lines:
                        res.has_non_stmt = True
                    notes = notes + [f"probe failed {type(e).__name__}"]
                notes = notes + [f"probe cpu {children_cpu() - p0:.2f}s"]
            elif res.import_stmt_lines:
                res.has_non_stmt = True
                res.has_non_class = True

            fast_mn = max_nodes_fast_ok(changes, active_paths, patch_text, edited_lines, res)
            keep, how, stopped_early = chosen, "listed", False
            pre_tpc_info = ""
            tpc_done = set()
            if VERIFY and changes is not None and keep & set(TPC):
                try:
                    keep, pre_tpc_info = tpc_check(
                        cid, keep, changes, ms, deadline, res, active_paths,
                        get_tree=get_tree, max_estimate=600, checked_suites=tpc_done,
                    )
                except Exception as e:  # noqa: BLE001
                    pre_tpc_info = f" digest failed {type(e).__name__}"

            if VERIFY and changes is not None and keep:
                try:
                    keep, how, stopped_early = check(
                        cid,
                        keep,
                        changes,
                        ms,
                        deadline,
                        notes,
                        res=res,
                        active_paths=active_paths,
                        get_tree=get_tree,
                        fast_max_nodes=fast_mn,
                    )
                except Exception as e:  # a failed check keeps every candidate
                    log(f"{cid}: check failed ({type(e).__name__}: {e})")
                    keep, how, stopped_early = keep, "check-error", False
            if pre_tpc_info:
                how = (how + pre_tpc_info) if how != "listed" else pre_tpc_info.lstrip()

            if VERIFY and changes is not None and keep & set(TPC):
                before_tpc = len(keep)
                how_pre = how
                try:
                    keep, more = tpc_check(
                        cid, keep, changes, ms, deadline, res, active_paths,
                        get_tree=get_tree, checked_suites=tpc_done,
                    )
                    how += more
                except Exception as e:  # unsure: keep them
                    how += f" digest failed {type(e).__name__}"
                if (
                    stopped_early
                    or how_pre == "listed"
                    or how_pre.startswith("subset 1 ")
                    or len(keep) == 1
                ) and len(keep) < before_tpc and keep:
                    try:
                        keep, how2, _ = check(
                            cid,
                            keep,
                            changes,
                            ms,
                            deadline,
                            notes,
                            allow_stop_early=False,
                            res=res,
                            active_paths=active_paths,
                            get_tree=get_tree,
                            fast_max_nodes=fast_mn,
                        )
                        if how2 != "listed":
                            how += " + " + how2
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if tree_holder[0] is not None:
                overlay.remove(tree_holder[0])

        workers = pack(sorted(keep), ms)
        log(
            f"{cid}: {len(chosen)} candidates, {how}, {len(keep)} kept, {len(workers)} workers,"
            f" cost {pack_cost(workers, ms) / 1000:.2f}; {' '.join(notes)[:240]}"
        )
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
    log(f"total {time.process_time() - t0:.2f}s cpu, checks {children_cpu():.2f}s cpu")


if __name__ == "__main__":
    main()
