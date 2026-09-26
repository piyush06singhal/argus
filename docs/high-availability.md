# High availability, point-in-time recovery, and multi-region

This document describes what the HA overlay actually provides, how to rehearse a
recovery, and — just as importantly — the line between what is shipped and what
remains an operator's topology decision. Nothing here is aspirational: every
claim is exercised by `infrastructure/e2e-smoke-ha.sh`, which applies the
overlay, streams a real write to the replica, and performs a real
point-in-time recovery to a chosen moment.

---

## 1. What the overlay is

`docker-compose.ha.yml` is applied on top of the base file:

```bash
docker compose -f docker-compose.yml -f docker-compose.ha.yml up -d
```

It adds exactly two capabilities:

1. **Continuous WAL archiving** on the primary. Every completed write-ahead-log
   segment plus a base backup is enough to rebuild the database to any moment in
   the archived window. Without archived WAL the newest recovery point is the
   last `pg_dump` — the difference between "we lost an hour" and "we lost
   everything since the last backup".
2. **A streaming replica** (`postgres-replica`, published on host port **5434**,
   deliberately not the primary's 5433) that can be promoted and also serves as
   a live copy for an off-host restore rehearsal.

The overlay also sets `archive_timeout=60`, so a quiet primary still produces a
recoverable segment within a minute rather than holding an unarchived one until
it happens to fill.

### What the overlay deliberately does *not* do

* **It is not automatic failover.** There is no consensus layer, no leader
  election and nothing that promotes the replica on its own. Promotion is a
  deliberate operator action (`pg_ctl promote`, or pointing `DATABASE_URL` at
  the replica after promoting it). A fake failover baked into a compose file
  would look automatic and silently split the cluster when the two sides
  disagree — worse than no failover at all.
* **It does not split reads.** The API uses one DSN. Routing reads to a replica
  needs read-your-writes reasoning ARGUS does not have (a request that writes
  and then reads would see a stale copy), so the replica is for durability and
  failover, not throughput.
* **It is not multi-region.** A replica on the same host survives a disk failure
  and a bad migration; it does not survive the host. See §4.

---

## 2. Rehearsing a recovery

A backup nobody has restored is a belief. Two rehearsals exist, and both run in
CI on every pull request.

### Restore the newest dump into a scratch database

```bash
bash infrastructure/backup.sh drill      # from the base stack
```

`drill` restores the newest archive with `pg_restore --exit-on-error` into a
throwaway database and checks row counts against a floor recorded at dump time
(with an explicit tolerance, because ARGUS writes while it backs up). See
[operations.md §4](operations.md#4-backup-and-restore).

### Recover to a chosen moment (point-in-time recovery)

```bash
bash infrastructure/e2e-smoke-ha.sh
```

The gate is the reference procedure for a real PITR, and it is written so that a
restore which failed to replay *any* WAL cannot pass by accident:

* it takes a base backup into the archive volume and records a **recovery target
  moment between two marker rows**;
* it restores a scratch cluster with `recovery_target_time` set to that moment,
  and requires the earlier marker to be present **and** the later one absent;
* it performs a **second restore with no target** as a control, which must
  contain *both* markers — proving the difference between the runs is the
  recovery target, not a failure to replay.

If a recovery is ever needed for real, the steps are the control run's steps with
your target moment substituted, plus promoting the replica if the primary is
gone.

---

## 3. Operating the archive

WAL accumulates in the `argus_wal_archive` volume — intentionally, because a
recovery point is only useful while its WAL still exists. It is therefore
**bounded by the operator**, not by the platform:

* keep at least the window you promise in your recovery objective (a day of WAL
  is cheap; a month of a busy primary is not);
* ship a copy off-host — the archive volume is on the same disk as the primary
  otherwise, which defeats the purpose;
* prune segments older than your retention window only after confirming a base
  backup newer than the cutoff exists. `pg_archivecleanup` is the correct tool;
  deleting files by hand is how an archive becomes a gap.

The archive is populated by `archive_command`, which is **fail-loud**: it refuses
to overwrite an existing segment (`test ! -f`) rather than silently succeeding,
so a duplicate or truncated archive is noticed instead of trusted.
`pg_stat_archiver.failed_count` is exposed to the alerting rules; a non-zero
value means WAL is piling up on the primary.

---

## 4. Multi-region, honestly

ARGUS ships **observability** for a multi-region deployment, not a multi-region
data plane:

* every `/metrics` scrape carries `argus_instance_info{instance, region, version}`,
  set from `INSTANCE_ID` and `INSTANCE_REGION`. A dashboard can therefore tell
  its replicas apart and a per-region view is possible;
* **`INSTANCE_REGION` does not replicate anything.** ARGUS does not shard data,
  route writes across regions, or merge regions into one cluster. A multi-region
  topology is a Postgres/replica topology (one writable primary, asynchronous
  replicas per region, plus a failover decision) and that decision belongs to the
  operator and their platform, not to a compose file.

A workable reference topology, in words:

| Layer | Reference approach |
| --- | --- |
| Postgres | One primary; a streaming replica per region (the overlay's replica, extended across hosts); a WAL archive in object storage. Promotion is manual and rehearsed. |
| API | Stateless; run replicas in each region behind a regional load balancer and set `INSTANCE_REGION` per region. Rate limiting is shared through Redis (see [operations.md §6](operations.md#6-rate-limiting-and-capacity)), so a regional Redis with cross-region replication is the shared ceiling. |
| Redis | Not yet replicated by this stack: a region whose Redis is lost falls back to per-process limiting for that region (surfaced by `argus_rate_limit_backend`) and the region recovers its shared ceiling when Redis returns. |
| Reads | Single-DSN today. Read routing to replicas requires read-your-writes reasoning that is not implemented; do not point the API at a replica. |

Because the API is stateless and the database is the only durable store, adding
regions is a deployment exercise, not a code change.
