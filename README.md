# plum-dispatch race condition reproducer

`plum-dispatch`'s lazy signature resolution is not thread-safe. When two
worker threads simultaneously trigger the first dispatch on a `@dispatch`
function, beartype trips an internal assertion:

```
AssertionError: <union> not stringified type hint.
```

Originally observed as an intermittent CI failure; this reproducer
fires it reliably.

## Versions

- `plum-dispatch==2.9.0`
- `beartype==0.22.9`
- Python 3.13

## Run

```
uv venv -p 3.13
uv pip install -e .
.venv/bin/python repro.py
```

Run it a handful of times — the race is timing-dependent but hits
~half the runs. To get a hit rate, run:

```
hits=0; for i in $(seq 1 30); do
    out=$(.venv/bin/python repro.py)
    echo "$out" | grep -q REPRODUCED && hits=$((hits+1))
done
echo "$hits / 30"
```

## What's happening

Two threads call a `@dispatch` function for the first time concurrently.
Both enter `plum._function.Function._resolve_method_with_cache`, which calls
`_resolve_pending_registrations`. That method iterates `self._pending`
without removing entries while it processes them, so both threads end up
calling `Signature.from_callable(f, ...)` on the same `f`.

```
plum/_function.py:_resolve_method_with_cache
  → plum/_function.py:_resolve_pending_registrations
    → plum/_signature.py:Signature.from_callable
      → plum/_signature.py:_extract_signature
        → plum/_signature.py:resolve_pep563
          → beartype.peps.resolve_pep563(f)         # mutates f.__annotations__ in place
            → beartype._check.forward.fwdresolve.resolve_hint
              assert isinstance(hint, str)          # ← fires
```

Inside `beartype.peps.resolve_pep563`, the sequence is:

1. Read `f.__annotations__` (an immutable memoized FrozenDict view from
   `get_pep649_hintable_annotations`).
2. Iterate it and check: if any value is non-string, early-return as no-op.
3. Otherwise, copy it locally, iterate the copy, call `resolve_hint(hint, ...)`
   on each value, and write the resolved type back to `f.__annotations__` via
   `decor_meta.set_func_pith_hint`.

When two threads race here on the same `f`, thread B's local copy can include
values that thread A has already resolved. Step 2's check passed (the copy
was made before thread A's writes were observed), but step 3's `resolve_hint`
call receives a value that is no longer a `str`, and beartype's invariant
assertion fires.

## Why the reproducer cranks `sys.setswitchinterval`

CPython releases the GIL roughly every 5ms. The entire
`beartype.peps.resolve_pep563` call on a small function usually completes in
one GIL window, so the next thread sees fully-resolved state and silently
no-ops at step 2. The reproducer lowers the switch interval to `1e-6s` and
defines a function with many wide-union annotations, forcing the GIL to
preempt mid-resolution and exposing the window. The bug is real on default
settings too — it just fires rarely (which is exactly the "intermittent CI
failure" behavior).

## Suggested fix

`Function._resolve_pending_registrations` should be guarded by a
`threading.Lock` (or use a check-lock-check pattern). Alternatively, plum's
`resolve_pep563` could detect that `f.__annotations__` has already been
resolved by another thread and skip calling `beartype_resolve_pep563` —
but locking is the cleaner fix because the entire registration step
(iterating `_pending`, building methods, registering them) is not safe to
run concurrently on the same `Function` instance regardless of beartype.

A defensive secondary fix in beartype would be to have `resolve_hint`
tolerate non-string input by returning it unchanged rather than asserting,
since the assertion is an internal invariant violated only by external
concurrent mutation.

## Workaround (consumer side)

If you can't wait for an upstream fix, force resolution at module import
time (single-threaded under CPython's import lock) immediately after the
`@dispatch` declaration:

```python
@dispatch
def my_fn(x, y): ...

my_fn._resolve_pending_registrations()  # warm up; not thread-safe lazily
```

Worker threads then hit a fully-resolved dispatch table and never trigger
the racy lazy path.
