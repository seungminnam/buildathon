"""Run candidate tests on the patched tree, to keep only the ones that fail.

Running tests is allowed and its CPU time is charged, so this is used only for
cheap tests and only when it is expected to cost less than handing them all to
the workers (see selector.decide).

The patched tree is an overlay in /tmp: the directories on the path of every
changed file are real, everything else is a symlink to the pristine checkout
(including __pycache__, so unchanged modules load their precompiled bytecode).
A forked child imports sqlglot and the test modules from the overlay and runs
the tests with unittest (sqlglot's tests are unittest.TestCase classes),
recording failures, errors, failing subtests and timeouts.
"""

import json
import os
import select
import shutil
import signal
import sys
import time
import unittest


class _Timeout(Exception):
    pass


def build_overlay(root, dst, files):
    """files: {relpath: new text, or None for a deleted file}."""
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    real_dirs = {""}
    for path in files:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:i]))
    for d in sorted(real_dirs, key=len):
        src_dir = os.path.join(root, d)
        out_dir = os.path.join(dst, d)
        os.makedirs(out_dir, exist_ok=True)
        if not os.path.isdir(src_dir):
            continue
        for name in os.listdir(src_dir):
            rel = f"{d}/{name}" if d else name
            if rel in real_dirs or rel in files or name == ".git":
                continue
            os.symlink(os.path.join(src_dir, name), os.path.join(out_dir, name))
    for path, text in files.items():
        if text is not None:
            with open(os.path.join(dst, path), "w", encoding="utf-8") as f:
                f.write(text)


class _Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.failed = set()
        self.class_errors = []

    def _key(self, test):
        return getattr(test, "_blast_id", None)

    def addError(self, test, err):
        key = self._key(test)
        if key is None:
            # setUpClass / setUpModule failure: an _ErrorHolder
            self.class_errors.append(str(test))
        else:
            self.failed.add(key)

    def addFailure(self, test, err):
        key = self._key(test)
        if key is not None:
            self.failed.add(key)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            key = self._key(test)
            if key is not None:
                self.failed.add(key)

    def addUnexpectedSuccess(self, test):
        key = self._key(test)
        if key is not None:
            self.failed.add(key)


def _child(overlay, test_ids, limits, out_fd):
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.chdir(overlay)
    sys.path.insert(0, overlay)
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    failed = set()
    suite = unittest.TestSuite()
    by_class = {}
    for tid in test_ids:
        path, cls_name, method = tid.split("::")
        mod_name = path[:-3].replace("/", ".")
        try:
            module = __import__(mod_name, fromlist=["_"])
            cls = getattr(module, cls_name)
            case = cls(method)
        except BaseException:
            failed.add(tid)  # the file cannot be imported: the test fails
            continue
        case._blast_id = tid
        by_class.setdefault(f"{cls.__module__}.{cls.__qualname__}", []).append(tid)
        limit = limits.get(tid, 5.0)
        original = getattr(case, method)

        def timed(original=original, limit=limit):
            def on_alarm(signum, frame):
                raise _Timeout(f"over {limit:.1f}s")

            old = signal.signal(signal.SIGALRM, on_alarm)
            signal.setitimer(signal.ITIMER_REAL, limit)
            try:
                return original()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old)

        setattr(case, method, timed)
        suite.addTest(case)
    result = _Result()
    try:
        suite.run(result)
    except BaseException:
        failed.update(test_ids)
    failed |= result.failed
    for holder in result.class_errors:
        # "setUpClass (tests.x.TestX)" fails every test of that class
        for qual, tids in by_class.items():
            if qual in holder:
                failed.update(tids)
    os.write(out_fd, json.dumps(sorted(failed)).encode())
    os.close(out_fd)


def run_tests(root, tmp, files, test_ids, limits, budget):
    """Return the set of failing test ids, or None when the run did not finish."""
    overlay = os.path.join(tmp, "overlay")
    build_overlay(root, overlay, files)
    rfd, wfd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            _child(overlay, test_ids, limits, wfd)
        finally:
            os._exit(0)
    os.close(wfd)
    deadline = time.monotonic() + budget
    chunks = []
    finished = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([rfd], [], [], remaining)
        if not ready:
            break
        data = os.read(rfd, 65536)
        if not data:
            finished = True
            break
        chunks.append(data)
    if not finished:
        os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    if not finished:
        os.close(rfd)
        shutil.rmtree(overlay, ignore_errors=True)
        return None
    os.close(rfd)
    shutil.rmtree(overlay, ignore_errors=True)
    try:
        return set(json.loads(b"".join(chunks).decode()))
    except ValueError:
        return None
