"""Build-time per-fixture-item and per-dialect sub-coverage map.

Many of sqlglot's slowest tests iterate over dozens or hundreds of independent
SQL fixture cases (or all Dialects in test_generator_memory). At runtime, when
a patch only touches specific sqlglot statement lines or tracked component/Expr
classes (and does not edit tests or fixtures), we only need to execute the
fixture items that actually exercise those edited lines or classes while still
running any hand-written assertions outside the fixture loop.

    python -m blast.subcov /opt/sqlglot /app/maps/subcov.json
"""

import ast
import concurrent.futures
import importlib
import json
import os
import sys
import time
import unittest

from . import verify

CO_OPTIMIZED = 0x1
FIXTURE_TEST_FILES = (
    "tests/test_optimizer.py",
    "tests/test_transpile.py",
    "tests/test_serde.py",
)
GEN_MEM_TEST = "tests/test_generator.py::TestGenerator::test_generator_memory"
VALIDATOR_METHODS = ("validate_identity", "validate_all", "validate_transpile")


def _is_const_expr(node):
    if node is None or isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_const_expr(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_const_expr(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_is_const_expr(k) and _is_const_expr(v) for k, v in zip(node.keys, node.values))
    return False


def _is_const_iterable(node):
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_const_expr(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_is_const_expr(k) and _is_const_expr(v) for k, v in zip(node.keys, node.values))
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("range", "enumerate", "zip", "reversed", "sorted")
        and not node.keywords
    ):
        return all(_is_const_iterable(a) or _is_const_expr(a) for a in node.args)
    return False


def _is_subtest_with(node):
    return isinstance(node, ast.With) and all(
        isinstance(it.context_expr, ast.Call)
        and isinstance(it.context_expr.func, ast.Attribute)
        and isinstance(it.context_expr.func.value, ast.Name)
        and it.context_expr.func.value.id == "self"
        and it.context_expr.func.attr == "subTest"
        for it in node.items
    )


def _safe_validator_ranges(fn_node):
    """Return sorted [(start_ln, end_ln)] for standalone void self.validate_* calls."""
    ranges = []
    try_types = (ast.Try, getattr(ast, "TryStar", ast.Try), ast.While)

    def walk(node, parents):
        if isinstance(node, ast.Call):
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "self"
                and f.attr in VALIDATOR_METHODS
                and parents
                and isinstance(parents[-1], ast.Expr)
                and not any(
                    isinstance(a, try_types)
                    or (isinstance(a, ast.With) and not _is_subtest_with(a))
                    or (isinstance(a, ast.For) and not _is_const_iterable(a.iter))
                    for a in parents
                )
            ):
                ranges.append((node.lineno, node.end_lineno or node.lineno))
        for ch in ast.iter_child_nodes(node):
            walk(ch, parents + [node])

    walk(fn_node, [])
    ranges.sort()
    return ranges


def _discover_dialect_targets(tree, actual_ms=None, min_ms=3, min_safe=3):
    """Find dialect test methods with safe standalone Validator calls, grouped by file."""
    ddir = os.path.join(tree, "tests", "dialects")
    if not os.path.isdir(ddir):
        return []
    out = []
    for fname in sorted(os.listdir(ddir)):
        if not (fname.startswith("test_") and fname.endswith(".py")):
            continue
        rel = f"tests/dialects/{fname}"
        full = os.path.join(ddir, fname)
        try:
            src = ast.parse(open(full, encoding="utf-8").read())
        except SyntaxError:
            continue
        methods = {}
        for node in src.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not (isinstance(item, ast.FunctionDef) and item.name.startswith("test_")):
                    continue
                tid = f"{rel}::{node.name}::{item.name}"
                if actual_ms is not None and actual_ms.get(tid, 0) < min_ms:
                    continue
                ranges = _safe_validator_ranges(item)
                if len(ranges) >= min_safe:
                    methods[(node.name, item.name)] = ranges
        if methods:
            out.append((rel, methods))
    return out


SUBTEST_FILES = (
    "tests/test_optimizer.py",
    "tests/test_optimize_cross_joins.py",
    "tests/test_executor.py",
    "tests/test_parser.py",
    "tests/test_expressions.py",
)
_SAFE_OUTER_FUNCS = {
    "duckdb.connect",
    "sqlite3.connect",
    "self.addCleanup",
    "range",
    "len",
    "enumerate",
    "zip",
    "sorted",
    "reversed",
    "list",
    "dict",
    "set",
    "tuple",
    "str",
    "int",
    "float",
    "bool",
    "min",
    "max",
    "sum",
}
_SAFE_OUTER_ATTRS = {"execute", "executescript", "fetchall", "append", "extend", "format", "join"}
_SKIP_SUBTEST_IDS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    "tests/test_parser.py::TestParser::test_max_nodes",
}


def _is_safe_subtest_method(fn):
    """True when outermost `with self.subTest(...)` blocks are pure and loop counts are sqlglot-independent."""
    if fn.decorator_list:
        return False
    sts = []

    def find_st(node, in_st):
        if _is_subtest_with(node):
            if not in_st:
                sts.append(node)
            return
        for ch in ast.iter_child_nodes(node):
            find_st(ch, in_st)

    find_st(fn, False)
    if not sts:
        return False

    st_set = set(sts)
    outer_loads = set()
    outer_ok = [True]

    def check_outer(node):
        if node in st_set:
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            outer_loads.add(node.id)
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                if f.id not in _SAFE_OUTER_FUNCS:
                    outer_ok[0] = False
            elif isinstance(f, ast.Attribute):
                dotted = ast.unparse(f)
                if dotted not in _SAFE_OUTER_FUNCS and f.attr not in _SAFE_OUTER_ATTRS:
                    outer_ok[0] = False
            else:
                outer_ok[0] = False
        for ch in ast.iter_child_nodes(node):
            check_outer(ch)

    check_outer(fn)
    if not outer_ok[0]:
        return False

    bad_nodes = (ast.Return, ast.Yield, ast.YieldFrom, ast.Break, ast.Continue, ast.Global, ast.Nonlocal)
    for st in sts:
        for n in ast.walk(st):
            if isinstance(n, bad_nodes):
                return False
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in outer_loads:
                return False
            if (
                isinstance(n, ast.Attribute)
                and isinstance(n.ctx, (ast.Store, ast.Del))
                and isinstance(n.value, ast.Name)
                and n.value.id == "self"
            ):
                return False
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                u = n.value.upper()
                if any(k in u for k in ("CREATE TABLE", "INSERT INTO", "DROP TABLE", "UPDATE ", "DELETE FROM")):
                    return False
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "super":
                return False
    return True


def _discover_subtest_targets(tree, exclude_ids, actual_ms=None, min_ms=25):
    """Find slow non-fixture test methods with pure outermost self.subTest loops."""
    targets = []
    for rel in SUBTEST_FILES:
        full = os.path.join(tree, rel)
        if not os.path.isfile(full):
            continue
        try:
            src = ast.parse(open(full, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in src.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not (isinstance(item, ast.FunctionDef) and item.name.startswith("test_")):
                    continue
                tid = f"{rel}::{node.name}::{item.name}"
                if tid in exclude_ids or tid in _SKIP_SUBTEST_IDS:
                    continue
                if actual_ms is not None and actual_ms.get(tid, 0) < min_ms:
                    continue
                if _is_safe_subtest_method(item):
                    targets.append(tid)
    return targets


def _discover_targets(tree):
    """Find all test methods calling check_file or load_sql_fixture* plus test_generator_memory."""
    targets = []
    for rel in FIXTURE_TEST_FILES:
        full = os.path.join(tree, rel)
        if not os.path.isfile(full):
            continue
        src = ast.parse(open(full, encoding="utf-8").read())
        for node in src.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not (isinstance(item, ast.FunctionDef) and item.name.startswith("test_")):
                    continue
                hit = False
                for sub in ast.walk(item):
                    if isinstance(sub, ast.Call):
                        fn = sub.func
                        name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                        if name in ("check_file", "load_sql_fixture_pairs", "load_sql_fixtures"):
                            hit = True
                            break
                if hit:
                    targets.append(f"{rel}::{node.name}::{item.name}")
    targets.append(GEN_MEM_TEST)
    return targets


def _build_class_line_map(tree):
    """Map (relpath, lineno) -> tuple of enclosing class qualnames for sqlglot/*.py."""
    out = {}
    root = os.path.join(tree, "sqlglot")
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            full = os.path.join(d, fname)
            rel = os.path.relpath(full, tree)
            mod = rel[:-3]
            if mod.endswith("/__init__"):
                mod = mod[: -len("/__init__")]
            mod = mod.replace("/", ".")
            try:
                src = ast.parse(open(full, encoding="utf-8").read())
            except SyntaxError:
                continue

            def walk(node, chain):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.ClassDef):
                        nchain = chain + [child.name]
                        qual = mod + "." + ".".join(nchain)
                        for ln in range(child.lineno, child.end_lineno + 1):
                            out.setdefault((rel, ln), []).append(qual)
                        walk(child, nchain)
                    else:
                        walk(child, chain)

            walk(src, [])
    return {k: tuple(v) for k, v in out.items()}


def _trace_one_test_child(tree, nid):
    """Run inside a fresh forked child: trace per-item lines and instantiated classes."""
    os.chdir(tree)
    sys.path.insert(0, tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    verify._cheap_pools()

    root_prefix = os.path.realpath(tree) + os.sep
    mon = sys.monitoring
    tool_id = 4
    rel_cache = {}

    def _rel(path):
        rel = rel_cache.get(path)
        if rel is None:
            real = "" if path.startswith("<") else os.path.realpath(path)
            if real.startswith(root_prefix):
                r = real[len(root_prefix):]
                rel = r if r.startswith("sqlglot/") else ""
            else:
                rel = ""
            rel_cache[path] = rel
        return rel

    # Active bucket: either ("setup",), ("item", fixture_key, idx), or None
    cur_lines = None
    cur_types = None
    setup_lines = set()
    setup_types = set()
    # fixtures: {fixture_key: {"n": int, "lines": {idx: set((rel, ln))}, "types": {idx: set(type)}}}
    fixtures = {}

    def _switch(lines_set, types_set):
        nonlocal cur_lines, cur_types
        cur_lines = lines_set
        cur_types = types_set
        if lines_set is not None:
            mon.restart_events()

    def _on_line(code, line, _opt=CO_OPTIMIZED, _dis=mon.DISABLE):
        if cur_lines is None:
            return
        if code.co_flags & _opt:
            rel = _rel(code.co_filename)
            if rel:
                cur_lines.add((rel, line))
        return _dis

    def _on_start(code, offset, _opt=CO_OPTIMIZED, _dis=mon.DISABLE):
        if cur_lines is None:
            return
        if code.co_flags & _opt:
            rel = _rel(code.co_filename)
            if rel:
                cur_lines.add((rel, code.co_firstlineno))
        return _dis

    mon.use_tool_id(tool_id, "subcov")
    mon.register_callback(tool_id, mon.events.LINE, _on_line)
    mon.register_callback(tool_id, mon.events.PY_START, _on_start)
    mon.set_events(tool_id, mon.events.LINE | mon.events.PY_START)

    # Wrap __init__ on component and Expr base classes before running the test
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    def _wrap_init(cls):
        orig = cls.__init__

        def __init__(self, *args, **kwargs):
            if cur_types is not None:
                cur_types.add(type(self))
            res = orig(self, *args, **kwargs)
            if cur_types is not None:
                d = getattr(self, "dialect", None)
                if d is not None:
                    cur_types.add(type(d))
            return res

        cls.__init__ = __init__

    for base_cls in (Dialect, Tokenizer, Parser, Generator, Expr):
        _wrap_init(base_cls)

    fpath, cls_name, meth = nid.split("::")
    mod_name = fpath[:-3].replace("/", ".")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)

    if nid == GEN_MEM_TEST:
        # Trace Dialect.get_or_raise(name) + dialect.generator() per dialect (2 calls each)
        dialects = list(mod.Dialects)
        frec = fixtures.setdefault("__dialects__", {"n": len(dialects), "lines": {}, "types": {}})
        for idx, name in enumerate(dialects):
            ls = frec["lines"].setdefault(idx, set())
            ts = frec["types"].setdefault(idx, set())
            _switch(ls, ts)
            d = mod.Dialect.get_or_raise(name)
            d.generator()
            d.generator()
        _switch(None, None)
    else:
        import tests.helpers as helpers

        orig_pairs = helpers.load_sql_fixture_pairs
        orig_list = helpers.load_sql_fixtures
        cur_item_key = [None]

        def _wrap_loader(orig_fn):
            def wrapped(filename):
                items = list(orig_fn(filename))
                frec = fixtures.setdefault(filename, {"n": len(items), "lines": {}, "types": {}})
                frec["n"] = max(frec["n"], len(items))
                try:
                    for idx, val in enumerate(items):
                        ls = frec["lines"].setdefault(idx, set())
                        ts = frec["types"].setdefault(idx, set())
                        cur_item_key[0] = (filename, idx)
                        _switch(ls, ts)
                        yield val
                finally:
                    cur_item_key[0] = None
                    _switch(None, None)

            return wrapped

        w_pairs = _wrap_loader(orig_pairs)
        w_list = _wrap_loader(orig_list)
        helpers.load_sql_fixture_pairs = w_pairs
        helpers.load_sql_fixtures = w_list
        if hasattr(mod, "load_sql_fixture_pairs"):
            mod.load_sql_fixture_pairs = w_pairs
        if hasattr(mod, "load_sql_fixtures"):
            mod.load_sql_fixtures = w_list

        # Tag futures in InlineExecutor and restore per-item tracing during as_completed
        orig_submit = verify.InlineExecutor.submit

        def tagged_submit(self, fn, /, *args, **kwargs):
            fut = orig_submit(self, fn, *args, **kwargs)
            fut._subcov_key = cur_item_key[0]
            return fut

        verify.InlineExecutor.submit = tagged_submit

        if hasattr(mod, "as_completed"):
            orig_ac = mod.as_completed

            def tracked_ac(fs):
                try:
                    for fut in orig_ac(fs):
                        key = getattr(fut, "_subcov_key", None)
                        if key is not None:
                            fn, idx = key
                            frec = fixtures[fn]
                            _switch(frec["lines"][idx], frec["types"][idx])
                        else:
                            _switch(None, None)
                        yield fut
                finally:
                    _switch(None, None)

            mod.as_completed = tracked_ac

        class SilentResult(unittest.TestResult):
            def addFailure(self, test, err):
                pass

            def addError(self, test, err):
                pass

            def addSubTest(self, test, subtest, err):
                pass

        _switch(setup_lines, setup_types)
        cls.setUpClass()
        _switch(None, None)
        try:
            cls(meth).run(SilentResult())
        finally:
            _switch(None, None)
            try:
                cls.tearDownClass()
            except BaseException:  # noqa: BLE001
                pass
            try:
                cls.doClassCleanups()
            except BaseException:  # noqa: BLE001
                pass

    mon.set_events(tool_id, 0)
    mon.free_tool_id(tool_id)

    def _expand(tset):
        out = set()
        for t in tset:
            for k in t.__mro__:
                if k.__module__.startswith("sqlglot"):
                    out.add(f"{k.__module__}.{k.__qualname__}")
        return sorted(out)

    # Convert to JSON-friendly per-fixture inverted maps
    out_fixtures = {}
    for fname, frec in fixtures.items():
        by_line = {}
        by_cls = {}
        for idx, lset in frec["lines"].items():
            for rel, ln in lset:
                by_line.setdefault(rel, {}).setdefault(ln, []).append(idx)
        for idx, tset in frec["types"].items():
            for qual in _expand(tset):
                by_cls.setdefault(qual, []).append(idx)
        out_fixtures[fname] = {
            "n": frec["n"],
            "lines": {p: {str(ln): sorted(set(v)) for ln, v in d.items()} for p, d in by_line.items()},
            "classes": {q: sorted(set(v)) for q, v in by_cls.items()},
        }

    s_lines = {}
    for rel, ln in setup_lines:
        s_lines.setdefault(rel, []).append(ln)
    return {
        "id": nid,
        "cls": f"{fpath}::{cls_name}",
        "setup_lines": {p: sorted(v) for p, v in s_lines.items()},
        "setup_classes": _expand(setup_types),
        "fixtures": out_fixtures,
    }


def _in_ranges(ln, ranges):
    for a, b in ranges:
        if a <= ln <= b:
            return True
        if a > ln:
            return False
    return False


def _trace_dialect_file_child(tree, rel, methods_safe_ranges):
    """Run inside a fresh forked child: trace per-Validator-call lines & classes for one dialect test file."""
    os.chdir(tree)
    sys.path.insert(0, tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    verify._cheap_pools()

    root_prefix = os.path.realpath(tree) + os.sep
    mon = sys.monitoring
    tool_id = 4
    rel_cache = {}

    def _rel(path):
        r = rel_cache.get(path)
        if r is None:
            real = "" if path.startswith("<") else os.path.realpath(path)
            if real.startswith(root_prefix):
                rr = real[len(root_prefix):]
                r = rr if rr.startswith("sqlglot/") else ""
            else:
                r = ""
            rel_cache[path] = r
        return r

    cur_lines = None
    cur_types = None

    def _switch(lines_set, types_set):
        nonlocal cur_lines, cur_types
        cur_lines = lines_set
        cur_types = types_set
        if lines_set is not None:
            mon.restart_events()

    def _on_line(code, line, _opt=CO_OPTIMIZED, _dis=mon.DISABLE):
        if cur_lines is None:
            return
        if code.co_flags & _opt:
            r = _rel(code.co_filename)
            if r:
                cur_lines.add((r, line))
        return _dis

    def _on_start(code, offset, _opt=CO_OPTIMIZED, _dis=mon.DISABLE):
        if cur_lines is None:
            return
        if code.co_flags & _opt:
            r = _rel(code.co_filename)
            if r:
                cur_lines.add((r, code.co_firstlineno))
        return _dis

    mon.use_tool_id(tool_id, "subcov")
    mon.register_callback(tool_id, mon.events.LINE, _on_line)
    mon.register_callback(tool_id, mon.events.PY_START, _on_start)
    mon.set_events(tool_id, mon.events.LINE | mon.events.PY_START)

    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    def _wrap_init(cls):
        orig = cls.__init__

        def __init__(self, *args, **kwargs):
            if cur_types is not None:
                cur_types.add(type(self))
            res = orig(self, *args, **kwargs)
            if cur_types is not None:
                d = getattr(self, "dialect", None)
                if d is not None:
                    cur_types.add(type(d))
            return res

        cls.__init__ = __init__

    for base_cls in (Dialect, Tokenizer, Parser, Generator, Expr):
        _wrap_init(base_cls)

    from tests.dialects.test_dialect import Validator

    state = {"code": None, "ranges": (), "lines": None, "types": None, "unsafe": set()}

    def _wrap_val(mname):
        orig = getattr(Validator, mname)

        def wrapped(self, *args, **kwargs):
            code = state["code"]
            if code is not None:
                frame = sys._getframe(1)
                if frame.f_code is code:
                    ln = frame.f_lineno
                    if _in_ranges(ln, state["ranges"]):
                        ls = state["lines"].setdefault(ln, set())
                        ts = state["types"].setdefault(ln, set())
                        _switch(ls, ts)
                        try:
                            return orig(self, *args, **kwargs)
                        finally:
                            _switch(None, None)
                    else:
                        state["unsafe"].add(ln)
            return orig(self, *args, **kwargs)

        setattr(Validator, mname, wrapped)

    for mname in VALIDATOR_METHODS:
        _wrap_val(mname)

    class SilentResult(unittest.TestResult):
        def addFailure(self, test, err):
            pass

        def addError(self, test, err):
            pass

        def addSubTest(self, test, subtest, err):
            pass

    mod_name = rel[:-3].replace("/", ".")
    mod = importlib.import_module(mod_name)

    def _expand(tset):
        out = set()
        for t in tset:
            for k in t.__mro__:
                if k.__module__.startswith("sqlglot"):
                    out.add(f"{k.__module__}.{k.__qualname__}")
        return sorted(out)

    # Group by class
    by_cls_methods = {}
    for (cls_name, meth), ranges in methods_safe_ranges.items():
        by_cls_methods.setdefault(cls_name, []).append((meth, ranges))

    records = []
    for cls_name, mlist in by_cls_methods.items():
        cls = getattr(mod, cls_name)
        setup_lines = set()
        setup_types = set()
        _switch(setup_lines, setup_types)
        cls.setUpClass()
        _switch(None, None)
        s_lines = {}
        for r, ln in setup_lines:
            s_lines.setdefault(r, []).append(ln)
        s_lines_out = {p: sorted(v) for p, v in s_lines.items()}
        s_cls_out = _expand(setup_types)
        try:
            for meth, ranges in mlist:
                call_lines = {}
                call_types = {}
                state["code"] = getattr(cls, meth).__code__
                state["ranges"] = ranges
                state["lines"] = call_lines
                state["types"] = call_types
                state["unsafe"] = set()
                try:
                    cls(meth).run(SilentResult())
                finally:
                    state["code"] = None
                    _switch(None, None)

                skippable = sorted(call_lines.keys())
                if len(skippable) < 2:
                    continue
                by_line = {}
                by_cls = {}
                for ln_call, lset in call_lines.items():
                    for r, ln in lset:
                        by_line.setdefault(r, {}).setdefault(ln, []).append(ln_call)
                for ln_call, tset in call_types.items():
                    for qual in _expand(tset):
                        by_cls.setdefault(qual, []).append(ln_call)
                records.append({
                    "id": f"{rel}::{cls_name}::{meth}",
                    "cls": f"{rel}::{cls_name}",
                    "setup_lines": s_lines_out,
                    "setup_classes": s_cls_out,
                    "fixtures": {
                        "__vcalls__": {
                            "n": len(skippable) + len(state["unsafe"]),
                            "skippable": skippable,
                            "lines": {
                                p: {str(ln): sorted(set(v)) for ln, v in d.items()}
                                for p, d in by_line.items()
                            },
                            "classes": {q: sorted(set(v)) for q, v in by_cls.items()},
                        }
                    },
                })
        finally:
            try:
                cls.tearDownClass()
            except BaseException:  # noqa: BLE001
                pass
            try:
                cls.doClassCleanups()
            except BaseException:  # noqa: BLE001
                pass

    mon.set_events(tool_id, 0)
    mon.free_tool_id(tool_id)
    return records


def _trace_subtest_child(tree, nid):
    """Run inside a fresh forked child: trace per-subTest lines & instantiated classes."""
    os.chdir(tree)
    sys.path.insert(0, tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    verify._cheap_pools()

    root_prefix = os.path.realpath(tree) + os.sep
    mon = sys.monitoring
    tool_id = 4
    rel_cache = {}

    def _rel(path):
        r = rel_cache.get(path)
        if r is None:
            real = "" if path.startswith("<") else os.path.realpath(path)
            if real.startswith(root_prefix):
                rr = real[len(root_prefix):]
                r = rr if rr.startswith("sqlglot/") else ""
            else:
                r = ""
            rel_cache[path] = r
        return r

    cur_lines = None
    cur_types = None
    setup_lines = set()
    setup_types = set()
    pre_lines = set()
    pre_types = set()
    st_lines = {}
    st_types = {}

    def _switch(lines_set, types_set):
        nonlocal cur_lines, cur_types
        cur_lines = lines_set
        cur_types = types_set
        if lines_set is not None:
            mon.restart_events()

    def _on_line(code, line, _opt=CO_OPTIMIZED, _dis=mon.DISABLE):
        if cur_lines is None:
            return
        if code.co_flags & _opt:
            r = _rel(code.co_filename)
            if r:
                cur_lines.add((r, line))
        return _dis

    def _on_start(code, offset, _opt=CO_OPTIMIZED, _dis=mon.DISABLE):
        if cur_lines is None:
            return
        if code.co_flags & _opt:
            r = _rel(code.co_filename)
            if r:
                cur_lines.add((r, code.co_firstlineno))
        return _dis

    mon.use_tool_id(tool_id, "subcov")
    mon.register_callback(tool_id, mon.events.LINE, _on_line)
    mon.register_callback(tool_id, mon.events.PY_START, _on_start)
    mon.set_events(tool_id, mon.events.LINE | mon.events.PY_START)

    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    def _wrap_init(cls):
        orig = cls.__init__

        def __init__(self, *args, **kwargs):
            if cur_types is not None:
                cur_types.add(type(self))
            res = orig(self, *args, **kwargs)
            if cur_types is not None:
                d = getattr(self, "dialect", None)
                if d is not None:
                    cur_types.add(type(d))
            return res

        cls.__init__ = __init__

    for base_cls in (Dialect, Tokenizer, Parser, Generator, Expr):
        _wrap_init(base_cls)

    fpath, cls_name, meth = nid.split("::")
    mod_name = fpath[:-3].replace("/", ".")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)

    if mod_name == "tests.test_executor" and cls_name == "TestExecutor":
        verify._patch_light_executor(mod, cls, [meth])

    class _STCtx:
        def __init__(self, idx):
            self.idx = idx

        def __enter__(self):
            ls = st_lines.setdefault(self.idx, set())
            ts = st_types.setdefault(self.idx, set())
            _switch(ls, ts)

        def __exit__(self, *exc):
            _switch(pre_lines, pre_types)
            return False

    class SilentResult(unittest.TestResult):
        def addFailure(self, test, err):
            pass

        def addError(self, test, err):
            pass

        def addSubTest(self, test, subtest, err):
            pass

    orig_meth = cls.__dict__.get(meth)
    rewritten = verify.rewrite_subtest_method(cls, meth, "trace", _STCtx)
    setattr(cls, meth, rewritten)

    _switch(setup_lines, setup_types)
    cls.setUpClass()
    _switch(None, None)
    try:
        _switch(pre_lines, pre_types)
        cls(meth).run(SilentResult())
    finally:
        _switch(None, None)
        if orig_meth is not None:
            setattr(cls, meth, orig_meth)
        try:
            cls.tearDownClass()
        except BaseException:  # noqa: BLE001
            pass
        try:
            cls.doClassCleanups()
        except BaseException:  # noqa: BLE001
            pass

    mon.set_events(tool_id, 0)
    mon.free_tool_id(tool_id)

    def _expand(tset):
        out = set()
        for t in tset:
            for k in t.__mro__:
                if k.__module__.startswith("sqlglot"):
                    out.add(f"{k.__module__}.{k.__qualname__}")
        return sorted(out)

    if len(st_lines) < 2:
        return []

    by_line = {}
    by_cls = {}
    for idx, lset in st_lines.items():
        for r, ln in lset:
            by_line.setdefault(r, {}).setdefault(ln, []).append(idx)
    for idx, tset in st_types.items():
        for qual in _expand(tset):
            by_cls.setdefault(qual, []).append(idx)

    s_lines = {}
    for r, ln in setup_lines:
        s_lines.setdefault(r, []).append(ln)
    p_lines = {}
    for r, ln in pre_lines:
        p_lines.setdefault(r, []).append(ln)

    rec = {
        "id": nid,
        "cls": f"{fpath}::{cls_name}",
        "setup_lines": {p: sorted(v) for p, v in s_lines.items()},
        "setup_classes": _expand(setup_types),
        "fixtures": {
            "__subtests__": {
                "n": len(st_lines),
                "lines": {p: {str(ln): sorted(set(v)) for ln, v in d.items()} for p, d in by_line.items()},
                "classes": {q: sorted(set(v)) for q, v in by_cls.items()},
            }
        },
    }
    if p_lines or pre_types:
        rec["pre_lines"] = {p: sorted(v) for p, v in p_lines.items()}
        rec["pre_classes"] = _expand(pre_types)
    return [rec]


def _discover_block_targets(tree, exclude_ids, actual_ms=None, min_ms=40):
    """Find slow uncovered test methods whose statements partition into independent def-use blocks."""
    targets = []
    troot = os.path.join(tree, "tests")
    for d, dirs, files in os.walk(troot):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", "fixtures")]
        for fname in sorted(files):
            if not (fname.startswith("test_") and fname.endswith(".py")):
                continue
            full = os.path.join(d, fname)
            rel = os.path.relpath(full, tree)
            try:
                src = ast.parse(open(full, encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in src.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                for item in node.body:
                    if not (isinstance(item, ast.FunctionDef) and item.name.startswith("test_")):
                        continue
                    tid = f"{rel}::{node.name}::{item.name}"
                    if tid in exclude_ids or tid in _SKIP_SUBTEST_IDS:
                        continue
                    if actual_ms is not None and actual_ms.get(tid, 0) < min_ms:
                        continue
                    if verify.partition_stmts(item) is not None:
                        targets.append(tid)
    return targets


def _trace_blocks_child(tree, nid):
    """Run inside a fresh forked child: trace per-block lines, classes, and elapsed ms."""
    os.chdir(tree)
    sys.path.insert(0, tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    verify._cheap_pools()

    root_prefix = os.path.realpath(tree) + os.sep
    mon = sys.monitoring
    tool_id = 4
    rel_cache = {}

    def _rel(path):
        r = rel_cache.get(path)
        if r is None:
            real = "" if path.startswith("<") else os.path.realpath(path)
            if real.startswith(root_prefix):
                rr = real[len(root_prefix):]
                r = rr if rr.startswith("sqlglot/") else ""
            else:
                r = ""
            rel_cache[path] = r
        return r

    cur_lines = None
    cur_types = None
    setup_lines = set()
    setup_types = set()
    blk_lines = {}
    blk_types = {}
    blk_ms = {}

    def _switch(lines_set, types_set):
        nonlocal cur_lines, cur_types
        cur_lines = lines_set
        cur_types = types_set
        if lines_set is not None:
            mon.restart_events()

    def _on_line(code, line, _opt=CO_OPTIMIZED, _dis=mon.DISABLE):
        if cur_lines is None:
            return
        if code.co_flags & _opt:
            r = _rel(code.co_filename)
            if r:
                cur_lines.add((r, line))
        return _dis

    def _on_start(code, offset, _opt=CO_OPTIMIZED, _dis=mon.DISABLE):
        if cur_lines is None:
            return
        if code.co_flags & _opt:
            r = _rel(code.co_filename)
            if r:
                cur_lines.add((r, code.co_firstlineno))
        return _dis

    mon.use_tool_id(tool_id, "subcov")
    mon.register_callback(tool_id, mon.events.LINE, _on_line)
    mon.register_callback(tool_id, mon.events.PY_START, _on_start)
    mon.set_events(tool_id, mon.events.LINE | mon.events.PY_START)

    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    def _wrap_init(cls):
        orig = cls.__init__

        def __init__(self, *args, **kwargs):
            if cur_types is not None:
                cur_types.add(type(self))
            res = orig(self, *args, **kwargs)
            if cur_types is not None:
                d = getattr(self, "dialect", None)
                if d is not None:
                    cur_types.add(type(d))
            return res

        cls.__init__ = __init__

    for base_cls in (Dialect, Tokenizer, Parser, Generator, Expr):
        _wrap_init(base_cls)

    fpath, cls_name, meth = nid.split("::")
    mod_name = fpath[:-3].replace("/", ".")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)

    if mod_name == "tests.test_executor" and cls_name == "TestExecutor":
        verify._patch_light_executor(mod, cls, [meth])

    class _BlkCtx:
        def __init__(self, idx):
            self.idx = idx
            self.t0 = 0.0

        def __enter__(self):
            ls = blk_lines.setdefault(self.idx, set())
            ts = blk_types.setdefault(self.idx, set())
            _switch(ls, ts)
            self.t0 = time.perf_counter()

        def __exit__(self, *exc):
            blk_ms[self.idx] = blk_ms.get(self.idx, 0.0) + (time.perf_counter() - self.t0) * 1000.0
            _switch(None, None)
            return False

    class SilentResult(unittest.TestResult):
        def addFailure(self, test, err):
            pass

        def addError(self, test, err):
            pass

        def addSubTest(self, test, subtest, err):
            pass

    orig_meth = cls.__dict__.get(meth)
    rewritten = verify.rewrite_blocks_method(cls, meth, "trace", _BlkCtx)
    if rewritten is None:
        mon.set_events(tool_id, 0)
        mon.free_tool_id(tool_id)
        return []
    setattr(cls, meth, rewritten)

    _switch(setup_lines, setup_types)
    cls.setUpClass()
    _switch(None, None)
    try:
        cls(meth).run(SilentResult())
    finally:
        _switch(None, None)
        if orig_meth is not None:
            setattr(cls, meth, orig_meth)
        try:
            cls.tearDownClass()
        except BaseException:  # noqa: BLE001
            pass
        try:
            cls.doClassCleanups()
        except BaseException:  # noqa: BLE001
            pass

    mon.set_events(tool_id, 0)
    mon.free_tool_id(tool_id)

    def _expand(tset):
        out = set()
        for t in tset:
            for k in t.__mro__:
                if k.__module__.startswith("sqlglot"):
                    out.add(f"{k.__module__}.{k.__qualname__}")
        return sorted(out)

    if len(blk_lines) < 2:
        return []

    n_blk = max(blk_lines.keys()) + 1
    by_line = {}
    by_cls = {}
    for idx, lset in blk_lines.items():
        for r, ln in lset:
            by_line.setdefault(r, {}).setdefault(ln, []).append(idx)
    for idx, tset in blk_types.items():
        for qual in _expand(tset):
            by_cls.setdefault(qual, []).append(idx)

    s_lines = {}
    for r, ln in setup_lines:
        s_lines.setdefault(r, []).append(ln)

    weights = [round(max(0.1, blk_ms.get(i, 0.1)), 1) for i in range(n_blk)]
    return [
        {
            "id": nid,
            "cls": f"{fpath}::{cls_name}",
            "setup_lines": {p: sorted(v) for p, v in s_lines.items()},
            "setup_classes": _expand(setup_types),
            "fixtures": {
                "__blocks__": {
                    "n": n_blk,
                    "w": weights,
                    "lines": {
                        p: {str(ln): sorted(set(v)) for ln, v in d.items()} for p, d in by_line.items()
                    },
                    "classes": {q: sorted(set(v)) for q, v in by_cls.items()},
                }
            },
        }
    ]


def _spawn_one(tree, job):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            if job[0] == "test":
                res = [_trace_one_test_child(tree, job[1])]
            elif job[0] == "subtest":
                res = _trace_subtest_child(tree, job[1])
            elif job[0] == "blocks":
                res = _trace_blocks_child(tree, job[1])
            else:
                res = _trace_dialect_file_child(tree, job[1], job[2])
            data = json.dumps(res, separators=(",", ":")).encode()
            view = memoryview(data)
            off = 0
            while off < len(view):
                off += os.write(w, view[off:])
        except BaseException:  # noqa: BLE001
            pass
        finally:
            os._exit(0)
    os.close(w)
    return pid, r


def _run_fork_pool(tree, jobs, n_workers):
    import select

    pending = list(jobs)
    active = {}  # fd -> (pid, label, bytearray)
    results = []
    while pending or active:
        while pending and len(active) < n_workers:
            job = pending.pop(0)
            pid, fd = _spawn_one(tree, job)
            active[fd] = (pid, job[1], bytearray())
        ready, _, _ = select.select(list(active.keys()), [], [])
        for fd in ready:
            pid, label, buf = active[fd]
            chunk = os.read(fd, 1 << 20)
            if chunk:
                buf.extend(chunk)
            else:
                os.close(fd)
                os.waitpid(pid, 0)
                del active[fd]
                if not buf:
                    raise RuntimeError(f"subcov child failed for {label}")
                results.extend(json.loads(buf))
    return results


def main():
    t0 = time.monotonic()
    tree, out_path = sys.argv[1], sys.argv[2]
    actual_ms = None
    if len(sys.argv) >= 4 and os.path.isfile(sys.argv[3]):
        actual_ms = json.load(open(sys.argv[3])).get("tests")

    targets = _discover_targets(tree)
    subtest_targets = _discover_subtest_targets(tree, set(targets), actual_ms=actual_ms, min_ms=25)
    dialect_targets = _discover_dialect_targets(tree, actual_ms=actual_ms, min_ms=3, min_safe=3)
    covered = set(targets) | set(subtest_targets) | {
        f"{rel}::{cname}::{mname}"
        for rel, methods in dialect_targets
        for (cname, mname) in methods
    }
    block_targets = _discover_block_targets(tree, covered, actual_ms=actual_ms, min_ms=40)
    cls_line_map = _build_class_line_map(tree)

    jobs = (
        [("test", nid) for nid in targets]
        + [("subtest", nid) for nid in subtest_targets]
        + [("blocks", nid) for nid in block_targets]
        + [("dialect_file", rel, methods) for rel, methods in dialect_targets]
    )
    n_workers = min(4, max(1, os.cpu_count() or 1))
    results = _run_fork_pool(tree, jobs, n_workers)

    # Deduplicate index sets into a compact pool
    pool_sets = []
    pool_idx = {}

    def intern(lst):
        key = tuple(lst)
        idx = pool_idx.get(key)
        if idx is None:
            idx = len(pool_sets)
            pool_idx[key] = idx
            pool_sets.append(list(key))
        return idx

    setup_by_cls = {}
    tests_out = {}
    for rec in results:
        cls_key = rec["cls"]
        sb = setup_by_cls.setdefault(cls_key, {"lines": {}, "classes": set()})
        for p, lns in rec["setup_lines"].items():
            sb["lines"].setdefault(p, set()).update(lns)
        sb["classes"].update(rec["setup_classes"])

        fix_out = {}
        for fname, fdata in rec["fixtures"].items():
            # Also fold class-body executed lines (e.g. @classmethod/@staticmethod) into classes
            cls_items = {q: set(v) for q, v in fdata["classes"].items()}
            lines_out = {}
            for p, lmap in fdata["lines"].items():
                po = {}
                for ln_s, idxs in lmap.items():
                    po[ln_s] = intern(idxs)
                    quals = cls_line_map.get((p, int(ln_s)))
                    if quals:
                        for q in quals:
                            cls_items.setdefault(q, set()).update(idxs)
                lines_out[p] = po
            cls_out = {q: intern(sorted(v)) for q, v in cls_items.items()}
            entry = {"n": fdata["n"], "lines": lines_out, "classes": cls_out}
            if "skippable" in fdata:
                entry["skippable"] = fdata["skippable"]
            if "w" in fdata:
                entry["w"] = fdata["w"]
            fix_out[fname] = entry
        t_entry = {"cls": cls_key, "fixtures": fix_out}
        if "pre_lines" in rec or "pre_classes" in rec:
            t_entry["pre_lines"] = rec.get("pre_lines", {})
            t_entry["pre_classes"] = rec.get("pre_classes", [])
        tests_out[rec["id"]] = t_entry

    setup_out = {
        k: {"lines": {p: sorted(v) for p, v in d["lines"].items()}, "classes": sorted(d["classes"])}
        for k, d in setup_by_cls.items()
    }
    payload = {"sets": pool_sets, "setup": setup_out, "tests": tests_out}
    with open(out_path, "w") as f:
        json.dump(payload, f, sort_keys=True, separators=(",", ":"))
    dt = time.monotonic() - t0
    sz = os.path.getsize(out_path) / (1024 * 1024)
    print(
        f"subcov traced {len(tests_out)} tests ({len(pool_sets)} unique item sets) "
        f"in {dt:.1f}s -> {sz:.2f} MB",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
