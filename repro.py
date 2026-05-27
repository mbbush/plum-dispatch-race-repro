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

This script lowers `sys.setswitchinterval` to force the GIL to preempt
threads more aggressively, which makes the race much more likely to fire.
Without that, the entire beartype resolve_pep563 call often completes in
one GIL window and the race rarely manifests.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import overload

sys.setswitchinterval(1e-6)

from plum import dispatch


class A: pass
class B: pass
class C: pass
class D: pass
class E: pass
class F: pass
class G: pass
class H: pass
class I: pass
class J: pass


# Many overloads, each with many parameters annotated with multi-element unions,
# so beartype's per-function resolution loop is long enough to be preempted by
# another thread that's about to call resolve_pep563 on the same function.

@overload
def fn(
    a: A | B | C | D | E,
    b: F | G | H | I | J,
    c: A | C | E | G | I,
    d: B | D | F | H | J,
    e: A | B | C | D | E | F,
    f: G | H | I | J | A | B,
) -> A | B | C | D | E | F | G | H | I | J:
    return a


@overload
def fn(
    a: B | C | D | E | F,
    b: G | H | I | J | A,
    c: B | D | F | H | J,
    d: A | C | E | G | I,
    e: B | C | D | E | F | G,
    f: H | I | J | A | B | C,
) -> B | C | D | E | F | G | H | I | J | A:
    return a


@overload
def fn(
    a: C | D | E | F | G,
    b: H | I | J | A | B,
    c: C | E | G | I | A,
    d: D | F | H | J | B,
    e: C | D | E | F | G | H,
    f: I | J | A | B | C | D,
) -> C | D | E | F | G | H | I | J | A | B:
    return a


@overload
def fn(
    a: D | E | F | G | H,
    b: I | J | A | B | C,
    c: D | F | H | J | B,
    d: C | E | G | I | A,
    e: D | E | F | G | H | I,
    f: J | A | B | C | D | E,
) -> D | E | F | G | H | I | J | A | B | C:
    return a


@dispatch
def fn(a, b, c, d, e, f): ...


def main(n_threads: int = 64) -> int:
    barrier = threading.Barrier(n_threads)

    def worker() -> None:
        barrier.wait()
        fn(A(), F(), A(), B(), A(), G())

    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        futs = [ex.submit(worker) for _ in range(n_threads)]
        for fut in futs:
            try:
                fut.result()
            except BaseException as e:
                errors.append(e)

    if errors:
        print(f"REPRODUCED: {len(errors)}/{n_threads} threads failed")
        print(f"{type(errors[0]).__name__}: {errors[0]}")
        return 1
    print(f"no error this run ({n_threads} threads)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
