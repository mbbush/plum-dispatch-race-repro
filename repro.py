"""Reproducer for a thread-safety bug in plum-dispatch's lazy signature resolution.

When multiple threads simultaneously make the *first* dispatch call on a
plum @dispatch Function, two threads can both enter
`Function._resolve_pending_registrations`. That method iterates
`self._pending` without removing entries, so both threads call
`Signature.from_callable(f)` on the same `f`, which calls
`beartype.peps.resolve_pep563(f)`. resolve_pep563 mutates
`f.__annotations__` in place, racing with the other thread's iteration.

The race surfaces as one of several symptoms:
  - AssertionError: "<hint> not stringified type hint."
        (beartype's resolve_hint asserts on an already-resolved annotation)
  - RuntimeError: "dictionary changed size during iteration"
        (concurrent mutation of f.__annotations__)
  - plum.NotFoundLookupError after a successful resolve
        (dispatch table left in a corrupt state by a partial resolve)

Each plum.Function only races on its *first* dispatch call, so this script
generates fresh @dispatch functions and races a thread pool on each one's
first call for DURATION_S seconds, then reports how many times each error
category fired.

Run:  uv pip install -e . && .venv/bin/python repro.py
"""

from __future__ import annotations

import sys
import threading
import time
import types
from collections import Counter

THREADS = 32
DURATION_S = 60.0

# Source for one trial. Each trial uses its own Dispatcher() so its Function
# is isolated from other trials (plum's module-level `dispatch` is a singleton
# keyed by function name, so reusing `dispatch` across trials would share state).
# Multiple wide-union methods make _resolve_pending_registrations slow enough
# that the GIL is likely to preempt a resolving thread mid-mutation.
TRIAL_SRC = """\
from __future__ import annotations
from plum import Dispatcher
dispatcher = Dispatcher()

class A: pass
class B: pass
class C: pass
class D: pass
class E: pass
class F: pass
class G: pass
class H: pass

@dispatcher
def fn(x: A, y: A | B | C | D | E | F | G | H) -> A: return x
@dispatcher
def fn(x: B, y: A | B | C | D | E | F | G | H) -> B: return x
@dispatcher
def fn(x: C, y: A | B | C | D | E | F | G | H) -> C: return x
@dispatcher
def fn(x: D, y: A | B | C | D | E | F | G | H) -> D: return x
@dispatcher
def fn(x: E, y: A | B | C | D | E | F | G | H) -> E: return x
@dispatcher
def fn(x: F, y: A | B | C | D | E | F | G | H) -> F: return x
@dispatcher
def fn(x: G, y: A | B | C | D | E | F | G | H) -> G: return x
@dispatcher
def fn(x: H, y: A | B | C | D | E | F | G | H) -> H: return x
"""


def make_trial(i: int) -> types.ModuleType:
    """Compile TRIAL_SRC into a fresh module so its `fn` is a fresh plum.Function."""
    mod = types.ModuleType(f"_plum_race_trial_{i}")
    sys.modules[mod.__name__] = mod  # so beartype can resolve forward refs
    exec(compile(TRIAL_SRC, mod.__name__, "exec"), mod.__dict__)
    return mod


def race(mod: types.ModuleType) -> list[BaseException]:
    """Fire THREADS workers at mod.fn's first call; return any exceptions raised.

    mod.fn(A, B) matches the first method cleanly, so under correct
    behavior all workers return without error.
    """
    barrier = threading.Barrier(THREADS, timeout=5.0)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait()
            mod.fn(mod.A(), mod.B())
        except BaseException as e:
            with lock:
                errors.append(e)

    # Use fresh daemon threads per trial: a leaked worker from a corrupted
    # plum state shouldn't starve future trials, and daemons exit cleanly
    # with the process.
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    return errors


def categorize(e: BaseException) -> str:
    msg = str(e)
    if isinstance(e, AssertionError) and "stringified type hint" in msg:
        return 'AssertionError: "<hint> not stringified type hint."'
    if isinstance(e, RuntimeError) and "dictionary changed size" in msg:
        return 'RuntimeError: "dictionary changed size during iteration"'
    if type(e).__name__ == "NotFoundLookupError":
        return "plum.NotFoundLookupError (dispatch corrupted by partial resolve)"
    if isinstance(e, threading.BrokenBarrierError):
        return "BrokenBarrierError (siblings hung past barrier timeout)"
    if isinstance(e, NameError) and "__beartype_object" in msg:
        return 'NameError: "__beartype_object_<id> not defined" (is_bearable checker body/scope mismatch)'
    return f"other ({type(e).__name__}): {msg[:80]}"


def main() -> int:
    error_counts: Counter[str] = Counter()
    trials_clean = 0
    trials_with_errors = 0
    trials = 0

    print(f"Running for {DURATION_S:.0f} seconds...", flush=True)
    start = time.monotonic()
    deadline = start + DURATION_S
    while time.monotonic() < deadline:
        errors = race(make_trial(trials))
        trials += 1
        if errors:
            trials_with_errors += 1
            for e in errors:
                error_counts[categorize(e)] += 1
            print(".", end="", flush=True)
        else:
            trials_clean += 1
    elapsed = time.monotonic() - start
    if trials_with_errors:
        print()

    print(f"Ran {trials} trials × {THREADS} threads in {elapsed:.1f}s")
    print(f"  clean trials:           {trials_clean}")
    print(f"  trials with >=1 error:  {trials_with_errors}")
    print()
    if error_counts:
        total = sum(error_counts.values())
        print(f"Errors across all workers ({total} total):")
        for cat, n in error_counts.most_common():
            print(f"  {n:>6}  {cat}")
    else:
        print("No errors observed.")
    return 0 if error_counts else 1


if __name__ == "__main__":
    sys.exit(main())
