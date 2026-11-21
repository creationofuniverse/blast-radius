import sys, os, json
import concurrent.futures as _cf
import pickle as _pk

class _SyncPool:
    """Runs submitted work in this process so coverage and file opens land on the running test."""
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer is not None:
            initializer(*_pk.loads(_pk.dumps(initargs)))
    def submit(self, fn, *a, **k):
        f = _cf.Future()
        try:
            fn, a, k = _pk.loads(_pk.dumps((fn, a, k)))
            f.set_result(_pk.loads(_pk.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f
    def map(self, fn, *its, **k):
        return map(fn, *its)
    def shutdown(self, *a, **k):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        self.shutdown()

_cf.ProcessPoolExecutor = _SyncPool

_cur = [None]; _opens = {}
ROOT = os.path.abspath(os.environ.get("SQLGLOT_ROOT", "."))
def _hook(ev, args):
    if ev == "open" and _cur[0] is not None:
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                p = os.path.abspath(os.fsdecode(p))
            except Exception:
                return
            if p.startswith(ROOT) and not p.endswith(".pyc"):
                _opens.setdefault(_cur[0], set()).add(os.path.relpath(p, ROOT))
sys.addaudithook(_hook)
import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _cur[0] = item.nodeid + "|setup"
    yield
    _cur[0] = None
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _cur[0] = item.nodeid + "|run"
    yield
    _cur[0] = None
def pytest_sessionfinish(session):
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(f"/home/claude/work/opens_{wid}.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _opens.items()}, f)
