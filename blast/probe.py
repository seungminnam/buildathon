"""Does a patch change anything at import time?

Some edits land in code that runs while sqlglot imports: functions that
build parser tables, classes instantiated by module-level code. The
coverage map cannot say which tests depend on what such code builds, so
the analyzer falls back to every test. This probe narrows that down by
looking at the import itself:

  1. import every sqlglot module and every test module from the tree;
     any import error means the patch breaks loading, keep every test
  2. fingerprint the state import leaves behind: class attributes and
     module globals of every sqlglot module (tables, tries, settings,
     closures made by factory functions). Function bodies are left out;
     the coverage map handles those.

Comparing the patched fingerprint with the pinned one tells which
(owner, name) pairs the patch changed at import time.
"""

import functools
import hashlib
import os
import re
import pkgutil
import sys
import types

_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")
# state that differs between two imports of the same tree
_NOISE = re.compile(r"(_abc_impl|_lock|_import_lock)$")
_SKIP = (types.ModuleType, types.BuiltinFunctionType, property, staticmethod, classmethod)


def _canon(v, depth=0, seen=None):
    if depth > 40:
        return "<deep>"
    if v is None or isinstance(v, (bool, int, float, str, bytes, complex)):
        return repr(v)
    if seen is None:
        seen = set()
    if id(v) in seen:
        return "<cycle>"
    if isinstance(v, type):
        return f"T:{v.__module__}.{v.__qualname__}"
    if isinstance(v, types.FunctionType):
        seen = seen | {id(v)}
        cells = []
        for c in v.__closure__ or ():
            try:
                cells.append(_canon(c.cell_contents, depth + 1, seen))
            except ValueError:
                cells.append("<empty>")
        return f"F:{v.__module__}.{v.__qualname__}({_canon(v.__defaults__, depth + 1, seen)})[{','.join(cells)}]"
    if isinstance(v, functools.partial):
        seen = seen | {id(v)}
        return f"P:{_canon(v.func, depth + 1, seen)}{_canon(v.args, depth + 1, seen)}{_canon(v.keywords, depth + 1, seen)}"
    if isinstance(v, types.MethodType):
        return f"M:{_canon(v.__func__, depth + 1, seen)}"
    if isinstance(v, _SKIP):
        return f"S:{type(v).__name__}"
    seen = seen | {id(v)}
    if isinstance(v, dict):
        items = sorted((_canon(k, depth + 1, seen), _canon(x, depth + 1, seen)) for k, x in v.items())
        return "{" + ",".join(f"{k}:{x}" for k, x in items) + "}"
    if isinstance(v, (list, tuple)):
        return ("[" if isinstance(v, list) else "(") + ",".join(_canon(x, depth + 1, seen) for x in v) + "]"
    if isinstance(v, (set, frozenset)):
        return "s{" + ",".join(sorted(_canon(x, depth + 1, seen) for x in v)) + "}"
    if hasattr(v, "__dict__") and not callable(v):
        return f"O:{type(v).__qualname__}" + _canon(vars(v), depth + 1, seen)
    return f"R:{type(v).__qualname__}:{v!r}"[:200]


def fingerprint():
    """{"module:Class.attr" or "module:name": sha1 of its canonical form}."""
    out = {}
    for modname, mod in list(sys.modules.items()):
        if not (modname == "sqlglot" or modname.startswith("sqlglot.")) or mod is None:
            continue
        for name, val in list(vars(mod).items()):
            if name.startswith("__"):
                continue
            if isinstance(val, type) and val.__module__ == modname:
                for attr, x in list(vars(val).items()):
                    if attr.startswith("__") and attr not in ("__slots__",):
                        continue
                    if isinstance(x, (types.FunctionType,) + _SKIP):
                        continue
                    out[f"{modname}:{val.__qualname__}.{attr}"] = _canon(x)
            elif isinstance(val, (type, types.FunctionType, types.ModuleType)):
                continue
            else:
                out[f"{modname}:{name}"] = _canon(val)
    return {
        k: hashlib.sha1(_ADDR.sub("", v).encode("utf-8", "replace")).hexdigest()[:16]
        for k, v in out.items()
        if not _NOISE.search(k)
    }


def load_everything(tree):
    """Import every sqlglot module and every test module under tree.

    Returns a list of import errors as (module, message)."""
    errors = []
    sys.path.insert(0, tree)
    os.chdir(tree)
    os.environ["SKIP_INTEGRATION"] = "0"
    import importlib

    for base in ("sqlglot", "tests"):
        try:
            pkg = importlib.import_module(base)
        except BaseException as e:  # noqa: BLE001
            errors.append((base, f"{type(e).__name__}: {e}"))
            continue
        for info in pkgutil.walk_packages(pkg.__path__, base + "."):
            name = info.name
            if base == "tests" and ".fixtures" in name:
                continue
            if name.endswith("__main__") or name in ("tests.gen_fixtures", "tests.leakcheck"):
                continue
            try:
                importlib.import_module(name)
            except BaseException as e:  # noqa: BLE001
                errors.append((name, f"{type(e).__name__}: {e}"[:200]))
    return errors


def run_probe(tree, fd):
    """Child side: write {"errors": [...], "fp": {...}} as JSON to fd."""
    import json

    try:
        errors = load_everything(tree)
        fp = fingerprint()
        data = json.dumps({"errors": errors, "fp": fp})
    except BaseException as e:  # noqa: BLE001
        data = json.dumps({"errors": [["<probe>", repr(e)]], "fp": {}})
    os.write(fd, data.encode())


def probe(tree, timeout=30):
    """Fork a child that imports `tree` and fingerprints it. None on failure."""
    import json
    import select
    import signal
    import time

    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(null, fd)
            run_probe(tree, w)
        finally:
            os._exit(0)
    os.close(w)
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
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


def diff(base, new):
    """Keys whose fingerprint changed, appeared or vanished."""
    keys = set(base) | set(new)
    return sorted(k for k in keys if base.get(k) != new.get(k))
