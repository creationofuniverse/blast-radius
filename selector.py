"""Blast Radius selector.

For each patch, pick the sqlglot tests that can catch it, using a per-test coverage
map of the pinned sqlglot commit (built offline, shipped as map.json.gz), then split
the picked tests over the number of workers that makes the commit cheapest.

When it is unsure, it runs more tests: a missed failure costs 300 s, while running
every test costs about 45 s.
"""

import ast
import gzip
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = Path(os.environ.get("BR_MAP", Path(__file__).with_name("map.json.gz")))

DIALECT_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")
SPECIAL_TYPING = "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata"
SPECIAL_LAZY = "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"
COMMON_WORDS = {
    "self", "exp", "cls", "t", "None", "True", "False", "return", "def", "class", "if",
    "else", "elif", "for", "in", "not", "and", "or", "is", "import", "from", "as", "lambda",
    "expression", "e", "x", "args", "kwargs", "this", "super", "isinstance", "len", "str",
    "int", "list", "dict", "set", "tuple", "k", "v", "i", "n", "s", "typing", "sqlglot",
}


def log(*a):
    print(*a, file=sys.stderr)


# ----------------------------------------------------------------------------- data

INDEX = Path(os.environ.get("BR_INDEX", Path(__file__).with_name("index.pkl.gz")))


def load_index():
    """The coverage map, preprocessed at build time into index.pkl (falls back to map.json.gz)."""
    import pickle

    if INDEX.exists():
        with gzip.open(INDEX, "rb") as fh:
            return pickle.load(fh)
    m = json.loads(gzip.open(MAP, "rt").read())
    return {
        "map_ids": {int(k): v for k, v in m["tests"].items()},
        "sets": m["sets"],
        "lines": {f: {int(l): i for l, i in d.items()} for f, d in m["lines"].items()},
        "opens": m["opens"],
        "stems": m["stems"],
        "words": None,
        "import_lines": m.get("import_lines", {}),
        "testwords": m.get("testwords", {}),
    }


class Ctx:
    def __init__(self):
        raw = json.loads((DATA / "tests.json").read_text())
        self.tests = raw["tests"]
        self.secs_ms = {t["number"]: round(t["seconds"] * 1000) for t in self.tests}
        self.num_by_id = {t["id"]: t["number"] for t in self.tests}
        self.all = set(self.secs_ms)
        self.by_file = {}
        for t in self.tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])

        idx = load_index()
        map_ids = idx["map_ids"]
        known_ids = set(map_ids.values())
        identity = all(self.num_by_id.get(i) == n for n, i in map_ids.items())

        if identity:
            def tr(nums):
                return set(nums)
        else:
            def tr(nums):
                out = set()
                for n in nums:
                    i = map_ids.get(n)
                    if i is not None and i in self.num_by_id:
                        out.add(self.num_by_id[i])
                return out

        self.sets = [tr(x) for x in idx["sets"]]
        self.lines = {f: {l: self.sets[i] for l, i in d.items()} for f, d in idx["lines"].items()}
        self.opens = {p: tr(v) for p, v in idx["opens"].items()}
        self.stems = {k: tr(v) for k, v in idx["stems"].items()}
        self._word_index = {w: self.sets[i] for w, i in idx["words"].items()} if idx.get("words") is not None else None
        # tests the map does not know (added since the pinned commit): always run them
        self.unknown = {n for i, n in self.num_by_id.items() if i not in known_ids}
        self.special_typing = {self.num_by_id[SPECIAL_TYPING]} if SPECIAL_TYPING in self.num_by_id else set()
        self.special_lazy = {self.num_by_id[SPECIAL_LAZY]} if SPECIAL_LAZY in self.num_by_id else set()
        self._src_cache = {}
        self.import_lines = {f: set(v) for f, v in idx.get("import_lines", {}).items()}
        self.testwords = {w: self.sets[i] for w, i in idx.get("testwords", {}).items()}

    def source(self, rel):
        if rel not in self._src_cache:
            p = SRC / rel
            self._src_cache[rel] = p.read_text("utf-8") if p.exists() else None
        return self._src_cache[rel]

    def word_index(self):
        """identifier -> tests covering a sqlglot line that mentions it."""
        if self._word_index is None:
            idx = {}
            for f, d in self.lines.items():
                src = self.source(f)
                if src is None:
                    continue
                lines = src.split("\n")
                for ln, s in d.items():
                    if 0 < ln <= len(lines):
                        for w in set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", lines[ln - 1])):
                            if w in COMMON_WORDS:
                                continue
                            cur = idx.get(w)
                            if cur is None:
                                idx[w] = set(s)
                            else:
                                cur |= s
            self._word_index = idx
        return self._word_index


# ----------------------------------------------------------------------------- patches


def parse_patch(text):
    """-> list of files: dict(old, new, status, hunks=[(old_start, [(tag, line)])])"""
    files = []
    cur = None
    hunk = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = {"old": m.group(1) if m else None, "new": m.group(2) if m else None,
                   "status": "modified", "hunks": [], "binary": False}
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif line.startswith("new file mode"):
            cur["status"] = "added"
        elif line.startswith("deleted file mode"):
            cur["status"] = "deleted"
        elif line.startswith("rename from ") or line.startswith("copy from "):
            cur["status"] = "renamed"
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur["binary"] = True
        elif line.startswith("old mode") or line.startswith("new mode"):
            cur["status"] = "mode" if cur["status"] == "modified" else cur["status"]
        elif line.startswith("--- ") and hunk is None:
            p = line[4:].strip()
            if p != "/dev/null":
                cur["old"] = p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ ") and hunk is None:
            p = line[4:].strip()
            if p != "/dev/null":
                cur["new"] = p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            hunk = (int(m.group(1)), [])
            cur["hunks"].append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk[1].append((line[0], line[1:]))
        elif hunk is not None and line.startswith("\\"):
            pass
    return files


def apply_hunks(old_text, hunks):
    old = old_text.split("\n")
    out = []
    pos = 0  # 0-based index into old
    for start, body in hunks:
        s = max(start - 1, 0)
        out.extend(old[pos:s])
        pos = s
        for tag, l in body:
            if tag == " ":
                out.append(old[pos] if pos < len(old) else l)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(l)
    out.extend(old[pos:])
    return "\n".join(out)


def changed_old_lines(hunks, header_lines=frozenset()):
    """old line numbers that were removed or replaced; for pure insertions, the old lines
    around the insertion point (the line before is skipped when it is a def/decorator line)."""
    lines = set()
    for start, body in hunks:
        ln = start
        prev_context = None
        i = 0
        while i < len(body):
            tag = body[i][0]
            if tag == " ":
                prev_context = ln
                ln += 1
                i += 1
                continue
            j = i
            removed = []
            added = 0
            while j < len(body) and body[j][0] != " ":
                if body[j][0] == "-":
                    removed.append(ln)
                    ln += 1
                else:
                    added += 1
                j += 1
            if removed:
                lines.update(removed)
            else:
                lines.add(ln)  # old line that now follows the insertion
                if prev_context is not None and prev_context not in header_lines:
                    lines.add(prev_context)
            i = j
        if start == 0:
            lines.add(1)
    return lines


def code_tokens(text):
    toks = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
                continue
            if tok.type == tokenize.NEWLINE:
                toks.append((tokenize.NEWLINE, ""))
                continue
            toks.append((tok.type, tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return toks


# ----------------------------------------------------------------------------- python analysis


class Structure:
    def __init__(self, src):
        self.tree = ast.parse(src)
        self.funcs = []  # (body_start, end, def_start, node)
        self.top = []  # (start, end, node, class_node or None)
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                deco_start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                self.funcs.append((node.body[0].lineno, node.end_lineno, deco_start, node))
        self.classes = [n for n in ast.walk(self.tree) if isinstance(n, ast.ClassDef)]

    def innermost_func(self, line):
        best = None
        for b, e, d, node in self.funcs:
            if b <= line <= e:
                if best is None or b >= best[0]:
                    best = (b, e, d, node)
        return best

    def def_header(self, line):
        for b, e, d, node in self.funcs:
            if d <= line < b:
                return (b, e, d, node)
        return None

    def enclosing_classes(self, line):
        return [c for c in self.classes if c.lineno <= line <= c.end_lineno]

    def statement_at(self, line):
        """innermost module- or class-level statement holding the line."""
        body = self.tree.body
        found = None
        while True:
            nxt = None
            for st in body:
                start = min([getattr(d, "lineno", st.lineno) for d in getattr(st, "decorator_list", [])] + [st.lineno])
                if start <= line <= st.end_lineno:
                    nxt = st
                    break
            if nxt is None:
                return found
            found = nxt
            if isinstance(nxt, ast.ClassDef):
                body = nxt.body
                continue
            if isinstance(nxt, (ast.If, ast.Try, ast.With)):
                body = list(getattr(nxt, "body", [])) + list(getattr(nxt, "orelse", [])) + list(getattr(nxt, "finalbody", []))
                for h in getattr(nxt, "handlers", []):
                    body += h.body
                continue
            return found


def assigned_names(st):
    names = set()
    if st is None:
        return names
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(st.name)
        return names
    for node in ast.walk(st):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
    return names


def dialect_stem(rel, ctx):
    parts = rel.split("/")
    if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in DIALECT_DIRS:
        stem = parts[2][:-3]
        if stem in ctx.stems:
            return stem
    return None


def tests_in_range(ctx, rel, a, b):
    d = ctx.lines.get(rel, {})
    s = set()
    for ln, ts in d.items():
        if a <= ln <= b:
            s |= ts
    return s


def qual_funcs(tree):
    """-> list of (qualname, body_start, end, header_start, node, class_names)"""
    out = []

    def walk(node, prefix, classes):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + child.name
                hs = min([d.lineno for d in child.decorator_list] + [child.lineno])
                out.append((q, child.body[0].lineno, child.end_lineno, hs, child, classes))
                walk(child, q + ".", classes)
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".", classes + [child.name])
            else:
                walk(child, prefix, classes)

    walk(tree, "", [])
    return out


def class_ranges(tree, prefix=""):
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out.setdefault(node.name, []).append((node.lineno, node.end_lineno))
    return out


def split_changes(hunks):
    """-> list of runs: dict(removed=[old lines], added=[new lines], prev_old, next_old)"""
    runs = []
    offset = 0
    for start, body in hunks:
        o = start
        nn = start + offset
        prev_old = None
        i = 0
        while i < len(body):
            if body[i][0] == " ":
                prev_old = o
                o += 1
                nn += 1
                i += 1
                continue
            run = {"removed": [], "added": [], "prev_old": prev_old, "next_old": None}
            while i < len(body) and body[i][0] != " ":
                if body[i][0] == "-":
                    run["removed"].append(o)
                    o += 1
                    offset -= 1
                else:
                    run["added"].append(nn)
                    nn += 1
                    offset += 1
                i += 1
            run["next_old"] = o
            runs.append(run)
    return runs


STRUCTURE = re.compile(r"^\s*(else\b|elif\b|except\b|finally\b|def\b|async\s+def\b|class\b|@)")


def is_trivial(line_text):
    t = line_text.strip()
    return t == "" or t.startswith("#")


def select_python(ctx, rel, f, reasons):
    old_src = ctx.source(rel)
    if old_src is None:
        reasons.append(f"{rel}: not in pinned tree -> all")
        return set(ctx.all)
    try:
        new_src = apply_hunks(old_src, f["hunks"])
    except Exception:
        reasons.append(f"{rel}: patch does not apply -> all")
        return set(ctx.all)
    a, b = code_tokens(old_src), code_tokens(new_src)
    if a is not None and b is not None and a == b:
        reasons.append(f"{rel}: comments/blank only")
        return set()
    try:
        old_tree = ast.parse(old_src)
        new_tree = ast.parse(new_src)
    except SyntaxError:
        reasons.append(f"{rel}: does not parse -> all")
        return set(ctx.all)
    old_lines = old_src.split("\n")
    new_lines = new_src.split("\n")
    old_funcs = qual_funcs(old_tree)
    new_funcs = qual_funcs(new_tree)
    old_by_q = {}
    for q, b_, e_, h_, node, cl in old_funcs:
        old_by_q[q] = (b_, e_, h_, node, cl)
    old_classes = class_ranges(old_tree)
    stem = dialect_stem(rel, ctx)
    wi = ctx.word_index()

    sel = set()
    module_level = False

    def innermost(funcs, line):
        best = None
        for q, b_, e_, h_, node, cl in funcs:
            if b_ <= line <= e_ and (best is None or b_ >= best[1]):
                best = (q, b_, e_, h_, node, cl)
        return best

    def header(funcs, line):
        best = None
        for q, b_, e_, h_, node, cl in funcs:
            if h_ <= line < b_ and (best is None or h_ >= best[3]):
                best = (q, b_, e_, h_, node, cl)
        return best

    def func_tests(q, name):
        if q in old_by_q:
            b_, e_, h_, node, cl = old_by_q[q]
            return tests_in_range(ctx, rel, b_, e_)
        return None

    def level_tests(tree, line, text, classes_here):
        names = assigned_names(statement_in(tree, line))
        names |= {w for w in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text)}
        names |= set(classes_here)  # code that walks the class itself (enums, registries)
        names = {n for n in names if n not in COMMON_WORDS and len(n) > 1}
        s = set()
        for n in names:
            s |= wi.get(n, set())
        direct = set()  # tests whose own code names it (e.g. loops over every dialect)
        for n in names:
            direct |= ctx.testwords.get(n, set())
        for cname in classes_here:
            for (cb, ce) in old_classes.get(cname, []):
                s |= tests_in_range(ctx, rel, cb, ce)
        stmt = statement_in(tree, line)
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            s |= set(ctx.all)
        if stem:
            s2 = s & ctx.stems[stem]
            s = s2 if s2 else set(ctx.stems[stem])
        s |= direct
        if not s:
            s = set(ctx.all)
        return s

    old_stmts = [n for n in ast.walk(old_tree) if isinstance(n, ast.stmt) and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    cov = ctx.lines.get(rel, {})
    imp = ctx.import_lines.get(rel, set())

    def fine(line, fallback):
        """tests that executed the innermost statement holding an old line (header part)."""
        best = None
        for n in old_stmts:
            if n.lineno <= line <= n.end_lineno and (best is None or (n.end_lineno - n.lineno) < (best.end_lineno - best.lineno)):
                best = n
        if best is None:
            return fallback
        body = getattr(best, "body", None)
        head_end = best.end_lineno
        if body:
            head_end = body[0].lineno - 1
            if line > head_end:
                return fallback  # else:/except:/finally: lines and the like
        s = set()
        seen = False
        for ln in range(best.lineno, head_end + 1):
            if ln in imp:
                return None  # runs at import time
            if ln in cov:
                s |= cov[ln]
                seen = True
        if not seen:
            return fallback
        return s

    def handle(line, texts, funcs, tree, side, coarse=False):
        nonlocal module_level
        text = texts[line - 1] if 0 < line <= len(texts) else ""
        if is_trivial(text):
            return set()
        fn = innermost(funcs, line)
        if fn is not None:
            q, b_, e_, h_, node, cl = fn
            s = func_tests(q, node.name)
            if s is None:  # a function that is new in this patch: who calls or overrides it
                s = set(wi.get(node.name, set()))
                outer = q.rsplit(".", 1)[0] if "." in q else None
                while outer:
                    s2 = func_tests(outer, outer)
                    if s2 is not None:
                        s |= s2
                        break
                    outer = outer.rsplit(".", 1)[0] if "." in outer else None
                module_level = True
                if stem:
                    s = (s & ctx.stems[stem]) or set(ctx.stems[stem])
                if not s:
                    s = set(ctx.all)
                return s
            if side == "old" and not coarse:
                f = fine(line, s)
                if f is None:  # runs at import or test collection: a failure there takes whole test files down
                    module_level = True
                    return set(ctx.all)
                return f
            return s
        hd = header(funcs, line)
        if hd is not None:
            q, b_, e_, h_, node, cl = hd
            s = func_tests(q, node.name) or set()
            s |= wi.get(node.name, set())
            if stem:
                s = (s & ctx.stems[stem]) | (func_tests(q, node.name) or set())
            if not s:
                s = set(ctx.stems[stem]) if stem else set(ctx.all)
            module_level = True
            return s
        module_level = True
        classes_here = [c.name for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.lineno <= line <= c.end_lineno]
        return level_tests(tree, line, text, classes_here)

    def nearest_code(line, step):
        k = line
        while 0 < k <= len(old_lines) and is_trivial(old_lines[k - 1]):
            k += step
        return k if 0 < k <= len(old_lines) else None

    for run in split_changes(f["hunks"]):
        added_text = [new_lines[l - 1] for l in run["added"] if 0 < l <= len(new_lines)]
        structural = any(STRUCTURE.match(t) for t in added_text) or any(
            STRUCTURE.match(old_lines[l - 1]) for l in run["removed"] if 0 < l <= len(old_lines))
        # indentation changes alter structure too
        ind_old = {len(old_lines[l - 1]) - len(old_lines[l - 1].lstrip()) for l in run["removed"] if 0 < l <= len(old_lines) and not is_trivial(old_lines[l - 1])}
        ind_new = {len(t) - len(t.lstrip()) for t in added_text if not is_trivial(t)}
        if run["removed"] and ind_new and ind_old and ind_new != ind_old:
            structural = True
        for l in run["removed"]:
            sel |= handle(l, old_lines, old_funcs, old_tree, "old", coarse=structural)
        if run["removed"] and not structural:
            continue
        if not run["removed"] and not structural and added_text and all(not is_trivial(t) or True for t in added_text):
            # pure insertion of plain statements: the neighbours tell who reaches this point
            got = False
            for nb, step in ((run["prev_old"], -1), (run["next_old"], 1)):
                if nb is None:
                    continue
                k = nearest_code(nb, step)
                if k is None:
                    continue
                if header(old_funcs, k) is not None:
                    continue
                sel |= handle(k, old_lines, old_funcs, old_tree, "old")
                got = True
            # also the new lines themselves at function granularity when not inside a function
            for l in run["added"]:
                t_ = new_lines[l - 1] if 0 < l <= len(new_lines) else ""
                if is_trivial(t_):
                    continue
                if innermost(new_funcs, l) is None:
                    sel |= handle(l, new_lines, new_funcs, new_tree, "new")
                elif not got:
                    sel |= handle(l, new_lines, new_funcs, new_tree, "new", coarse=True)
            continue
        for l in run["added"]:
            sel |= handle(l, new_lines, new_funcs, new_tree, "new", coarse=True)
    if module_level:
        sel |= ctx.special_lazy
    if rel.startswith("sqlglot/typing/"):
        sel |= ctx.special_typing
    reasons.append(f"{rel}: {len(sel)} tests")
    return sel


def statement_in(tree, line):
    body = tree.body
    found = None
    while True:
        nxt = None
        for st in body:
            start = min([d.lineno for d in getattr(st, "decorator_list", [])] + [st.lineno])
            if start <= line <= st.end_lineno:
                nxt = st
                break
        if nxt is None:
            return found
        found = nxt
        if isinstance(nxt, ast.ClassDef):
            body = nxt.body
            continue
        if isinstance(nxt, (ast.If, ast.Try, ast.With)):
            body = list(getattr(nxt, "body", [])) + list(getattr(nxt, "orelse", [])) + list(getattr(nxt, "finalbody", []))
            for h in getattr(nxt, "handlers", []):
                body += h.body
            continue
        return found


def select_test_file(ctx, rel, f, reasons):
    """a change to a test module: the tests whose bodies changed, or the whole file."""
    own = ctx.by_file.get(rel)
    old_src = ctx.source(rel)
    if own is None or old_src is None:
        return None
    try:
        new_src = apply_hunks(old_src, f["hunks"])
        if code_tokens(old_src) == code_tokens(new_src) and code_tokens(old_src) is not None:
            reasons.append(f"{rel}: comments only")
            return set()
    except Exception:
        pass
    st = Structure(old_src)
    sel = set()
    for line in changed_old_lines(f["hunks"]):
        fn = st.innermost_func(line)
        hit = None
        if fn is not None:
            node = fn[3]
            # the outermost function inside a class, named test_*
            for c in st.enclosing_classes(line):
                for item in c.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.lineno <= line <= item.end_lineno:
                        if item.name.startswith("test"):
                            tid = f"{rel}::{c.name}::{item.name}"
                            if tid in ctx.num_by_id:
                                hit = {ctx.num_by_id[tid]}
        if hit is None:
            return set(own)
        sel |= hit
    return sel


def select_commit(ctx, text, reasons):
    files = parse_patch(text)
    if not files:
        reasons.append("empty patch")
        return set()
    sel = set()
    for f in files:
        rel = f["new"] if f["status"] != "deleted" else f["old"]
        oldrel = f["old"]
        if f["binary"] or f["status"] in ("added", "deleted", "renamed", "mode"):
            reasons.append(f"{rel}: {f['status']} -> all")
            return set(ctx.all)
        if rel.startswith("sqlglot/") and rel.endswith(".py"):
            sel |= select_python(ctx, rel, f, reasons)
        elif rel.startswith("tests/fixtures/"):
            s = ctx.opens.get(rel)
            if not s:
                base = os.path.basename(rel)
                s = set()
                for tf, nums in ctx.by_file.items():
                    src = ctx.source(tf) or ""
                    if base in src or os.path.splitext(base)[0] in src:
                        s |= nums
                if not s:
                    s = set(ctx.all)
                reasons.append(f"{rel}: fixture via grep {len(s)}")
            sel |= s
        elif rel.startswith("tests/") and rel.endswith(".py"):
            s = select_test_file(ctx, rel, f, reasons)
            if s is None:
                reasons.append(f"{rel}: test helper -> all")
                return set(ctx.all)
            sel |= s
        else:
            reasons.append(f"{rel}: other file -> all")
            return set(ctx.all)
    return sel | ctx.unknown if sel else sel


# ----------------------------------------------------------------------------- splitting


def split(ctx, sel):
    if not sel:
        return [], 0
    items = sorted(sel, key=lambda n: -ctx.secs_ms[n])
    best = None
    for k in range(1, 9):
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            workers[i].append(n)
            loads[i] += ctx.secs_ms[n]
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, [w for w in workers if w])
    return best[1], best[0]


def main():
    ctx = Ctx()
    total = 0
    for patch in sorted((DATA / "commits").glob("*.patch")):
        reasons = []
        try:
            sel = select_commit(ctx, patch.read_text("utf-8", errors="replace"), reasons)
        except Exception as e:  # never crash: run everything
            reasons.append(f"error {e!r} -> all")
            sel = set(ctx.all)
        workers, cost = split(ctx, sel)
        total += cost
        print(json.dumps({"commit": patch.stem, "workers": workers}), flush=True)
        log(patch.stem, f"{cost / 1000:.1f}s", "; ".join(reasons))
    log(f"estimated simulated total {total / 1000:.1f}s")


if __name__ == "__main__":
    main()
