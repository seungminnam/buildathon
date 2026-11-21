"""Generate validation commits: small edits to sqlglot at the pinned commit.

    python3 tools/mutate/gen.py <sqlglot checkout> <out dir> [--n 200] [--seed 1]

Writes <out dir>/<id>.patch (unified diffs in `git diff` form) and
<out dir>/kinds.json ({id: kind}). The edits copy the shape of the sample
commits (one-token operator and constant flips, inserted comments, corrupted
optimizer fixtures, combinations) and add kinds the samples do not have
(deleted statements, early returns, renamed functions, removed imports,
docstring and docs edits, test edits, new files, whitespace) so the selector
is judged on more than the 30 samples. Ground truth comes from label.py,
which runs the real tests on each patch.
"""

import argparse
import ast
import difflib
import io
import json
import os
import random
import re
import tokenize

COMMENTS = [
    "# Cheap check first",
    "# Keep in sync with the generator",
    "# Kept for backwards compatibility",
    "# Intentionally not cached",
    "# Mirrors the behavior of the reference implementation",
    "# TODO: revisit once the parser handles this natively",
    "# NOTE: order matters here",
    "# See the dialect docs for details",
]

OP_FLIPS = {
    "==": ["!="],
    "!=": ["=="],
    "<": ["<=", ">"],
    "<=": ["<"],
    ">": [">=", "<"],
    ">=": [">"],
    "+": ["-"],
    "-": ["+"],
    "*": ["//"],
}
NAME_FLIPS = {"True": "False", "False": "True", "and": "or", "or": "and"}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def diff(rel, old, new):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=3))
    if not body:
        return ""
    return f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body


def new_file_diff(rel, text):
    lines = text.splitlines(keepends=True)
    body = "".join(difflib.unified_diff([], lines, "/dev/null", f"b/{rel}", n=3))
    return f"diff --git a/{rel} b/{rel}\nnew file mode 100644\nindex 0000000..1111111\n" + body


class Source:
    """One python file with token sites for mutation."""

    def __init__(self, root, rel):
        self.rel = rel
        self.text = read(os.path.join(root, rel))
        self.lines = self.text.splitlines(keepends=True)
        self.tree = ast.parse(self.text)
        self.docstring_lines = set()
        self.func_ranges = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], "value", None), ast.Constant
                ) and isinstance(body[0].value.value, str):
                    for ln in range(body[0].lineno, body[0].end_lineno + 1):
                        self.docstring_lines.add(ln)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.func_ranges.append(node)
        self.tokens = list(tokenize.generate_tokens(io.StringIO(self.text).readline))

    def token_sites(self):
        sites = []
        toks = self.tokens
        for i, tok in enumerate(toks):
            if tok.start[0] != tok.end[0]:
                continue
            if tok.type == tokenize.OP and tok.string in OP_FLIPS:
                # skip unary minus / star-args / annotations "->"
                prev = toks[i - 1] if i else None
                if tok.string in "-+*" and prev is not None and (
                    prev.type == tokenize.OP and prev.string in "(,=[{:" or prev.string == "return"
                ):
                    continue
                for repl in OP_FLIPS[tok.string]:
                    sites.append((tok, repl))
            elif tok.type == tokenize.NAME and tok.string in NAME_FLIPS:
                sites.append((tok, NAME_FLIPS[tok.string]))
            elif tok.type == tokenize.NAME and tok.string == "is":
                nxt = toks[i + 1]
                if nxt.string == "not":
                    sites.append(((tok, nxt), "!="))
                else:
                    sites.append((tok, "=="))
            elif tok.type == tokenize.NAME and tok.string == "not":
                prev = toks[i - 1]
                if prev.string not in ("is",) and toks[i + 1].string != "in":
                    sites.append(((tok, toks[i + 1]), "<drop-not>"))
            elif tok.type == tokenize.NUMBER and re.fullmatch(r"\d+", tok.string):
                n = int(tok.string)
                sites.append((tok, str(n + 1)))
                if n > 0:
                    sites.append((tok, str(n - 1)))
            elif tok.type == tokenize.STRING and re.fullmatch(r"[\"'][A-Z_ ]{3,}[\"']", tok.string):
                q = tok.string[0]
                inner = tok.string[1:-1]
                sites.append((tok, q + inner[:-1] + q))
        return sites

    def replace_tok(self, tok, repl):
        if not isinstance(tok, tokenize.TokenInfo):
            first, second = tok
            (l1, c1), (l2, c2) = first.start, second.start
            line = self.lines[l1 - 1]
            if repl == "<drop-not>":
                new_line = line[:c1] + line[c2:]
            else:
                new_line = line[:c1] + repl + line[second.end[1]:]
            return self._with_line(l1, new_line)
        (ln, c1), (_, c2) = tok.start, tok.end
        line = self.lines[ln - 1]
        return self._with_line(ln, line[:c1] + repl + line[c2:])

    def _with_line(self, ln, new_line):
        lines = list(self.lines)
        lines[ln - 1] = new_line
        return "".join(lines)

    def code_lines(self):
        """Lines that hold a whole simple statement inside a function body."""
        out = []
        for fn in self.func_ranges:
            for node in ast.walk(fn):
                if isinstance(node, (ast.Assign, ast.Expr, ast.AugAssign, ast.Return, ast.Raise)) and (
                    node.lineno == node.end_lineno
                ) and node.lineno not in self.docstring_lines and node.lineno > fn.lineno:
                    out.append(node)
        return out


def valid_python(text):
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


def indent_of(line):
    return line[: len(line) - len(line.lstrip())]


class Gen:
    def __init__(self, root, seed):
        self.root = root
        self.rng = random.Random(seed)
        self.py = []
        for d, _, files in os.walk(os.path.join(root, "sqlglot")):
            for f in files:
                if f.endswith(".py"):
                    self.py.append(os.path.relpath(os.path.join(d, f), root))
        self.py.sort()
        self.fixtures = []
        for d, _, files in os.walk(os.path.join(root, "tests", "fixtures")):
            for f in files:
                if f.endswith(".sql"):
                    self.fixtures.append(os.path.relpath(os.path.join(d, f), root))
        self.fixtures.sort()
        self.tests_py = sorted(
            os.path.relpath(os.path.join(d, f), root)
            for d, _, files in os.walk(os.path.join(root, "tests"))
            for f in files
            if f.startswith("test_") and f.endswith(".py")
        )
        self.doc_files = sorted(
            f for f in ["README.md", "CHANGELOG.md", "CONTRIBUTING.md"] if os.path.exists(os.path.join(root, f))
        ) + sorted(
            os.path.relpath(os.path.join(d, f), root)
            for d, _, files in os.walk(os.path.join(root, "posts"))
            for f in files
            if f.endswith(".md")
        )
        self._src = {}
        self._sites = None

    def src(self, rel):
        if rel not in self._src:
            self._src[rel] = Source(self.root, rel)
        return self._src[rel]

    def all_sites(self):
        if self._sites is None:
            self._sites = []
            for rel in self.py:
                s = self.src(rel)
                for tok, repl in s.token_sites():
                    first = tok[0] if not isinstance(tok, tokenize.TokenInfo) else tok
                    if first.start[0] in s.docstring_lines:
                        continue
                    self._sites.append((rel, tok, repl))
        return self._sites

    # --- single edits: each returns (rel, new_text) or None -------------------------------

    def code(self):
        for _ in range(50):
            rel, tok, repl = self.rng.choice(self.all_sites())
            s = self.src(rel)
            new = s.replace_tok(tok, repl)
            if new != s.text and valid_python(new):
                return rel, new
        return None

    def comment(self, rel=None):
        rel = rel or self.rng.choice(self.py)
        s = self.src(rel)
        if not s.lines:
            return None
        for _ in range(50):
            ln = self.rng.randrange(1, len(s.lines) + 1)
            nxt = s.lines[ln - 1]
            if not nxt.strip() or ln in s.docstring_lines:
                continue
            lines = list(s.lines)
            lines.insert(ln - 1, indent_of(nxt) + self.rng.choice(COMMENTS) + "\n")
            new = "".join(lines)
            if valid_python(new):
                return rel, new
        return None

    def fixture(self):
        rel = self.rng.choice(self.fixtures)
        text = read(os.path.join(self.root, rel))
        lines = text.splitlines(keepends=True)
        idx = [i for i, l in enumerate(lines) if l.strip()]
        if not idx:
            return None
        i = self.rng.choice(idx)
        line = lines[i]
        words = [m.start() for m in re.finditer(r"\b\w", line)]
        op = self.rng.choice(["insert", "insert", "delete_word", "delete_char"])
        if op == "insert" and words:
            p = self.rng.choice(words)
            line = line[:p] + self.rng.choice(["42 ", "0 ", "NULL ", "1 ", "x "]) + line[p:]
        elif op == "delete_word" and words:
            p = self.rng.choice(words)
            m = re.match(r"\w+", line[p:])
            line = line[:p] + line[p + m.end():]
        else:
            body = line.rstrip("\n")
            if len(body) < 2:
                return None
            p = self.rng.randrange(len(body))
            line = body[:p] + body[p + 1:] + "\n"
        lines[i] = line
        return rel, "".join(lines)

    def delete_stmt(self):
        for _ in range(50):
            rel = self.rng.choice(self.py)
            s = self.src(rel)
            nodes = s.code_lines()
            if not nodes:
                continue
            node = self.rng.choice(nodes)
            lines = list(s.lines)
            ln = node.lineno
            lines[ln - 1] = indent_of(lines[ln - 1]) + "pass\n"
            new = "".join(lines)
            if valid_python(new):
                return rel, new
        return None

    def early_return(self):
        for _ in range(50):
            rel = self.rng.choice(self.py)
            s = self.src(rel)
            fns = [f for f in s.func_ranges if len(f.body) > 1]
            if not fns:
                continue
            fn = self.rng.choice(fns)
            stmt = self.rng.choice(fn.body[1:])
            lines = list(s.lines)
            first = stmt.lineno
            if stmt.decorator_list if hasattr(stmt, "decorator_list") else False:
                first = stmt.decorator_list[0].lineno
            lines.insert(first - 1, indent_of(lines[first - 1]) + "return None\n")
            new = "".join(lines)
            if valid_python(new):
                return rel, new
        return None

    def rename_function(self):
        for _ in range(50):
            rel = self.rng.choice(self.py)
            s = self.src(rel)
            if not s.func_ranges:
                continue
            fn = self.rng.choice(s.func_ranges)
            if fn.name.startswith("__"):
                continue
            lines = list(s.lines)
            line = lines[fn.lineno - 1]
            new_line = line.replace(f"def {fn.name}(", f"def {fn.name}_impl(", 1)
            if new_line == line:
                continue
            lines[fn.lineno - 1] = new_line
            return rel, "".join(lines)
        return None

    def drop_import(self):
        for _ in range(50):
            rel = self.rng.choice(self.py)
            s = self.src(rel)
            imps = [n for n in s.tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
                    and n.lineno == n.end_lineno and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")]
            if not imps:
                continue
            n = self.rng.choice(imps)
            lines = list(s.lines)
            del lines[n.lineno - 1]
            return rel, "".join(lines)
        return None

    def docstring(self):
        for _ in range(50):
            rel = self.rng.choice(self.py)
            s = self.src(rel)
            cand = sorted(ln for ln in s.docstring_lines if s.lines[ln - 1].strip() and ">>>" not in s.lines[ln - 1])
            if not cand:
                continue
            ln = self.rng.choice(cand)
            lines = list(s.lines)
            line = lines[ln - 1]
            words = [m.start() for m in re.finditer(r"\b[a-z]{4,}\b", line)]
            if not words:
                continue
            p = self.rng.choice(words)
            lines[ln - 1] = line[:p] + "usually " + line[p:]
            new = "".join(lines)
            if valid_python(new):
                return rel, new
        return None

    def docs(self):
        rel = self.rng.choice(self.doc_files)
        text = read(os.path.join(self.root, rel))
        lines = text.splitlines(keepends=True)
        i = self.rng.randrange(len(lines))
        lines.insert(i, self.rng.choice(["Note: this section is a draft.\n", "\n", "See also the docs.\n"]))
        return rel, "".join(lines)

    def whitespace(self):
        rel = self.rng.choice(self.py)
        s = self.src(rel)
        lines = list(s.lines)
        i = self.rng.randrange(len(lines))
        lines.insert(i, "\n")
        new = "".join(lines)
        return (rel, new) if valid_python(new) else None

    def test_edit(self):
        for _ in range(50):
            rel = self.rng.choice(self.tests_py)
            s = Source(self.root, rel)
            strs = [t for t in s.tokens if t.type == tokenize.STRING and t.start[0] == t.end[0]
                    and len(t.string) > 8 and t.start[0] not in s.docstring_lines]
            if not strs:
                continue
            tok = self.rng.choice(strs)
            q = 1
            while tok.string[q - 1] not in "'\"":
                q += 1
            # insert a space after the first word inside the literal
            inner = tok.string
            m = re.search(r"\w\b", inner[q:])
            if not m:
                continue
            p = q + m.end()
            new_tok = inner[:p] + " 1" + inner[p:]
            new = s.replace_tok(tok, new_tok)
            if valid_python(new):
                return rel, new
        return None

    def test_comment(self):
        rel = self.rng.choice(self.tests_py)
        s = Source(self.root, rel)
        self._src[rel] = s
        return self.comment(rel)

    def new_file(self):
        choice = self.rng.random()
        if choice < 0.4:
            rel = f"sqlglot/optimizer/helper_{self.rng.randrange(1000)}.py"
            text = '"""Small helpers."""\n\n\ndef identity(x):\n    return x\n'
        elif choice < 0.7:
            rel = f"tests/fixtures/optimizer/extra_{self.rng.randrange(1000)}.sql"
            text = "SELECT 1;\nSELECT 1 AS \"1\";\n"
        else:
            rel = f"docs/notes_{self.rng.randrange(1000)}.md"
            text = "# Notes\n\nNothing here yet.\n"
        return ("new", rel, text)

    # --- commits -------------------------------------------------------------------------

    def make(self, kind):
        if kind == "new_file":
            _, rel, text = self.new_file()
            return new_file_diff(rel, text)
        if kind == "multi":
            parts = []
            used = set()
            picks = self.rng.sample(["code", "comment", "fixture", "code", "comment"], self.rng.choice([2, 3]))
            for p in picks:
                r = getattr(self, p)()
                if r and r[0] not in used:
                    used.add(r[0])
                    parts.append(diff(r[0], self.src(r[0]).text if r[0].endswith(".py") else read(os.path.join(self.root, r[0])), r[1]))
            return "".join(sorted(parts))
        r = getattr(self, kind)()
        if not r:
            return ""
        rel, new = r
        return diff(rel, read(os.path.join(self.root, rel)), new)


MIX = [
    ("code", 40),
    ("fixture", 12),
    ("comment", 8),
    ("multi", 10),
    ("delete_stmt", 6),
    ("early_return", 4),
    ("rename_function", 3),
    ("drop_import", 2),
    ("docstring", 3),
    ("docs", 2),
    ("whitespace", 2),
    ("test_edit", 4),
    ("test_comment", 2),
    ("new_file", 2),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("out")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--prefix", default="v")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    g = Gen(args.root, args.seed)
    kinds_path = os.path.join(args.out, "kinds.json")
    kinds = json.load(open(kinds_path)) if os.path.exists(kinds_path) else {}
    names = [k for k, _ in MIX]
    weights = [w for _, w in MIX]
    made = 0
    while made < args.n:
        kind = g.rng.choices(names, weights)[0]
        patch = g.make(kind)
        if not patch:
            continue
        cid = f"{args.prefix}{args.seed:02d}{made:04d}"
        with open(os.path.join(args.out, cid + ".patch"), "w") as f:
            f.write(patch)
        kinds[cid] = kind
        made += 1
    with open(kinds_path, "w") as f:
        json.dump(kinds, f, indent=0, sort_keys=True)


if __name__ == "__main__":
    main()
