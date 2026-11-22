"""Run a set of sqlglot tests against a patched tree and report what failed.

The parent process imports unittest (and heavy third-party libraries on demand)
once, then forks one child per commit. The child points sys.path at the patched
tree, imports sqlglot fresh from there, and runs the requested unittest.TestCase
methods directly without pytest startup or traceback-formatting overhead.

Child protocol (one line each):
    F <nodeid>    this test failed (setup, call, teardown, class setup, or subtest)
    D <nodeid>    the test finished
    C <path>      importing this test file failed
"""

import os
import select
import signal
import sys
import time
import unittest

NULL = os.open(os.devnull, os.O_RDWR)
_HEAVY_WARMED = False
TPC_TESTS = {"test_execute_tpch", "test_execute_tpcds", "test_optimized_tpch"}
HEAVY_FILES = ("tests/test_executor.py", "tests/test_optimizer.py")


class _SubTestBailout(BaseException):
    """Stop a test on its first failing subtest; one failing subtest fails the test."""


class _LazyFuture:
    """Deferred future for InlineExecutor: runs fn(*args, **kwargs) on .result()."""

    def __init__(self, fn, args, kwargs):
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._done = False
        self._val = None
        self._exc = None

    def result(self, timeout=None):
        if not self._done:
            self._done = True
            try:
                self._val = self._fn(*self._args, **self._kwargs)
            except BaseException as e:  # noqa: BLE001
                self._exc = e
        if self._exc is not None:
            raise self._exc
        return self._val

    def exception(self, timeout=None):
        try:
            self.result()
            return None
        except BaseException as e:  # noqa: BLE001
            return e

    def done(self):
        return True


class InlineExecutor:
    """ProcessPoolExecutor that runs pure tasks lazily in-process.

    Deferring execution until future.result() lets _SubTestBailout stop a
    fixture test on its first failing query instead of executing every query
    in the fixture upfront.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        return _LazyFuture(fn, args, kwargs)

    def map(self, fn, *iterables, **_):
        return (fn(*a) for a in zip(*iterables))

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _cheap_pools():
    """Replace spawn pools with inline execution (or fork if requested)."""
    import multiprocessing

    real_get_context = multiprocessing.get_context

    def get_context(method=None):
        if method == "spawn":
            method = "fork"
        return real_get_context(method)

    multiprocessing.get_context = get_context
    os.process_cpu_count = lambda: 2
    os.cpu_count = lambda: 2
    if os.environ.get("BLAST_POOL", "inline") == "inline":
        import concurrent.futures

        concurrent.futures.ProcessPoolExecutor = InlineExecutor
        concurrent.futures.as_completed = lambda fs, timeout=None: iter(fs)
        concurrent.futures.wait = lambda fs, timeout=None, return_when=None: (set(fs), set())


def _patch_light_executor(mod, cls, method_names):
    """Skip loading TPC-H / TPC-DS tables when none of the 3 TPC tests are run.

    TestExecutor.setUpClass parses every TPC-H and TPC-DS table (~3 s CPU) for
    all 60 tests of the class, though only the three TPC tests touch them.
    """
    if any(m in TPC_TESTS for m in method_names):
        return

    def setUpClass(klass):
        klass.tpch_conn = mod.duckdb.connect()
        klass.tpcds_conn = mod.duckdb.connect()
        klass.tpch_tables, klass.tpcds_tables, klass.cache = {}, {}, {}
        klass.tpch_sqls = list(mod.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        klass.tpcds_sqls = list(mod.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))

    cls.setUpClass = classmethod(setUpClass)


class _FastResult(unittest.TestResult):
    """unittest.TestResult that streams F/D lines with zero traceback formatting."""

    def __init__(self, fd, stop_on_fail=False):
        super().__init__()
        self.fd = fd
        self.stop_on_fail = stop_on_fail
        self.cur_nodeid = None
        self.cur_failed = False
        self.any_failed = False

    def _send(self, tag, text):
        os.write(self.fd, f"{tag} {text}\n".encode("utf-8", "replace"))

    def _mark_fail(self):
        if not self.cur_failed and self.cur_nodeid is not None:
            self.cur_failed = True
            self.any_failed = True
            self._send("F", self.cur_nodeid)
            if self.stop_on_fail:
                self.shouldStop = True

    def addFailure(self, test, err):
        self._mark_fail()

    def addError(self, test, err):
        self._mark_fail()

    def addUnexpectedSuccess(self, test):
        self._mark_fail()

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark_fail()
            raise _SubTestBailout()


def rewrite_subtest_method(cls, meth, mode, hook):
    """Return a compiled copy of cls.meth wrapping outermost `with self.subTest(...)` blocks.

    mode == "trace": wraps each outermost subTest block in `with hook(idx):`.
    mode == "filter": executes each outermost subTest block only when `hook(idx)` is truthy.
    """
    import ast
    import inspect
    import textwrap

    fn = getattr(cls, meth)
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    fdef = tree.body[0]
    fdef.decorator_list = []

    counter_name = "__blast_st_idx"
    hook_name = "__blast_st_hook"

    class _Rewriter(ast.NodeTransformer):
        def visit_With(self, node):
            is_st = any(
                isinstance(it.context_expr, ast.Call)
                and isinstance(it.context_expr.func, ast.Attribute)
                and isinstance(it.context_expr.func.value, ast.Name)
                and it.context_expr.func.value.id == "self"
                and it.context_expr.func.attr == "subTest"
                for it in node.items
            )
            if not is_st:
                return self.generic_visit(node)
            inc = ast.AugAssign(
                target=ast.Name(id=counter_name, ctx=ast.Store()),
                op=ast.Add(),
                value=ast.Constant(value=1),
            )
            idx_expr = ast.BinOp(
                left=ast.Name(id=counter_name, ctx=ast.Load()),
                op=ast.Sub(),
                right=ast.Constant(value=1),
            )
            call = ast.Call(
                func=ast.Name(id=hook_name, ctx=ast.Load()),
                args=[idx_expr],
                keywords=[],
            )
            if mode == "trace":
                wrapped = ast.With(
                    items=[ast.withitem(context_expr=call, optional_vars=None)],
                    body=[node],
                )
            else:
                wrapped = ast.If(test=call, body=[node], orelse=[])
            return [inc, wrapped]

    fdef = _Rewriter().visit(fdef)
    init = ast.Assign(
        targets=[ast.Name(id=counter_name, ctx=ast.Store())],
        value=ast.Constant(value=0),
    )
    fdef.body.insert(0, init)
    ast.fix_missing_locations(tree)

    ns = dict(fn.__globals__)
    ns[hook_name] = hook
    exec(compile(tree, f"<subtest:{meth}>", "exec"), ns)  # noqa: S102
    return ns[fdef.name]


_UNSAFE_BLOCK_NAMES = {"mock", "patch", "os", "sys", "duckdb", "sqlite3", "warnings", "logging"}
_UNSAFE_BLOCK_ATTRS = {
    "addCleanup",
    "patch",
    "environ",
    "setUp",
    "tearDown",
    "connect",
    "execute",
    "executescript",
}


def partition_stmts(fn):
    """Partition top-level statements of a test method into def-use independent components.

    Returns (stmt_comp_ids, num_components) where stmt_comp_ids[i] is -1 for a leading
    docstring or 0..K-1 for its component ID, or None if unsafe or fewer than 2 components.
    """
    import ast

    if fn.decorator_list:
        return None
    stmts = fn.body
    if len(stmts) < 2:
        return None

    bad_ctrl = (
        ast.Global,
        ast.Nonlocal,
        ast.Return,
        ast.Yield,
        ast.YieldFrom,
        ast.Try,
        getattr(ast, "TryStar", ast.Try),
    )

    def _stmt_info(stmt):
        defs, uses = set(), set()
        unsafe = False

        def walk(node):
            nonlocal unsafe
            if isinstance(node, bad_ctrl):
                unsafe = True
                return
            if isinstance(node, ast.Attribute):
                if isinstance(node.ctx, (ast.Store, ast.Del)) or node.attr in _UNSAFE_BLOCK_ATTRS:
                    unsafe = True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defs.add(node.name)
                for d in node.decorator_list:
                    walk(d)
                if isinstance(node, ast.ClassDef):
                    for b in node.bases:
                        walk(b)
                    for kw in node.keywords:
                        walk(kw)
                else:
                    for d in node.args.defaults:
                        walk(d)
                    for d in node.args.kw_defaults:
                        if d is not None:
                            walk(d)
                for nd in ast.walk(node):
                    if isinstance(nd, (ast.Global, ast.Nonlocal)):
                        unsafe = True
                    elif isinstance(nd, ast.Attribute) and (
                        isinstance(nd.ctx, (ast.Store, ast.Del)) or nd.attr in _UNSAFE_BLOCK_ATTRS
                    ):
                        unsafe = True
                    elif isinstance(nd, ast.Name):
                        if nd.id in _UNSAFE_BLOCK_NAMES:
                            unsafe = True
                        elif isinstance(nd.ctx, ast.Load):
                            uses.add(nd.id)
                return
            if isinstance(node, ast.Lambda):
                for d in node.args.defaults:
                    walk(d)
                for d in node.args.kw_defaults:
                    if d is not None:
                        walk(d)
                args = {a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}
                if node.args.vararg:
                    args.add(node.args.vararg.arg)
                if node.args.kwarg:
                    args.add(node.args.kwarg.arg)
                for nd in ast.walk(node.body):
                    if isinstance(nd, ast.Attribute) and (
                        isinstance(nd.ctx, (ast.Store, ast.Del)) or nd.attr in _UNSAFE_BLOCK_ATTRS
                    ):
                        unsafe = True
                    elif isinstance(nd, ast.Name):
                        if nd.id in _UNSAFE_BLOCK_NAMES:
                            unsafe = True
                        elif isinstance(nd.ctx, ast.Load) and nd.id not in args:
                            uses.add(nd.id)
                return
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                comp_locals = set()
                for idx, gen in enumerate(node.generators):
                    if idx == 0:
                        walk(gen.iter)
                    else:
                        for nd in ast.walk(gen.iter):
                            if isinstance(nd, ast.Name) and isinstance(nd.ctx, ast.Load) and nd.id not in comp_locals:
                                uses.add(nd.id)
                    for nd in ast.walk(gen.target):
                        if isinstance(nd, ast.Name):
                            comp_locals.add(nd.id)
                    for ifc in gen.ifs:
                        for nd in ast.walk(ifc):
                            if isinstance(nd, ast.Name) and isinstance(nd.ctx, ast.Load) and nd.id not in comp_locals:
                                uses.add(nd.id)
                elts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
                for e in elts:
                    for nd in ast.walk(e):
                        if isinstance(nd, ast.Attribute) and (
                            isinstance(nd.ctx, (ast.Store, ast.Del)) or nd.attr in _UNSAFE_BLOCK_ATTRS
                        ):
                            unsafe = True
                        elif isinstance(nd, ast.Name):
                            if nd.id in _UNSAFE_BLOCK_NAMES:
                                unsafe = True
                            elif isinstance(nd.ctx, ast.Load) and nd.id not in comp_locals:
                                uses.add(nd.id)
                return
            if isinstance(node, ast.Name):
                if node.id in _UNSAFE_BLOCK_NAMES:
                    unsafe = True
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    defs.add(node.id)
                else:
                    uses.add(node.id)
                return
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    defs.add((a.asname or a.name).split(".")[0])
                return
            for ch in ast.iter_child_nodes(node):
                walk(ch)

        walk(stmt)
        uncond = set()
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    uncond.add(t.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            uncond.add(stmt.target.id)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            uncond.add(stmt.name)
        return defs, uses, uncond, unsafe

    n = len(stmts)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    is_doc = [False] * n
    active_def = {}
    for i, s in enumerate(stmts):
        if i == 0 and isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            is_doc[i] = True
            continue
        defs, uses, uncond, unsafe = _stmt_info(s)
        if unsafe:
            return None
        # Reject mutating a subscript whose base is not a locally defined variable
        for nd in ast.walk(s):
            if isinstance(nd, ast.Subscript) and isinstance(nd.ctx, (ast.Store, ast.Del)):
                base = nd.value
                while isinstance(base, (ast.Subscript, ast.Attribute)):
                    base = base.value
                if not (isinstance(base, ast.Name) and base.id in active_def):
                    return None
        for u in uses:
            if u in active_def:
                for d_idx in active_def[u]:
                    union(i, d_idx)
        for d in defs:
            if d in uncond:
                active_def[d] = [i]
            else:
                active_def.setdefault(d, []).append(i)
                for d_idx in active_def[d]:
                    union(i, d_idx)

    comp_map = {}
    stmt_comp = []
    for i in range(n):
        if is_doc[i]:
            stmt_comp.append(-1)
            continue
        r = find(i)
        cid = comp_map.get(r)
        if cid is None:
            cid = len(comp_map)
            comp_map[r] = cid
        stmt_comp.append(cid)
    if len(comp_map) < 2:
        return None
    return stmt_comp, len(comp_map)


def rewrite_blocks_method(cls, meth, mode, hook):
    """Return a compiled copy of cls.meth wrapping def-use independent statement blocks.

    mode == "trace": wraps each contiguous run of component c in `with hook(c):`.
    mode == "filter": wraps each contiguous run of component c in `if hook(c):`.
    """
    import ast
    import inspect
    import textwrap

    fn = getattr(cls, meth)
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    fdef = tree.body[0]
    part = partition_stmts(fdef)
    if part is None:
        return None
    stmt_comp, _n_comp = part
    hook_name = "__blast_blk_hook"

    new_body = []
    i = 0
    n = len(fdef.body)
    while i < n:
        c = stmt_comp[i]
        if c < 0:
            new_body.append(fdef.body[i])
            i += 1
            continue
        j = i + 1
        while j < n and stmt_comp[j] == c:
            j += 1
        chunk = fdef.body[i:j]
        call = ast.Call(
            func=ast.Name(id=hook_name, ctx=ast.Load()),
            args=[ast.Constant(value=c)],
            keywords=[],
        )
        if mode == "trace":
            wrapped = ast.With(items=[ast.withitem(context_expr=call, optional_vars=None)], body=chunk)
        else:
            wrapped = ast.If(test=call, body=chunk, orelse=[])
        new_body.append(wrapped)
        i = j

    fdef.body = new_body
    ast.fix_missing_locations(tree)
    ns = dict(fn.__globals__)
    ns[hook_name] = hook
    exec(compile(tree, f"<blocks:{meth}>", "exec"), ns)  # noqa: S102
    return ns[fdef.name]


def _apply_item_filter(mod, cls, meth, flt):
    """Install temporary fixture/dialect/validator/subtest/block filters for one test method; return restore callback."""
    restores = []
    if flt.get("__fast_max_nodes__") and meth == "test_max_nodes" and meth in cls.__dict__:
        orig_meth = cls.__dict__[meth]

        def _fast_max_nodes(self):
            parse_one = mod.parse_one
            exp = mod.exp
            ParseError = mod.ParseError
            self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

            cols = [f"col{i}" for i in range(500)]
            sql = f"SELECT {','.join(cols)} FROM t"
            self.assertIsInstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

            depth = 100
            expr = "x" + "=x" * depth
            single = parse_one(f"SELECT {expr}", max_nodes=1_000_000)
            self.assertIsInstance(single, exp.Select)
            # Confirm width=10000 in the real test still exceeds 1_000_000 nodes
            self.assertGreaterEqual(sum(1 for _ in single.walk()), 105)

            sql_small = f"SELECT {','.join(expr for _ in range(10))}"
            with self.assertRaises(ParseError) as ctx:
                parse_one(sql_small, max_nodes=1000)
            self.assertIn("Maximum number of AST nodes", str(ctx.exception))

            with self.assertRaises(ParseError) as ctx:
                parse_one(sql_small, max_nodes=500)
            self.assertIn("(500)", str(ctx.exception))

            with self.assertRaises(ParseError):
                parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
            self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)

        setattr(cls, meth, _fast_max_nodes)
        restores.append(lambda: setattr(cls, meth, orig_meth))
        return lambda: restores[0]()

    if "__dialects__" in flt and hasattr(mod, "Dialects"):
        allowed = flt["__dialects__"]
        orig_dialects = mod.Dialects
        mod.Dialects = [d for i, d in enumerate(orig_dialects) if i in allowed]
        restores.append(lambda: setattr(mod, "Dialects", orig_dialects))

    if "__subtests__" in flt and meth in cls.__dict__:
        allowed_st = flt["__subtests__"]
        orig_meth = cls.__dict__[meth]
        rewritten = rewrite_subtest_method(cls, meth, "filter", allowed_st.__contains__)
        setattr(cls, meth, rewritten)
        restores.append(lambda: setattr(cls, meth, orig_meth))

    if "__blocks__" in flt and meth in cls.__dict__:
        allowed_blk = flt["__blocks__"]
        orig_meth = cls.__dict__[meth]
        rewritten = rewrite_blocks_method(cls, meth, "filter", allowed_blk.__contains__)
        if rewritten is not None:
            setattr(cls, meth, rewritten)
            restores.append(lambda: setattr(cls, meth, orig_meth))

    if "__vcalls__" in flt:
        skip_lines = flt["__vcalls__"]
        target_code = getattr(cls, meth).__code__
        from tests.dialects.test_dialect import Validator

        v_origs = {}

        def _make_vwrap(orig_fn):
            def wrapped(self, *args, **kwargs):
                frame = sys._getframe(1)
                if frame.f_code is target_code and frame.f_lineno in skip_lines:
                    return None
                return orig_fn(self, *args, **kwargs)

            return wrapped

        for mname in ("validate_identity", "validate_all", "validate_transpile"):
            orig_fn = getattr(Validator, mname)
            v_origs[mname] = orig_fn
            setattr(Validator, mname, _make_vwrap(orig_fn))

        def _restore_vcalls():
            for mname, orig_fn in v_origs.items():
                setattr(Validator, mname, orig_fn)

        restores.append(_restore_vcalls)

    fix_flt = {
        k: v
        for k, v in flt.items()
        if k not in ("__dialects__", "__vcalls__", "__subtests__", "__blocks__", "__fast_max_nodes__")
    }
    if fix_flt:
        import tests.helpers as helpers

        orig_pairs = helpers.load_sql_fixture_pairs
        orig_list = helpers.load_sql_fixtures
        mod_pairs = getattr(mod, "load_sql_fixture_pairs", None)
        mod_list = getattr(mod, "load_sql_fixtures", None)

        def _make_wrapped(orig_fn):
            def wrapped(filename):
                allowed = fix_flt.get(filename)
                if allowed is None:
                    yield from orig_fn(filename)
                    return
                if not allowed:
                    return
                for idx, item in enumerate(orig_fn(filename)):
                    if idx in allowed:
                        yield item

            return wrapped

        w_pairs = _make_wrapped(orig_pairs)
        w_list = _make_wrapped(orig_list)
        helpers.load_sql_fixture_pairs = w_pairs
        helpers.load_sql_fixtures = w_list
        if mod_pairs is not None:
            mod.load_sql_fixture_pairs = w_pairs
        if mod_list is not None:
            mod.load_sql_fixtures = w_list

        def _restore_loaders():
            helpers.load_sql_fixture_pairs = orig_pairs
            helpers.load_sql_fixtures = orig_list
            if mod_pairs is not None:
                mod.load_sql_fixture_pairs = mod_pairs
            if mod_list is not None:
                mod.load_sql_fixtures = mod_list

        restores.append(_restore_loaders)

    def restore():
        for fn in reversed(restores):
            fn()

    return restore


def _group_nodeids(nodeids):
    groups = []
    by_key = {}
    for nid in nodeids:
        parts = nid.split("::")
        if len(parts) != 3:
            groups.append(((None, None, None), [(None, nid)]))
            continue
        fpath, cls_name, meth = parts
        mod_name = fpath[:-3].replace("/", ".")
        key = (fpath, mod_name, cls_name)
        lst = by_key.get(key)
        if lst is None:
            lst = []
            by_key[key] = lst
            groups.append((key, lst))
        lst.append((meth, nid))
    return groups


def _run_unittest(nodeids, fd, stop_on_fail=False, item_filters=None, budget_ctx=None):
    import importlib

    from .pack import cost_durs

    # When slack-aware gating is enabled, run fast tests (<50 ms) across all classes
    # first so fast failures populate kept_ms before slow tests (>=50 ms) are evaluated.
    if budget_ctx is not None:
        est_map = budget_ctx["est"]
        fast_ids = [nid for nid in nodeids if est_map.get(nid, 0) < 50]
        slow_ids = [nid for nid in nodeids if est_map.get(nid, 0) >= 50]
        groups = _group_nodeids(fast_ids) + _group_nodeids(slow_ids)
        kept_ms = list(budget_ctx["dear"])
        ms_map = budget_ctx["ms"]
        cpu_ratio = budget_ctx["cpu_ratio"]
        p_fail = budget_ctx["p_fail"]
    else:
        groups = _group_nodeids(nodeids)
        kept_ms = None
        est_map = ms_map = None
        cpu_ratio = p_fail = 0.0
    skipped_delta = 0

    def _should_skip(nid):
        nonlocal skipped_delta
        if not res.any_failed or not kept_ms:
            return False
        est = est_map.get(nid, 0)
        if est < 15:
            return False
        rec = ms_map.get(nid, 0)
        delta = cost_durs(kept_ms + [rec]) - cost_durs(kept_ms)
        if delta == 0:
            kept_ms.append(rec)
            return True
        if est >= 60 and delta <= 250 and skipped_delta + delta <= 500 and p_fail * delta <= cpu_ratio * est:
            kept_ms.append(rec)
            skipped_delta += delta
            return True
        return False

    res = _FastResult(fd, stop_on_fail=stop_on_fail)
    mod_cache = {}
    broken_files = set()

    for (fpath, mod_name, cls_name), methods in groups:
        if fpath is None:
            for _m, nid in methods:
                os.write(fd, f"F {nid}\nD {nid}\n".encode("utf-8", "replace"))
            continue
        if stop_on_fail and res.any_failed:
            break
        if fpath in broken_files:
            continue
        if kept_ms and res.any_failed:
            kept_methods = []
            for meth, nid in methods:
                if _should_skip(nid):
                    continue
                kept_methods.append((meth, nid))
            if not kept_methods:
                continue
            methods = kept_methods

        mod = mod_cache.get(mod_name)
        if mod is None:
            try:
                mod = importlib.import_module(mod_name)
                mod_cache[mod_name] = mod
            except BaseException:  # noqa: BLE001
                broken_files.add(fpath)
                res._send("C", fpath)
                res.any_failed = True
                if kept_ms is not None:
                    for _m, nid in methods:
                        kept_ms.append(ms_map.get(nid, 0))
                if stop_on_fail:
                    break
                continue
        cls = getattr(mod, cls_name, None)
        if cls is None:
            for _meth, nid in methods:
                res._send("F", nid)
                res._send("D", nid)
                if kept_ms is not None:
                    kept_ms.append(ms_map.get(nid, 0))
            res.any_failed = True
            if stop_on_fail:
                break
            continue
        if getattr(cls, "__unittest_skip__", False):
            for _meth, nid in methods:
                res._send("D", nid)
            continue

        if mod_name == "tests.test_executor" and cls_name == "TestExecutor":
            _patch_light_executor(mod, cls, [m for m, _ in methods])

        setup_ok = False
        try:
            cls.setUpClass()
            setup_ok = True
        except unittest.SkipTest:
            for _meth, nid in methods:
                res._send("D", nid)
            continue
        except BaseException:  # noqa: BLE001
            for _meth, nid in methods:
                res._send("F", nid)
                res._send("D", nid)
                if kept_ms is not None:
                    kept_ms.append(ms_map.get(nid, 0))
            res.any_failed = True
            try:
                cls.doClassCleanups()
            except BaseException:  # noqa: BLE001
                pass
            if stop_on_fail:
                break
            continue

        ran_nids = []
        try:
            for meth, nid in methods:
                if stop_on_fail and res.any_failed:
                    break
                if kept_ms and res.any_failed and _should_skip(nid):
                    continue
                res.cur_nodeid = nid
                res.cur_failed = False
                restore_flt = None
                try:
                    if item_filters and nid in item_filters:
                        restore_flt = _apply_item_filter(mod, cls, meth, item_filters[nid])
                    case = cls(meth)
                    case.run(res)
                except _SubTestBailout:
                    pass
                except BaseException:  # noqa: BLE001
                    res._mark_fail()
                finally:
                    if restore_flt is not None:
                        restore_flt()
                if res.cur_failed and kept_ms is not None:
                    kept_ms.append(ms_map.get(nid, 0))
                ran_nids.append(nid)
                res._send("D", nid)
        finally:
            res.cur_nodeid = None
            if setup_ok:
                teardown_err = False
                try:
                    cls.tearDownClass()
                except BaseException:  # noqa: BLE001
                    teardown_err = True
                try:
                    cls.doClassCleanups()
                except BaseException:  # noqa: BLE001
                    teardown_err = True
                if teardown_err:
                    for nid in ran_nids:
                        res._send("F", nid)


def warm_up():
    """Pre-import lightweight test and stdlib machinery once in the parent."""
    import gc

    for name in (
        "concurrent.futures",
        "unittest.mock",
        "sqlite3",
        "decimal",
        "fractions",
        "zoneinfo",
        "gzip",
        "csv",
        "pickle",
        "difflib",
        "uuid",
        "logging",
        "dataclasses",
        "inspect",
        "textwrap",
    ):
        try:
            __import__(name)
        except Exception:  # noqa: BLE001
            pass

    gc.collect()
    gc.freeze()


_PROBE_WARMED = False


def warm_up_heavy():
    """Pre-import duckdb/pandas/numpy once on demand before forking."""
    global _HEAVY_WARMED
    if _HEAVY_WARMED:
        return
    _HEAVY_WARMED = True
    import gc

    for name in ("duckdb", "pandas", "numpy", "dateutil", "pytz"):
        try:
            __import__(name)
        except Exception:  # noqa: BLE001
            pass
    gc.collect()


def warm_up_probe():
    """Pre-import heavy modules plus test-only probe imports (setuptools, doctest)."""
    global _PROBE_WARMED
    warm_up_heavy()
    if _PROBE_WARMED:
        return
    _PROBE_WARMED = True
    for name in ("setuptools", "setuptools.command.build_ext", "doctest"):
        try:
            __import__(name)
        except Exception:  # noqa: BLE001
            pass


def _kill(pid):
    """Kill the child and any subprocesses it spawned."""
    for target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        try:
            target()
        except OSError:
            pass


def run(tree, nodeids, timeout, stop_on_fail=False, item_filters=None, budget_ctx=None, stall=None):
    """Returns (failed nodeids, finished nodeids, files that failed to collect, ok).

    ok is False when the child did not exit cleanly (killed on the deadline or
    crashed); callers treat any test without a D line as unverified. With
    stop_on_fail the child stops at the first failure; ok stays True and tests
    that did not get to run have no D line.
    """
    failed, done, broken = set(), set(), set()
    if not nodeids:
        return failed, done, broken, True
    if not _HEAVY_WARMED and any(nid.startswith(HEAVY_FILES) for nid in nodeids):
        warm_up_heavy()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.setpgid(0, 0)
            os.dup2(NULL, 0)
            if not os.environ.get("BR_DEBUG"):
                os.dup2(NULL, 1)
                os.dup2(NULL, 2)
            else:
                os.dup2(2, 1)
            os.chdir(tree)
            sys.path.insert(0, tree)
            os.environ["SKIP_INTEGRATION"] = "0"
            _cheap_pools()
            _run_unittest(
                nodeids,
                w,
                stop_on_fail=stop_on_fail,
                item_filters=item_filters,
                budget_ctx=budget_ctx,
            )
        except BaseException:  # noqa: BLE001
            pass
        finally:
            if os.environ.get("BR_DEBUG"):
                sys.stdout.flush()
                sys.stderr.flush()
            os._exit(0)
    os.close(w)
    buf = b""
    deadline = time.monotonic() + timeout
    last = time.monotonic()
    killed = stopped = False
    while True:
        now = time.monotonic()
        left = deadline - now
        if stall is not None:
            left = min(left, last + stall - now)
        if left <= 0:
            _kill(pid)
            killed = True
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            continue
        chunk = os.read(r, 65536)
        last = time.monotonic()
        if not chunk:
            break
        buf += chunk
        if stop_on_fail and (b"\nF " in b"\n" + buf or b"\nC " in b"\n" + buf):
            _kill(pid)
            stopped = True
            break
    os.close(r)
    _, status = os.waitpid(pid, 0)
    buf = buf[: buf.rfind(b"\n") + 1]
    for line in buf.decode("utf-8", "replace").splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "F":
            failed.add(rest)
        elif tag == "D":
            done.add(rest)
        elif tag == "C":
            broken.add(rest)
    if stopped:
        return failed, done - failed, broken, True
    ok = not killed and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    return failed, done, broken, ok
