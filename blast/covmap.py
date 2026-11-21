"""The coverage map built at the pinned commit (see tools/covmap)."""

import json


class CoverageMap:
    def __init__(self, path):
        with open(path) as f:
            raw = json.load(f)
        self.tests = raw["tests"]  # local index -> node id
        self.sets = raw["sets"]
        self._lines_raw = raw["lines"]
        self._lines = {}
        self.opens = raw["opens"]
        self.classes = raw["classes"]
        self.import_lines = {p: set(v) for p, v in raw["import_lines"].items()}
        self.all = frozenset(range(len(self.tests)))
        self.by_file = {}
        for i, tid in enumerate(self.tests):
            self.by_file.setdefault(tid.split("::", 1)[0], set()).add(i)

    def lines(self, path):
        """{line: set index} of function-body lines in `path` some test ran."""
        got = self._lines.get(path)
        if got is None:
            flat = self._lines_raw.get(path, [])
            got = self._lines[path] = dict(zip(flat[0::2], flat[1::2]))
        return got

    def tests_for_lines(self, path, lines):
        per = self.lines(path)
        out = set()
        seen = set()
        for ln in lines:
            s = per.get(ln)
            if s is not None and s not in seen:
                seen.add(s)
                out.update(self.sets[s])
        return out

    def covered(self, path, ln):
        return ln in self.lines(path)

    def opened(self, path):
        s = self.opens.get(path)
        return set(self.sets[s]) if s is not None else set()

    def instantiated(self, qualname):
        s = self.classes.get(qualname)
        return set(self.sets[s]) if s is not None else None
