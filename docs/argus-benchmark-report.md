# ARGUS Benchmark Report

Measured performance of the packaged Docker stack under synthetic load, using
`infrastructure/load-soak.py`. Every number in this document was **measured on
the hardware named below** — there are no extrapolations, no vendor benchmark
borrowing, and no round numbers chosen to look good.

If you need a number for *your* hardware, run the harness. It is stdlib-only
Python 3.12+, so it runs on any host that can reach the API.

---

## 1. Method

**Harness**: `infrastructure/load-soak.py` — a closed-loop load generator with
`--concurrency` in-flight workers, a fixed `--duration` window, and per-request
latency recorded for **every** completed request (percentiles are computed from
the full sample, not a reservoir).

**Scenarios**

| Scenario | What it measures |
| --- | --- |
| `ingest` | `POST /api/v1/ingestion/queue` — the async edge (enqueue only) |
| `ingest-sync` | Synchronous ingestion — the whole normalize + persist pipeline |
| `metric` | One metric-sample write — the cheapest useful write |
| `read` | Dashboard read mix (the queries the overview pages issue) |
| `mixed` | 4 ingest : 2 read : 1 metric — the shape of real traffic |

**Error accounting is deliberately split**: a `4xx` is attributed to the harness
(a malformed request), a `5xx` to the platform, a timeout to "no answer". A
number is only comparable when its error classes are known, so they are always
reported.

**Queue depth** is sampled from `/metrics` (`argus_ingestion_queue_depth`,
summed across queues) while load runs, because the one thing a burst can do
invisibly is pile up behind the API.

---

## 2. Tested environment

| | |
| --- | --- |
| Host | Apple Silicon (arm64), macOS 26.5.1, 6 CPU |
| Runtime | Docker Desktop; API, Postgres 16 and Redis 7 each in their own container |
| Topology | Single container (`BACKGROUND_JOBS_ENABLED=true`) — the default `docker compose up` |
| Harness location | Host process, over the published port (i.e. numbers include Docker's NAT) |
| Concurrency | 8 in-flight requests |
| Duration | 20 s per scenario |
| API workers | 1 |

> **Read this before quoting a number.** The harness runs on the host and the
> service runs in a container, so every figure below already includes
> Docker-desktop networking. A same-machine, bare-metal deployment will
> typically measure higher; a deployment with a network hop between client and
> API will typically measure lower.

---

## 3. Measured envelope — shipping defaults

The shipped defaults include the edge rate limiter
(`RATE_LIMIT_PER_MINUTE=600`, `RATE_LIMIT_BURST=120`, per token). That limit is
**part of the product**, so the first envelope is the one a client actually
experiences:

| Scenario | Requests | 2xx | 429 | 5xx |
| --- | --- | --- | --- | --- |
| `ingest` | 19,319 | 268 | 19,051 | 0 |
| `ingest-sync` | 11,532 | 171 | 11,361 | 0 |

**Interpretation:** under a deliberately abusive burst (8 concurrent workers
hammering one token) the limiter sheds the excess and the platform stays up —
zero `5xx`, zero timeouts, no crash, no unbounded queue. That is the intended
behaviour of a protected edge, and it is the correct answer to "what happens if
one client goes rogue?".

It is *not* a capacity number. For that, the limiter has to be lifted — which is
exactly what the next section does.

---

## 4. Measured envelope — limiter lifted (`RATE_LIMIT_ENABLED=false`)

Same host, same topology, same concurrency. This is the raw throughput of the
pipeline with the edge policy removed.

| Scenario | Throughput | p50 | p95 | p99 | max | 2xx | 4xx | 5xx |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ingest` (async enqueue) | **171 req/s** | 42.2 ms | 61.9 ms | 194.4 ms | 443.9 ms | 3,424 | 0 | 0 |
| `ingest-sync` (full pipeline) | **313 req/s** | 21.6 ms | 33.8 ms | 123.5 ms | 429.6 ms | 6,258 | 0 | 0 |
| `metric` (single sample) | **274 req/s** | 25.6 ms | 39.1 ms | 140.3 ms | — | 5,472 | 0 | 0 |
| `read` (dashboard mix) | **249 req/s** | 27.5 ms | 46.4 ms | 114.6 ms | — | 4,980 | 0 | 0 |
| `mixed` (realistic shape) | **294 req/s** | 24.1 ms | 36.4 ms | 131.4 ms | — | 5,894 | 0 | 0 |

**Zero `5xx` and zero timeouts in every scenario at every concurrency tested.**

### Reading these numbers honestly

* **`ingest-sync` out-measures `ingest`.** This looks backwards — the async path
  should be cheaper — and it is worth understanding rather than hiding. The
  async path performs an enqueue plus a queue-depth metric scrape per request,
  so the measured edge is doing *more* work per call, while the synchronous path
  is a direct write. The async path's advantage is not latency at this scale;
  it is that the work is *deferrable*, which is what keeps p99 bounded when the
  downstream consumers are slow.
* **p99 is roughly 5× p50 throughout.** This is a single-worker API with a
  connection pool; the tail is pool acquisition plus scheduler hand-off, not a
  pathological code path. The spread is stable across scenarios, which is the
  property that matters for SLO writing.
* **Throughput is not linear in concurrency — it plateaus.** The table above is a
  *tested floor* at 8 clients; §4b scales it and shows where the ceiling is.

---

## 4b. Concurrency scaling (8 → 64 clients)

Same host, same topology, limiter lifted, same scenario run at four concurrency
levels — the measurement §6 used to list as missing.

**`read` (dashboard mix), 15 s window per level**

| Concurrency | Throughput | p50 | p95 | p99 | Requests | 2xx | 5xx | Timeouts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8 | **242.9 req/s** | 28 ms | 46 ms | 112 ms | 3,647 | 3,647 | 0 | 0 |
| 16 | 182.1 req/s | 78 ms | 148 ms | 264 ms | 2,747 | 2,747 | 0 | 0 |
| 32 | 190.3 req/s | 148 ms | 289 ms | 357 ms | 2,873 | 2,873 | 0 | 0 |
| 64 | 197.8 req/s | 231 ms | 822 ms | 1,156 ms | 3,003 | 3,003 | 0 | 0 |

**`metric` (single sample write), 12 s window per level**

| Concurrency | Throughput | p50 | p95 | p99 | Requests | 2xx | 5xx | Timeouts |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8 | **214.7 req/s** | 32 ms | 65 ms | 152 ms | 2,580 | 2,580 | 0 | 0 |
| 16 | 194.4 req/s | 75 ms | 114 ms | 258 ms | 2,339 | 2,339 | 0 | 0 |
| 32 | 172.1 req/s | 165 ms | 303 ms | 420 ms | 2,081 | 2,081 | 0 | 0 |
| 64 | 186.2 req/s | 283 ms | 745 ms | 1,000 ms | 2,274 | 2,274 | 0 | 0 |

### Reading these numbers honestly

* **The single-container stack saturates at ~180-240 req/s, and it saturates at
  about 8 concurrent clients.** Going from 8 to 64 clients multiplies latency by
  ~8× (p50 28 ms → 231 ms) and buys **no throughput at all**. Extra concurrency
  here converts into queueing, not capacity.
* **Saturation is graceful, which is the property that matters.** Across eight
  runs at four concurrency levels: **zero `5xx`, zero timeouts, zero errors**.
  The system gets slower under load; it does not start failing.
* **The ceiling is the host, not a code path.** API, Postgres, Redis and the
  background sweeps all share six CPUs in these runs, so the plateau mixes
  database work, container scheduling and sweep passes. It is a statement about
  this hardware, not a limit of the architecture — the split topology
  (`--profile worker`, see `docs/operations.md` §6) is how you lift it.
* **What this means for sizing.** Size by *clients*, not by hoped-for throughput:
  if your ingest is above ~150 req/s sustained on one host, split the roles rather
  than adding concurrent clients. Below that, the shipped defaults are fine and
  the edge limiter (600 req/min per token) is the binding constraint before the
  pipeline is.

---

## 5. Backlog behaviour under burst

Write bursts do not block; they accumulate in Redis and drain.

| Observation | Value |
| --- | --- |
| Peak backlog observed mid-run (summed across queues) | ~9,000 jobs |
| Backlog ~60 s after load stopped | **0 jobs** |
| Jobs lost | **0** (every enqueued job was consumed) |

A backlog of ~9,000 jobs drained completely with no load and no intervention.
This is the intended shape: **absorb the spike, drain deterministically, never
drop work**. An operator watching `argus_ingestion_queue_depth` should see a
saw-tooth under burst, not a monotonically rising line. If the line rises and
does not fall, the consumer is the bottleneck — not the API.

---

## 6. What was measured but not solved

Honest limits of this pass:

1. ~~**Concurrency was not scaled past 8.**~~ **Closed** — §4b characterises
   `read` and `metric` at `--concurrency 8 / 16 / 32 / 64`: throughput plateaus
   at ~8 clients, latency grows roughly linearly with concurrency, and the error
   count stays at zero throughout. Write-heavy scenarios at 64 clients
   (`ingest`, `ingest-sync`, `mixed`) are still only characterised at 8.
2. **The load was synthetic.** Scenario payloads are representative in shape and
   size, but they are not a recording of production traffic. A workload with
   much larger trace trees will exercise the parser harder than `mixed` does.
3. **Postgres and Redis shared one host with the API.** Disk contention between
   the three containers is inside these numbers and cannot be separated from
   them.
4. **The `read` mix is the overview-page query set, not every endpoint.** Deep
   or adversarial queries (wide graph traversals, unbounded exports) were not
   load-tested; their boundedness is asserted structurally instead (every
   list endpoint is paginated and capped — see `docs/production-readiness.md`).

---

## 7. Capacity guidance

Derived only from the table above, on the named hardware:

| If you are deploying for | Guidance |
| --- | --- |
| A team (a few dozen services, bursty telemetry) | The default single-container topology is comfortably sufficient; keep the rate limiter at its defaults. |
| Heavy sustained ingest (> 100 req/s sustained) | Run the split topology — `BACKGROUND_JOBS_ENABLED=false` for HTTP, a separate worker (`--profile worker`). This moves all sweep/detection work off the request path. See `docs/operations.md`. |
| A trusted internal batch client | Raise `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_BURST` for that client's token rather than disabling the limiter. |
| Anything where p99 matters more than throughput | Measure your own. These p99s include Docker-desktop NAT and are not a promise about your network. |

---

## 8. Reproducing this report

```bash
# 1. bring the stack up
docker compose up -d

# 2. mint a token (printed once in the API logs on first boot)
docker compose logs api | grep -i "admin token"

# 3. run the same pass this document reports
export ARGUS_TOKEN=<token>
python infrastructure/load-soak.py \
  --scenario all --duration 20 --concurrency 8 --json > load-raw.json

# optional: measure the unprotected ceiling instead of the shipped policy
RATE_LIMIT_ENABLED=false docker compose up -d api
python infrastructure/load-soak.py --scenario all --duration 20 --concurrency 8 --json

# reproduce the §4b concurrency sweep
for c in 8 16 32 64; do
  python infrastructure/load-soak.py --scenario read --duration 15 --concurrency "$c" --json \
    | sed -n '/^{/,$p' > "read-c$c.json"
done

# restore the shipped defaults when you are done
docker compose up -d api
```

Note on `--json`: the machine-readable object is printed **after** the human
summary, so redirecting stdout to a file captures both. `sed -n '/^{/,$p'`
(the example above) keeps only the JSON.

Run it on your hardware before you size anything. The only trustworthy capacity
number is the one you measured.
