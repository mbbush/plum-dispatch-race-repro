"""Minimal reproducer for a thread-safety bug in plum-dispatch's lazy
signature resolution.

When multiple threads simultaneously trigger the first dispatch on a plum
@dispatch function, two threads can both enter
`Function._resolve_pending_registrations`, which iterates `self._pending`
without removing entries. Each pending entry's signature is then resolved
by `Signature.from_callable` -> `resolve_pep563` -> `beartype.peps.resolve_pep563`,
which mutates the function's `__annotations__` in place. Concurrent calls
on the same function can leave annotations in a partially-resolved state,
tripping `beartype._check.forward.fwdresolve.resolve_hint`'s assertion:

    AssertionError: <union> not stringified type hint.

Observed in the wild as an intermittent CI failure.

This script generates many independent @dispatch functions and races a
thread pool on the first call to each. The race only fires on the *first*
call to a given Function (subsequent calls find a populated dispatch
cache), so each generated function is one race trial. The trial count and
function complexity are tuned to reliably reproduce at the default GIL
switch interval (5 ms).
"""

from __future__ import annotations

import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor

N_TRIALS = 200    # independent @dispatch functions; each is one first-call race
N_OVERLOADS = 20  # @overloads per function; widens _resolve_pending_registrations
N_PARAMS = 8      # parameters per overload; widens per-resolve_pep563 mutation window
N_UNION = 6       # union arms per parameter; widens per-resolve_hint work
N_TYPES = 24      # bank of types to pick from
N_THREADS = 32    # workers racing each first call


def build_source() -> str:
    """Generate source for N_TRIALS independent @dispatch functions.

    Each function has one narrow overload (matches the call args we use
    below) plus N_OVERLOADS-1 wide overloads with multi-element unions.
    The wide overloads are unreachable from our call site but sit in the
    plum Function's _pending list and slow down signature resolution,
    widening the race window.
    """
    lines = [
        "from __future__ import annotations",
        "from typing import overload",
        "from plum import dispatch",
    ]
    for i in range(N_TYPES):
        lines.append(f"class T{i}: pass")
    for trial in range(N_TRIALS):
        name = f"fn_{trial}"
        # Narrow overload: all params are T0; matches our test call.
        narrow_params = ", ".join(f"p{p}: T0" for p in range(N_PARAMS))
        lines += [
            "@overload",
            f"def {name}({narrow_params}) -> T0:",
            "    return p0",
        ]
        # Wide overloads: rotating multi-element unions, unreachable from T0 args
        # but expensive to resolve (more name lookups, more `|` operations).
        for ov in range(1, N_OVERLOADS):
            params = []
            for p in range(N_PARAMS):
                # Skip T0 so we don't accidentally match the narrow call
                ts = [f"T{1 + (trial * 7 + ov * 3 + p * 5 + i) % (N_TYPES - 1)}"
                      for i in range(N_UNION)]
                params.append(f"p{p}: {' | '.join(ts)}")
            ret_ts = [f"T{1 + (trial * 11 + ov * 5 + i) % (N_TYPES - 1)}"
                      for i in range(N_UNION)]
            lines += [
                "@overload",
                f"def {name}({', '.join(params)}) -> {' | '.join(ret_ts)}:",
                "    return p0",
            ]
        sig = ", ".join(f"p{p}" for p in range(N_PARAMS))
        lines += [
            "@dispatch",
            f"def {name}({sig}): ...",
        ]
    return "\n".join(lines)


TRIAL_TIMEOUT_S = 5.0  # if a trial hangs longer than this, give up on it


def race(executor: ThreadPoolExecutor, fn, arg) -> list[BaseException]:
    """Fire N_THREADS threads at fn(arg, ...) simultaneously via a barrier.

    Returns the list of exceptions raised by workers. Workers that
    don't return within TRIAL_TIMEOUT_S are abandoned (race hit
    + the broken plum.Function state can cause sibling threads to
    deadlock; we accept the leak to keep progressing).
    """
    barrier = threading.Barrier(N_THREADS, timeout=TRIAL_TIMEOUT_S)
    errs: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait()
            fn(*[arg] * N_PARAMS)
        except BaseException as e:
            with lock:
                errs.append(e)

    futures = [executor.submit(worker) for _ in range(N_THREADS)]
    for f in futures:
        try:
            f.result(timeout=TRIAL_TIMEOUT_S)
        except TimeoutError:
            pass  # leaked worker; the post-hit plum state is corrupt anyway
    return errs


def is_race_assertion(e: BaseException) -> bool:
    return isinstance(e, AssertionError) and "stringified type hint" in str(e)


def main() -> int:
    print(f"compiling {N_TRIALS} trial functions...", flush=True)
    mod = types.ModuleType("plum_race_trials")
    sys.modules[mod.__name__] = mod
    exec(compile(build_source(), "plum_race_trials.py", "exec"), mod.__dict__)
    ns = mod.__dict__
    arg = ns["T0"]()
    print(f"running up to {N_TRIALS} trials × {N_THREADS} threads "
          f"(default switch interval, stop on first hit)...", flush=True)

    # One shared pool — creating/tearing down N_TRIALS executors is wasteful.
    with ThreadPoolExecutor(max_workers=N_THREADS) as executor:
        for trial in range(N_TRIALS):
            fn = ns[f"fn_{trial}"]
            errs = race(executor, fn, arg)
            race_errs = [e for e in errs if is_race_assertion(e)]
            if race_errs:
                e = race_errs[0]
                print(f"trial {trial}: REPRODUCED "
                      f"({len(race_errs)}/{N_THREADS} threads tripped assertion)",
                      flush=True)
                print(f"  {type(e).__name__}: {e}", flush=True)
                return 1
            other = [e for e in errs if not is_race_assertion(e)]
            if other:
                # First trial's dispatch had unrelated errors -> bug in test setup
                print(f"trial {trial}: {len(other)} unrelated errors, e.g. "
                      f"{type(other[0]).__name__}: {other[0]!r}", flush=True)
                return 2
            if (trial + 1) % 10 == 0:
                print(f"  {trial + 1} trials clean", flush=True)
    print(f"no race observed across {N_TRIALS} trials", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
