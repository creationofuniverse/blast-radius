# Blast Radius — Team MOON

Test selection for sqlglot changes (Buildathon 2026, RWX track).

## How it works

1. **Coverage map, built offline from the pinned commit.** Every sqlglot test was run once
   under `coverage` with one context per test, and with every file it opened recorded
   (`tools/opentrack.py`, `tools/build_map.py`). Process pools in the tests were made to run
   in-process (with the same pickling) so work done in pool workers is credited to the test.
   The result maps each executed sqlglot line to the tests that ran it, each fixture file to the
   tests that read it, and each dialect to the tests that use it or a dialect built on it.
   Nothing in it is derived from any patch.
2. **Reading a patch.**
   - Only comments or blank lines changed (same Python tokens) → nothing can break → run nothing.
   - A change inside a function → every test that executed that function.
   - A change at class or module level (attributes, tables, imports, signatures) → every test
     that executed a line mentioning the changed names, narrowed to the dialect's users when the
     file belongs to one dialect, plus the tests of the enclosing class.
   - A fixture file → the tests that opened it. A test file → the changed tests.
   - Anything unexpected (new, deleted or renamed files, files outside `sqlglot/` and
     `tests/`, parse failures) → run every test. A missed failure costs 300 s; running
     everything costs about 45 s.
3. **Splitting.** Longest test first onto the least loaded worker, trying 1 to 8 workers and
   keeping the cheapest (largest load + 2 s per worker).

## Layout

- `selector.py` — the program the image runs.
- `index.pkl.gz` — the coverage map, preprocessed.
- `ref.tar.gz` — sqlglot's Python sources and tests at the pinned commit
  (eb11aec4be8cb2122afe713a4816662c3f0fb2fa), MIT licensed, see `LICENSE` inside the archive. Used to read
  the code around each change.
- `tools/` — how the map was built.

No outside code besides sqlglot itself (MIT). Licensed under MIT, see `LICENSE`.
