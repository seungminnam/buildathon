"""A cheap stand-in for the TPC-H / TPC-DS tests in test_executor.py.

test_execute_tpcds (21 s) runs 99 queries through sqlglot's Python executor
and compares each result with DuckDB running sqlglot's DuckDB translation.
What a patch can change on the sqlglot side is a short chain of
deterministic outputs per query:

    parse -> pickle round trip -> optimize -> plan -> Python code the
    executor compiles -> rows it returns;  and  parse -> DuckDB SQL

This module records all of them for every query, executing on the first
few rows of each table only. If a patched tree gives the same record as the
pinned tree for every query, the tests that consume those outputs see the
same inputs and run the same unchanged executor code on them, so they pass
or fail together with the pinned tree, which passes.

That reasoning breaks when the patch edits the executor's runtime
(sqlglot/executor/) or the TPC data files; the selector does not use the
digest then.

    python -m blast.tpcdigest TREE OUT.json      writes {test node id: {query index: hash}}
"""

import csv
import hashlib
import os
import pickle
import sys

ROWS = 30
_IDS = __import__("re").compile(r"\(\d{6,}\)|0x[0-9a-f]{6,}")

TESTS = {
    "tpch": (
        "tests/test_executor.py::TestExecutor::test_execute_tpch",
        "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    ),
    "tpcds": ("tests/test_executor.py::TestExecutor::test_execute_tpcds",),
}


def _mini_tables(directory, schema, open_file):
    import ast as pyast

    from sqlglot.executor.table import Table

    tables = {}
    for table, columns in schema.items():
        reader = csv.reader(open_file(f"{directory}{table}.csv.gz"), delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for k, row in enumerate(reader):
            if k >= ROWS:
                break
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(pyast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[table] = Table(columns=tuple(columns), rows=rows)
    return tables


def _plan_key(plan):
    """The plan as a sorted list of steps; dependency order is not stable."""

    def head(step):
        return _IDS.sub("", str(step)).split("Dependencies:")[0]

    seen, out, todo = set(), [], [plan.root]
    while todo:
        step = todo.pop()
        if id(step) in seen:
            continue
        seen.add(id(step))
        deps = list(step.dependencies)
        out.append(head(step) + "->" + "|".join(sorted(head(d) for d in deps)))
        todo.extend(deps)
    return "\n".join(sorted(out))


def _h(*parts):
    return hashlib.sha1("\x00".join(map(str, parts)).encode("utf-8", "replace")).hexdigest()[:16]


def digest(tree, base=None, suites=("tpch", "tpcds")):
    """{node id: {query: hash}}; with `base`, {node id: equal to base} and stop
    a suite at its first differing query."""
    sys.path.insert(0, tree)
    os.chdir(tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor.table import ensure_tables
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs
    import tests.test_executor as te

    code = []
    real_generate = PythonExecutor.generate

    def generate(self, expression):
        out = real_generate(self, expression)
        code.append(getattr(out, "co_filename", repr(out)))
        return out

    PythonExecutor.generate = generate
    result = {}
    same = {}
    for name, schema, fixture, directory in (
        x for x in (
        ("tpch", TPCH_SCHEMA, "optimizer/tpc-h/tpc-h.sql", FIXTURES_DIR + "/optimizer/tpc-h/"),
        ("tpcds", TPCDS_SCHEMA, "optimizer/tpc-ds/tpc-ds.sql", FIXTURES_DIR + "/optimizer/tpc-ds/"),
        ) if x[0] in suites
    ):
        ref = base.get(TESTS[name][0]) if base else None
        equal = ref is not None
        tables = _mini_tables(directory, schema, te.open_file)
        per = {}
        tables_ = ensure_tables(tables)
        for i, (meta, sql, optimized) in enumerate(load_sql_fixture_pairs(fixture)):
            parts = [repr(sorted(meta.items())), sql, optimized]
            plan = None
            for step in (
                lambda: pickle.loads(pickle.dumps(parse_one(sql))).sql(),
                lambda: sorted(t.name for t in find_tables(parse_one(sql))),
                lambda: transpile(sql, write="duckdb")[0],
                lambda: transpile(optimized, write="duckdb")[0] if name == "tpch" else "",
            ):
                try:
                    parts.append(step())
                except Exception as e:  # noqa: BLE001
                    parts.append(f"!{type(e).__name__}: {e}")
            try:
                # what execute() does, with the plan kept for the record
                # the worker gets the parsed tree through pickle
                expression = pickle.loads(pickle.dumps(parse_one(sql)))
                expression = optimize(expression, schema, leave_tables_isolated=True)
                parts.append(expression.sql())
                plan = Plan(expression)
                parts.append(_plan_key(plan))
            except Exception as e:  # noqa: BLE001
                parts.append(f"!{type(e).__name__}: {e}")
            if meta.get("execute") and plan is not None:
                code.clear()
                try:
                    table = PythonExecutor(tables=tables_).execute(plan)
                    # the pool sends results back pickled (our own objects)
                    table = pickle.loads(pickle.dumps(table))
                    parts.append(repr(table.columns))
                    parts.append(repr(sorted(map(repr, table.rows))))
                except Exception as e:  # noqa: BLE001
                    parts.append(f"!{type(e).__name__}: {e}")
                parts.append(_IDS.sub("", repr(sorted(code))))
            per[str(i)] = _h(*parts)
            if ref is not None and ref.get(str(i)) != per[str(i)]:
                equal = False
                break
        if ref is not None and len(per) != len(ref):
            equal = False
        for node in TESTS[name]:
            result[node] = per
            same[node] = equal
    return same if base else result


def run(tree, timeout=60, base=None, suites=("tpch", "tpcds")):
    """Digest `tree` in a forked child. None when the child fails."""
    import json
    import select
    import signal
    import time

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.setpgid(0, 0)
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            data = json.dumps(digest(tree, base, suites))
            os.write(w, data.encode())
        except BaseException:  # noqa: BLE001
            pass
        finally:
            os._exit(0)
    os.close(w)
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            continue
        chunk = os.read(r, 1 << 20)
        if not chunk:
            break
        buf += chunk
    os.close(r)
    os.waitpid(pid, 0)
    try:
        return json.loads(buf)
    except ValueError:
        return None


if __name__ == "__main__":
    import json

    tree, out = sys.argv[1], sys.argv[2]
    got = run(tree, timeout=600)
    if not got:
        sys.exit("digest failed")
    json.dump(got, open(out, "w"), sort_keys=True)
