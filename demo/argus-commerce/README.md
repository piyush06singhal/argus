# ARGUS Demo Commerce

A deliberately small, real source tree representing the checkout path of the
`argus-demo-commerce` project used by the end-to-end smoke tests.

It exists so that Phase 6 code intelligence can be exercised against **actual
files and actual commits** instead of fixtures: the tree is mounted read-only
into the API container at `/repos/demo-commerce`, and
`infrastructure/e2e-smoke-phase6.sh` copies it into a scratch directory, builds
a two-commit history with real `git`, and then drives the whole pipeline
(index → symbol search → trace-to-code mapping → debug session → AI analysis)
through the HTTP API.

## The planted defect

`services/inventory/repository.py` guards every query with
`DB_TIMEOUT_SECONDS`. At the first commit it is `0.5`; the second commit
("perf: halve the database timeout") drops it to `0.25`, which makes
`_query()` raise `TimeoutError` for every call. `services/checkout/service.py`
retries seven times with linear backoff, so the timeout is amplified into a
multi-second request that the checkout endpoint reports as `504`.

This is the shape the phase is built to investigate:

```text
POST /checkout
   → CheckoutService.process()      services/checkout/service.py
   → InventoryRepository.fetch_stock()   services/inventory/repository.py
   → database query times out
```

Nothing in the demo tells ARGUS the answer. The defect is discovered from the
evidence: the failing trace, the stack trace, the trace-to-code mapping, the
deployed snapshot, and the diff between the snapshot and its parent commit.

## The counterexample

`services/marketing/banners.py` is touched by a **third**, later commit that is
unrelated to the failure. ARGUS must report it as temporally recent but
unconnected (a `RELEVANT_CHANGE` ranking of `UNRELATED`/`WEAK`), and must not
blame the newest commit.

## Layout

```text
services/checkout/service.py     retry loop + checkout endpoint
services/checkout/retry.py       retry policy constants
services/inventory/repository.py database access (the planted defect)
services/api/routes.py           HTTP surface
services/marketing/banners.py    unrelated recent change
web/checkout.ts                  the same call path, client side
tests/test_checkout.py           a test that would catch the regression
```
