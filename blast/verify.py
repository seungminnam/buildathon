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


class InlineExecutor:
    """ProcessPoolExecutor that does the work in-process.

    The tests only use the pool to spread pure functions (parse and optimize a
    query, run one TPC query) over cores. Running them in this process gives
    the same results without forking workers and pickling ASTs back and forth.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        import concurrent.futures

        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, **_):
        return [fn(*a) for a in zip(*iterables)]

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


def _run_unittest(nodeids, fd, stop_on_fail=False):
    import importlib

    # Group by (fpath, mod_name, cls_name) preserving caller's ordering
    groups = []
    by_key = {}
    for nid in nodeids:
        parts = nid.split("::")
        if len(parts) != 3:
            os.write(fd, f"F {nid}\nD {nid}\n".encode("utf-8", "replace"))
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

    res = _FastResult(fd, stop_on_fail=stop_on_fail)
    mod_cache = {}
    broken_files = set()

    for (fpath, mod_name, cls_name), methods in groups:
        if stop_on_fail and res.any_failed:
            break
        if fpath in broken_files:
            continue
        mod = mod_cache.get(mod_name)
        if mod is None:
            try:
                mod = importlib.import_module(mod_name)
                mod_cache[mod_name] = mod
            except BaseException:  # noqa: BLE001
                broken_files.add(fpath)
                res._send("C", fpath)
                res.any_failed = True
                if stop_on_fail:
                    break
                continue
        cls = getattr(mod, cls_name, None)
        if cls is None:
            for _meth, nid in methods:
                res._send("F", nid)
                res._send("D", nid)
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
                res.cur_nodeid = nid
                res.cur_failed = False
                try:
                    case = cls(meth)
                    case.run(res)
                except _SubTestBailout:
                    pass
                except BaseException:  # noqa: BLE001
                    res._mark_fail()
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
    """Pre-import lightweight test machinery once in the parent."""
    import concurrent.futures  # noqa: F401
    import gc
    import unittest.mock  # noqa: F401

    gc.collect()
    gc.freeze()


def warm_up_heavy():
    """Pre-import duckdb/pandas/numpy once on demand before forking."""
    global _HEAVY_WARMED
    if _HEAVY_WARMED:
        return
    _HEAVY_WARMED = True
    for name in ("duckdb", "pandas", "numpy", "dateutil", "pytz"):
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


def run(tree, nodeids, timeout, stop_on_fail=False):
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
            _run_unittest(nodeids, w, stop_on_fail=stop_on_fail)
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
    killed = stopped = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            _kill(pid)
            killed = True
            break
        ready, _, _ = select.select([r], [], [], left)
        if not ready:
            continue
        chunk = os.read(r, 65536)
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
