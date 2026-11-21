"""Line tracing for helper processes started by a test (spawned pools, `python -c`).

The tracer plugin sets TRACE_SUB_DIR in the forked test process; any Python
process started from there imports this module at startup, records each
function-body line under TRACE_ROOT the first time it runs, and appends it to
TRACE_SUB_DIR/<pid>.lines right away (pool workers may be killed, not exited).
The plugin merges those files into the test's record.
"""

import os

_dir = os.environ.get("TRACE_SUB_DIR")
if _dir:
    import sys

    _root = os.path.realpath(os.environ.get("TRACE_ROOT", os.getcwd())) + os.sep
    _fd = os.open(os.path.join(_dir, f"{os.getpid()}.lines"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    _rel = {}
    _mon = sys.monitoring

    def _path(p):
        r = _rel.get(p)
        if r is None:
            real = "" if p.startswith("<") else os.path.realpath(p)
            r = _rel[p] = real[len(_root):] if real.startswith(_root) else ""
        return r

    def _on_line(code, line, _disable=_mon.DISABLE, _write=os.write):
        if code.co_flags & 1:
            r = _path(code.co_filename)
            if r:
                _write(_fd, f"L\t{r}\t{line}\n".encode())
        return _disable

    def _on_start(code, offset, _disable=_mon.DISABLE, _write=os.write):
        # one-line functions get no LINE event for their only line
        if code.co_flags & 1:
            r = _path(code.co_filename)
            if r:
                _write(_fd, f"L\t{r}\t{code.co_firstlineno}\n".encode())
        return _disable

    _seen_types = set()

    def _wrap(cls, _write=os.write):
        original = cls.__init__

        def __init__(self, *args, **kwargs):
            t = type(self)
            if t not in _seen_types:
                _seen_types.add(t)
                for k in t.__mro__:
                    _write(_fd, f"C\t{k.__module__}.{k.__qualname__}\n".encode())
            return original(self, *args, **kwargs)

        cls.__init__ = __init__

    _WANT = {
        "sqlglot.expressions.core": "Expr",
        "sqlglot.dialects.dialect": "Dialect",
        "sqlglot.tokens": "Tokenizer",
        "sqlglot.parser": "Parser",
        "sqlglot.generator": "Generator",
    }

    class _WrapOnImport:
        """Wrap __init__ of the classes the map tracks as soon as their module runs."""

        def find_spec(self, name, path, target=None):
            if name not in _WANT:
                return None
            for finder in sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(name, path, target)
                if spec is not None:
                    break
            else:
                return None
            run = spec.loader.exec_module

            def exec_module(module, _run=run, _name=name):
                _run(module)
                _wrap(getattr(module, _WANT[_name]))

            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, _WrapOnImport())

    def _audit(event, args, _write=os.write):
        if event == "open" and args and isinstance(args[0], str):
            r = _path(args[0])
            if r:
                _write(_fd, f"O\t{r}\n".encode())

    _mon.use_tool_id(_mon.COVERAGE_ID, "covmap-sub")
    _mon.register_callback(_mon.COVERAGE_ID, _mon.events.LINE, _on_line)
    _mon.register_callback(_mon.COVERAGE_ID, _mon.events.PY_START, _on_start)
    _mon.set_events(_mon.COVERAGE_ID, _mon.events.LINE | _mon.events.PY_START)
    sys.addaudithook(_audit)
