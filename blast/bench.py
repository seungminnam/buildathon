"""Measure actual per-test CPU time under blast/verify.py at build time.

The recorded runtimes in tests.json include pytest and spawn-pool overhead from
the labeling box. Under our direct unittest runner with InlineExecutor and
lightweight TestExecutor setup, most tests run 3x-50x faster. Measuring real
CPU times during docker build calibrates selector.py to the exact scorer CPU.

    python -m blast.bench /opt/sqlglot /app/maps/coverage.json /app/maps/actual_ms.json
"""

import concurrent.futures
import importlib
import json
import os
import sys
import time
import unittest

from . import verify

SKIP_BENCH = {
    "tests/test_parser.py::TestParser::test_max_nodes": 30000,
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": 22000,
    "tests/test_executor.py::TestExecutor::test_execute_tpch": 4000,
    "tests/test_executor.py::TestExecutor::test_optimized_tpch": 3500,
}


def _bench_shard(args):
    tree, groups = args
    os.chdir(tree)
    sys.path.insert(0, tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    verify._cheap_pools()

    out = {}
    mod_cache = {}

    class SilentResult(unittest.TestResult):
        def addFailure(self, test, err):
            pass

        def addError(self, test, err):
            pass

        def addSubTest(self, test, subtest, err):
            pass

    for (fpath, mod_name, cls_name), methods in groups:
        mod = mod_cache.get(mod_name)
        if mod is None:
            try:
                mod = importlib.import_module(mod_name)
                mod_cache[mod_name] = mod
            except BaseException:  # noqa: BLE001
                continue
        cls = getattr(mod, cls_name, None)
        if cls is None or getattr(cls, "__unittest_skip__", False):
            continue
        if mod_name == "tests.test_executor" and cls_name == "TestExecutor":
            verify._patch_light_executor(mod, cls, [m for m, _ in methods])
        try:
            cls.setUpClass()
        except BaseException:  # noqa: BLE001
            continue
        res = SilentResult()
        try:
            for meth, nid in methods:
                t0 = time.process_time()
                try:
                    cls(meth).run(res)
                except BaseException:  # noqa: BLE001
                    pass
                dt = time.process_time() - t0
                out[nid] = max(1, round(dt * 1000))
        finally:
            try:
                cls.tearDownClass()
            except BaseException:  # noqa: BLE001
                pass
            try:
                cls.doClassCleanups()
            except BaseException:  # noqa: BLE001
                pass
    return out


def _measure_overhead(tree, sample_nid):
    """Measure one fork + sqlglot import + 1 tiny test via verify.run."""
    import resource

    verify.warm_up()
    c0 = sum(resource.getrusage(resource.RUSAGE_CHILDREN)[:2])
    runs = 3
    for _ in range(runs):
        verify.run(tree, [sample_nid], timeout=30)
    c1 = sum(resource.getrusage(resource.RUSAGE_CHILDREN)[:2])
    return max(30, round((c1 - c0) / runs * 1000))


def main():
    tree, cov_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    cov = json.load(open(cov_path))
    tests = cov["tests"]

    groups_by_key = {}
    groups = []
    for nid in tests:
        if nid in SKIP_BENCH:
            continue
        parts = nid.split("::")
        if len(parts) != 3:
            continue
        fpath, cls_name, meth = parts
        mod_name = fpath[:-3].replace("/", ".")
        key = (fpath, mod_name, cls_name)
        lst = groups_by_key.get(key)
        if lst is None:
            lst = []
            groups_by_key[key] = lst
            groups.append((key, lst))
        lst.append((meth, nid))

    # Spread test classes across build cores (largest classes first)
    n_workers = min(4, max(1, os.cpu_count() or 1))
    shards = [[] for _ in range(n_workers)]
    for i, grp in enumerate(sorted(groups, key=lambda g: -len(g[1]))):
        shards[i % n_workers].append(grp)

    timings = dict(SKIP_BENCH)
    if n_workers == 1:
        timings.update(_bench_shard((tree, shards[0])))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=n_workers) as pool:
            for part in pool.map(_bench_shard, [(tree, s) for s in shards if s]):
                timings.update(part)

    # Pick a tiny non-heavy test to measure baseline fork+import overhead
    tiny = min(
        (nid for nid in timings if nid not in SKIP_BENCH and not nid.startswith(verify.HEAVY_FILES)),
        key=lambda nid: timings[nid],
    )
    overhead_ms = _measure_overhead(tree, tiny)

    data = {"overhead_ms": overhead_ms, "tests": timings}
    with open(out_path, "w") as f:
        json.dump(data, f, sort_keys=True, separators=(",", ":"))
    print(
        f"benched {len(timings)} tests (sum={sum(v for k, v in timings.items() if k not in SKIP_BENCH)/1000:.2f}s), "
        f"overhead={overhead_ms}ms",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
