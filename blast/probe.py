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
# state that differs between two imports of the same tree (or depending on loaded stdlib)
_NOISE = re.compile(r"(_abc_impl|_lock|_import_lock|:UNITTEST)$")
_SKIP = (types.ModuleType, types.BuiltinFunctionType, property, staticmethod, classmethod)


_PRIMS = {bool, int, float, str, bytes, complex}


def _canon(v, depth=0, seen=None, memo=None):
    t = type(v)
    if v is None or t in _PRIMS:
        return repr(v)
    vid = id(v)
    if memo is not None:
        m = memo.get(vid)
        if m is not None:
            return m
    if isinstance(v, (bool, int, float, str, bytes, complex)):
        r = repr(v)
        if memo is not None:
            memo[vid] = r
        return r
    if depth > 40:
        return "<deep>"
    if isinstance(v, type):
        r = f"T:{v.__module__}.{v.__qualname__}"
        if memo is not None:
            memo[vid] = r
        return r
    if isinstance(v, _SKIP):
        r = f"S:{t.__name__}"
        if memo is not None:
            memo[vid] = r
        return r
    if seen is None:
        seen = set()
    if vid in seen:
        return "<cycle>"
    if t is types.FunctionType or isinstance(v, types.FunctionType):
        if not v.__closure__:
            r = f"F:{v.__module__}.{v.__qualname__}()[]"
            if memo is not None:
                memo[vid] = r
            return r
        seen.add(vid)
        try:
            cells = []
            for c in v.__closure__:
                try:
                    cells.append(_canon(c.cell_contents, depth + 1, seen, memo))
                except ValueError:
                    cells.append("<empty>")
            defaults = _canon(v.__defaults__, depth + 1, seen, memo)
            r = f"F:{v.__module__}.{v.__qualname__}({defaults})[{','.join(cells)}]"
        finally:
            seen.discard(vid)
        if memo is not None and "<cycle>" not in r and "<deep>" not in r:
            memo[vid] = r
        return r
    if t is functools.partial or isinstance(v, functools.partial):
        seen.add(vid)
        try:
            r = (
                f"P:{_canon(v.func, depth + 1, seen, memo)}"
                f"{_canon(v.args, depth + 1, seen, memo)}"
                f"{_canon(v.keywords, depth + 1, seen, memo)}"
            )
        finally:
            seen.discard(vid)
        if memo is not None and "<cycle>" not in r and "<deep>" not in r:
            memo[vid] = r
        return r
    if t is types.MethodType or isinstance(v, types.MethodType):
        return f"M:{_canon(v.__func__, depth + 1, seen, memo)}"
    seen.add(vid)
    try:
        if t is dict or isinstance(v, dict):
            if not v:
                r = "{}"
            else:
                items = sorted(
                    (_canon(k, depth + 1, seen, memo), _canon(x, depth + 1, seen, memo))
                    for k, x in v.items()
                )
                r = "{" + ",".join(f"{k}:{x}" for k, x in items) + "}"
        elif t is list or t is tuple or isinstance(v, (list, tuple)):
            if not v:
                r = "[]" if isinstance(v, list) else "(]"
            else:
                r = ("[" if isinstance(v, list) else "(") + ",".join(
                    _canon(x, depth + 1, seen, memo) for x in v
                ) + "]"
        elif t is set or t is frozenset or isinstance(v, (set, frozenset)):
            if not v:
                r = "s{}"
            else:
                r = "s{" + ",".join(sorted(_canon(x, depth + 1, seen, memo) for x in v)) + "}"
        elif hasattr(v, "__dict__") and not callable(v):
            r = f"O:{t.__qualname__}" + _canon(vars(v), depth + 1, seen, memo)
        else:
            r = f"R:{t.__qualname__}:{v!r}"[:200]
    finally:
        seen.discard(vid)
    if memo is not None and "<cycle>" not in r and "<deep>" not in r:
        memo[vid] = r
    return r


def fingerprint():
    """{"module:Class.attr" or "module:name": sha1 of its canonical form}."""
    out = {}
    memo = {}
    seen = set()
    sha1 = hashlib.sha1
    for modname, mod in list(sys.modules.items()):
        if not (modname == "sqlglot" or modname.startswith("sqlglot.")) or mod is None:
            continue
        for name, val in list(vars(mod).items()):
            if name.startswith("__"):
                continue
            if isinstance(val, type) and val.__module__ == modname:
                prefix = f"{modname}:{val.__qualname__}."
                for attr, x in list(vars(val).items()):
                    if attr.startswith("__") and attr not in ("__slots__",):
                        continue
                    if isinstance(x, (types.FunctionType,) + _SKIP):
                        continue
                    k = prefix + attr
                    if _NOISE.search(k):
                        continue
                    v = _canon(x, 0, seen, memo)
                    if " at 0x" in v:
                        v = _ADDR.sub("", v)
                    out[k] = sha1(v.encode("utf-8", "replace")).hexdigest()[:16]
            elif isinstance(val, (type, types.FunctionType, types.ModuleType)):
                continue
            else:
                k = f"{modname}:{name}"
                if _NOISE.search(k):
                    continue
                v = _canon(val, 0, seen, memo)
                if " at 0x" in v:
                    v = _ADDR.sub("", v)
                out[k] = sha1(v.encode("utf-8", "replace")).hexdigest()[:16]
    return out


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


def run_probe(tree, fd, with_fp=True):
    """Child side: write {"errors": [...], "fp": {...}} as JSON to fd."""
    import json

    try:
        errors = load_everything(tree)
        fp = fingerprint() if with_fp else {}
        data = json.dumps({"errors": errors, "fp": fp})
    except BaseException as e:  # noqa: BLE001
        data = json.dumps({"errors": [["<probe>", repr(e)]], "fp": {}})
    os.write(fd, data.encode())


def probe(tree, timeout=30, with_fp=True):
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
            run_probe(tree, w, with_fp)
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


if __name__ == "__main__":
    import json

    tree, out = sys.argv[1], sys.argv[2]
    a, b = probe(tree), probe(tree)
    if not (a and b and not a["errors"] and not b["errors"]):
        sys.exit("probe baseline failed")
    noise = sorted(set(diff(a["fp"], b["fp"])))
    noise_set = set(noise)
    base_fp = {k: v for k, v in a["fp"].items() if k not in noise_set}
    json.dump({"fp": base_fp, "noise": noise}, open(out, "w"), sort_keys=True, separators=(",", ":"))

