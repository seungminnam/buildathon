"""pytest plugin: per-test line coverage of function bodies plus files opened.

Load with `-p trace_plugin` (this directory on PYTHONPATH) while running
sqlglot's tests at the pinned commit. Writes one JSON line per test to
$TRACE_OUT:

    {"id": nodeid, "outcome": "passed", "dur": 0.12,
     "lines": {"sqlglot/parser.py": [12, 13]},
     "opens": ["tests/fixtures/x.sql", "dir:sqlglot/typing"],
     "classes": ["sqlglot.parsers.tsql.TSQLParser", ...]}

Only lines inside functions (code objects with CO_OPTIMIZED) are recorded:
module and class bodies run once at import, so their line numbers say
nothing about which test depends on them. Those are handled statically by
the selector. The id "<import>" collects what ran outside any test
(collection, module import).

sys.monitoring reports each line once per test: the callback returns DISABLE
and restart_events() re-arms every location before the next test.
"""

import json
import os
import sys
import time

import pytest

ROOT = os.path.realpath(os.environ.get("TRACE_ROOT", os.getcwd())) + os.sep
OUT = os.environ.get("TRACE_OUT", "/tmp/trace.jsonl")

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
CO_OPTIMIZED = 0x1

_lines: set = set()
_opens: set = set()
_classes: set = set()
_relcache: dict = {}
_out = None


def _rel(path):
    rel = _relcache.get(path)
    if rel is None:
        real = "" if path.startswith("<") else os.path.realpath(path)
        rel = real[len(ROOT):] if real.startswith(ROOT) else ""
        _relcache[path] = rel
    return rel


def _on_line(code, line, _optimized=CO_OPTIMIZED, _disable=mon.DISABLE):
    if code.co_flags & _optimized:
        rel = _rel(code.co_filename)
        if rel:
            _lines.add((rel, line))
    return _disable


def _audit(event, args):
    if event == "open" and args and isinstance(args[0], str):
        rel = _rel(args[0])
        if rel:
            _opens.add(rel)
    elif event in ("os.listdir", "os.scandir") and args and isinstance(args[0], (str, os.PathLike)):
        rel = _rel(os.fspath(args[0]))
        if rel:
            _opens.add("dir:" + rel)


def _wrap_init(cls):
    original = cls.__init__

    def __init__(self, *args, **kwargs):
        for t in type(self).__mro__:
            _classes.add(f"{t.__module__}.{t.__qualname__}")
        return original(self, *args, **kwargs)

    cls.__init__ = __init__


def _flush(nodeid, outcome, dur):
    by_file: dict = {}
    for rel, line in _lines:
        by_file.setdefault(rel, []).append(line)
    for v in by_file.values():
        v.sort()
    _out.write(
        json.dumps(
            {
                "id": nodeid,
                "outcome": outcome,
                "dur": round(dur, 4),
                "lines": by_file,
                "opens": sorted(_opens),
                "classes": sorted(_classes),
            }
        )
        + "\n"
    )
    _lines.clear()
    _opens.clear()
    _classes.clear()


def pytest_configure(config):
    global _out
    _out = open(OUT, "w")
    mon.use_tool_id(TOOL, "covmap")
    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.set_events(TOOL, mon.events.LINE)
    sys.addaudithook(_audit)
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for cls in (Dialect, Tokenizer, Parser, Generator):
        _wrap_init(cls)


def _merge_sub(sub):
    """Fold in what helper processes of this test ran (see sitecustomize.py)."""
    for name in os.listdir(sub):
        with open(os.path.join(sub, name)) as f:
            for raw in f:
                parts = raw.rstrip("\n").split("\t")
                if parts[0] == "L" and len(parts) == 3:
                    _lines.add((parts[1], int(parts[2])))
                elif parts[0] == "O" and len(parts) == 2:
                    _opens.add(parts[1])


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    """Run every test in a forked child of the collected session.

    The child starts from the state right after collection, so per-process
    caches (dispatch tables, lazily built tries, lazily imported dialects) are
    empty for every test and the lines that fill them count for each test that
    needs them, not just the first one.
    """
    from _pytest.runner import runtestprotocol

    if not getattr(item.session, "_covmap_import_done", False):
        _flush("<import>", "import", 0.0)
        _out.flush()
        item.session._covmap_import_done = True
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            _lines.clear()
            _opens.clear()
            _classes.clear()
            sub = f"/tmp/covsub/{os.getpid()}"
            os.makedirs(sub, exist_ok=True)
            os.environ["TRACE_SUB_DIR"] = sub
            mon.restart_events()
            start = time.perf_counter()
            reports = runtestprotocol(item, nextitem=None, log=False)
            dur = time.perf_counter() - start
            _merge_sub(sub)
            outcome = "passed"
            for rep in reports:
                if rep.failed:
                    outcome = "failed"
                    break
                if rep.skipped and rep.when in ("setup", "call"):
                    outcome = "skipped"
            _flush(item.nodeid, outcome, dur)
            _out.flush()
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        _out.write(json.dumps({"id": item.nodeid, "outcome": "crashed"}) + "\n")
        _out.flush()
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    reports = getattr(item, "_covmap_reports", None)
    if reports is not None:
        reports.append(rep)


def pytest_unconfigure(config):
    mon.set_events(TOOL, 0)
    mon.register_callback(TOOL, mon.events.LINE, None)
    mon.free_tool_id(TOOL)
    if _out:
        _flush("<end>", "end", 0.0)
        _out.close()
