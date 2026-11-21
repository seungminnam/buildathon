"""Per-query line coverage for the TPC-H / TPC-DS executor tests.

    python -u tools/covmap/trace_queries.py OUT.json     (inside the tools env, cwd = sqlglot)

test_execute_tpcds runs 99 queries and costs 21 s; most patches it can
reach only touch a few of them. For each query this records the
function-body lines the query runs through: parsing it, pickling the parsed
tree to the pool worker and back, executing it with the Python executor,
and transpiling it to DuckDB for the reference result. Lines that run while
the class sets up its tables are recorded separately: a change there
concerns every query.

Each query is traced in its own forked child, so lazily built state counts
for every query that needs it. Output:

    {"setup": {file: [lines]},
     "tests": {nodeid: {"list": "tpcds_sqls", "n": 99,
                        "lines": {file: {line: hex bitmask of query indexes}}}}}
"""

import json
import os
import pickle
import sys

ROOT = os.path.realpath(os.getcwd()) + os.sep
sys.path.insert(0, os.getcwd())
os.environ["SKIP_INTEGRATION"] = "0"

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
CO_OPTIMIZED = 0x1
_lines = set()
_rel = {}


def rel(path):
    r = _rel.get(path)
    if r is None:
        real = os.path.realpath(path)
        r = real[len(ROOT):] if real.startswith(ROOT) else ""
        _rel[path] = r
    return r


def on_line(code, line):
    if code.co_flags & CO_OPTIMIZED:
        r = rel(code.co_filename)
        if r:
            _lines.add((r, line))
    return mon.DISABLE


def grouped(lines):
    out = {}
    for f, ln in lines:
        out.setdefault(f, []).append(ln)
    return {f: sorted(v) for f, v in out.items()}


def main(out_path):
    mon.use_tool_id(TOOL, "queries")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.set_events(TOOL, mon.events.LINE)

    import tests.test_executor as te
    from sqlglot import parse_one, transpile

    _lines.clear()
    mon.restart_events()
    te.TestExecutor.setUpClass()
    setup = grouped(_lines)
    cls = te.TestExecutor
    jobs = [
        ("tests/test_executor.py::TestExecutor::test_execute_tpcds", "tpcds_sqls", te.TPCDS_SCHEMA, cls.tpcds_tables, False),
        ("tests/test_executor.py::TestExecutor::test_execute_tpch", "tpch_sqls", te.TPCH_SCHEMA, cls.tpch_tables, True),
    ]
    result = {"setup": setup, "tests": {}}
    for nodeid, attr, schema, tables, tpch in jobs:
        sqls = getattr(cls, attr)
        per_line = {}
        for i, (meta, sql, _) in enumerate(sqls):
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                _lines.clear()
                mon.restart_events()
                try:
                    # the pool pickles the tree out and the result back; do
                    # the same so serialisation code counts (our own objects)
                    expr = pickle.loads(pickle.dumps(parse_one(sql)))
                    te.initializer(schema, tables)
                    table = te.mp_execute(expr, meta)
                    table = pickle.loads(pickle.dumps(table))
                    if table is not None:
                        conn = cls.tpch_conn if tpch else cls.tpcds_conn
                        conn.execute(transpile(sql, write="duckdb")[0]).fetchdf()
                except BaseException as e:  # noqa: BLE001
                    print(f"{attr}[{i}] raised {type(e).__name__}: {e}", file=sys.stderr)
                os.write(w, json.dumps(sorted(_lines)).encode())
                os.close(w)
                os._exit(0)
            os.close(w)
            data = b""
            while chunk := os.read(r, 1 << 20):
                data += chunk
            os.close(r)
            os.waitpid(pid, 0)
            for f, ln in json.loads(data):
                d = per_line.setdefault(f, {})
                d[ln] = d.get(ln, 0) | (1 << i)
            print(f"{attr}[{i}] traced", file=sys.stderr, flush=True)
        result["tests"][nodeid] = {
            "list": attr,
            "n": len(sqls),
            "lines": {f: {str(ln): format(m, "x") for ln, m in sorted(d.items())} for f, d in per_line.items()},
        }
    json.dump(result, open(out_path, "w"), separators=(",", ":"))


if __name__ == "__main__":
    main(sys.argv[1])
