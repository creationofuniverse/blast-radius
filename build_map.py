"""Build the coverage map from a per-test coverage run of sqlglot at the pinned commit.

Output: map.json.gz with
  tests: number -> id
  sets: list of test-number lists (deduplicated)
  lines: {file: {line: set_index}}  lines executed while a test ran
  opens: {path: [test numbers]}     files a test opened (fixtures)
  stems: {dialect stem: [test numbers]} tests that use a dialect or one of its subclasses
"""

import glob
import gzip
import json
import os
import re
import sys

from coverage import CoverageData

SRC = "/home/claude/sqlglot"
tests = json.load(open("/home/claude/br/sample/tests.json"))["tests"]
by_id = {t["id"]: t["number"] for t in tests}
cls_of = {t["number"]: t["id"].rsplit("::", 1)[0] for t in tests}
members = {}
for n, c in cls_of.items():
    members.setdefault(c, set()).add(n)

d = CoverageData("/home/claude/work/.coverage")
d.read()

sets = []
set_index = {}


def intern(s):
    key = tuple(sorted(s))
    if key not in set_index:
        set_index[key] = len(sets)
        sets.append(list(key))
    return set_index[key]


lines_out = {}
import_lines = {}
file_tests = {}
for f in d.measured_files():
    rel = os.path.relpath(f, SRC)
    per_line = {}
    for line, ctxs in d.contexts_by_lineno(f).items():
        if "" in ctxs:
            import_lines.setdefault(rel, []).append(line)
        s = set()
        for c in ctxs:
            if not c:
                continue
            base, phase = c.rsplit("|", 1)
            n = by_id.get(base)
            if n is None:
                continue
            if phase == "setup":
                s |= members[cls_of[n]]
            else:
                s.add(n)
        if s:
            per_line[line] = s
    lines_out[rel] = {str(k): intern(v) for k, v in per_line.items()}
    allt = set()
    for v in per_line.values():
        allt |= v
    file_tests[rel] = allt

# fixture opens: a file opened in a test's setup counts for its whole class (setUpClass)
opens = {}
for p in glob.glob("/home/claude/work/opens_*.json"):
    for key, paths in json.load(open(p)).items():
        nodeid, phase = key.rsplit("|", 1)
        n = by_id.get(nodeid)
        if n is None:
            continue
        ns = members[cls_of[n]] if phase == "setup" else {n}
        for path in paths:
            opens.setdefault(path, set()).update(ns)

# dialect usage with subclasses
sys.path.insert(0, SRC)
import sqlglot  # noqa
from sqlglot.dialects.dialect import Dialect

stems = sorted(
    os.path.basename(p)[:-3]
    for p in glob.glob(SRC + "/sqlglot/dialects/*.py")
    if not p.endswith(("__init__.py", "dialect.py"))
)
classes = {}
for s in stems:
    try:
        classes[s] = Dialect.get_or_raise(s).__class__
    except Exception as e:
        print("no dialect", s, e)
DIRS = ["dialects", "parsers", "generators", "tokenizers", "typing"]


def direct_usage(stem):
    u = set()
    for dname in DIRS:
        u |= file_tests.get(f"sqlglot/{dname}/{stem}.py", set())
    tf = f"tests/dialects/test_{stem}.py"
    u |= {t["number"] for t in tests if t["file"] == tf}
    return u


stem_usage = {}
for s in stems:
    u = direct_usage(s)
    c = classes.get(s)
    if c is not None:
        for s2, c2 in classes.items():
            if s2 != s and issubclass(c2, c):
                u |= direct_usage(s2)
    stem_usage[s] = sorted(u)

# words that each test's own code mentions (test method plus its class's setUp/setUpClass)
import ast as _ast

testwords = {}
for tf in sorted({t["file"] for t in tests}):
    try:
        tree = _ast.parse(open(os.path.join(SRC, tf), encoding="utf-8").read())
    except Exception:
        continue
    srclines = open(os.path.join(SRC, tf), encoding="utf-8").read().split("\n")
    for cls in [n for n in tree.body if isinstance(n, _ast.ClassDef)]:
        setup_words = set()
        for item in cls.body:
            if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and item.name in ("setUp", "setUpClass"):
                setup_words |= set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", "\n".join(srclines[item.lineno - 1:item.end_lineno])))
        for item in cls.body:
            if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and item.name.startswith("test"):
                n = by_id.get(f"{tf}::{cls.name}::{item.name}")
                if n is None:
                    continue
                words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", "\n".join(srclines[item.lineno - 1:item.end_lineno]))) | setup_words
                for w in words:
                    testwords.setdefault(w, set()).add(n)
testword_idx = {w: intern(v) for w, v in testwords.items()}

# words that each test's own code mentions (test method plus its class's setUp/setUpClass)
import ast as _ast

testwords = {}
for tf in sorted({t["file"] for t in tests}):
    try:
        tree = _ast.parse(open(os.path.join(SRC, tf), encoding="utf-8").read())
    except Exception:
        continue
    srclines = open(os.path.join(SRC, tf), encoding="utf-8").read().split("\n")
    for cls in [n for n in tree.body if isinstance(n, _ast.ClassDef)]:
        setup_words = set()
        for item in cls.body:
            if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and item.name in ("setUp", "setUpClass"):
                setup_words |= set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", "\n".join(srclines[item.lineno - 1:item.end_lineno])))
        for item in cls.body:
            if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and item.name.startswith("test"):
                n = by_id.get(f"{tf}::{cls.name}::{item.name}")
                if n is None:
                    continue
                words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", "\n".join(srclines[item.lineno - 1:item.end_lineno]))) | setup_words
                for w in words:
                    testwords.setdefault(w, set()).add(n)
testword_idx = {w: intern(v) for w, v in testwords.items()}

out = {
    "tests": {t["number"]: t["id"] for t in tests},
    "sets": sets,
    "lines": lines_out,
    "opens": {k: sorted(v) for k, v in opens.items()},
    "stems": stem_usage,
    "import_lines": import_lines,
    "testwords": testword_idx,
}
with gzip.open("/home/claude/work/map.json.gz", "wt") as f:
    json.dump(out, f, separators=(",", ":"))
print("sets", len(sets), "files", len(lines_out), "opens", len(opens))
for s in stems:
    print(s, len(stem_usage[s]), end="; ")
print()
print(os.path.getsize("/home/claude/work/map.json.gz"))
