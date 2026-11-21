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

MAX_CHANGED_LINES = 1500
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
        self.patched = {}
        self.import_lines = {f: set(v) for f, v in idx.get("import_lines", {}).items()}
        self.testwords = {w: self.sets[i] for w, i in idx.get("testwords", {}).items()}
        self._importers = {}
        self._range_cache = {}
        # tests whose own code names a dialect (read="duckdb", write={"spark": ...}, DuckDB())
        named = {}
        for w, s in self.testwords.items():
            lw = w.lower()
            if lw in self.stems:
                named.setdefault(lw, set()).update(s)
        # dialect subclassing, read from the pinned sources: class Spark2(Hive) -> hive's users include spark2's
        cls_stem, parent = {}, {}
        for stem in self.stems:
            src = self.source(f"sqlglot/dialects/{stem}.py") or ""
            for cname, base in re.findall(r"^class\s+(\w+)\((?:[\w.]*\.)?(\w+)\)\s*:", src, re.M):
                cls_stem[cname] = stem
                parent[stem] = base
        children = {}
        for stem, base in parent.items():
            b = cls_stem.get(base)
            if b and b != stem:
                children.setdefault(b, set()).add(stem)

        def family(stem, seen=None):
            seen = seen if seen is not None else set()
            if stem in seen:
                return seen
            seen.add(stem)
            for c in children.get(stem, ()):
                family(c, seen)
            return seen

        self.stem_users = {}
        for k, v in self.stems.items():
            u = set(v)
            for st in family(k):
                u |= named.get(st, set())
            self.stem_users[k] = u

    def source(self, rel):
        if rel not in self._src_cache:
            p = SRC / rel
            self._src_cache[rel] = p.read_text("utf-8") if p.exists() else None
        return self._src_cache[rel]

    def derived_attrs(self, start, limit=80):
        """Follow where class attributes flow: every attribute or class-level name that is set by a
        statement reading one of `start` (or reading a local computed from it), across all of
        sqlglot, until nothing new appears. e.g. NUMERIC_ESCAPES -> (local numeric_escapes ->
        fmt ->) ESCAPED_SEQUENCES. Returns None when the flow spreads too wide to be useful."""
        if not hasattr(self, "_pyfiles"):
            self._pyfiles = {}
            for p in (SRC / "sqlglot").rglob("*.py"):
                try:
                    self._pyfiles[p] = [p.read_text("utf-8"), None]
                except Exception:
                    pass
        MUT = {"setdefault", "update", "append", "extend", "add", "insert", "pop", "remove",
               "discard", "clear", "__setitem__", "popitem"}

        def scopes(tree):
            fs = []

            def collect(body, out):
                for st in body:
                    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # only code that runs while classes are built can copy an attribute
                        # somewhere else; what ordinary methods read at run time is in the
                        # coverage map already
                        if st.name in ("__new__", "__init_subclass__", "__set_name__", "__init__"):
                            inner = []
                            collect(st.body, inner)
                            fs.append((False, inner))
                    elif isinstance(st, ast.ClassDef):
                        collect(st.body, out)  # class level shares the module-level scope
                    else:
                        out.append(st)
                        for fld in ("body", "orelse", "finalbody"):
                            sub = getattr(st, fld, None)
                            if isinstance(sub, list):
                                collect(sub, out)
                        for h in getattr(st, "handlers", []) or []:
                            collect(h.body, out)
                        for c in getattr(st, "cases", []) or []:
                            collect(c.body, out)
            mod = []
            collect(tree.body, mod)
            return [(True, mod)] + fs

        def info(st):
            ment, loc, glo = set(), set(), set()
            comp = {x.id for c in ast.walk(st) if isinstance(c, ast.comprehension)
                    for x in ast.walk(c.target) if isinstance(x, ast.Name)}
            for n in ast.walk(st):
                if isinstance(n, ast.Name):
                    if n.id not in comp:
                        ment.add(n.id)
                        if isinstance(n.ctx, ast.Store):
                            loc.add(n.id)
                elif isinstance(n, ast.Attribute):
                    ment.add(n.attr)
                    if isinstance(n.ctx, ast.Store):
                        glo.add(n.attr)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                    ment.add(n.value)
                tgt = None
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store):
                    tgt = n.value
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUT:
                    tgt = n.func.value
                if isinstance(tgt, ast.Name):
                    loc.add(tgt.id)
                elif isinstance(tgt, ast.Attribute):
                    glo.add(tgt.attr)
            return ment, loc, glo

        glob = set(start)
        while True:
            before = len(glob)
            pat = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(glob))) + r")\b")
            for p, ent in self._pyfiles.items():
                if not pat.search(ent[0]):
                    continue
                if ent[1] is None:
                    try:
                        tree = ast.parse(ent[0])
                    except SyntaxError:
                        return None
                    ent[1] = [(is_mod, [info(st) for st in sts]) for is_mod, sts in scopes(tree)]
                for is_mod, infos in ent[1]:
                    local = set()
                    grew = True
                    while grew:
                        grew = False
                        for ment, loc, glo in infos:
                            if ment & glob or ment & local:
                                new_g = glo | (loc if is_mod else set())
                                new_l = set() if is_mod else loc
                                if not new_g <= glob or not new_l <= local:
                                    glob |= new_g
                                    local |= new_l
                                    grew = True
                    if len(glob) > limit:
                        return None
            if len(glob) == before:
                return {g for g in glob if g not in COMMON_WORDS and len(g) > 1}

    def class_users(self, name):
        """tests that can hold an instance of class `name` or of a subclass: code lines and test
        code naming any of them, plus their SQL function names (ArrayContains -> ARRAY_CONTAINS)."""
        if not hasattr(self, "_children"):
            children = {}
            for p in (SRC / "sqlglot").rglob("*.py"):
                try:
                    src = p.read_text("utf-8")
                except Exception:
                    continue
                for cname, bases in re.findall(r"^\s*class\s+(\w+)\s*\(([^)]*)\)\s*:", src, re.M):
                    for b in re.findall(r"(\w+)\s*(?:,|$)", bases):
                        children.setdefault(b, set()).add(cname)
            self._children = children
            low = {}
            for w, s in self.testwords.items():
                low.setdefault(w.lower(), set()).update(s)
            self._testwords_lower = low
        names, todo = set(), [name]
        while todo:
            n = todo.pop()
            if n in names:
                continue
            names.add(n)
            todo.extend(self._children.get(n, ()))
        wi = self.word_index()
        out = set()
        for n in names:
            out |= wi.get(n, set())
            out |= self.testwords.get(n, set())
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", n).lower()
            out |= self._testwords_lower.get(snake, set())
            out |= self._testwords_lower.get(n.lower(), set())
        return out

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


class PatchMismatch(ValueError):
    pass


def apply_hunks(old_text, hunks):
    """Apply hunks to the pinned source. Raises PatchMismatch when a context or removed line
    differs from the pinned file: the patch is against other code, so no line map holds."""
    old = old_text.split("\n")
    out = []
    pos = 0  # 0-based index into old
    for start, body in hunks:
        s = max(start - 1, 0)
        if s < pos:
            raise PatchMismatch("overlapping hunks")
        out.extend(old[pos:s])
        pos = s
        for tag, l in body:
            if tag in (" ", "-"):
                if pos >= len(old) or old[pos] != l:
                    raise PatchMismatch(f"line {pos + 1} differs")
            if tag == " ":
                out.append(old[pos])
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
    key = (rel, a, b)
    hit = ctx._range_cache.get(key)
    if hit is None:
        d = ctx.lines.get(rel, {})
        hit = set()
        for ln, ts in d.items():
            if a <= ln <= b:
                hit |= ts
        ctx._range_cache[key] = hit
    return set(hit)


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
    n_changed = sum(1 for _, body in f["hunks"] for tag, _ in body if tag != " ")
    if n_changed > MAX_CHANGED_LINES:
        # a rewrite this large reaches nearly every test anyway; keep the CPU bill small
        reasons.append(f"{rel}: {n_changed} changed lines -> all")
        return set(ctx.all)
    try:
        old_tree = ast.parse(old_src)
        new_tree = ast.parse(new_src)
    except SyntaxError:
        reasons.append(f"{rel}: does not parse -> all")
        return set(ctx.all)
    try:
        compile(new_src, rel, "exec", dont_inherit=True)  # errors ast.parse lets through
    except (SyntaxError, ValueError):
        reasons.append(f"{rel}: does not compile -> all")
        return set(ctx.all)
    ctx.patched[rel] = new_src
    old_lines = old_src.split("\n")
    new_lines = new_src.split("\n")
    old_funcs = qual_funcs(old_tree)
    new_funcs = qual_funcs(new_tree)
    old_by_q = {}
    for q, b_, e_, h_, node, cl in old_funcs:
        old_by_q[q] = (b_, e_, h_, node, cl)
    old_classes = class_ranges(old_tree)
    old_cls = [c for c in ast.walk(old_tree) if isinstance(c, ast.ClassDef)]
    new_cls = [c for c in ast.walk(new_tree) if isinstance(c, ast.ClassDef)]
    stem = dialect_stem(rel, ctx)
    wi = ctx.word_index()

    sel = set()
    touched = set()
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
        # tests that ran the changed line itself (lambdas and calls inside tables run under the test)
        for ln in range(max(line - 1, 1), line + 2):
            s |= cov.get(ln, set())
        # a plain attribute of one class (arg_types, a flag, a table) only reaches code that holds
        # that class or a subclass: the tests that name it, build it, or run its methods
        cls_list = old_cls if tree is old_tree else new_cls
        inner = max((c for c in cls_list if c.lineno <= line <= c.end_lineno), key=lambda c: c.lineno, default=None)
        if (not stem and inner is not None and stmt is not None and any(stmt is b for b in inner.body)
                and isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign))):
            narrowed = ctx.class_users(inner.name)
            for (cb, ce) in old_classes.get(inner.name, []):
                narrowed |= tests_in_range(ctx, rel, cb, ce)
            if tree is old_tree:
                for ln in range(max(line - 1, 1), line + 2):
                    narrowed |= cov.get(ln, set())
            if narrowed:
                return narrowed
        if (stem and inner is not None and stmt is not None and any(stmt is b for b in inner.body)
                and isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign))):
            # a dialect's class attribute: follow it (and whatever sqlglot builds from it when the
            # class is created) to the code that reads it, among the tests that use this dialect
            attrs = {n for n in assigned_names(stmt) if n not in COMMON_WORDS}
            der = ctx.derived_attrs(attrs) if attrs else None
            if der:
                users = ctx.stem_users[stem]
                hits = set()
                for d in der:
                    hits |= wi.get(d, set())
                    hits |= ctx.testwords.get(d, set())
                narrowed = hits & users
                if tree is old_tree:
                    for ln in range(max(line - 1, 1), line + 2):
                        narrowed |= cov.get(ln, set())
                if narrowed:
                    reasons.append(f"{rel}: {sorted(attrs)[:3]} -> {len(der)} names, {len(narrowed)} tests")
                    return narrowed
        if stem:
            # class tables are often copied or renamed when the class is built, so names can't be
            # traced reliably: every test that uses this dialect (or one built on it)
            s = set(ctx.stem_users[stem]) | (s & ctx.stem_users[stem])
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
            touched.add(q)
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
                    s = (s & ctx.stem_users[stem]) or set(ctx.stem_users[stem])
                if not s:
                    s = set(ctx.all)
                return s
            if side == "old":
                f = fine(line, s)
                if f is None:  # runs at import or test collection: a failure there takes whole test files down
                    module_level = True
                    return set(ctx.all)
            return s  # every test that ran the function, not only the changed statement
        hd = header(funcs, line)
        if hd is not None:
            q, b_, e_, h_, node, cl = hd
            s = func_tests(q, node.name) or set()
            s |= wi.get(node.name, set())
            if stem:
                s = (s & ctx.stem_users[stem]) | (func_tests(q, node.name) or set())
            if not s:
                s = set(ctx.stem_users[stem]) if stem else set(ctx.all)
            module_level = True
            return s
        module_level = True
        classes_here = [c.name for c in (old_cls if tree is old_tree else new_cls) if c.lineno <= line <= c.end_lineno]
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
    # the real import check is only needed when the change can run while modules load:
    # module or class level code, a new function, a function that runs at import, or a module
    # whose import-time lines the map credits to tests (dialects and other lazily loaded modules)
    need = module_level or bool(stem) or not imp or rel.endswith("__init__.py")
    for q in touched:
        if need:
            break
        if q not in old_by_q:
            need = True
        else:
            b_, e_, h_, _n, _c = old_by_q[q]
            # def/class/decorator lines of nested definitions run when the outer one is defined,
            # not when it is called: only body lines say the function itself ran at import
            heads = set()
            for sub in ast.walk(_n):
                if sub is not _n and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    hs = min([d.lineno for d in sub.decorator_list] + [sub.lineno])
                    heads.update(range(hs, sub.body[0].lineno))
            need = any(b_ <= l <= e_ and l not in heads for l in imp)
    if need:
        ctx.need_import = True
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


def test_importers(ctx, rel):
    """-> (names imported from this test module by other test files, their tests).
    e.g. tests/dialects/test_dialect.py defines Validator, which every dialect test file uses."""
    if rel in ctx._importers:
        return ctx._importers[rel]
    mod = re.escape(rel[:-3].replace("/", "."))
    pat = re.compile(r"^\s*(?:from\s+" + mod + r"\s+import\s+\(?([^\n)]*)|import\s+" + mod + r"\b)", re.M)
    names, tests = set(), set()
    for tf, nums in ctx.by_file.items():
        if tf == rel:
            continue
        src = ctx.source(tf) or ""
        hits = pat.findall(src)
        if hits:
            tests |= nums
            for h in hits:
                names |= set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", h)) or {"*"}
    ctx._importers[rel] = (names, tests)
    return ctx._importers[rel]


def select_test_file(ctx, rel, f, reasons):
    """a change to a test module: the tests whose bodies changed, or the whole file
    (plus every test file that imports from it when the change is outside a test)."""
    own = ctx.by_file.get(rel)
    old_src = ctx.source(rel)
    if own is None or old_src is None:
        return None
    try:
        new_src = apply_hunks(old_src, f["hunks"])
    except Exception:
        reasons.append(f"{rel}: patch does not apply -> all")
        return set(ctx.all)
    a, b = code_tokens(old_src), code_tokens(new_src)
    if a is not None and a == b:
        reasons.append(f"{rel}: comments only")
        return set()
    imp_names, imp_tests = test_importers(ctx, rel)
    wide = set(own) | imp_tests
    st = Structure(old_src)
    sel = set()
    for line in changed_old_lines(f["hunks"]):
        fn = st.innermost_func(line) or st.def_header(line)
        hit = None
        if fn is not None:
            # the outermost function inside a class, named test_*
            for c in st.enclosing_classes(line):
                if c.name in imp_names or "*" in imp_names:
                    continue  # a class other files import (and may subclass): not just one test
                for item in c.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.lineno <= line <= item.end_lineno:
                        if item.name.startswith("test"):
                            tid = f"{rel}::{c.name}::{item.name}"
                            if tid in ctx.num_by_id:
                                hit = {ctx.num_by_id[tid]}
        if hit is None:
            if imp_tests:
                reasons.append(f"{rel}: shared test code -> {len(wide)} tests")
            return wide
        sel |= hit
    return sel


def select_commit(ctx, text, reasons):
    ctx.patched = {}
    ctx.need_import = False
    files = parse_patch(text)
    if not files:
        # diff-looking text we could not read (no "diff --git" header): never run nothing
        if re.search(r"^(--- |\+\+\+ |@@ |Index: |Binary files |GIT binary patch)", text, re.M):
            reasons.append("unrecognized diff -> all")
            return set(ctx.all)
        reasons.append("empty patch")
        return set()
    if any(not f["hunks"] and not f["binary"] and f["status"] == "modified" for f in files):
        reasons.append("file header without hunks -> all")
        return set(ctx.all)
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


CHECK_ROOT = Path("/tmp/brcheck")
CHECK_CODE = r"""
import importlib, sys
mods, stems = sys.argv[1].split(","), [x for x in sys.argv[2].split(",") if x]
import sqlglot
for m in mods:
    if m:
        importlib.import_module(m)
from sqlglot.dialects.dialect import Dialect
for s in stems:
    d = Dialect.get_or_raise(s)
    sqlglot.parse_one("SELECT a, b FROM t WHERE a = 1", read=s).sql(dialect=s)
print("ok")
"""


# One long-lived helper loads the standard-library modules sqlglot needs (never sqlglot itself),
# then forks a child per commit that runs exactly CHECK_CODE. Each child imports sqlglot from
# scratch like a fresh interpreter would, but skips starting Python and loading the standard
# library again. Anything unusual about the helper -> the plain fresh-interpreter check.
SERVER_CODE = r"""
import json, os, signal, sys, importlib
code = sys.argv[1]
proto = os.fdopen(os.dup(1), "w")
_null = os.open(os.devnull, os.O_WRONLY)
os.dup2(_null, 1)

def run(mods, stems):
    old = sys.argv
    try:
        importlib.invalidate_caches()
        sys.argv = ["-c", ",".join(mods), ",".join(stems)]
        g = {"__name__": "__main__"}
        exec(compile(code, "<check>", "exec"), g)
        return {"ok": True}
    except BaseException as e:
        return {"ok": False, "msg": (type(e).__name__ + ": " + str(e))[:200]}
    finally:
        sys.argv = old

def in_child(fn):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            signal.alarm(20)
            out = json.dumps(fn()).encode()
        except BaseException:
            out = b'{"ok": false, "msg": "child"}'
        os.write(w, out)
        os._exit(0)
    os.close(w)
    chunks = []
    while True:
        c = os.read(r, 65536)
        if not c:
            break
        chunks.append(c)
    os.close(r)
    os.waitpid(pid, 0)
    return json.loads(b"".join(chunks).decode() or '{"ok": false, "msg": "child died"}')

def stdlib_names():
    # which modules does importing sqlglot pull in? (asked in a throwaway child)
    import sqlglot
    from sqlglot.dialects.dialect import Dialect
    for d in ("duckdb", "snowflake", "bigquery"):
        Dialect.get_or_raise(d)
    return {"ok": True, "mods": sorted(m for m in sys.modules if m.split(".")[0] != "sqlglot" and m != "__main__")}

def preload(names):
    for m in names:
        try:
            importlib.import_module(m)
        except BaseException:
            pass

if len(sys.argv) > 2:  # one request without fork (local tests)
    if sys.argv[3] == "1":
        try:
            preload(stdlib_names()["mods"])
        except BaseException:
            pass
        for m in [m for m in sys.modules if m.split(".")[0] == "sqlglot"]:
            del sys.modules[m]
    req = json.loads(sys.argv[2])
    proto.write(json.dumps(run(req["mods"], req["stems"])) + "\n"); proto.flush()
    os._exit(0)
info = in_child(stdlib_names)
preload(info.get("mods", []))
if any(m.split(".")[0] == "sqlglot" for m in sys.modules):
    raise SystemExit("sqlglot leaked into the helper")
proto.write("ready\n"); proto.flush()
for line in sys.stdin:
    req = json.loads(line)
    res = in_child(lambda: run(req["mods"], req["stems"]))
    proto.write(json.dumps(res) + "\n"); proto.flush()
"""


class CheckServer:
    def __init__(self, env):
        import subprocess

        self.proc = subprocess.Popen(
            [sys.executable, "-c", SERVER_CODE, CHECK_CODE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env, cwd="/tmp",
        )
        if self._line(60) != "ready":
            self.close()
            raise RuntimeError("check server did not start")

    def _line(self, timeout):
        import threading

        box = []
        t = threading.Thread(target=lambda: box.append(self.proc.stdout.readline()), daemon=True)
        t.start()
        t.join(timeout)
        if not box:
            raise TimeoutError("check server timeout")
        return box[0].strip()

    def ask(self, mods, stems):
        self.proc.stdin.write(json.dumps({"mods": mods, "stems": stems}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self._line(30))

    def close(self):
        try:
            self.proc.kill()
        except Exception:
            pass


_server = [None, False]  # [helper, gave up on it]


def check_env():
    return {"PYTHONPATH": str(CHECK_ROOT), "HOME": "/tmp", "PATH": os.environ.get("PATH", "")}


def prepare_check_root():
    import shutil

    pkg = CHECK_ROOT / "sqlglot"
    if not pkg.exists():
        # plain copies: the source tree is read-only and its modes must not come along
        shutil.copytree(SRC / "sqlglot", pkg, copy_function=shutil.copyfile,
                        ignore=shutil.ignore_patterns("__pycache__"))
        for d, _, _ in os.walk(pkg):
            os.chmod(d, 0o755)


def get_server():
    if _server[0] is None and not _server[1]:
        if not hasattr(os, "fork"):
            _server[1] = True
            return None
        try:
            prepare_check_root()
            _server[0] = CheckServer(check_env())
        except Exception as e:
            log(f"check server unavailable {e!r}")
            _server[1] = True
    return _server[0]


def import_check(ctx, reasons):
    """Import the patched modules for real: code that breaks at import takes whole test
    files down, and no coverage map can see that coming."""
    import shutil
    import subprocess

    if not ctx.patched:
        return True
    server = get_server()
    prepare_check_root()
    mods, stems = [], []
    for rel, src in ctx.patched.items():
        _drop_pyc(rel)  # .pyc freshness is mtime(seconds)+size: a same-size rewrite could reuse it
        (CHECK_ROOT / rel).write_text(src, encoding="utf-8")
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        mods.append(mod)
        st = dialect_stem(rel, ctx)
        if st:
            stems.append(st)
    try:
        ok = None
        if server is not None:
            try:
                res = server.ask(mods, stems)
                ok = bool(res.get("ok"))
                if not ok:
                    reasons.append("import check failed -> all: " + str(res.get("msg", "?"))[:120])
            except Exception as e:  # helper broke: never use it again
                server.close()
                _server[0], _server[1] = None, True
                log(f"check server failed {e!r}")
                if isinstance(e, TimeoutError):
                    ok = False
                    reasons.append("import check timeout -> all")
        if ok is None:
            r = subprocess.run(
                [sys.executable, "-c", CHECK_CODE, ",".join(mods), ",".join(stems)],
                capture_output=True, text=True, timeout=30, env=check_env(), cwd="/tmp",
            )
            ok = r.returncode == 0 and r.stdout.strip().endswith("ok")
            if not ok:
                reasons.append("import check failed -> all: " + (r.stderr.strip().splitlines() or ["?"])[-1][:120])
    except Exception as e:
        ok = False
        reasons.append(f"import check error {e!r} -> all")
    finally:
        for rel in ctx.patched:
            _drop_pyc(rel)
            shutil.copyfile(SRC / rel, CHECK_ROOT / rel)
    return ok


def _drop_pyc(rel):
    p = CHECK_ROOT / rel
    cache = p.parent / "__pycache__"
    if cache.is_dir():
        for c in cache.glob(p.stem + ".*.pyc"):
            try:
                c.unlink()
            except OSError:
                pass


def list_commits():
    """-> [(commit id, patch text)], one per commit, whatever the layout of /data/commits:
    <id>.patch / <id>.diff / <id> files, or <id>/ directories holding the patch."""
    root = DATA / "commits"
    out, seen = [], set()
    entries = sorted(root.glob("*.patch")) or sorted(root.glob("*.diff")) or sorted(root.iterdir())
    for p in entries:
        if p.name.startswith("."):
            continue
        cid = p.name.split(".", 1)[0] if p.is_file() else p.name
        if not cid or cid in seen:
            continue
        try:
            if p.is_dir():
                parts = [q.read_text("utf-8", errors="replace") for q in sorted(p.rglob("*")) if q.is_file()]
                text = "\n".join(parts)
            else:
                text = p.read_text("utf-8", errors="replace")
        except Exception as e:
            log(cid, f"unreadable {e!r}")
            text = None
        seen.add(cid)
        out.append((cid, text))
    return out


def all_numbers():
    """test numbers straight from tests.json, for when the full setup fails."""
    try:
        raw = json.loads((DATA / "tests.json").read_text())
        tests = raw["tests"] if isinstance(raw, dict) else raw
        return [t["number"] for t in tests if isinstance(t.get("number"), int)]
    except Exception as e:
        log(f"tests.json unreadable {e!r}")
        return []


def emit(cid, workers):
    sys.stdout.write(json.dumps({"commit": cid, "workers": workers}) + "\n")
    sys.stdout.flush()


def main():
    try:
        commits = list_commits()
    except Exception as e:
        log(f"cannot list commits {e!r}")
        return
    try:
        ctx = Ctx()
    except Exception as e:  # never print nothing: run every test on every commit
        log(f"setup failed {e!r} -> all tests for every commit")
        nums = all_numbers()
        workers = [w for w in (nums[i::8] for i in range(8)) if w]
        for cid, _ in commits:
            emit(cid, workers)
        return
    total = 0
    for cid, text in commits:
        reasons = []
        try:
            if text is None:
                raise ValueError("unreadable patch")
            sel = select_commit(ctx, text, reasons)
            if sel != ctx.all and ctx.patched and ctx.need_import and not import_check(ctx, reasons):
                sel = set(ctx.all)
        except Exception as e:  # never crash: run everything
            reasons.append(f"error {e!r} -> all")
            sel = set(ctx.all)
        try:
            workers, cost = split(ctx, sel)
        except Exception as e:
            reasons.append(f"split error {e!r}")
            nums = sorted(ctx.all)
            workers, cost = [w for w in (nums[i::8] for i in range(8)) if w], 0
        total += cost
        emit(cid, workers)
        log(cid, f"{cost / 1000:.1f}s", "; ".join(reasons))
    log(f"estimated simulated total {total / 1000:.1f}s")
    if _server[0] is not None:
        _server[0].close()


if __name__ == "__main__":
    main()
