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
        # Tests that matter only if the patch changes what import builds.
        # The selector checks that with blast/probe.py; `allowed` lists
        # fingerprint key patterns the edit is expected to change.
        self.if_import_changed = set()
        self.allowed = []
        self.import_paths = set()
        self.stmt_lines = {}
        self.has_non_stmt = False

    def add(self, tests, note):
        self.tests |= set(tests)
        self.notes.append(f"{note}:{len(tests)}")

    def everything(self, cov, note):
        self.fallback = True
        self.has_non_stmt = True
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


def same_behaviour(old, new):
    """True when two sources parse to the same tree, docstrings aside."""
    try:
        a = ast.dump(strip_docstrings(ast.parse(old)))
        b = ast.dump(strip_docstrings(ast.parse(new)))
    except SyntaxError:
        return False
    return a == b


class PySource:
    """Parsed source with parent links and per-line lookups."""

    def __init__(self, text):
        self.text = text
        self.lines = text.split("\n")
        self.tree = ast.parse(text)
        self.parent = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node

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
            hits = [i + 1 for i, line in enumerate(t.split("\n")) if pat.search(line)]
            if not hits:
                continue
            tests |= self.cov.tests_for_lines(p, hits)
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
        # imported while collecting: everyone has it, which is only true if
        # the change alters the import itself; the probe decides that
        self.import_sensitive.add(path)
        return self.module_users(path)

    def module_users(self, path):
        """Tests that ran code of the module, opened it, or built one of its classes."""
        out = set()
        for s in set(self.cov.lines(path).values()):
            out.update(self.cov.sets[s])
        out |= self.cov.opened(path)
        prefix = module_name(path) + "."
        for qual, s in self.cov.classes.items():
            if qual.startswith(prefix):
                out.update(self.cov.sets[s])
        return out

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
        self.import_sensitive = set()
        self._analyze(fd, res)
        if self.import_sensitive:
            # module_tests() narrowed "everyone imports it" to the module's
            # users; the selector widens back unless the import probe shows
            # the patched modules import cleanly and build the same state
            res.if_import_changed |= self.cov.all
            res.import_paths |= self.import_sensitive

    def _analyze(self, fd, res):
        old_path, new_path = fd.old_path, fd.new_path
        path = new_path or old_path
        if fd.binary:
            res.has_non_stmt = True
            res.add(self.cov.opened(path), f"binary {path}")
            return
        if old_path is None:
            res.has_non_stmt = True
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
            res.has_non_stmt = True
            res.add(self.cov.opened(old_path), f"renamed {old_path}")
        if old_path.endswith(".py") and old_path.startswith(("sqlglot/", "tests/")):
            self.python(old_path, old, new, fd, res)
        else:
            res.has_non_stmt = True
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
            res.has_non_stmt = True
            res.add(self.module_tests(path), f"deleted {path}")
            return
        if same_behaviour(old, new):
            res.notes.append(f"no-op {path}")
            return
        src = self.source(path)
        if src is None:
            res.everything(self.cov, f"unparsable {path}")
            return
        try:
            new_src = PySource(new)
        except SyntaxError:
            res.has_non_stmt = True
            res.add(self.module_tests(path), f"syntax error {path}")
            return
        for ch in patchlib.changes(fd):
            self.one_change(path, src, new_src, ch, res)

    def one_change(self, path, src, new_src, ch, res):
        if ch.kind == "mod" and self.dict_entry_change(path, src, new_src, ch, res):
            return
        self._one_change(path, src, new_src, ch, res)

    # --- one entry of a class-level table -------------------------------------------------

    # tables keyed by SQL text (function names, keywords, format tokens): a
    # test can only reach an entry by writing its key somewhere
    _TEXT_TABLES = re.compile(r"^(FUNCTIONS|NO_PAREN_FUNCTIONS|KEYWORDS|[A-Z_]*_PARSERS|[A-Z_]*_MAPPING)$")

    @staticmethod
    def _entry_key(src, line):
        """Key node of the class-level dict entry on `line`, or None.

        Only entries that sit on one line and belong to a dict that is the
        value of a class-body assignment qualify."""
        col = src.first_col(line)
        chain = src.chain_at(line, col)
        for i, node in enumerate(chain):
            if isinstance(node, FUNC_NODES):
                break
            if isinstance(node, ast.Dict) and i + 1 < len(chain):
                child = chain[i + 1]
                for k, v in zip(node.keys, node.values):
                    if k is None:
                        continue
                    if child is k or child is v:
                        first = k.lineno
                        last = v.end_lineno
                        owner = [n for n in chain[:i] if isinstance(n, (ast.Assign, ast.AnnAssign))]
                        if first == line == last and owner and any(isinstance(n, ast.ClassDef) for n in chain[:i]):
                            tgt = owner[-1].targets[0] if isinstance(owner[-1], ast.Assign) else owner[-1].target
                            k.table = tgt.id if isinstance(tgt, ast.Name) else ""
                            return k
                        return None
        return None

    def _key_tests(self, key):
        """Tests that could reach entry `key`; None when we cannot tell."""
        if isinstance(key, ast.Attribute) and isinstance(key.value, ast.Name) and key.value.id == "exp":
            name = key.attr
            out, found = set(), False
            for qual, s in self.cov.classes.items():
                if qual.startswith("sqlglot.expressions.") and qual.rsplit(".", 1)[-1] == name:
                    out.update(self.cov.sets[s])
                    found = True
            return out if found else set()
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and len(key.value.strip()) >= 2
            and self._TEXT_TABLES.match(getattr(key, "table", ""))
        ):
            return self.tests_mentioning(key.value)
        return None

    def tests_mentioning(self, text):
        """Tests whose test class source, or a fixture they opened, contains `text`
        (case-insensitive)."""
        needle = text.lower()
        out = set()
        for path in self.py_files():
            if not path.startswith("tests/"):
                continue
            t = self.text(path) or ""
            if needle not in t.lower():
                continue
            src = self.source(path)
            if src is None:
                out |= self.cov.by_file.get(path, set())
                continue
            hit_module_level = False
            for node in src.tree.body:
                seg = "\n".join(src.lines[node.lineno - 1:node.end_lineno]).lower()
                if needle not in seg:
                    continue
                if isinstance(node, ast.ClassDef):
                    for i, tid in enumerate(self.cov.tests):
                        if tid.startswith(f"{path}::{node.name}::"):
                            out.add(i)
                else:
                    hit_module_level = True
            if hit_module_level:
                # a helper or constant: every test of the file, and of files using it
                out |= self.test_module_tests(path)
        for fpath, s in self.cov.opens.items():
            if fpath.startswith("dir:") or fpath.endswith(".py"):
                continue
            t = self.text(fpath)
            if t and needle in t.lower():
                out.update(self.cov.sets[s])
        return out

    def dict_entry_change(self, path, src, new_src, ch, res):
        """A one-line edit of one entry of a class-level table (FUNCTIONS,
        KEYWORDS, TRANSFORMS...). Narrow the usual class-users answer to the
        tests that can reach that entry: for an expression-class key, the
        tests that built that expression; for a string key, the tests whose
        source or fixtures mention it. Returns False when it does not apply."""
        if not ch.new_lines:
            return False
        k_old = self._entry_key(src, ch.old_line)
        k_new = self._entry_key(new_src, ch.new_lines[0])
        if k_old is None or k_new is None:
            return False
        reach = set()
        for k in (k_old, k_new):
            t = self._key_tests(k)
            if t is None:
                return False
            reach |= t
        part = Result()
        self._one_change(path, src, new_src, ch, part)
        if part.fallback:
            return False
        key_txt = ast.unparse(k_old)
        res.tests |= part.tests & reach
        res.notes.append(f"entry {key_txt}: {len(part.tests & reach)} of {len(part.tests)}")
        res.if_import_changed |= part.if_import_changed
        res.allowed += part.allowed
        res.import_paths |= part.import_paths
        res.has_non_stmt = True
        return True

    def _one_change(self, path, src, new_src, ch, res):
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
        imp = self.cov.import_lines.get(path)
        fl = set(range(func.lineno, func.end_lineno + 1))
        if imp and fl & imp:
            # the function runs while modules import (decorators, registries)
            res.if_import_changed |= self.module_tests(path)
            res.notes.append(f"import-time function {path}:{line}")
            res.has_non_stmt = True
        else:
            res.stmt_lines.setdefault(path, set()).update(lines)
        res.add(tests, f"stmt {path}:{line}")

    def definition_level(self, path, src, chain, line, res):
        res.has_non_stmt = True
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
            qual = module_name(path) + "." + ".".join(c.name for c in cls_chain)
            res.add(self.users(path, cls_chain), f"class header {stmt.name}")
            if qual in self.cov.import_classes:
                res.if_import_changed |= self.cov.all
                res.allowed.append(f"{module_name(path)}:{stmt.name}.")
                res.notes.append(f"class header {stmt.name} built at import")
            names.discard(stmt.name)
            cls_chain_for_names = None
        else:
            cls_chain_for_names = classes or None
        exclude = set(range(stmt.lineno, stmt.end_lineno + 1))
        for name in names:
            self.name_impact(path, name, cls_chain_for_names, exclude, res)
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            # a broken import fails the module's import
            res.if_import_changed |= self.module_tests(path)
            res.notes.append(f"import in {path}")

    def name_impact(self, path, name, cls_chain, exclude, res, depth=0):
        """Tests affected when the definition of `name` changes."""
        tests, at_import = self.refs(name)
        if cls_chain:
            qual = module_name(path) + "." + ".".join(c.name for c in cls_chain)
            if qual in self.cov.import_classes:
                res.if_import_changed |= self.cov.all
                res.allowed.append(f"{module_name(path)}:{'.'.join(c.name for c in cls_chain)}.")
                res.notes.append(f"{cls_chain[-1].name}.{name} built at import")
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
        res.has_non_stmt = True
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
