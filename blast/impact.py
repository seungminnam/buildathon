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
        self.import_stmt_lines = {}
        self.has_non_stmt = False
        self.class_quals = set()
        self.class_line_pairs = []
        self.class_class_pairs = []
        self.has_non_class = False

    def add(self, tests, note):
        self.tests |= set(tests)
        self.notes.append(f"{note}:{len(tests)}")

    def everything(self, cov, note):
        self.fallback = True
        self.has_non_stmt = True
        self.has_non_class = True
        self.add(cov.all, "ALL " + note)


# --- SQL fixture parsing (matching tests/helpers.py) ------------------------------------

_SQL_FIXTURE_LIST_FILES = {
    "tests/fixtures/identity.sql",
    "tests/fixtures/partial.sql",
    "tests/fixtures/optimizer/qualify_columns__invalid.sql",
}


def _filter_sql_comments(text):
    return "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))


def parse_sql_fixtures(text):
    """Replicate tests.helpers.load_sql_fixtures on raw text."""
    out, lines = [], []
    for line in _filter_sql_comments(text).splitlines():
        lines.append(line)
        if not line.startswith("#"):
            out.append("\n".join(lines))
            lines.clear()
    return out


def parse_sql_fixture_pairs(text):
    """Replicate tests.helpers.load_sql_fixture_pairs on raw text; None on malformed meta."""
    statements = _filter_sql_comments(text).split(";")
    size = len(statements)
    pairs = []
    for i in range(0, size, 2):
        if i + 1 < size:
            sql_raw = statements[i].strip()
            meta = {}
            sql_lines = sql_raw.split("\n")
            j = 0
            while j < len(sql_lines) and sql_lines[j].startswith("#"):
                if ":" not in sql_lines[j]:
                    return None
                k, v = sql_lines[j].split(":", maxsplit=1)
                meta[k.lstrip("#").strip()] = v.strip()
                j += 1
            sql = "\n".join(sql_lines[j:])
            expected = statements[i + 1].strip()
            pairs.append((meta, sql, expected))
    return pairs


def is_sql_fixture_pair_file(path):
    return (
        path == "tests/fixtures/pretty.sql"
        or (path.startswith("tests/fixtures/optimizer/") and path.endswith(".sql") and path not in _SQL_FIXTURE_LIST_FILES)
    )


def tpc_sql_changed_indices(old_text, new_text):
    """Return (exec_changed, opt_changed) index sets for a TPC fixture pair file.

    Each value is a set of changed query indices, or None if the query count
    changed or parsing failed (meaning all queries must be treated as changed).
    """
    a = parse_sql_fixture_pairs(old_text)
    b = parse_sql_fixture_pairs(new_text)
    if a is None or b is None or len(a) != len(b):
        return None, None
    exec_changed = set()
    opt_changed = set()
    for i, ((ma, sa, ea), (mb, sb, eb)) in enumerate(zip(a, b)):
        ma_nt = {k: v for k, v in ma.items() if k != "title"}
        mb_nt = {k: v for k, v in mb.items() if k != "title"}
        if ma_nt != mb_nt or sa != sb:
            exec_changed.add(i)
            opt_changed.add(i)
        elif ea != eb:
            opt_changed.add(i)
    return exec_changed, opt_changed


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


_PASS_BODY = [ast.Pass()]


def _drop_doc(body):
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:] or _PASS_BODY
    return body


def _same_ast(a, b, hint_line=None):
    """Fast structural AST equality ignoring docstrings and source locations."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        doc_owner = isinstance(a, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for field in a._fields:
            va = getattr(a, field)
            vb = getattr(b, field)
            if doc_owner and field == "body":
                va = _drop_doc(va)
                vb = _drop_doc(vb)
                if hint_line is not None and len(va) == len(vb) and len(va) > 1:
                    hi = None
                    for idx, st in enumerate(va):
                        end = getattr(st, "end_lineno", None) or getattr(st, "lineno", 0)
                        if getattr(st, "lineno", 0) <= hint_line <= end:
                            hi = idx
                            break
                    if hi is not None:
                        if not _same_ast(va[hi], vb[hi], hint_line):
                            return False
                        for idx in range(len(va)):
                            if idx != hi and not _same_ast(va[idx], vb[idx]):
                                return False
                        continue
            if not _same_ast(va, vb):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        for xa, xb in zip(a, b):
            if not _same_ast(xa, xb):
                return False
        return True
    return a == b


def same_behaviour(old, new):
    """True when two sources parse to the same tree, docstrings aside."""
    try:
        return _same_ast(ast.parse(old), ast.parse(new))
    except SyntaxError:
        return False


class PySource:
    """Parsed source with per-line lookups."""

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
        self._text_lower = {}
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

    def text_lower(self, path):
        if path not in self._text_lower:
            t = self.text(path)
            self._text_lower[path] = t.lower() if t is not None else None
        return self._text_lower[path]

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

    def refs_with_lines(self, name, files=None):
        """Like refs(), also returning {path: set(lines)} of mentioning lines."""
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        tests, at_import, lines_map = set(), False, {}
        for p in files or self.py_files():
            t = self.text(p)
            if not t or name not in t:
                continue
            hits = [i + 1 for i, line in enumerate(t.split("\n")) if name in line and pat.search(line)]
            if not hits:
                continue
            lines_map[p] = set(hits)
            tests |= self.cov.tests_for_lines(p, hits)
            imp = self.cov.import_lines.get(p)
            if imp and any(h in imp for h in hits):
                at_import = True
        return tests, at_import, lines_map

    def refs(self, name, files=None):
        """Tests that ran a function-body line mentioning identifier `name`.

        Returns (tests, ran_at_import) where ran_at_import says a mentioning
        line ran while modules were imported (so the name feeds import-time
        work such as a metaclass)."""
        tests, at_import, _ = self.refs_with_lines(name, files)
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
            res.has_non_class = True
            res.add(self.cov.opened(path), f"binary {path}")
            return
        if old_path is None:
            res.has_non_stmt = True
            res.has_non_class = True
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
            res.has_non_class = True
            res.add(self.cov.opened(old_path), f"renamed {old_path}")
            if old_path.endswith(".py") and old_path.startswith("sqlglot/"):
                # sqlglot also imports modules by name at run time (a dialect
                # is loaded from sqlglot.dialects.<name>), so nothing may import
                # the old path statically and still every user of it breaks
                users = self.module_users(old_path)
                res.add(users, f"renamed module users {old_path}")
        if old_path.endswith(".py") and old_path.startswith(("sqlglot/", "tests/")):
            self.python(old_path, old, new, fd, res)
        else:
            self.data_file(old_path, old, new, res)

    def data_file(self, path, old, new, res):
        if new is not None and path.endswith(".sql"):
            if is_sql_fixture_pair_file(path):
                a = parse_sql_fixture_pairs(old)
                b = parse_sql_fixture_pairs(new)
                if a is not None and b is not None and len(a) == len(b):
                    same = all(
                        sa == sb
                        and ea == eb
                        and {k: v for k, v in ma.items() if k != "title"}
                        == {k: v for k, v in mb.items() if k != "title"}
                        for (ma, sa, ea), (mb, sb, eb) in zip(a, b)
                    )
                    if same:
                        res.notes.append(f"no-op {path}")
                        return
            elif path in _SQL_FIXTURE_LIST_FILES:
                if parse_sql_fixtures(old) == parse_sql_fixtures(new):
                    res.notes.append(f"no-op {path}")
                    return
        opened = self.cov.opened(path)
        if not (path.startswith("tests/fixtures/") and "/tpc-" not in path):
            res.has_non_stmt = True
        res.add(opened, f"opened {path}")

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
            res.has_non_class = True
            res.add(self.module_tests(path), f"deleted {path}")
            return
        src = self.source(path)
        if src is None:
            res.everything(self.cov, f"unparsable {path}")
            return
        try:
            new_src = PySource(new)
        except SyntaxError:
            res.has_non_stmt = True
            res.has_non_class = True
            res.add(self.module_tests(path), f"syntax error {path}")
            return
        changes = list(patchlib.changes(fd))
        hint_line = None
        if changes:
            c0 = changes[0]
            hint_line = getattr(c0, "old_line", None) or getattr(c0, "after", None)
        if _same_ast(src.tree, new_src.tree, hint_line):
            res.notes.append(f"no-op {path}")
            return
        dict_cache = {}
        for ch in changes:
            self.one_change(path, src, new_src, ch, res, dict_cache)

    def one_change(self, path, src, new_src, ch, res, dict_cache=None):
        if self.dict_entry_change(path, src, new_src, ch, res, dict_cache):
            return
        self._one_change(path, src, new_src, ch, res)

    # --- entries of a class-level or module-level table -----------------------------------

    # tables keyed by SQL text (function names, keywords, format tokens): a
    # test can only reach an entry by writing its key somewhere
    _TEXT_TABLES = re.compile(r"^(FUNCTIONS|NO_PAREN_FUNCTIONS|KEYWORDS|[A-Z_]*_PARSERS|[A-Z_]*_MAPPING)$")

    @staticmethod
    def _enclosing_dict(src, line):
        """Return (cls_names_tuple, target_name, dict_node) if `line` sits inside a
        class-body or module-body dict assignment, else None."""
        if not (1 <= line <= len(src.lines)):
            return None
        col = src.first_col(line)
        chain = src.chain_at(line, col)
        for i, node in enumerate(chain):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            if isinstance(node, ast.Dict):
                if i == 0:
                    return None
                owner = chain[i - 1]
                if not isinstance(owner, (ast.Assign, ast.AnnAssign)) or owner.value is not node:
                    return None
                if isinstance(owner, ast.Assign):
                    if len(owner.targets) != 1 or not isinstance(owner.targets[0], ast.Name):
                        return None
                    target_name = owner.targets[0].id
                else:
                    if not isinstance(owner.target, ast.Name):
                        return None
                    target_name = owner.target.id
                if any(isinstance(n, FUNC_NODES) for n in chain[:i]):
                    return None
                cls_names = tuple(n.name for n in chain[:i] if isinstance(n, ast.ClassDef))
                return cls_names, target_name, node
        return None

    @staticmethod
    def _find_dict_by_owner(src, cls_names, target_name):
        """Find the unique dict assigned to `target_name` inside `cls_names`."""
        container = src.tree
        for cname in cls_names:
            matches = [n for n in container.body if isinstance(n, ast.ClassDef) and n.name == cname]
            if len(matches) != 1:
                return None
            container = matches[0]
        found = []
        for st in container.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                if st.targets[0].id == target_name:
                    found.append(st.value)
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                if st.target.id == target_name:
                    found.append(st.value)
        if len(found) == 1 and isinstance(found[0], ast.Dict):
            return found[0]
        return None

    @staticmethod
    def _is_exp_attr(node):
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "exp"
        )

    @classmethod
    def _expand_dict(cls, dict_node):
        """Return (spread_dumps, [(key_dump, key_node, val_dump), ...])."""
        spreads = []
        entries = []
        for k, v in zip(dict_node.keys, dict_node.values):
            if k is None:
                if (
                    isinstance(v, ast.DictComp)
                    and len(v.generators) == 1
                    and not v.generators[0].ifs
                    and isinstance(v.key, ast.Name)
                    and isinstance(v.generators[0].target, ast.Name)
                    and v.key.id == v.generators[0].target.id
                    and isinstance(v.generators[0].iter, (ast.Set, ast.Tuple, ast.List))
                    and v.generators[0].iter.elts
                    and all(cls._is_exp_attr(el) for el in v.generators[0].iter.elts)
                ):
                    vd = ast.dump(v.value)
                    for el in v.generators[0].iter.elts:
                        entries.append((ast.dump(el), el, vd))
                else:
                    spreads.append(ast.dump(v))
            else:
                entries.append((ast.dump(k), k, ast.dump(v)))
        return spreads, entries

    def _diff_dict_table(self, path, src, new_src, cls_names, target_name, dict_cache):
        """Diff old vs new dict for `(cls_names, target_name)`.

        Returns:
          - None if narrowing does not safely apply
          - ("noop", None, None, None) if the dict AST is completely unchanged
          - ("ok", reach_set, changed_key_nodes, all_exp_quals)
        """
        ckey = (cls_names, target_name)
        if dict_cache is not None and ckey in dict_cache:
            return dict_cache[ckey]

        def _store(val):
            if dict_cache is not None:
                dict_cache[ckey] = val
            return val

        old_dict = self._find_dict_by_owner(src, cls_names, target_name)
        new_dict = self._find_dict_by_owner(new_src, cls_names, target_name)
        if old_dict is None or new_dict is None:
            return _store(None)
        old_spreads, old_entries = self._expand_dict(old_dict)
        new_spreads, new_entries = self._expand_dict(new_dict)
        if old_spreads != new_spreads:
            return _store(None)
        if [(kd, vd) for kd, _, vd in old_entries] == [(kd, vd) for kd, _, vd in new_entries]:
            return _store(("noop", None, None, None))
        old_vals, old_node = {}, {}
        for kd, k, vd in old_entries:
            old_vals.setdefault(kd, []).append(vd)
            old_node[kd] = k
        new_vals, new_node = {}, {}
        for kd, k, vd in new_entries:
            new_vals.setdefault(kd, []).append(vd)
            new_node[kd] = k
        changed_kds = {
            kd
            for kd in (old_vals.keys() | new_vals.keys())
            if old_vals.get(kd) != new_vals.get(kd)
        }
        # Reject edits that reorder unchanged keys (dict iteration order can matter)
        old_order = [kd for kd, _, _ in old_entries if kd not in changed_kds]
        new_order = [kd for kd, _, _ in new_entries if kd not in changed_kds]
        if old_order != new_order or not changed_kds:
            return _store(None)

        reach = set(self.cov.opened(path))
        if target_name == "EXPRESSION_METADATA":
            reach |= self.cov.by_file.get("tests/test_typing.py", set())
        changed_nodes = []
        exp_quals = set()
        all_exp = True
        for kd in sorted(changed_kds):
            k = old_node[kd] if kd in old_node else new_node[kd]
            if not cls_names and not self._is_exp_attr(k):
                return _store(None)
            k.table = target_name
            t = self._key_tests(k)
            if t is None:
                return _store(None)
            reach |= t
            changed_nodes.append(k)
            if self._is_exp_attr(k):
                ename = k.attr
                for qual in self.cov.classes:
                    if qual.startswith("sqlglot.expressions.") and qual.rsplit(".", 1)[-1] == ename:
                        exp_quals.add(qual)
            else:
                all_exp = False
        return _store(("ok", reach, changed_nodes, exp_quals if all_exp else None))

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
        """Tests whose test method/class source, or a fixture they opened, contains
        `text` (case-insensitive)."""
        needle = text.lower()
        out = set()
        for path in self.py_files():
            if not path.startswith("tests/"):
                continue
            tl = self.text_lower(path)
            if not tl or needle not in tl:
                continue
            src = self.source(path)
            if src is None:
                out |= self.cov.by_file.get(path, set())
                continue
            llines = tl.split("\n")
            hit_module_level = False
            for node in src.tree.body:
                seg = "\n".join(llines[node.lineno - 1:node.end_lineno])
                if needle not in seg:
                    continue
                if isinstance(node, ast.ClassDef):
                    # Narrow to individual test_* methods unless needle appears in
                    # class-body code or a non-test helper/setup method.
                    hit_whole_class = False
                    hit_methods = set()
                    covered = set()
                    for item in node.body:
                        iseg = "\n".join(llines[item.lineno - 1:item.end_lineno])
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_"):
                            covered.update(range(item.lineno, item.end_lineno + 1))
                            if needle in iseg:
                                hit_methods.add(item.name)
                        elif needle in iseg:
                            hit_whole_class = True
                            break
                    if not hit_whole_class:
                        for ln in range(node.lineno, node.end_lineno + 1):
                            if ln not in covered and needle in llines[ln - 1]:
                                hit_whole_class = True
                                break
                    prefix = f"{path}::{node.name}::"
                    if hit_whole_class:
                        for i, tid in enumerate(self.cov.tests):
                            if tid.startswith(prefix):
                                out.add(i)
                    else:
                        wanted = {prefix + m for m in hit_methods}
                        for i, tid in enumerate(self.cov.tests):
                            if tid in wanted:
                                out.add(i)
                else:
                    hit_module_level = True
            if hit_module_level:
                # a helper or constant: every test of the file, and of files using it
                out |= self.test_module_tests(path)
        for fpath, s in self.cov.opens.items():
            if fpath.startswith("dir:") or fpath.endswith(".py"):
                continue
            tl = self.text_lower(fpath)
            if tl and needle in tl:
                out.update(self.cov.sets[s])
        return out

    def dict_entry_change(self, path, src, new_src, ch, res, dict_cache=None):
        """An edit (mod/ins/del, single- or multi-line) inside a class-level or
        module-level table (FUNCTIONS, KEYWORDS, TRANSFORMS, EXPRESSION_METADATA...).
        Narrow the usual class/module-users answer to the tests that can reach the
        changed entries. Returns False when it does not apply."""
        owner = None
        if ch.kind in ("mod", "del"):
            if ch.old_text.strip() == "" or ch.old_text.lstrip().startswith("#"):
                return False
            info = self._enclosing_dict(src, ch.old_line)
            if info is None:
                return False
            cls_names, target_name, old_dict = info
            if ch.kind == "mod" and ch.new_lines:
                for nl in ch.new_lines:
                    if 1 <= nl <= len(new_src.lines):
                        txt = new_src.lines[nl - 1].strip()
                        if txt and not txt.startswith("#"):
                            ninfo = self._enclosing_dict(new_src, nl)
                            if ninfo is None or (ninfo[0], ninfo[1]) != (cls_names, target_name):
                                return False
            owner = (cls_names, target_name, old_dict)
        else:
            code_nls = [
                nl
                for nl in ch.new_lines
                if 1 <= nl <= len(new_src.lines)
                and new_src.lines[nl - 1].strip() != ""
                and not new_src.lines[nl - 1].lstrip().startswith("#")
            ]
            if not code_nls:
                return False
            first = self._enclosing_dict(new_src, code_nls[0])
            if first is None:
                return False
            cls_names, target_name, _ = first
            for nl in code_nls[1:]:
                ni = self._enclosing_dict(new_src, nl)
                if ni is None or (ni[0], ni[1]) != (cls_names, target_name):
                    return False
            old_dict = self._find_dict_by_owner(src, cls_names, target_name)
            if old_dict is None:
                return False
            before = ch.old_line - 1
            while before >= 1 and (
                src.lines[before - 1].strip() == "" or src.lines[before - 1].lstrip().startswith("#")
            ):
                before -= 1
            after = ch.old_line
            while after <= len(src.lines) and (
                src.lines[after - 1].strip() == "" or src.lines[after - 1].lstrip().startswith("#")
            ):
                after += 1
            if not (old_dict.lineno <= before <= old_dict.end_lineno and old_dict.lineno <= after <= old_dict.end_lineno):
                return False
            owner = (cls_names, target_name, old_dict)

        cls_names, target_name, _ = owner
        diff = self._diff_dict_table(path, src, new_src, cls_names, target_name, dict_cache)
        if diff is None:
            return False
        kind, reach, changed_nodes, exp_quals = diff
        if kind == "noop":
            return True

        part = Result()
        self._one_change(path, src, new_src, ch, part)
        if part.fallback:
            return False
        key_txt = ",".join(ast.unparse(k) for k in changed_nodes[:3])
        if len(changed_nodes) > 3:
            key_txt += f"+{len(changed_nodes) - 3}"
        narrowed = part.tests & reach
        res.tests |= narrowed
        res.notes.append(f"entry {key_txt}: {len(narrowed)} of {len(part.tests)}")
        res.if_import_changed |= part.if_import_changed
        res.allowed += part.allowed
        res.import_paths |= part.import_paths
        for p, lns in part.stmt_lines.items():
            res.stmt_lines.setdefault(p, set()).update(lns)
        for p, lns in part.import_stmt_lines.items():
            res.import_stmt_lines.setdefault(p, set()).update(lns)
        res.has_non_stmt = res.has_non_stmt or part.has_non_stmt
        if exp_quals and part.class_quals and not part.has_non_class:
            for cq in part.class_quals:
                res.class_class_pairs.append((cq, exp_quals))
            res.class_line_pairs.extend(part.class_line_pairs)
        else:
            res.class_quals |= part.class_quals
            res.class_line_pairs.extend(part.class_line_pairs)
            res.class_class_pairs.extend(part.class_class_pairs)
        res.has_non_class = res.has_non_class or part.has_non_class
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
            self.at_position(path, src, new_src, line, col, res, insertion=ch.kind == "ins")

    def at_position(self, path, src, new_src, line, col, res, insertion=False):
        if (path == "sqlglot/generator.py" and (70 <= line <= 92 or 1003 <= line <= 1011)) or (
            path == "sqlglot/dialects/dialect.py" and 230 <= line <= 310
        ):
            res.everything(self.cov, f"lazy cache {path}:{line}")
            return
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
            self.definition_level(path, src, new_src, chain, line, res)

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
            # the function runs while modules import (decorators, registries);
            # keep its stmt_lines separately so if the import probe proves no
            # import state changed, downstream query/subtest filters still apply.
            res.if_import_changed |= self.module_tests(path)
            res.notes.append(f"import-time function {path}:{line}")
            res.import_stmt_lines.setdefault(path, set()).update(lines)
        else:
            res.stmt_lines.setdefault(path, set()).update(lines)
        res.add(tests, f"stmt {path}:{line}")

    def _tok_core_info(self):
        """Lazily map Tokenizer class attrs -> TokenizerCore read-site lines."""
        key = ("tok_core_info",)
        if key in self._src:
            return self._src[key]
        tok_map = {}
        init_core_range = (0, 0)
        src_tok = self.source("sqlglot/tokens.py")
        if src_tok is not None:
            for node in ast.walk(src_tok.tree):
                if isinstance(node, ast.FunctionDef) and node.name == "_init_core":
                    init_core_range = (node.lineno, node.end_lineno)
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "TokenizerCore":
                            for kw in sub.keywords:
                                attrs = [
                                    a.attr
                                    for a in ast.walk(kw.value)
                                    if isinstance(a, ast.Attribute)
                                    and isinstance(a.value, ast.Name)
                                    and a.value.id == "self"
                                ]
                                if len(attrs) == 1:
                                    tok_map[attrs[0]] = kw.arg
        core_use_lines = {}
        src_core = self.source("sqlglot/tokenizer_core.py")
        if src_core is not None:
            for node in ast.walk(src_core.tree):
                if isinstance(node, ast.ClassDef) and node.name == "TokenizerCore":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name != "__init__":
                            aliases = {}
                            for st in ast.walk(item):
                                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                                    v = st.value
                                    if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self":
                                        aliases[st.targets[0].id] = (v.attr, st.lineno)
                            alias_lines = {ln for _, ln in aliases.values()}
                            for sub in ast.walk(item):
                                if (
                                    isinstance(sub, ast.Attribute)
                                    and isinstance(sub.value, ast.Name)
                                    and sub.value.id == "self"
                                    and isinstance(sub.ctx, ast.Load)
                                    and sub.lineno not in alias_lines
                                ):
                                    core_use_lines.setdefault(sub.attr, set()).add(sub.lineno)
                                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) and sub.id in aliases:
                                    core_use_lines.setdefault(aliases[sub.id][0], set()).add(sub.lineno)
        dialect_new_range = (0, 0)
        src_dia = self.source("sqlglot/dialects/dialect.py")
        if src_dia is not None:
            for node in ast.walk(src_dia.tree):
                if isinstance(node, ast.ClassDef) and node.name == "_Dialect":
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef) and item.name == "__new__":
                            dialect_new_range = (item.lineno, item.end_lineno)
        info = (tok_map, init_core_range, core_use_lines, dialect_new_range)
        self._src[key] = info
        return info

    @staticmethod
    def _find_matching_func(new_src, classes, stmt):
        """Find the unique FunctionDef with the same class path and name in new_src."""
        container = new_src.tree
        for c in classes:
            matches = [n for n in container.body if isinstance(n, ast.ClassDef) and n.name == c.name]
            if len(matches) != 1:
                return None
            container = matches[0]
        fmatches = [
            n
            for n in container.body
            if type(n) is type(stmt) and n.name == stmt.name
        ]
        return fmatches[0] if len(fmatches) == 1 else None

    @staticmethod
    def _is_default_only_sig_change(old_fn, new_fn):
        """True when only default values or annotations changed on a def header."""
        if new_fn is None or type(old_fn) is not type(new_fn):
            return False
        if ast.dump(ast.Module(body=old_fn.decorator_list, type_ignores=[])) != ast.dump(
            ast.Module(body=new_fn.decorator_list, type_ignores=[])
        ):
            return False
        oa, na = old_fn.args, new_fn.args
        if [a.arg for a in oa.posonlyargs] != [a.arg for a in na.posonlyargs]:
            return False
        if [a.arg for a in oa.args] != [a.arg for a in na.args]:
            return False
        if (oa.vararg.arg if oa.vararg else None) != (na.vararg.arg if na.vararg else None):
            return False
        if [a.arg for a in oa.kwonlyargs] != [a.arg for a in na.kwonlyargs]:
            return False
        if (oa.kwarg.arg if oa.kwarg else None) != (na.kwarg.arg if na.kwarg else None):
            return False
        if len(oa.defaults) != len(na.defaults):
            return False
        if [d is None for d in oa.kw_defaults] != [d is None for d in na.kw_defaults]:
            return False
        return True

    def definition_level(self, path, src, new_src, chain, line, res):
        classes = [n for n in chain if isinstance(n, ast.ClassDef)]
        stmts = [n for n in chain if isinstance(n, ast.stmt)]
        stmt = stmts[-1] if stmts else None
        if stmt is None:
            res.has_non_stmt = True
            res.has_non_class = True
            res.add(self.module_tests(path), f"module-level {path}:{line}")
            return

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            new_fn = self._find_matching_func(new_src, classes, stmt)
            if self._is_default_only_sig_change(stmt, new_fn):
                first_line = min([stmt.lineno] + [d.lineno for d in stmt.decorator_list])
                b0 = stmt.body[0]
                if (
                    len(stmt.body) > 1
                    and isinstance(b0, ast.Expr)
                    and isinstance(b0.value, ast.Constant)
                    and isinstance(b0.value.value, str)
                ):
                    b0 = stmt.body[1]
                entry_lines = {first_line, stmt.lineno} | header_lines(b0)
                body = {first_line, stmt.lineno} | set(range(stmt.body[0].lineno, stmt.end_lineno + 1))
                res.import_stmt_lines.setdefault(path, set()).update(entry_lines)
                res.if_import_changed |= self.module_tests(path)
                res.add(self.cov.tests_for_lines(path, body), f"default {stmt.name}")
                return

        res.has_non_stmt = True
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
            res.has_non_class = True
            res.add(self.module_tests(path), f"module statement {type(stmt).__name__} {path}:{line}")
            return

        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # signature, defaults or decorators: every caller of the function
            res.has_non_class = True
            body = set(range(stmt.body[0].lineno, stmt.end_lineno + 1))
            res.add(self.cov.tests_for_lines(path, body), f"signature {stmt.name}")

        if isinstance(stmt, ast.ClassDef):
            # class header (bases, decorators, keywords): everyone using the class
            res.has_non_class = True
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
            res.has_non_class = True
            res.if_import_changed |= self.module_tests(path)
            res.notes.append(f"import in {path}")

    def name_impact(self, path, name, cls_chain, exclude, res, depth=0):
        """Tests affected when the definition of `name` changes."""
        tests, at_import, ref_lines = self.refs_with_lines(name)
        if cls_chain:
            qual = module_name(path) + "." + ".".join(c.name for c in cls_chain)
            if qual in self.cov.import_classes:
                res.if_import_changed |= self.cov.all
                res.allowed.append(f"{module_name(path)}:{'.'.join(c.name for c in cls_chain)}.")
                res.notes.append(f"{cls_chain[-1].name}.{name} built at import")
            users = self.users(path, cls_chain)
            if self.cov.instantiated(qual) is not None:
                # Tokenizer attrs forwarded into TokenizerCore: use TokenizerCore's
                # actual read sites instead of the import-time _init_core forwarding.
                if cls_chain[-1].name == "Tokenizer":
                    tok_map, init_rng, core_lines, dia_rng = self._tok_core_info()
                    core_attr = tok_map.get(name)
                    if core_attr and core_lines.get(core_attr):
                        t_core = self.cov.tests_for_lines("sqlglot/tokenizer_core.py", core_lines[core_attr])
                        t_other, other_imp, hit_dia_new = set(), False, False
                        core_ref_lines = {"sqlglot/tokenizer_core.py": set(core_lines[core_attr])}
                        for p, ln in self.mentions(name):
                            if p == "sqlglot/tokens.py" and init_rng[0] <= ln <= init_rng[1]:
                                continue
                            if p == "sqlglot/dialects/dialect.py" and dia_rng[0] <= ln <= dia_rng[1]:
                                hit_dia_new = True
                                continue
                            core_ref_lines.setdefault(p, set()).add(ln)
                            t_other |= self.cov.tests_for_lines(p, {ln})
                            imp = self.cov.import_lines.get(p)
                            if imp and ln in imp:
                                other_imp = True
                        if not other_imp:
                            if hit_dia_new:
                                res.if_import_changed |= users
                                res.allowed.append(f"{module_name(path)}:{'.'.join(c.name for c in cls_chain)}.{name}")
                            if all(p.startswith("sqlglot/") for p in core_ref_lines):
                                res.class_line_pairs.append((qual, core_ref_lines))
                            else:
                                res.class_quals.add(qual)
                            res.add((t_core | t_other) & users, f"{cls_chain[-1].name}.{name} core&users")
                            return
                if at_import or not tests:
                    res.class_quals.add(qual)
                    res.add(users, f"{cls_chain[-1].name}.{name} users")
                else:
                    if all(p.startswith("sqlglot/") for p in ref_lines):
                        res.class_line_pairs.append((qual, ref_lines))
                    else:
                        res.class_quals.add(qual)
                    res.add(tests & users, f"{cls_chain[-1].name}.{name} refs&users")
            else:
                res.has_non_class = True
                res.add(users, f"{cls_chain[-1].name}.{name} class users")
            return
        res.has_non_class = True
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
            res.has_non_class = True
            res.add(self.module_tests(path), f"new module statement {path}:{nl}")
            return
        classes = [n for n in chain if isinstance(n, ast.ClassDef) and n is not stmt]
        for name in names:
            tests, _ = self.refs(name)
            if classes:
                # a new class member may override an inherited one: the class's users
                qual = module_name(path) + "." + ".".join(c.name for c in classes)
                if self.cov.instantiated(qual) is not None:
                    res.class_quals.add(qual)
                else:
                    res.has_non_class = True
                res.add(self.users(path, classes), f"new {classes[-1].name}.{name}")
                continue
            res.has_non_class = True
            res.add(tests, f"new {name}")
