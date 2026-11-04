#!/usr/bin/env python3
"""check_text.py - enforce the repo text-encoding policy (rules E1–E8).

Checks every text file (default: tracked + untracked-but-not-ignored files):
  E1  must decode as UTF-8; no UTF-8 BOM; no U+FFFD replacement characters;
      no control characters other than TAB / LF / CR (catches printf escape damage)
  E2  .bat / .cmd must be pure ASCII (cmd.exe parses batch files in the console code page, 936 here)
  E4  .py: open(...) and text=True must state encoding= on the same line
      (heuristic; binary modes such as "rb" are exempt)

Usage:
  python -X utf8 tools/check_text.py             # whole repo
  python -X utf8 tools/check_text.py PATH ...    # given files or directories
  python -X utf8 tools/check_text.py --staged    # staged content (used by .githooks/pre-commit)
  python -X utf8 tools/check_text.py --selftest  # verify the checker against built-in bad samples
Exit code: 0 clean, 1 violations found, 2 usage error.

This file is kept pure ASCII on purpose, so it cannot itself carry the damage it looks for.
"""
import os
import re
import subprocess
import sys

TEXT_EXT = {".py", ".sh", ".bat", ".cmd", ".md", ".txt", ".json", ".ini", ".cs", ".cpp", ".h",
            ".uproject", ".uplugin", ".yml", ".yaml", ".toml", ".cfg", ".rsp"}
TEXT_NAMES = {".gitignore", ".gitattributes", ".editorconfig", "pre-commit"}
ASCII_ONLY_EXT = {".bat", ".cmd"}
SKIP_DIRS = {".git", "runs", "Binaries", "Intermediate", "Saved", "DerivedDataCache", "__pycache__"}
BOM = b"\xef\xbb\xbf"
REPLACEMENT_CHAR = "\ufffd"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
OPEN_CALL_RE = re.compile(r"\bopen\(")
TEXT_TRUE_RE = re.compile(r"\btext\s*=\s*True")
BINARY_MODE_RE = re.compile(r"""["'][rwax+]*b[rwax+]*["']""")


def is_text(path: str) -> bool:
    name = os.path.basename(path)
    return name in TEXT_NAMES or os.path.splitext(name)[1].lower() in TEXT_EXT


def check_bytes(path: str, data: bytes):
    problems = []
    if data.startswith(BOM):
        problems.append("E1 UTF-8 BOM present")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        problems.append("E1 invalid UTF-8 at byte %d" % e.start)
        return problems
    ext = os.path.splitext(path)[1].lower()
    for no, line in enumerate(text.split("\n"), 1):
        if REPLACEMENT_CHAR in line:
            problems.append("E1 line %d: U+FFFD replacement character" % no)
        if CONTROL_RE.search(line):
            problems.append("E1 line %d: control character" % no)
        if ext in ASCII_ONLY_EXT and any(ord(c) > 127 for c in line):
            problems.append("E2 line %d: non-ASCII character in batch file" % no)
        if ext == ".py":
            code = line.split("#", 1)[0]
            if OPEN_CALL_RE.search(code) and "encoding=" not in code and not BINARY_MODE_RE.search(code):
                problems.append("E4 line %d: open() without encoding=" % no)
            if TEXT_TRUE_RE.search(code) and "encoding=" not in code:
                problems.append("E4 line %d: text=True without encoding=" % no)
    return problems


def git(args, cwd=None) -> bytes:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, check=True).stdout


def repo_root() -> str:
    return git(["rev-parse", "--show-toplevel"]).decode("utf-8").strip()


def nul_list(raw: bytes):
    return [p for p in raw.decode("utf-8").split("\0") if p]


def walk(top: str):
    if os.path.isfile(top):
        yield top
        return
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def collect(args):
    staged = "--staged" in args
    paths = [a for a in args if a != "--staged"]
    if any(a.startswith("-") for a in paths):
        return None
    items = []
    if staged:
        root = repo_root()
        for p in nul_list(git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=root)):
            if is_text(p):
                items.append((p, git(["show", ":" + p], cwd=root)))
    elif paths:
        for top in paths:
            for p in walk(top):
                if is_text(p):
                    items.append((p, read_bytes(p)))
    else:
        root = repo_root()
        for p in nul_list(git(["ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root)):
            full = os.path.join(root, p)
            if is_text(p) and os.path.isfile(full):
                items.append((p, read_bytes(full)))
    return items


SELFTEST_CASES = [
    # (file name, content, expected problem prefixes). The "op" "en(" and "te" "xt=" literals are
    # split on purpose so this file passes its own E4 check.
    ("fffd.md", "abc \u7edf\ufffd\ufffd\ufffd\n".encode("utf-8"), ["E1 line 1: U+FFFD"]),
    ("bom.md", BOM + b"abc\n", ["E1 UTF-8 BOM"]),
    ("invalid.md", b"ok\n\xe4\xb8\n", ["E1 invalid UTF-8"]),
    ("ctrl.md", b"D:\x1bpic\n", ["E1 line 1: control"]),
    ("cn.bat", "@echo off\r\nrem \u4e2d\u6587\r\n".encode("utf-8"), ["E2 line 2"]),
    ("bad_open.py", b"f = op" b"en('x.txt', 'w')\nsubprocess.run(['a'], te" b"xt=True)\n", ["E4 line 1", "E4 line 2"]),
    ("good.md", "\u4e2d\u6587 ok\n".encode("utf-8"), []),
    ("good.py", b"a = open('x', 'rb'); b = open('y', 'w', encoding='utf-8')\n", []),
]


def selftest() -> int:
    failures = 0
    for name, data, expected in SELFTEST_CASES:
        got = check_bytes(name, data)
        ok = len(got) == len(expected) and all(any(g.startswith(e) for g in got) for e in expected)
        failures += 0 if ok else 1
        print("%-12s %s %s" % (name, "ok  " if ok else "FAIL", got))
    print("selftest: %d case(s), %d failure(s)" % (len(SELFTEST_CASES), failures))
    return 1 if failures else 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:] if argv is None else argv
    if args == ["--selftest"]:
        return selftest()
    items = collect(args)
    if items is None:
        print(__doc__)
        return 2
    bad = 0
    for path, data in items:
        for msg in check_bytes(path, data):
            bad += 1
            print("%s: %s" % (path, msg))
    print("checked %d text file(s), %d problem(s)" % (len(items), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
