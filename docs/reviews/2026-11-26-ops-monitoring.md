# Review: Phase 5.0 health checks and background-work monitoring

- **Branch reviewed:** `feat/backend-ops-monitoring` (8a73c5a, fixes in 7d956de and a schema rename)
- **Reviewer:** independent review agent (Opus 5.5), read-only, running mutations on a copy of the code
- **Integration check:** Sonnet 5.5, over real HTTP with real worker processes
- **Date:** 2026-11-26

## Scope

- **Worker heartbeats** (`worker_heartbeats`):
  - Each worker reports every `WORKER_HEARTBEAT_SECONDS` (default 30) from a separate thread.
  - A report says when the worker last finished a task or loop, and how many tasks it has run.
  - A worker marks itself stopped on shutdown.
  - Rows gone for 7 days are pruned.
- **`GET /api/v1/health/ready`** (no sign-in):
  - Says only whether the database and Redis answer.
  - Returns 503 when the database is down, and "degraded" when Redis is down.
- **`GET /api/v1/tasks/overview`** (admin):
  - Worker states: running, stuck, not reporting, stopped.
  - Queue counts per task type, and the daily expiry check.
  - Plain-language problems and warnings.
  - No payloads, errors or keys.
- **`migrations/env.py`:** Alembic no longer disables existing loggers. Before this, running migrations in-process (as the tests do) silenced the app's loggers, which made earlier "no secrets in logs" checks meaningless.

## Findings

| ID | Severity | Finding | Outcome |
|---|---|---|---|
| F1 | Medium | Readiness kept its database connection checked out while it pinged Redis, and it needs no sign-in. During a Redis outage, frequent probes could fill the connection pool and take the API down. | **Fixed.** The connection is given back before Redis is pinged. Only one probe runs at a time, and its answer is reused for 2 seconds. The database check has a 2-second statement limit. |
| F2 | Low | The database connection had no connect timeout, so an unreachable database hung probes and requests for minutes. | **Fixed.** The engine now has a 5-second connect timeout and TCP keepalives. The integration check measured a 503 in 0.04 s. |
| F3 | Medium | A worker that crashed kept the overview "unhealthy" for 24 hours, even after a replacement was running. | **Fixed.** A worker lost more than an hour ago is still listed, but no longer counts as a problem. |
| F4 | Low | Any task that failed for good made the overview unhealthy, including expected cases such as an unreadable scan. | **Fixed.** Failures are now shown as `warnings` and don't affect `healthy`. |
| F5 | Low | A worker that had paused claiming (too many hung handlers) still looked active. | **Fixed.** No activity is recorded while the worker is paused, so it shows as stuck. |
| F6 | Low | The heartbeat interval was not validated. A value of 0 caused a busy loop, and values near 2 minutes caused false "not reporting" states. | **Fixed.** `WORKER_HEARTBEAT_SECONDS` must be between 5 and 30, or the worker refuses to start. |
| F7 | Low | The overview's counts scanned all task history. | **Fixed.** Added an index on `background_tasks.finished_at`. Retention of old tasks is planned for Phase 5.3. |
| F8 | Low | Workers in containers that share a host name and pid would share one row. | **Fixed.** The default worker name now includes a random part. |
| F9 | Low | Two new routes are missing from the contract. | **For the owner** to approve in the contract PR: additions only. |
| F10 | Low | Test gaps: an idle loop counting as activity, the Indian business date, the database-down path, and the worker being marked stopped when its loop crashes. | **Fixed,** except the database-down path, which is tested with a fake session. The integration check covered it against a real unreachable port. |

Found after the review: the health model was named `Readiness`, the same name as the existing job readiness enum. In the generated contract that replaced the existing schema. The model is now `HealthReadiness`, and the contract diff contains additions only.

## Mutation checks

Each mutation was restored and checked for equality afterwards.

- **The reviewer's 21 mutations:** 16 were caught. Two survivors were equivalent changes, and three exposed test gaps.
- **After the fixes,** 9 mutations were run, covering the three gaps and each fix. All 9 were caught:
  - an idle loop not counted as activity
  - a paused worker counted as active
  - the UTC date used as the business date
  - no rollback before the Redis ping
  - no readiness cache
  - no one-hour limit on "not reporting" problems
  - failures counted as problems
  - no interval validation
  - no random part in the default worker name
- **Logging:** with the `env.py` fix in place, making the lookup handler log a chassis number now fails the Phase 4.5 flow test.

## Integration check (real HTTP, real worker)

All 8 checks passed:

1. **Readiness:**
   - Everything up: 200 "ok".
   - Redis unreachable: 200 "degraded".
   - Database unreachable: 503 in 0.04 s.
   - No hosts or passwords appear in the responses.
2. **Overview access:** 401 without sign-in, 403 for an employee, 200 for an admin.
3. **No worker running:** reported as "No worker is running."
4. **Worker started:** it appeared within 12 s as running, with a name of the form `host:pid:random`.
5. **SIGTERM:** the worker showed as stopped within 3 s.
6. **SIGKILL:** the worker showed as not reporting, with a problem naming it, about 2 minutes later.
7. **No task contents** appear in the overview.
8. **50 rapid readiness calls:** all 200, each under 6 ms.

## Checks run

- `bash scripts/check.sh backend`: **759 passed**, Ruff clean.
- The contract check fails until the contract PR merges. The regenerated contract matches the backend code byte for byte.
- Migration: downgrade and upgrade both ran, and `alembic check` reports no drift.
