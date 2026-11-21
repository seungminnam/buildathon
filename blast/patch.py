"""Unified diff parsing and application."""

import re
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list = field(default_factory=list)  # (tag, text) with tag in " -+"


@dataclass
class FileDiff:
    old_path: str | None  # None for a new file
    new_path: str | None  # None for a deleted file
    hunks: list = field(default_factory=list)
    binary: bool = False

    @property
    def path(self):
        return self.new_path or self.old_path


def _strip_prefix(p):
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def parse(text):
    """Parse a (git) unified diff into FileDiffs. Tolerates missing headers."""
    files = []
    cur = None
    hunk = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FileDiff(m.group(1) if m else None, m.group(2) if m else None)
            files.append(cur)
            hunk = None
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _strip_prefix(line[4:])
            new = _strip_prefix(lines[i + 1][4:])
            if cur is None or cur.hunks:
                cur = FileDiff(old, new)
                files.append(cur)
            else:
                cur.old_path, cur.new_path = old, new
            hunk = None
            i += 1
        elif line.startswith("new file mode") and cur is not None:
            cur.old_path = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur.new_path = None
        elif line.startswith("rename from ") and cur is not None:
            cur.old_path = line[len("rename from "):]
        elif line.startswith("rename to ") and cur is not None:
            cur.new_path = line[len("rename to "):]
        elif line.startswith("Binary files") and cur is not None:
            cur.binary = True
        elif line.startswith("@@") and cur is not None:
            m = HUNK.match(line)
            if m:
                hunk = Hunk(
                    int(m.group(1)),
                    int(m.group(2)) if m.group(2) is not None else 1,
                    int(m.group(3)),
                    int(m.group(4)) if m.group(4) is not None else 1,
                )
                cur.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append((line[0], line[1:]))
        elif hunk is not None and line.startswith("\\"):
            # "\ No newline at end of file" applies to the previous line
            if hunk.lines:
                tag, txt = hunk.lines[-1]
                hunk.lines[-1] = (tag, txt + "\\no-newline")
        elif hunk is not None and line == "" and i < len(lines) - 1:
            # some tools drop the leading space on empty context lines
            hunk.lines.append((" ", ""))
        i += 1
    return files


def apply(old_text, fd):
    """Return the new text of a file, or None when the hunks do not apply."""
    old = old_text.split("\n") if old_text else []
    out = []
    pos = 0  # index into old (0-based)
    for h in fd.hunks:
        start = h.old_start - 1 if h.old_len else h.old_start
        if start < pos or start > len(old):
            return None
        out.extend(old[pos:start])
        pos = start
        for tag, txt in h.lines:
            no_nl = txt.endswith("\\no-newline")
            if no_nl:
                txt = txt[: -len("\\no-newline")]
            if tag == " ":
                if pos >= len(old) or old[pos] != txt:
                    return None
                out.append(txt)
                pos += 1
            elif tag == "-":
                if pos >= len(old) or old[pos] != txt:
                    return None
                pos += 1
            else:
                out.append(txt)
    out.extend(old[pos:])
    return "\n".join(out)


@dataclass
class Change:
    kind: str  # "mod" (one line rewritten), "del" (old line removed), "ins" (new lines added)
    old_line: int  # the old line; for "ins", the old line the new lines go before
    new_lines: list  # new line numbers ("mod": one, "del": none)
    old_text: str = ""
    new_text: str = ""


def changes(fd):
    """Every edit of a file diff, positioned in both the old and the new file."""
    out = []
    for h in fd.hunks:
        old_ln = h.old_start if h.old_len else h.old_start + 1
        new_ln = h.new_start if h.new_len else h.new_start + 1
        minus, plus = [], []

        def flush():
            if minus and len(minus) == len(plus):
                for (o, a), (n, b) in zip(minus, plus):
                    out.append(Change("mod", o, [n], a, b))
            else:
                for o, a in minus:
                    out.append(Change("del", o, [], a, ""))
                if plus:
                    out.append(Change("ins", minus[0][0] if minus else old_ln, [n for n, _ in plus], "",
                                      "\n".join(b for _, b in plus)))
            minus.clear()
            plus.clear()

        for tag, txt in h.lines:
            if txt.endswith("\\no-newline"):
                txt = txt[: -len("\\no-newline")]
            if tag == " ":
                flush()
                old_ln += 1
                new_ln += 1
            elif tag == "-":
                if plus:
                    flush()
                minus.append((old_ln, txt))
                old_ln += 1
            else:
                plus.append((new_ln, txt))
                new_ln += 1
        flush()
    return out
