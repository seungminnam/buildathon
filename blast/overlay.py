"""A patched copy of the sqlglot tree that costs almost no disk.

/tmp holds 64 MB, so instead of copying the checkout we mirror it with
symlinks and only write real files for what the patch changes. A directory
that holds a changed file becomes a real directory whose other entries are
symlinks back to the original.
"""

import os
import shutil


def _materialize(src_root, dst_root, rel_dir):
    """Make dst_root/rel_dir a real directory mirroring src_root/rel_dir."""
    parts = [p for p in rel_dir.split("/") if p]
    cur_rel = ""
    for part in parts:
        cur_rel = f"{cur_rel}/{part}" if cur_rel else part
        dst = os.path.join(dst_root, cur_rel)
        src = os.path.join(src_root, cur_rel)
        if os.path.islink(dst):
            os.unlink(dst)
        if not os.path.isdir(dst):
            os.mkdir(dst)
            if os.path.isdir(src):
                for name in os.listdir(src):
                    os.symlink(os.path.join(src, name), os.path.join(dst, name))


def build(src_root, dst_root, changes):
    """changes: {relative path: new text, or None to delete the file}."""
    if os.path.exists(dst_root):
        shutil.rmtree(dst_root)
    os.makedirs(dst_root)
    for name in os.listdir(src_root):
        os.symlink(os.path.join(src_root, name), os.path.join(dst_root, name))
    for rel, text in changes.items():
        _materialize(src_root, dst_root, os.path.dirname(rel))
        dst = os.path.join(dst_root, rel)
        if os.path.lexists(dst):
            os.unlink(dst)
        if text is not None:
            with open(dst, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
                f.write(text)
    return dst_root


def remove(path):
    shutil.rmtree(path, ignore_errors=True)
