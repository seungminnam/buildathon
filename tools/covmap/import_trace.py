"""Which sqlglot lines run while each test file is imported.

    python tools/covmap/import_trace.py <out.json>     (cwd = sqlglot checkout)

A test file whose import fails fails every test in it, so a change to code
that runs at import time (a parse at module level in a test file, a
metaclass, a registry) affects whole files. The parent imports sqlglot and
records what that runs ("*": every file needs it); then one forked child per
test file imports that file and records the function-body lines that ran.
Output: {"*": {path: [lines]}, "<test file>": {path: [lines]}, ...}
"""

import glob
import json
import os
import sys

ROOT = os.getcwd() + os.sep
mon = sys.monitoring
TOOL = mon.COVERAGE_ID
lines = set()


def on_line(code, line):
    if code.co_flags & 1 and code.co_filename.startswith(ROOT):
        lines.add((code.co_filename[len(ROOT):], line))
    return mon.DISABLE


def on_start(code, offset):
    if code.co_name == "<lambda>" and code.co_filename.startswith(ROOT):
        lines.add((code.co_filename[len(ROOT):], code.co_firstlineno))
    return mon.DISABLE


def grouped():
    out = {}
    for p, ln in lines:
        out.setdefault(p, []).append(ln)
    return {p: sorted(v) for p, v in out.items()}


def main():
    sys.path.insert(0, os.getcwd())
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    mon.use_tool_id(TOOL, "imports")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
    import sqlglot  # noqa: F401

    result = {"*": grouped()}
    files = sorted(glob.glob("tests/test_*.py") + glob.glob("tests/dialects/test_*.py"))
    for f in files:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            lines.clear()
            mon.restart_events()
            try:
                __import__(f[:-3].replace("/", "."), fromlist=["_"])
            except BaseException:
                pass
            os.write(w, json.dumps(grouped()).encode())
            os._exit(0)
        os.close(w)
        chunks = []
        while True:
            data = os.read(r, 1 << 20)
            if not data:
                break
            chunks.append(data)
        os.close(r)
        os.waitpid(pid, 0)
        result[f] = json.loads(b"".join(chunks))
    with open(sys.argv[1], "w") as out:
        json.dump(result, out)


if __name__ == "__main__":
    main()
