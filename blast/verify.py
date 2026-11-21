"""Run a set of sqlglot tests against a patched tree and report what failed.

The parent process imports pytest and the heavy third-party libraries once,
then forks one child per commit. The child points sys.path at the patched
tree, imports sqlglot fresh from there and runs the requested node ids. It
streams one line per event over a pipe, so if it has to be killed on a
deadline we still know which tests finished.

Child protocol (one line each):
    F <nodeid>    a report for this test failed (setup, call, teardown or a subtest)
    D <nodeid>    the test finished
    C <path>      collecting this file failed (import error and the like)
"""

import os
import select
import signal
import sys
import time

NULL = os.open(os.devnull, os.O_RDWR)


class Collector:
    def __init__(self, fd):
        self.fd = fd

    def _send(self, tag, text):
        os.write(self.fd, f"{tag} {text}\n".encode("utf-8", "replace"))

    def pytest_collectreport(self, report):
        if report.failed:
            self._send("C", report.nodeid.split("::")[0])

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self._send("F", report.nodeid)

    def pytest_runtest_logfinish(self, nodeid, location):
        self._send("D", nodeid)


def _cheap_pools():
    """Make the tests' process pools cheap without changing what they compute.

    test_optimizer and test_executor start ProcessPoolExecutor with the spawn
    method and one worker per host core. Every spawned worker starts Python
    and imports sqlglot, pandas and duckdb again, all charged to us as CPU.
    A forked worker already has all of that loaded, and two workers match
    the two cores we get.
    """
    import multiprocessing

    real_get_context = multiprocessing.get_context

    def get_context(method=None):
        if method == "spawn":
            method = "fork"
        return real_get_context(method)

    multiprocessing.get_context = get_context
    os.process_cpu_count = lambda: 2
    os.cpu_count = lambda: 2


def warm_up():
    """Import what every child would otherwise import again."""
    import pytest  # noqa: F401
    import _pytest.unittest  # noqa: F401
    import _pytest.python  # noqa: F401

    for name in ("duckdb", "pandas", "numpy", "dateutil", "pytz"):
        try:
            __import__(name)
        except Exception:
            pass


def _kill(pid):
    """Kill the child and the pool workers it forked."""
    for target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
        try:
            target()
        except OSError:
            pass


def run(tree, nodeids, timeout, stop_on_fail=False):
    """Returns (failed nodeids, finished nodeids, files that failed to collect, ok).

    ok is False when the child did not exit cleanly (killed on the deadline or
    crashed); callers treat any test without a D line as unverified. With
    stop_on_fail the child is stopped at the first failure; ok stays True
    and the tests that did not get to run have no D line.
    """
    failed, done, broken = set(), set(), set()
    if not nodeids:
        return failed, done, broken, True
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
            import pytest

            pytest.main(
                [
                    "-q", *os.environ.get("BR_PYTEST_ARGS", "").split(),
                    "-p", "no:cacheprovider",
                    "-p", "no:xdist",
                    "-p", "no:randomly",
                    "--rootdir", tree,
                    "-o", "addopts=",
                    *nodeids,
                ],
                plugins=[Collector(w)],
            )
        except BaseException:
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
