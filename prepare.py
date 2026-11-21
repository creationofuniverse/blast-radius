"""Build step: fetch sqlglot at the pinned commit and preprocess the coverage map.

Runs once inside `docker build` (network on). Writes:
  /opt/sqlglot           sqlglot source at the pinned commit (read-only reference)
  /app/index.pkl         the coverage map plus a word index, ready to load fast
"""

import gzip
import io
import json
import os
import pickle
import re
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://codeload.github.com/tobymao/sqlglot/tar.gz/{COMMIT}"
DEST = "/opt/sqlglot"
APP = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, APP)
from selector import COMMON_WORDS  # noqa: E402


def fetch():
    data = urllib.request.urlopen(URL, timeout=120).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            rel = parts[1]
            if not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
                continue
            target = os.path.join(DEST, rel)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tar.extractfile(member) as src, open(target, "wb") as out:
                    out.write(src.read())


def build_index():
    m = json.loads(gzip.open(os.path.join(APP, "map.json.gz"), "rt").read())
    sets = [list(s) for s in m["sets"]]
    index_of = {tuple(s): i for i, s in enumerate(sets)}
    lines = {f: {int(l): i for l, i in d.items()} for f, d in m["lines"].items()}

    words = {}
    for f, d in lines.items():
        path = os.path.join(DEST, f)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read().split("\n")
        for ln, i in d.items():
            if 0 < ln <= len(src):
                for w in set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", src[ln - 1])):
                    if w in COMMON_WORDS:
                        continue
                    words.setdefault(w, set()).update(sets[i])
    word_idx = {}
    for w, s in words.items():
        key = tuple(sorted(s))
        if key not in index_of:
            index_of[key] = len(sets)
            sets.append(list(key))
        word_idx[w] = index_of[key]

    idx = {
        "map_ids": {int(k): v for k, v in m["tests"].items()},
        "sets": sets,
        "lines": lines,
        "opens": m["opens"],
        "stems": m["stems"],
        "words": word_idx,
        "import_lines": {f: sorted(v) for f, v in m.get("import_lines", {}).items()},
        "testwords": m.get("testwords", {}),
    }
    with open(os.path.join(APP, "index.pkl"), "wb") as fh:
        pickle.dump(idx, fh, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--no-fetch":
        DEST = sys.argv[2]
    else:
        fetch()
    build_index()
    print("prepared", file=sys.stderr)
