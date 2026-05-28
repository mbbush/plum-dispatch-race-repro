# plum-dispatch race condition reproducer

`plum-dispatch`'s lazy signature resolution is not thread-safe. When two
worker threads simultaneously make the *first* dispatch call on a
`@dispatch` Function, concurrent mutation of the function's
`__annotations__` dict produces several distinct symptoms, all rooted in
the same race. Observed examples:

- `AssertionError: <hint> not stringified type hint.`
  (beartype's `resolve_hint` asserts on an already-resolved annotation)
- `RuntimeError: dictionary changed size during iteration`
  (concurrent mutation of `f.__annotations__`)
- `plum.NotFoundLookupError` for an argument set that should resolve cleanly
  (dispatch table left in a corrupt state by a partial resolve)
- `NameError: name '__beartype_object_<id>' is not defined`
  (beartype-synthesized wrapper references a transient object another
  thread cleaned up)

Originally observed as an intermittent CI failure on a multi-threaded
solver.

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

The script runs for 60 seconds, creating fresh `@dispatch` Functions and
racing 32 threads on each one's first call. It prints a tally of how
many trials were clean and how often each error symptom fired. A typical
run looks like:

```
Ran 5441 trials × 32 threads in 60.0s
  clean trials:           5431
  trials with >=1 error:  10

Errors across all workers (40 total):
      36  RuntimeError: "dictionary changed size during iteration"
       3  AssertionError: "<hint> not stringified type hint."
       1  other (NameError): name '__beartype_object_103372960812752' is not defined
```

Per-trial hit rate is ~0.2% at default GIL switch interval; one
out-of-the-box 60s run is enough to see multiple symptoms.

## What's happening

Two threads call a `@dispatch` Function for the first time concurrently.
Both enter `plum._function.Function._resolve_method_with_cache`, which
calls `_resolve_pending_registrations`. That method iterates
`self._pending` without removing entries while it processes them, so
both threads end up calling `Signature.from_callable(f, ...)` on the
same `f`:

```
plum/_function.py:_resolve_method_with_cache
  → plum/_function.py:_resolve_pending_registrations
    → plum/_signature.py:Signature.from_callable
      → plum/_signature.py:_extract_signature
        → plum/_signature.py:resolve_pep563
          → beartype.peps.resolve_pep563(f)   # mutates f.__annotations__ in place
```

`beartype.peps.resolve_pep563` reads `f.__annotations__`, copies it,
iterates the copy, and writes each resolved type back into
`f.__annotations__` via `decor_meta.set_func_pith_hint`. When two
threads race here on the same `f`, several invariants get violated
depending on exact interleaving:

- One thread's locally-copied annotation may already have been resolved
  to a non-string by the other thread before `resolve_hint` is called
  on it → **AssertionError**.
- One thread iterates `f.__annotations__` while the other mutates it →
  **RuntimeError: dictionary changed size during iteration**.
- A partial resolve leaves the plum Function's dispatch table half-built
  → subsequent successful args can't find a match →
  **NotFoundLookupError**.
- Beartype synthesizes per-call wrapper objects whose names are
  referenced from generated code; concurrent invocations can drop them
  before the wrapper is called → **NameError** on the synthesized
  identifier.

All four symptoms share a root cause: `_resolve_pending_registrations`
has no concurrency control around the per-function annotation mutation.

## Suggested fix

`Function._resolve_pending_registrations` should be guarded by a
`threading.Lock` (or use a check-lock-check pattern). The entire
registration step (iterating `_pending`, resolving signatures, building
methods, registering them) is not safe to run concurrently on the same
`Function` instance.

A defensive secondary fix in beartype would be to have `resolve_hint`
tolerate non-string input by returning it unchanged rather than
asserting, since the assertion is an internal invariant violated only
by external concurrent mutation. That would address one symptom; the
other symptoms still require fixing the race in plum.

## Workaround (consumer side)

If you can't wait for an upstream fix, force resolution at module
import time (single-threaded under CPython's import lock) immediately
after the `@dispatch` declaration:

```python
@dispatch
def my_fn(x, y): ...

my_fn._resolve_pending_registrations()  # warm up; not thread-safe lazily
```

Worker threads then hit a fully-resolved dispatch table and never
trigger the racy lazy path.
