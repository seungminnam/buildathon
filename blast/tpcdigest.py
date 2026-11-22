"""A fast stand-in for the TPC-H / TPC-DS tests in test_executor.py.

test_execute_tpcds (21.5 s) runs 57 executable queries from tpc-ds.sql through
sqlglot's Python executor and compares each result with DuckDB running sqlglot's
DuckDB translation. test_execute_tpch (3.9 s) does the same for tpc-h.sql.
test_optimized_tpch (3.5 s) only transpiles each tpc-h query and its expected
optimized SQL to DuckDB and compares their DuckDB results.

For each query we hash the deterministic sqlglot outputs split into two stages:
  - duck: parse_one(sql) -> sql("duckdb")  (plus optimized -> sql("duckdb") for tpch_opt)
  - py:   parse_one(sql) -> pickle -> find_tables -> optimize -> Plan -> PythonExecutor on mini tables

If a patched tree matches the pinned tree's hashes for every affected query,
the real test sees identical inputs and unchanged executor code, so it passes.
At build time we also record per-query line coverage and per-stage files so
runtime checks only digest the queries and stages a patch can reach.

    python -m blast.tpcdigest TREE OUT.json
"""

import csv
import hashlib
import os
import pickle
import re
import sys

ROWS = 30
_IDS = re.compile(r"\(\d{6,}\)|0x[0-9a-f]{6,}")

TEST_TPCDS = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
TEST_TPCH = "tests/test_executor.py::TestExecutor::test_execute_tpch"
TEST_TPCH_OPT = "tests/test_executor.py::TestExecutor::test_optimized_tpch"

TESTS = {
    "tpch_exec": (TEST_TPCH,),
    "tpch_opt": (TEST_TPCH_OPT,),
    "tpcds_exec": (TEST_TPCDS,),
}

SUITES_INFO = {
    "tpch": ("TPCH_SCHEMA", "optimizer/tpc-h/tpc-h.sql", "/optimizer/tpc-h/"),
    "tpcds": ("TPCDS_SCHEMA", "optimizer/tpc-ds/tpc-ds.sql", "/optimizer/tpc-ds/"),
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


def _step_head(step):
    """Non-recursive summary of one Plan Step (O(1) instead of recursive to_s)."""
    lines = [f"{step.type_name}:{step.name}"]
    lines.extend(step._to_s(""))
    lines.extend(e.sql() for e in step.projections)
    if step.condition:
        lines.append("C:" + step.condition.sql())
    if step.limit != float("inf"):
        lines.append(f"L:{step.limit}")
    if step.offset:
        lines.append(f"O:{step.offset}")
    return _IDS.sub("", "\n".join(lines))


def _plan_key(plan):
    """Canonical DAG fingerprint of a Plan; each Step is formatted once."""
    heads = {}
    todo = [plan.root]
    nodes = []
    while todo:
        step = todo.pop()
        sid = id(step)
        if sid in heads:
            continue
        heads[sid] = _step_head(step)
        nodes.append(step)
        todo.extend(step.dependencies)
    out = [
        heads[id(s)] + "->" + "|".join(sorted(heads[id(d)] for d in s.dependencies))
        for s in nodes
    ]
    return "\n".join(sorted(out))


def _h(*parts):
    return hashlib.sha1("\x00".join(map(str, parts)).encode("utf-8", "replace")).hexdigest()[:16]


def _digest_one_query(meta, sql, optimized, schema, tables_, need_duck, need_py, need_opt, code_log):
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan

    is_exec = bool(meta.get("execute"))
    meta_repr = repr(sorted(meta.items()))

    # Parse once (needed for all queries since _mp_execute calls parse_one(sql) before checking meta)
    parsed = None
    parse_err = None
    pickled = None
    parsed_sql = None
    had_err = False
    try:
        parsed = parse_one(sql)
        pickled = pickle.loads(pickle.dumps(parsed))
        if is_exec or need_opt:
            parsed_sql = pickled.sql()
    except Exception as e:  # noqa: BLE001
        parse_err = f"!{type(e).__name__}: {e}"
        pickled = None
        parsed_sql = parse_err
        had_err = True

    duck_h = None
    if need_duck:
        if not is_exec:
            d_parts = ["skip", parse_err is None]
        else:
            d_parts = [meta_repr, sql, parsed_sql]
            try:
                d_parts.append(parsed.sql("duckdb") if parsed is not None else transpile(sql, write="duckdb")[0])
            except Exception as e:  # noqa: BLE001
                d_parts.append(f"!{type(e).__name__}: {e}")
                had_err = True
        duck_h = _h(*d_parts)

    opt_h = None
    if need_opt:
        o_parts = [sql, optimized]
        try:
            o_parts.append(parsed.sql("duckdb") if parsed is not None else transpile(sql, write="duckdb")[0])
        except Exception as e:  # noqa: BLE001
            o_parts.append(f"!{type(e).__name__}: {e}")
            had_err = True
        try:
            o_parts.append(transpile(optimized, write="duckdb")[0])
        except Exception as e:  # noqa: BLE001
            o_parts.append(f"!{type(e).__name__}: {e}")
            had_err = True
        opt_h = _h(*o_parts)

    py_h = None
    if need_py:
        if not is_exec:
            p_parts = ["skip", parse_err is None]
        else:
            p_parts = [meta_repr, sql, parsed_sql]
            if pickled is not None:
                try:
                    p_parts.append(repr(sorted(t.name for t in find_tables(pickled))))
                except Exception as e:  # noqa: BLE001
                    p_parts.append(f"!{type(e).__name__}: {e}")
                    had_err = True
                plan = None
                try:
                    expr = optimize(pickled, schema, leave_tables_isolated=True)
                    p_parts.append(expr.sql())
                    plan = Plan(expr)
                    p_parts.append(_plan_key(plan))
                except Exception as e:  # noqa: BLE001
                    p_parts.append(f"!{type(e).__name__}: {e}")
                    had_err = True
                if plan is not None:
                    code_log.clear()
                    try:
                        table = PythonExecutor(tables=tables_).execute(plan)
                        table = pickle.loads(pickle.dumps(table))
                        p_parts.append(repr(table.columns))
                        p_parts.append(repr(sorted(map(repr, table.rows))))
                    except Exception as e:  # noqa: BLE001
                        p_parts.append(f"!{type(e).__name__}: {e}")
                        had_err = True
                    p_parts.append(_IDS.sub("", repr(sorted(code_log))))
            else:
                p_parts.append(parse_err)
        py_h = _h(*p_parts)

    return duck_h, py_h, opt_h, had_err


MAX_REAL_VERIFY = 4


class _RealVerifier:
    """Lazily loads only the CSV tables needed by mismatching queries and runs
    the exact TestExecutor DuckDB vs PythonExecutor assertion."""

    def __init__(self, directory, schema, open_file):
        self.directory = directory
        self.schema = schema
        self.open_file = open_file
        self.conn = None
        self.duck_views = set()
        self.py_tables = {}
        self.duck_cache = {}

    def _ensure_conn(self):
        if self.conn is None:
            import duckdb

            self.conn = duckdb.connect()

    def _ensure_duck_table(self, table):
        if table in self.duck_views:
            return
        self._ensure_conn()
        columns = self.schema[table]
        file_name = f"{self.directory}{table}.csv.gz"
        self.conn.execute(
            f"""
            CREATE VIEW {table} AS
            SELECT *
            FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
            """
        )
        self.duck_views.add(table)

    def _ensure_py_table(self, table):
        if table in self.py_tables:
            return self.py_tables[table]
        import ast as pyast

        from sqlglot.executor.table import Table

        columns = self.schema[table]
        file_name = f"{self.directory}{table}.csv.gz"
        reader = csv.reader(self.open_file(file_name), delimiter="|")
        next(reader)
        rows, ctypes = [], []
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(pyast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(
                tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row))
            )
        tbl = Table(columns=columns, rows=rows)
        self.py_tables[table] = tbl
        return tbl

    def _duck_df(self, sql, tables_needed):
        from sqlglot import transpile

        for tbl in tables_needed:
            self._ensure_duck_table(tbl)
        if sql not in self.duck_cache:
            self.duck_cache[sql] = self.conn.execute(transpile(sql, write="duckdb")[0]).fetchdf()
        return self.duck_cache[sql]

    def verify_exec(self, meta, sql):
        import numpy as np
        import pandas as pd
        from pandas.testing import assert_frame_equal

        from sqlglot import find_tables, parse_one
        from sqlglot.executor import execute

        try:
            expr = pickle.loads(pickle.dumps(parse_one(sql)))
            if not meta.get("execute"):
                return True
            used = sorted({t.name for t in find_tables(expr)})
            a = self._duck_df(sql, used)
            tables = {name: self._ensure_py_table(name) for name in used}
            table = execute(expr, schema=self.schema, tables=tables)
            table = pickle.loads(pickle.dumps(table))
            b = pd.DataFrame(
                ((np.nan if c is None else c for c in r) for r in table.rows),
                columns=table.columns,
            )
            assert_frame_equal(a, b, check_dtype=False, check_index_type=False)
            return True
        except BaseException:  # noqa: BLE001
            return False

    def verify_opt(self, sql, optimized):
        from pandas.testing import assert_frame_equal

        from sqlglot import find_tables, parse_one, transpile

        try:
            used_a = {t.name for t in find_tables(parse_one(sql))}
            used_b = {t.name for t in find_tables(parse_one(optimized))}
            for tbl in used_a | used_b:
                self._ensure_duck_table(tbl)
            a = self._duck_df(sql, used_a)
            b = self.conn.execute(transpile(optimized, write="duckdb")[0]).fetchdf()
            for i, column in enumerate(b.columns):
                if "_col_" in column:
                    b.rename(columns={column: a.columns[i]}, inplace=True)
            assert_frame_equal(a, b)
            return True
        except BaseException:  # noqa: BLE001
            return False

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None


def _make_tracer(tree):
    """Lightweight sys.monitoring line+call tracer for build-time query coverage."""
    mon = sys.monitoring
    prefix = os.path.abspath(tree) + os.sep
    active = [False]
    hit_lines = {}  # relpath -> set(line)

    def _rel(filename):
        if filename.startswith(prefix):
            rel = filename[len(prefix):]
            if rel.startswith("sqlglot/"):
                return rel
        return None

    def on_line(code, lineno):
        if not active[0]:
            return
        rel = _rel(code.co_filename)
        if rel is not None:
            s = hit_lines.get(rel)
            if s is None:
                s = set()
                hit_lines[rel] = s
            s.add(lineno)

    def on_start(code, offset):
        if not active[0]:
            return
        rel = _rel(code.co_filename)
        if rel is not None:
            s = hit_lines.get(rel)
            if s is None:
                s = set()
                hit_lines[rel] = s
            s.add(code.co_firstlineno)

    tool_id = 3
    mon.use_tool_id(tool_id, "tpcdigest")
    mon.register_callback(tool_id, mon.events.LINE, on_line)
    mon.register_callback(tool_id, mon.events.PY_START, on_start)
    mon.set_events(tool_id, mon.events.LINE | mon.events.PY_START)

    def start():
        hit_lines.clear()
        active[0] = True

    def stop():
        active[0] = False
        return {p: sorted(v) for p, v in hit_lines.items()}

    def close():
        active[0] = False
        mon.set_events(tool_id, 0)
        mon.free_tool_id(tool_id)

    return start, stop, close


def _open_file(file_name):
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    if gzipped:
        import gzip

        return gzip.open(file_name, "rt", newline="")
    return open(file_name, encoding="utf-8", newline="")


def digest(tree, base=None, suites=("tpch_exec", "tpch_opt", "tpcds_exec"), query_filter=None, stages=None):
    """Build or compare TPC query digests.

    Without `base`: returns full build-time artifact dict including per-query
    hashes (`hashes`), per-stage files (`stage_files`), and per-query line
    coverage (`q_lines`).
    With `base`: returns {test_node_id: True/False}, stopping each test at its
    first differing query (after checking up to MAX_REAL_VERIFY non-error
    mismatches against the exact TestExecutor DuckDB/PythonExecutor comparison).
    `query_filter`: optional {suite_group ("tpch"/"tpcds"): set of query indices}
    `stages`: optional {suite_group: ("duck", "py")} restricting which stages run.
    """
    sys.path.insert(0, tree)
    os.chdir(tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    from sqlglot.executor.table import ensure_tables
    from sqlglot.executor.python import PythonExecutor
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    schemas = {"TPCH_SCHEMA": TPCH_SCHEMA, "TPCDS_SCHEMA": TPCDS_SCHEMA}
    code_log = []
    real_generate = PythonExecutor.generate

    def generate(self, expression):
        out = real_generate(self, expression)
        code_log.append(getattr(out, "co_filename", repr(out)))
        return out

    PythonExecutor.generate = generate

    if base is None:
        # Build-time mode: compute all hashes + per-query coverage + stage files
        t_start, t_stop, t_close = _make_tracer(tree)
        out_hashes = {TEST_TPCH: {}, TEST_TPCH_OPT: {}, TEST_TPCDS: {}}
        stage_files = {}  # "tpch:duck", "tpch:py", "tpcds:duck", "tpcds:py" -> sorted files
        # q_lines: {group: {relpath: {line: [query_indices]}}}
        q_lines = {}
        try:
            for grp, (schema_name, fixture, rel_dir) in SUITES_INFO.items():
                schema = schemas[schema_name]
                tables_ = ensure_tables(_mini_tables(FIXTURES_DIR + rel_dir, schema, _open_file))
                exec_test = TEST_TPCH if grp == "tpch" else TEST_TPCDS
                d_files, p_files = set(), set()
                grp_lines = {}
                for i, (meta, sql, optimized) in enumerate(load_sql_fixture_pairs(fixture)):
                    t_start()
                    dh, _, oh, _ = _digest_one_query(
                        meta, sql, optimized, schema, tables_,
                        need_duck=True, need_py=False, need_opt=(grp == "tpch"), code_log=code_log,
                    )
                    dl = t_stop()
                    d_files.update(dl.keys())

                    t_start()
                    _, ph, _, _ = _digest_one_query(
                        meta, sql, optimized, schema, tables_,
                        need_duck=False, need_py=True, need_opt=False, code_log=code_log,
                    )
                    pl = t_stop()
                    p_files.update(pl.keys())

                    out_hashes[exec_test][str(i)] = [dh, ph]
                    if grp == "tpch":
                        out_hashes[TEST_TPCH_OPT][str(i)] = oh
                    # merge lines for query i
                    for p, lns in dl.items():
                        per = grp_lines.setdefault(p, {})
                        for ln in lns:
                            per.setdefault(ln, set()).add(i)
                    for p, lns in pl.items():
                        per = grp_lines.setdefault(p, {})
                        for ln in lns:
                            per.setdefault(ln, set()).add(i)
                stage_files[f"{grp}:duck"] = sorted(d_files)
                stage_files[f"{grp}:py"] = sorted(p_files)
                q_lines[grp] = {
                    p: {str(ln): sorted(qs) for ln, qs in per.items()}
                    for p, per in grp_lines.items()
                }
        finally:
            t_close()
        return {"hashes": out_hashes, "stage_files": stage_files, "q_lines": q_lines}

    # Runtime comparison mode
    base_hashes = base["hashes"]
    same = {}
    want = set(suites)
    for grp, (schema_name, fixture, rel_dir) in SUITES_INFO.items():
        exec_key = f"{grp}_exec"
        opt_key = f"{grp}_opt"
        do_exec = exec_key in want
        do_opt = opt_key in want and grp == "tpch"
        if not do_exec and not do_opt:
            continue
        exec_test = TEST_TPCH if grp == "tpch" else TEST_TPCDS
        ref_exec = base_hashes.get(exec_test, {})
        ref_opt = base_hashes.get(TEST_TPCH_OPT, {}) if do_opt else {}

        allowed_stages = set((stages or {}).get(grp, ("duck", "py")))
        need_duck = do_exec and ("duck" in allowed_stages)
        need_py = do_exec and ("py" in allowed_stages)

        schema = schemas[schema_name]
        tables_ = None
        if need_py:
            tables_ = ensure_tables(_mini_tables(FIXTURES_DIR + rel_dir, schema, _open_file))

        q_subset = (query_filter or {}).get(grp)
        pairs = list(load_sql_fixture_pairs(fixture))
        exec_ok = do_exec and len(pairs) == len(ref_exec)
        opt_ok = do_opt and len(pairs) == len(ref_opt)

        verifier = None
        real_exec_checks = 0
        real_opt_checks = 0
        try:
            for i, (meta, sql, optimized) in enumerate(pairs):
                if q_subset is not None and i not in q_subset:
                    continue
                cur_duck = need_duck and exec_ok
                cur_py = need_py and exec_ok
                cur_opt = do_opt and opt_ok
                if not (cur_duck or cur_py or cur_opt):
                    break
                dh, ph, oh, had_err = _digest_one_query(
                    meta, sql, optimized, schema, tables_,
                    need_duck=cur_duck, need_py=cur_py, need_opt=cur_opt, code_log=code_log,
                )
                si = str(i)
                if exec_ok:
                    ref_pair = ref_exec.get(si)
                    exec_mismatch = (
                        ref_pair is None
                        or (cur_duck and dh != ref_pair[0])
                        or (cur_py and ph != ref_pair[1])
                    )
                    if exec_mismatch:
                        if had_err or real_exec_checks >= MAX_REAL_VERIFY:
                            exec_ok = False
                        else:
                            real_exec_checks += 1
                            if verifier is None:
                                verifier = _RealVerifier(FIXTURES_DIR + rel_dir, schema, _open_file)
                            if not verifier.verify_exec(meta, sql):
                                exec_ok = False
                if opt_ok and cur_opt:
                    if oh != ref_opt.get(si):
                        if had_err or real_opt_checks >= MAX_REAL_VERIFY:
                            opt_ok = False
                        else:
                            real_opt_checks += 1
                            if verifier is None:
                                verifier = _RealVerifier(FIXTURES_DIR + rel_dir, schema, _open_file)
                            if not verifier.verify_opt(sql, optimized):
                                opt_ok = False
        finally:
            if verifier is not None:
                verifier.close()

        if do_exec:
            same[exec_test] = exec_ok
        if do_opt:
            same[TEST_TPCH_OPT] = opt_ok
    return same


def run(tree, timeout=60, base=None, suites=("tpch_exec", "tpch_opt", "tpcds_exec"), query_filter=None, stages=None):
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
            data = json.dumps(digest(tree, base, suites, query_filter=query_filter, stages=stages))
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
    json.dump(got, open(out, "w"), sort_keys=True, separators=(",", ":"))
