"""Turn the tracer output into the committed map, maps/coverage.json.

    python3 tools/covmap/build_map.py <trace.jsonl> maps/coverage.json <sqlglot checkout> <imports.json>

A statement spread over several lines only reports some of them as run, so
every line of a statement inside a function body (or of a compound
statement's header) gets the tests of the whole statement.

Layout (test sets are stored once and referred to by index):

    tests:   [node id, ...]            index = local test number
    sets:    [[test index, ...], ...]  sorted, deduplicated
    lines:   {path: [line, set, line, set, ...]}   function-body lines
    opens:   {path: set}               files a test opened ("dir:<path>" = listed)
    classes: {module.qualname: set}    tokenizer/parser/generator/dialect classes
                                       instantiated (any class in the MRO)
    import_lines: {path: [line, ...]}  function lines that ran during collection
    import_files: {path: [line, files, ...]}  lines a test file's import runs
                                       (files: index into import_filesets; "*" = all)
"""

import ast
import json
import os
import sys


def statement_spans(text):
    """Line spans of statements inside function bodies (compound: header only)."""
    tree = ast.parse(text)
    spans = []

    def visit(node, in_func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, True)
            elif isinstance(child, ast.ClassDef):
                visit(child, False)
            elif isinstance(child, ast.stmt):
                if in_func:
                    kids = [getattr(child, f, None) for f in ("body", "orelse", "finalbody")]
                    kids = [k for k in kids if isinstance(k, list) and k and isinstance(k[0], ast.stmt)]
                    kids += [h.body for h in getattr(child, "handlers", []) or []]
                    kids += [c.body for c in getattr(child, "cases", []) or []]
                    if kids:
                        first = min(k[0].lineno for k in kids)
                        spans.append(range(child.lineno, max(child.lineno, first - 1) + 1))
                    else:
                        spans.append(range(child.lineno, child.end_lineno + 1))
                visit(child, in_func)
            else:
                visit(child, in_func)

    visit(tree, False)
    return spans


def main():
    src, dst, root, imports_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    tests, recs, imp = [], [], None
    for raw in open(src):
        r = json.loads(raw)
        if r["id"] == "<import>":
            imp = r
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

    for path, per in line_tests.items():
        if not path.endswith(".py"):
            continue
        try:
            text = open(os.path.join(root, path), encoding="utf-8").read()
            spans = statement_spans(text)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for span in spans:
            hit = [per[ln] for ln in span if ln in per]
            if not hit or len(span) == 1:
                continue
            union = sorted(set().union(*map(set, hit)))
            for ln in span:
                per[ln] = union

    # import-time lines per test file, spread over whole statements like above
    imp_files = {}
    for tfile, per_path in json.load(open(imports_path)).items():
        for path, lns in per_path.items():
            imp_files.setdefault(path, {})
            for ln in lns:
                imp_files[path].setdefault(ln, set()).add(tfile)
    filesets, fileset_index = [], {}
    import_out = {}
    for path, per in sorted(imp_files.items()):
        try:
            spans = statement_spans(open(os.path.join(root, path), encoding="utf-8").read())
        except (OSError, SyntaxError, UnicodeDecodeError):
            spans = []
        for span in spans:
            hit = [per[ln] for ln in span if ln in per]
            if hit and len(span) > 1:
                union = set().union(*hit)
                for ln in span:
                    per[ln] = union
        flat = []
        for ln in sorted(per):
            key = tuple(sorted(per[ln]))
            if key not in fileset_index:
                fileset_index[key] = len(filesets)
                filesets.append(list(key))
            flat += [ln, fileset_index[key]]
        import_out[path] = flat

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
        "import_files": import_out,
        "import_filesets": filesets,
    }
    out["sets"] = sets
    with open(dst, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{len(tests)} tests, {len(sets)} sets, {sum(len(s) for s in sets)} set entries", file=sys.stderr)


if __name__ == "__main__":
    main()
