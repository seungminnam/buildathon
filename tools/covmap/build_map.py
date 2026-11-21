"""Turn the tracer output into the committed map, maps/coverage.json.

    python3 tools/covmap/build_map.py <trace.jsonl> maps/coverage.json

Layout (test sets are stored once and referred to by index):

    tests:   [node id, ...]            index = local test number
    sets:    [[test index, ...], ...]  sorted, deduplicated
    lines:   {path: [line, set, line, set, ...]}   function-body lines
    opens:   {path: set}               files a test opened ("dir:<path>" = listed)
    classes: {module.qualname: set}    tokenizer/parser/generator/dialect classes
                                       instantiated (any class in the MRO)
    import_lines: {path: [line, ...]}  function lines that ran during collection
    import_classes: [module.qualname]  classes (any in the MRO) instantiated
                                       during collection, i.e. at import time
"""

import json
import sys


def main():
    src, dst = sys.argv[1], sys.argv[2]
    tests, recs, imp, imp_classes = [], [], None, set()
    for raw in open(src):
        r = json.loads(raw)
        if r["id"] == "<import>":
            imp = r
            imp_classes.update(r.get("classes", []))
        elif not r["id"].startswith("<"):
            if r.get("outcome") not in ("passed", "skipped"):
                print("warning:", r["id"], r.get("outcome"), file=sys.stderr)
            if "lines" not in r:
                continue
            tests.append(r["id"])
            recs.append(r)

    sets, set_index = [], {}

    def intern(members):
        key = tuple(sorted(members))
        idx = set_index.get(key)
        if idx is None:
            idx = set_index[key] = len(sets)
            sets.append(list(key))
        return idx

    line_tests, open_tests, class_tests = {}, {}, {}
    for i, r in enumerate(recs):
        for path, lines in r["lines"].items():
            per = line_tests.setdefault(path, {})
            for ln in lines:
                per.setdefault(ln, []).append(i)
        for path in r["opens"]:
            if "__pycache__" in path:
                continue
            open_tests.setdefault(path, []).append(i)
        for name in r["classes"]:
            class_tests.setdefault(name, []).append(i)

    lines_out = {}
    for path in sorted(line_tests):
        flat = []
        for ln in sorted(line_tests[path]):
            flat += [ln, intern(line_tests[path][ln])]
        lines_out[path] = flat
    out = {
        "tests": tests,
        "sets": None,
        "lines": lines_out,
        "opens": {p: intern(v) for p, v in sorted(open_tests.items())},
        "classes": {c: intern(v) for c, v in sorted(class_tests.items())},
        "import_lines": {p: sorted(v) for p, v in sorted((imp or {}).get("lines", {}).items())},
        "import_classes": sorted(imp_classes),
    }
    out["sets"] = sets
    with open(dst, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{len(tests)} tests, {len(sets)} sets, {sum(len(s) for s in sets)} set entries", file=sys.stderr)


if __name__ == "__main__":
    main()
