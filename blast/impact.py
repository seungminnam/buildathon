"""Which tests can a file change affect? Static analysis over the coverage map.

Every rule here answers "which tests execute, import or read what changed",
using only the pinned checkout and the coverage map built from it:

* A Python edit that leaves the syntax tree unchanged (comments, blank lines,
  formatting, docstrings) changes no behaviour: no tests.
* An edit inside a function body affects the tests that ran the edited
  statement (statement-level coverage).
* An edit at definition level (module or class body, a signature, a
  decorator, an import) changes a name; it affects the tests that ran a line
  referring to that name, narrowed to the tests that used the class for
  tokenizer/parser/generator/dialect classes.
* A data file (fixture) affects the tests that opened it; a new file affects
  the tests that listed its directory.
* Anything the rules cannot place falls back to every test of the module,
  or every test.
"""

import ast
import copy
import io
import os
import re
import tokenize

from . import patch as patchlib

FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
COMPONENT_BASES = {
    "sqlglot.dialects.dialect.Dialect",
    "sqlglot.tokens.Tokenizer",
    "sqlglot.parser.Parser",
    "sqlglot.generator.Generator",
}


class Result:
    """Tests a change may break, plus notes on how each part was decided."""

    def __init__(self):
        self.tests = set()
        self.notes = []
        self.fallback = False

    def add(self, tests, note):
        self.tests |= set(tests)
        self.notes.append(f"{note}:{len(tests)}")

    def everything(self, cov, note):
        self.fallback = True
        self.add(cov.all, "ALL " + note)


def module_name(path):
    p = path[:-3] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def strip_docstrings(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _is_docstring(st):
    return isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)


def _same_body(xs, ys, old_lines, new_lines):
    if xs and ys and _is_docstring(xs[0]) and _is_docstring(ys[0]):
        xs, ys = xs[1:], ys[1:]
    if len(xs) != len(ys):
        return False
    for x, y in zip(xs, ys):
        if type(x) is not type(y):
            return False
        if old_lines[x.lineno - 1 : x.end_lineno] == new_lines[y.lineno - 1 : y.end_lineno]:
            continue
        if isinstance(x, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            # compare the header, then the body statement by statement
            hx = copy.copy(x)
            hy = copy.copy(y)
            hx.body = hy.body = []
            if ast.dump(hx) != ast.dump(hy):
                return False
            if not _same_body(x.body, y.body, old_lines, new_lines):
                return False
            continue
        if ast.dump(x) != ast.dump(y):
            return False
    return True


def same_behaviour(old, new, old_tree=None, new_tree=None):
    """True when two sources parse to the same tree, docstrings aside.

    Statements whose text is unchanged are skipped and classes and functions
    are compared statement by statement, which keeps this cheap on big modules."""
    try:
        a = old_tree or ast.parse(old)
        b = new_tree or ast.parse(new)
    except SyntaxError:
        return False
    return _same_body(a.body, b.body, old.split("\n"), new.split("\n"))


class PySource:
    """Parsed source with parent links and per-line lookups."""

    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        self.tree = ast.parse(text)

    def chain_at(self, line, col):
        """Nodes from the module down to the deepest one holding (line, col)."""
        chain = [self.tree]
        node = self.tree
        pos = (line, col)
        while True:
            nxt = None
            for child in ast.iter_child_nodes(node):
                if not hasattr(child, "lineno") or child.end_lineno is None:
                    continue
                start = (child.lineno, child.col_offset)
                end = (child.end_lineno, child.end_col_offset)
                if start <= pos < end:
                    nxt = child
                    break
            if nxt is None:
                # a position on a line but outside every child (e.g. "else:")
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt) and child.lineno <= line <= child.end_lineno:
                        nxt = child
                        break
            if nxt is None:
                return chain
            chain.append(nxt)
            node = nxt

    def first_col(self, line):
        if 1 <= line <= len(self.lines):
            txt = self.lines[line - 1]
            return len(txt) - len(txt.lstrip())
        return 0


def body_lists(stmt):
    out = []
    for name in ("body", "orelse", "finalbody"):
        lst = getattr(stmt, name, None)
        if isinstance(lst, list) and lst and isinstance(lst[0], ast.stmt):
            out.append(lst)
    for h in getattr(stmt, "handlers", []) or []:
        out.append(h.body)
    for c in getattr(stmt, "cases", []) or []:
        out.append(c.body)
    return out


def header_lines(stmt):
    """Lines that run whenever `stmt` runs (header of a compound statement)."""
    lists = body_lists(stmt)
    if not lists:
        return set(range(stmt.lineno, stmt.end_lineno + 1))
    first_child = min(lst[0].lineno for lst in lists)
    lines = set(range(stmt.lineno, max(stmt.lineno, first_child - 1) + 1))
    for lst in lists:
        lines.add(lst[0].lineno)
    return lines


class Analyzer:
    def __init__(self, cov, root, all_paths):
        self.cov = cov
        self.root = root
        self.all_paths = all_paths  # repo-relative files at the pinned commit
        self._src = {}
        self._text = {}
        self._py_files = None
        self._upper = {}

    # --- sources ------------------------------------------------------------------------

    def text(self, path):
        if path not in self._text:
            full = os.path.join(self.root, path)
            try:
                with open(full, encoding="utf-8") as f:
                    self._text[path] = f.read()
            except (OSError, UnicodeDecodeError):
                self._text[path] = None
        return self._text[path]

    def source(self, path):
        if path not in self._src:
            t = self.text(path)
            try:
                self._src[path] = PySource(t) if t is not None else None
            except SyntaxError:
                self._src[path] = None
        return self._src[path]

    def py_files(self):
        if self._py_files is None:
            self._py_files = [
                p for p in self.all_paths if p.endswith(".py") and p.startswith(("sqlglot/", "tests/"))
            ]
        return self._py_files

    # --- building blocks ----------------------------------------------------------------

    def structure(self, path):
        """(lines inside function bodies, {line: class chain} for class-body lines)."""
        key = ("struct", path)
        if key not in self._src:
            src = self.source(path)
            func_lines, class_lines = set(), {}
            if src is not None:
                def walk(node, classes, in_func):
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child, ast.ClassDef) and not in_func:
                            for ln in range(child.lineno, child.end_lineno + 1):
                                class_lines[ln] = classes + [child]
                            walk(child, classes + [child], False)
                        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            for st in child.body:
                                func_lines.update(range(st.lineno, st.end_lineno + 1))
                            walk(child, classes, True)
                        elif isinstance(child, ast.Lambda):
                            func_lines.update(range(child.body.lineno, child.body.end_lineno + 1))
                            walk(child, classes, in_func)
                        else:
                            walk(child, classes, in_func)
                walk(src.tree, [], False)
            self._src[key] = (func_lines, class_lines)
        return self._src[key]

    def stmt_lines(self, path):
        """{line: lines of the statement (or compound header) holding it}."""
        key = ("stmts", path)
        if key not in self._src:
            out = {}
            src = self.source(path)
            if src is not None:
                for node in ast.walk(src.tree):
                    if not isinstance(node, ast.stmt) or isinstance(node, ast.ClassDef):
                        continue
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    lines = frozenset(header_lines(node)) if body_lists(node) else frozenset(
                        range(node.lineno, node.end_lineno + 1))
                    for ln in lines:
                        old = out.get(ln)
                        if old is None or len(lines) < len(old):
                            out[ln] = lines
            self._src[key] = out
        return self._src[key]

    def sql_name_tests(self, cls_node):
        """Tests whose SQL (test source or fixture files) names an expression class.

        Functions are parsed by name through a registry built at import, so no
        sqlglot line mentions the class; the SQL text in the tests does."""
        names = {re.sub(r"(?<!^)(?=[A-Z])", "_", cls_node.name).upper()}
        for st in cls_node.body:
            if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_sql_names" for t in st.targets):
                for el in ast.walk(st.value):
                    if isinstance(el, ast.Constant) and isinstance(el.value, str):
                        names.add(el.value.upper())
        pats = [re.compile(r"\b" + re.escape(n) + r"\b", re.IGNORECASE) for n in names if len(n) >= 3]
        out = set()
        for p in self.all_paths:
            if not p.startswith("tests/"):
                continue
            is_py = p.endswith(".py")
            if not is_py and not p.startswith("tests/fixtures/"):
                continue
            t = self.text(p)
            if not t:
                continue
            low = self._upper.get(p)
            if low is None:
                low = self._upper[p] = t.upper()
            if not any(n in low for n in names):
                continue
            if not is_py:
                if any(pt.search(t) for pt in pats):
                    out |= self.cov.opened(p)
                continue
            hits = [i + 1 for i, line in enumerate(t.split("\n")) if any(pt.search(line) for pt in pats)]
            if hits:
                out |= self.test_ids_at(p, hits)
        return out

    def test_ids_at(self, path, lines):
        """Tests whose method holds any of `lines`; the whole file otherwise."""
        key = ("testlines", path)
        if key not in self._src:
            local = {tid: i for i, tid in enumerate(self.cov.tests)}
            at = {}
            src = self.source(path)
            if src is not None:
                for cls in src.tree.body:
                    if not isinstance(cls, ast.ClassDef):
                        continue
                    for fn in cls.body:
                        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                            i = local.get(f"{path}::{cls.name}::{fn.name}")
                            if i is not None:
                                for ln in range(fn.lineno, fn.end_lineno + 1):
                                    at[ln] = i
            self._src[key] = at
        at = self._src[key]
        out = set()
        for ln in lines:
            i = at.get(ln)
            if i is None:
                return set(self.cov.by_file.get(path, set()))
            out.add(i)
        return out

    def mentions(self, name, files=None):
        """(path, line) of every line mentioning identifier `name`."""
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        out = []
        for p in files or self.py_files():
            t = self.text(p)
            if not t or name not in t:
                continue
            for i, line in enumerate(t.split("\n")):
                if name in line and pat.search(line):
                    out.append((p, i + 1))
        return out

    def users(self, path, cls_chain, depth=0):
        """Tests that use a class: built it (or a subclass), ran its methods,
        imported its module lazily, or (for plain classes) ran code naming it."""
        qual = module_name(path) + "." + ".".join(c.name for c in cls_chain)
        cls = cls_chain[-1]
        out = set(self.cov.tests_for_lines(path, range(cls.lineno, cls.end_lineno + 1)))
        inst = self.cov.instantiated(qual)
        if inst is not None:
            out |= inst
            out |= self.cov.opened(path)
        else:
            tests, _ = self.refs(cls.name)
            out |= tests
            if path.startswith("sqlglot/expressions/"):
                out |= self.sql_name_tests(cls)
        return out

    def refs(self, name, files=None):
        """Tests that ran a function-body line mentioning identifier `name`.

        Returns (tests, ran_at_import) where ran_at_import says a mentioning
        line ran while modules were imported (so the name feeds import-time
        work such as a metaclass)."""
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        tests, at_import = set(), False
        for p in files or self.py_files():
            t = self.text(p)
            if not t or name not in t:
                continue
            hits = [i + 1 for i, line in enumerate(t.split("\n")) if name in line and pat.search(line)]
            if not hits:
                continue
            # continuation lines carry their statement's tests (see build_map.py)
            tests |= self.cov.tests_for_lines(p, hits)
            # code that runs while test files import: those files fail to import
            tests |= self.cov.import_tests(p, hits)
            imp = self.cov.import_lines.get(p)
            if imp and any(h in imp for h in hits):
                at_import = True
        return tests, at_import

    def module_tests(self, path):
        """Tests that import the module at `path`."""
        if path.startswith("tests/"):
            return self.test_module_tests(path)
        opened = self.cov.opened(path)
        if opened:
            return opened
        return set(self.cov.all)  # imported while collecting: everyone has it

    def test_module_tests(self, path):
        out = set(self.cov.by_file.get(path, set()))
        mod = module_name(path)
        short = mod.rsplit(".", 1)[-1]
        for p in self.py_files():
            if not p.startswith("tests/") or p == path:
                continue
            t = self.text(p) or ""
            if mod in t or re.search(r"from \.+%s import|import \.+%s\b" % (short, short), t):
                out |= self.test_module_tests_direct(p)
        return out

    def test_module_tests_direct(self, path):
        return set(self.cov.by_file.get(path, set()))

    def class_tests(self, path, cls_chain):
        """Tests that use a class: instantiated it (or a subclass), or ran its methods."""
        qual = module_name(path) + "." + ".".join(c.name for c in cls_chain)
        inst = self.cov.instantiated(qual)
        cls = cls_chain[-1]
        ran = self.cov.tests_for_lines(path, range(cls.lineno, cls.end_lineno + 1))
        return inst, ran

    def is_component(self, path, cls_chain):
        qual = module_name(path) + "." + ".".join(c.name for c in cls_chain)
        return self.cov.instantiated(qual) is not None

    # --- per file -----------------------------------------------------------------------

    def analyze(self, fd, res):
        old_path, new_path = fd.old_path, fd.new_path
        path = new_path or old_path
        if fd.binary:
            res.add(self.cov.opened(path), f"binary {path}")
            return
        if old_path is None:
            self.new_file(fd, res)
            return
        old = self.text(old_path)
        if old is None:
            res.everything(self.cov, f"unknown file {old_path}")
            return
        new = patchlib.apply(old, fd) if new_path else None
        if new_path and new is None:
            res.everything(self.cov, f"patch does not apply {path}")
            return
        if new_path and old_path != new_path:
            # rename: the old path disappears
            res.add(self.cov.opened(old_path), f"renamed {old_path}")
        if old_path.endswith(".py") and old_path.startswith(("sqlglot/", "tests/")):
            self.python(old_path, old, new, fd, res)
        else:
            self.data_file(old_path, res)

    def data_file(self, path, res):
        res.add(self.cov.opened(path), f"opened {path}")

    def new_file(self, fd, res):
        path = fd.new_path
        d = os.path.dirname(path)
        res.add(self.cov.opened("dir:" + d), f"listed {d}")
        if path.endswith(".py") and path.startswith("sqlglot/"):
            # a new module is only imported by name; a sibling package might
            # import it lazily, which the listing above does not catch
            name = os.path.basename(path)[:-3]
            if name != "__init__":
                tests, _ = self.refs(name, [p for p in self.py_files() if p.startswith("sqlglot/")])
                res.add(tests, f"refs new module {name}")

    def python(self, path, old, new, fd, res):
        if new is None:
            res.add(self.module_tests(path), f"deleted {path}")
            return
        src = self.source(path)
        if src is None:
            res.everything(self.cov, f"unparsable {path}")
            return
        try:
            new_src = PySource(new)
        except SyntaxError:
            res.add(self.module_tests(path), f"syntax error {path}")
            return
        if same_behaviour(old, new, src.tree, new_src.tree):
            res.notes.append(f"no-op {path}")
            return
        for ch in patchlib.changes(fd):
            self.one_change(path, src, new_src, ch, res)

    def one_change(self, path, src, new_src, ch, res):
        positions = []
        if ch.kind == "mod":
            a, b = ch.old_text, ch.new_text
            p = 0
            while p < min(len(a), len(b)) and a[p] == b[p]:
                p += 1
            if a.strip() == "" or a.lstrip().startswith("#"):
                p = src.first_col(ch.old_line)
            positions.append((ch.old_line, min(p, max(len(a) - 1, 0))))
        elif ch.kind == "del":
            if ch.old_text.strip() == "" or ch.old_text.lstrip().startswith("#"):
                pass
            else:
                positions.append((ch.old_line, src.first_col(ch.old_line)))
        else:
            # insertion: place it on the statement it joins; look at the old
            # line after it and the last code line before it
            if all(t.strip() == "" or t.lstrip().startswith("#") for t in ch.new_text.split("\n")):
                return
            before = ch.old_line - 1
            while before >= 1 and (src.lines[before - 1].strip() == "" or src.lines[before - 1].lstrip().startswith("#")):
                before -= 1
            after = ch.old_line
            while after <= len(src.lines) and (src.lines[after - 1].strip() == "" or src.lines[after - 1].lstrip().startswith("#")):
                after += 1
            if before >= 1:
                positions.append((before, len(src.lines[before - 1]) - 1))
            if after <= len(src.lines):
                positions.append((after, src.first_col(after)))
            # what the inserted lines define
            for nl in ch.new_lines:
                self.new_side(path, new_src, nl, res)
        for line, col in positions:
            self.at_position(path, src, line, col, res, insertion=ch.kind == "ins")

    def at_position(self, path, src, line, col, res, insertion=False):
        chain = src.chain_at(line, max(col, 0))
        # innermost function whose body holds the position
        func_idx = None
        for i, node in enumerate(chain):
            if isinstance(node, FUNC_NODES) and i + 1 < len(chain):
                child = chain[i + 1]
                if isinstance(node, ast.Lambda):
                    if child is node.body:
                        func_idx = i
                elif child in node.body:
                    func_idx = i
        if func_idx is not None:
            self.in_function(path, src, chain, func_idx, line, res, insertion)
        else:
            self.definition_level(path, src, chain, line, res)

    def in_function(self, path, src, chain, func_idx, line, res, insertion):
        func = chain[func_idx]
        if isinstance(func, ast.Lambda):
            lines = set(range(func.body.lineno, func.body.end_lineno + 1))
        else:
            stmts = [n for n in chain[func_idx + 1:] if isinstance(n, ast.stmt)]
            stmt = stmts[-1]
            # a nested def/class header is a statement of its own
            lines = header_lines(stmt)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                lines = set(range(stmt.lineno, (stmt.body[0].lineno if stmt.body else stmt.lineno) + 1))
            if insertion and isinstance(stmt, ast.stmt):
                lines.add(stmt.lineno)
        tests = self.cov.tests_for_lines(path, lines)
        imp_tests = self.cov.import_tests(path, lines)
        if imp_tests:
            # the statement runs while test files import them
            res.add(imp_tests, f"import-time {path}:{line}")
        imp = self.cov.import_lines.get(path)
        if imp and lines & imp and not imp_tests:
            # ran during collection but not seen per file: assume the module's users
            res.add(self.module_tests(path), f"collection-time {path}:{line}")
        res.add(tests, f"stmt {path}:{line}")

    def definition_level(self, path, src, chain, line, res):
        classes = [n for n in chain if isinstance(n, ast.ClassDef)]
        stmts = [n for n in chain if isinstance(n, ast.stmt)]
        stmt = stmts[-1] if stmts else None
        if stmt is None:
            res.add(self.module_tests(path), f"module-level {path}:{line}")
            return
        # names this statement defines
        names = set()
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
            if isinstance(stmt, ast.ClassDef):
                classes = classes if classes and classes[-1] is stmt else classes + [stmt]
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
                    elif isinstance(n, ast.Attribute):
                        names.add(n.attr)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for a in stmt.names:
                names.add((a.asname or a.name).split(".")[0])
        else:
            res.add(self.module_tests(path), f"module statement {type(stmt).__name__} {path}:{line}")
            return

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # signature, defaults or decorators: every caller of the function
            body = set(range(stmt.body[0].lineno, stmt.end_lineno + 1))
            res.add(self.cov.tests_for_lines(path, body), f"signature {stmt.name}")

        if isinstance(stmt, ast.ClassDef):
            # class header (bases, decorators, keywords): everyone using the class
            cls_chain = classes[: classes.index(stmt) + 1] if stmt in classes else classes + [stmt]
            res.add(self.users(path, cls_chain), f"class header {stmt.name}")
            names.discard(stmt.name)
            cls_chain_for_names = None
        else:
            cls_chain_for_names = classes or None
        exclude = set(range(stmt.lineno, stmt.end_lineno + 1))
        for name in names:
            self.name_impact(path, name, cls_chain_for_names, exclude, res)
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            # a broken import fails the module's import
            res.add(self.module_tests(path), f"import in {path}")

    def name_impact(self, path, name, cls_chain, exclude, res, depth=0):
        """Tests affected when the definition of `name` changes."""
        tests, at_import = self.refs(name)
        if cls_chain:
            qual = module_name(path) + "." + ".".join(c.name for c in cls_chain)
            users = self.users(path, cls_chain)
            if self.cov.instantiated(qual) is not None:
                if at_import or not tests:
                    res.add(users, f"{cls_chain[-1].name}.{name} users")
                else:
                    res.add(tests & users, f"{cls_chain[-1].name}.{name} refs&users")
            else:
                res.add(users, f"{cls_chain[-1].name}.{name} class users")
            return
        res.add(tests, f"refs {name}")
        if at_import:
            res.add(self.module_tests(path), f"{name} used at import")
        for p, ln in self.mentions(name):
            if p == path and ln in exclude:
                continue
            func_lines, class_lines = self.structure(p)
            if ln in func_lines:
                continue
            chain = class_lines.get(ln)
            if chain:
                res.add(self.users(p, chain), f"{name} in class {chain[-1].name}")
                continue
            src = self.source(p)
            if src is None:
                res.add(self.module_tests(p), f"{name} in {p}")
                continue
            stmts = [n for n in src.chain_at(ln, src.first_col(ln)) if isinstance(n, ast.stmt)]
            st = stmts[0] if stmts else None
            if isinstance(st, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and depth < 2:
                targets = st.targets if isinstance(st, ast.Assign) else [st.target]
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name) and n.id != name:
                            self.name_impact(p, n.id, None, set(range(st.lineno, st.end_lineno + 1)), res,
                                             depth + 1)
                continue
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # default value or decorator of a function: its callers
                body = set(range(st.body[0].lineno, st.end_lineno + 1))
                res.add(self.cov.tests_for_lines(p, body), f"{name} in signature {st.name}")
                continue
            res.add(self.module_tests(p), f"{name} at module level in {p}")

    def new_side(self, path, new_src, nl, res):
        """Inserted lines that define something at module/class level."""
        chain = new_src.chain_at(nl, new_src.first_col(nl))
        for i, node in enumerate(chain):
            if isinstance(node, FUNC_NODES):
                return  # inside a function: handled through the old position
        stmts = [n for n in chain if isinstance(n, ast.stmt)]
        if not stmts:
            return
        stmt = stmts[-1]
        names = set()
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
                    elif isinstance(n, ast.Attribute):
                        names.add(n.attr)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            return  # a new import changes nothing unless it fails; failures show up below
        else:
            res.add(self.module_tests(path), f"new module statement {path}:{nl}")
            return
        classes = [n for n in chain if isinstance(n, ast.ClassDef) and n is not stmt]
        for name in names:
            tests, _ = self.refs(name)
            if classes:
                # a new class member may override an inherited one: the class's users
                res.add(self.users(path, classes), f"new {classes[-1].name}.{name}")
                continue
            res.add(tests, f"new {name}")
