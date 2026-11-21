# Review: background processing engine (Phase 2.2)

- **Branch:** `feat/backend-processing-engine` at `f7ac7bb`
- **Diff:** `git diff origin/main...feat/backend-processing-engine` (backend only: `background_tasks` model and migration `b9c0d1e2f3a4`, `task_queue`, `task_repository`, `notifier`, `registry`, `worker`, admin task routes/service/schemas, `redis` and `fakeredis` dependencies, tests)
- **Reviewed against:** `AGENTS.md`, `backend/AGENTS.md`, `docs/contracts/decisions.md` §10, the review prompt in `docs/SETUP-AI-WORKFLOW.md` §5, earlier reviews in `docs/reviews/`, and the Phase 2.3 plan (a `document_ocr` handler that writes OCR results and moves documents to `needs_review`).
- **Reviewer:** review agent. Findings only; no application code was changed.

## Checks run

- `TEST_DATABASE_URL=... uv run pytest -q` (test database `csc_crm_test` only): **417 passed**.
- `bash scripts/check.sh backend` on the branch: dependency lock, Ruff lint, Ruff format, pytest (417 passed) **PASS**; contract check **FAIL (contract drift)**. The generated spec only adds `/api/v1/tasks`, `/api/v1/tasks/{task_id}`, `/api/v1/tasks/{task_id}/retry` and the schemas `TaskListResponse`, `TaskRead`, `TaskStatus`, `TaskType`. Nothing is removed or changed (see Accepted gaps).
- Migration round trip (downgrade to `a7b8c9d0e1f2` and upgrade again) was **not run**: the tool permission layer refused the downgrade. The upgrade runs in every test session (`alembic upgrade head` in `conftest.py`), and the downgrade was reviewed by reading it.
- Probes. They used the test database and Redis database 15 only, with real commits. All probe rows were deleted afterwards (`background_tasks` 0 rows, no probe audit rows), and Redis db 15 was flushed (`dbsize` 0).
  1. Real Redis, `Worker._wait_for_wakeup()` with the production client (`socket_timeout=5`). With `poll_interval=5` (the default), every idle wait raised `redis.TimeoutError`, logged `Redis unavailable (TimeoutError); polling.` and took **10.01 s**. With `poll_interval=8`, each wait took **13.0 s** (see M-3).
  2. A worker whose database is unreachable: `run_forever()` raised **`OperationalError`** and returned to its caller, so the worker process exits (see H-1).
  3. Pool of 3 connections. The handler locks a row, then hangs, with a 1 s timeout. After attempt 1 there was 1 `idle in transaction` connection and 1 task thread. After attempt 2 there was one more connection **waiting on attempt 1's lock** (`active`, wait `Lock`) and 2 threads. On attempt 3, `_record_failure` could not get a connection and `run_one` raised **`TimeoutError: QueuePool limit ... reached`**. When the hang was released, the attempt-3 thread found the task still `running` and marked it `succeeded` (see M-1 and H-1).
  4. A single worker `host:1`, `max_attempts=1`. Attempt 1 timed out (its handler kept sleeping) and the task became `failed`. Then the same row change as `POST /tasks/{id}/retry` was applied, and the same worker ran the task again. Result: `succeeded`, `result = {"call": 1}`, and only `probe_write_call1` was committed. The **timed-out attempt's work was committed, and the live attempt's work was discarded** (see M-2).
  5. `enqueue()` inside `begin_nested()`, then the savepoint was released. `after_commit` fired at once and the id was published **before the outer commit**. After the outer transaction was rolled back, the row did not exist, but the wake-up had already been sent (see L-1).
  6. `REDIS_URL=localhost:6379` (no scheme): `db.commit()` raised **`ValueError`** from the `after_commit` hook, although the task row **was committed** (see L-2).
  7. A handler that runs `SELECT 1`, then sleeps 2 s: `finished_at - created_at` was **0.007 s**, while the run took 2.02 s of wall time (see L-3).
  8. `enqueue()` again with the key of a `failed` task and a different payload returned **`(7, False)`**. There was no status and no error, and the new payload was dropped (see L-4).

## Critical

None.

## High

### H-1: Any exception in the worker loop ends the worker process; nothing restarts it

- **File:** `backend/app/workers/worker.py:72-78` (`run_forever`), `:80-86` (`tick`), `:107-116` (`requeue_expired`, `run_one` claim), `:131-140`, `:162-181` (`_record_failure`)
- **What is wrong:** `run_forever` has no exception handling. Redis errors are caught (`_wait_for_wakeup`), but database errors are not. These include a Postgres restart, a network blip, a failover, a `QueuePool` timeout, or a deadlock in `requeue_expired`, `claim` or `_record_failure`, and they propagate out of `run_forever` and `main()`. A failure inside `_record_failure` that runs from `_execute` is stored on the future and raised again by `future.result()` in the main thread, so it also ends the loop. `pool_pre_ping=True` only replaces stale connections. It does not help while the database is unreachable.
- **How to trigger:** Probe 2: a worker that cannot reach the database leaves `run_forever()` with `OperationalError`. Probe 3: pool exhaustion makes `run_one` raise `TimeoutError`. In production, restart Postgres while the worker is idle. The next `requeue_expired()` raises and the process exits.
- **Impact:** After one transient database error, background processing stops for good. `compose.yaml` has no worker service, and nothing else in the repository supervises the worker. Queued tasks pile up silently. Their status stays `queued`, so nothing looks failed. In 2.3 this means uploaded documents never get OCR and never reach `needs_review`. A task claimed at the moment of the crash stays `running` until another worker's `requeue_expired` sees the expired lease, and if no other worker runs, it stays `running` indefinitely. This conflicts with decisions.md §10 ("Tasks must support retries, timeouts and failure handling"; "Failed tasks must be recoverable and observable").
- **Suggested fix:** Wrap each loop iteration (`tick()` and `_wait_for_wakeup()`) in `try/except Exception`. Log only the exception type and back off with `self._stop.wait(...)`, capped at something like 30-60 s, instead of exiting. In `run_one`, catch errors from `_record_failure` and from `future.result()` so that one task's bookkeeping failure leaves the task to lease expiry rather than killing the loop. Add a test in which the session factory raises `OperationalError` for the first N calls: the worker must survive and then process a due task. Separately, ask the owner to add a `worker` service with `restart: unless-stopped` to `compose.yaml` (owner file).

## Medium

### M-1: A timed-out handler keeps running and keeps its thread, its DB connection and its row locks

- **File:** `backend/app/workers/worker.py:131-140` (timeout path), `:142-157` (`_execute`), `:77`
- **What is wrong:** When the timeout fires, the worker abandons the thread (`shutdown(wait=False)`) and starts a new pool, but Python cannot stop the thread. If the handler has already used its session, its transaction stays open (`idle in transaction`), with any row locks it took. It holds a pooled connection until it returns, which may be never. The retry runs in the same worker while the old thread is still alive, and blocks on the old attempt's locks until it times out too. Each such cycle leaks one more thread and connection. Once the pool (default 5 + 10 overflow) is used up, `claim`/`_record_failure` raise and H-1 stops the worker. ThreadPoolExecutor threads are joined at interpreter exit, so a hung thread also keeps the process from exiting on SIGTERM (`run_forever` returns, but the interpreter waits). Docker would then kill it with SIGKILL after the grace period.
- **How to trigger:** Probe 3. The handler does `SELECT ... FOR UPDATE` on a row, then blocks, with `timeout_seconds=1`. Each attempt adds a stuck connection: attempt 2 waits on attempt 1's lock, and attempt 3 exhausts the pool. In 2.3, an OCR call that hangs (a stuck subprocess or a network OCR provider without its own timeout) after the handler has read or locked the document row gives this result.
- **Impact:** The engine's timeout limits only the bookkeeping. It limits neither the work nor the resources. One stuck document can block its own retries, which hit the same rows, and then take the whole worker down. Locks held on `documents` rows also block API requests on the same document, such as verify, reject or new version.
- **Suggested fix:** Make the time limit real and make it the handler's job, with the worker as a backstop:
  1. Give handlers a deadline (for example `info.deadline`). Require bounded external calls, such as `subprocess.run(..., timeout=)` or HTTP timeouts. For hard cases, run OCR in a child process that can be killed.
  2. Set `SET LOCAL statement_timeout` / `lock_timeout` (and `idle_in_transaction_session_timeout` on the worker engine), so that an abandoned transaction is ended by Postgres and its locks are released.
  3. Document and enforce "read what you need, close the transaction, do the slow work, then open a short transaction to write" for handlers.
  4. Count abandoned threads. When the count passes a small limit (for example 2), stop claiming and exit with a non-zero code so the supervisor restarts the process.
  
  Add a test showing that a handler that hangs after taking a row lock does not stop the next attempt or another task from running.

### M-2: An admin retry resets `attempts`, so a timed-out attempt can pass the ownership check and commit

- **File:** `backend/app/services/task_service.py:56-57` (`task.attempts = 0`), `backend/app/repositories/task_repository.py:73-85` (`lock_owned`: `status = 'running' AND locked_by = worker AND attempts = attempt`), `backend/app/workers/worker.py:61` (`worker_id = hostname:pid`)
- **What is wrong:** The ownership token is `(locked_by, attempts)`. A retry resets `attempts` to 0, so the next claim by the same worker again produces `(worker, 1)`, the same token an abandoned attempt-1 thread still holds. With one worker, the common deployment, it is the same worker every time. Without retries, `attempts` only increases and the check is sound.
- **How to trigger:** Probe 4. With `max_attempts=1`, attempt 1 times out and the handler keeps running, so the task becomes `failed`. An admin calls `POST /tasks/{id}/retry`, and the same worker claims the task again (`attempts=1`). The old thread finishes first, `lock_owned(id, "host:1", 1)` matches, and its writes and `{"call": 1}` are committed as the result. The live attempt then fails the check and its work is rolled back. The same happens with larger `max_attempts` once the retried task reaches the attempt number of the abandoned thread.
- **Impact:** This breaks the stated guarantee that "a timed-out handler that finishes late is rolled back" (worker docstring). In 2.3, OCR output from an attempt that the admin saw time out, and that may have run against an older document state, would be committed and the document moved to `needs_review`. Meanwhile the attempt the admin started is discarded.
- **Suggested fix:** Use a token that never repeats. Options: a separate claim counter (`claim_seq`) that is incremented on every claim and never reset, with retry leaving `attempts` as history and resetting a separate `attempts_in_run`, or adding the claim time/lease to the token. Alternatively, keep `attempts` monotonic and raise `max_attempts` on retry (`max_attempts = attempts + spec.max_attempts`). Add a test that reproduces probe 4.

### M-3: Redis `socket_timeout` (5 s) equals the blocking read time (5 s): every idle wait fails, logs "Redis unavailable", then sleeps another poll interval

- **File:** `backend/app/workers/notifier.py:22-27` (`socket_timeout=5`), `backend/app/workers/worker.py:88-103` (`block=int(self.poll_interval * 1000)`, `except RedisError: ... self._stop.wait(self.poll_interval)`), `:198` (`WORKER_POLL_SECONDS` default 5)
- **What is wrong:** `XREADGROUP ... BLOCK 5000` keeps the socket silent for 5 s, but the client gives up after 5 s and raises `redis.TimeoutError`, which is a `RedisError`. The handler treats that as an outage, logs a warning and then sleeps another `poll_interval` without listening to Redis. With any `WORKER_POLL_SECONDS` of 5 or more, this happens on every idle loop. The tests use `fakeredis`, which never times out, so they cannot catch it.
- **How to trigger:** Probe 1, against the local Redis: with `poll_interval=5`, every idle wait took 10.01 s and logged `Redis unavailable (TimeoutError); polling.`. With `poll_interval=8`, each took 13.0 s.
- **Impact:** A healthy Redis produces a warning every ~10 s for ever, which hides real outages. Wake-ups sent during the extra sleep are not seen until the next loop, so pick-up latency is up to ~10 s instead of near-instant. Stop latency is the same ~10 s. The behaviour matches the "polling" design, so nothing is lost, but the Redis fast path decisions.md §10 asks for mostly does not work.
- **Suggested fix:** Make the read timeout clearly longer than the block time. Either create the worker's client with `socket_timeout=None` (or `block_seconds + 5`), or keep `socket_timeout=5` and block for `min(poll, 2)` s. Treat `redis.TimeoutError` from the blocking read as "no message", without a warning or extra sleep. Add a test with a client whose `xreadgroup` raises `redis.TimeoutError`: no warning, no extra wait.

### M-4: Nothing stops a handler from committing, which skips the ownership check (readiness for 2.3)

- **File:** `backend/app/workers/registry.py:3-6` ("It must not commit"), `backend/app/workers/worker.py:144-157`
- **What is wrong:** The rule that "a timed-out handler's work is discarded" holds only if the handler never calls `db.commit()`. Nothing enforces it. The obvious way to write the 2.3 OCR handler is to reuse `document_service` functions, which commit internally (9 `db.commit()` calls in `document_service.py`, plus `audit_service` callers). Any such commit writes the handler's changes before `lock_owned` runs. A timed-out or taken-over attempt then still changes documents, and a duplicate concurrent attempt (see L-7) writes twice.
- **How to trigger:** Register a handler that calls a service function ending in `db.commit()` and sleeps past its timeout. Its changes stay committed while the task is retried.
- **Impact:** This weakens the engine's main safety property exactly where 2.3 needs it: OCR results and the `uploaded → processing → needs_review` move.
- **Suggested fix:** Give handlers a session whose commit is blocked. For example, run the handler inside `db.begin_nested()` with a `before_commit` listener that raises (or a session subclass whose `commit()` raises `RuntimeError`), and have the worker commit the outer transaction after `lock_owned`. Add a test in which a committing handler fails loudly. In 2.3, split document-service write functions into non-committing internals that both the routes and the handler call.

## Low

### L-1: `after_commit` also fires when a savepoint is released, so wake-ups are published before the real commit

- **File:** `backend/app/services/task_queue.py:55-57`
- **What is wrong:** In SQLAlchemy 2.1, the `Session` `after_commit` event fires when a nested transaction (savepoint) is committed. The listener then pops and publishes all pending ids. They are published while the outer transaction is still open, and nothing is left to publish when the outer transaction commits.
- **How to trigger:** Probe 5: `begin_nested()` → `enqueue()` → savepoint commit. The id is in the stream before the outer commit. If the outer transaction is then rolled back, the row does not exist, but the wake-up was sent. The test fixture (`join_transaction_mode="create_savepoint"`) goes through this path, so the tests rely on it.
- **Impact:** Small. A worker wakes, finds nothing committed, and the real task waits for the next poll (up to ~10 s, see M-3). It becomes a real problem if a later handler or service uses savepoints around `enqueue`.
- **Suggested fix:** Publish only at the end of the outermost transaction. For example, listen to `after_transaction_end(session, transaction)` and act only when `transaction.parent is None` (and `transaction.nested` is false), after a successful commit. Or check `not session.in_nested_transaction()` in `after_commit`. Add a test for a savepoint commit followed by an outer rollback.

### L-2: A non-Redis error in `publish` surfaces as a failed commit after the data was committed

- **File:** `backend/app/workers/notifier.py:43-60` (only `redis.RedisError` is caught), `backend/app/services/task_queue.py:55-57`
- **What is wrong:** `redis.Redis.from_url` raises `ValueError` for a malformed `REDIS_URL`, and other unexpected errors are possible too. Because `publish` runs inside the `after_commit` hook, the exception is raised from `db.commit()` after the transaction has committed.
- **How to trigger:** Probe 6: `REDIS_URL=localhost:6379`. `commit()` raises `ValueError`, but the row exists. In the API, the request returns 500 for work that was saved, and a client retry hits the idempotency key or duplicates non-idempotent work done in the same request.
- **Impact:** A configuration mistake turns into misleading 500s on every enqueueing request.
- **Suggested fix:** In `publish`, catch `Exception` and log only the type, as the docstring promises ("best effort"). Optionally validate `REDIS_URL` at startup.

### L-3: `finished_at` and `updated_at` record when the handler's transaction started, not when it finished

- **File:** `backend/app/workers/worker.py:155-156`
- **What is wrong:** In PostgreSQL, `now()` is the start time of the transaction. In the handler session that is the handler's first query, not the moment of success.
- **How to trigger:** Probe 7: a handler that queries, then works for 2 s, gets `finished_at` 0.007 s after `created_at`.
- **Impact:** Task durations shown to admins are wrong for any real handler (OCR will query first), which hurts "observable".
- **Suggested fix:** Use `clock_timestamp()` (or `statement_timestamp()`) for `finished_at`/`updated_at` here and in `_record_failure`.

### L-4: `enqueue` with an existing key returns the old task whatever its status or payload

- **File:** `backend/app/repositories/task_repository.py:36-43`, `backend/app/services/task_queue.py:20-47`
- **What is wrong:** On a key conflict the caller gets only `(id, False)`. A `failed` or `succeeded` task looks the same as one that is still queued, and a different payload under the same key is dropped silently.
- **How to trigger:** Probe 8: the key of a failed task, with a new payload, returns `(7, False)`.
- **Impact:** For `document_ocr` keyed per document version (`document_ocr:{id}:v{n}`), this is correct: a new version gets a new key. It is unsafe for 2.3 code that re-enqueues the same version (for example "re-run OCR" from the UI) or for the vehicle lookups, which will be repeated for the same vehicle over time. Those can never run again without an admin retry, and the caller cannot tell.
- **Suggested fix:** Return the existing status (or the row) as well, and raise when the stored payload differs from the requested one. Document the key format for each task type in `registry`/`task_queue` (for example include the document version, and a date or request id for lookups).

### L-5: A new Redis client (and TCP connection) on every publish and every worker wait

- **File:** `backend/app/workers/notifier.py:22-40`, `:49`; `backend/app/workers/worker.py:90`
- **What is wrong:** `get_redis()` calls `Redis.from_url` every time, which creates a new connection pool and connection. `publish` runs synchronously in the request thread after commit, so when Redis is unreachable (not refusing, but dropping packets), each enqueueing request waits up to `socket_connect_timeout=2` s. The worker reconnects on every loop and registers a new consumer name per process (`hostname:pid`), and consumers are never removed from the group.
- **Impact:** Small overhead per request, a possible 2 s stall per request during a Redis network problem, and a growing `XINFO CONSUMERS` list.
- **Suggested fix:** Create one client per process lazily and reuse it (redis-py clients are thread-safe). Optionally delete idle consumers (`XGROUP DELCONSUMER`) at worker start-up.

### L-6: Retry and timeout limits are copied from the registry of the process that enqueues

- **File:** `backend/app/services/task_queue.py:35-42`
- **What is wrong:** `enqueue` reads `max_attempts`/`timeout_seconds` from `registry.get_spec()` in the calling process (the API) and stores them in the row, and the worker then uses the row values. If the 2.3 handler module is imported only by the worker, the API stores the defaults (3 / 120 s), whatever the handler registered.
- **Impact:** OCR could get the wrong time limit without anyone noticing.
- **Suggested fix:** Keep per-type limits in one place that both processes import, for example a constant table next to `TASK_TYPES`, separate from handler registration. Or let the worker apply the spec when it claims the task. Add a test that enqueues with no handler registered.

### L-7: The lease ends exactly when the worker's own timeout does, so a slow but live handler is regularly run a second time at the same moment

- **File:** `backend/app/repositories/task_repository.py:52` (`lease = now() + timeout_seconds`), `backend/app/workers/worker.py:133` (`future.result(timeout=row.timeout_seconds)`, started after the claim commit)
- **What is wrong:** The lease is set at claim time, and the worker's timer starts slightly later, so the lease always expires first. In that gap, another worker's `requeue_expired` can requeue the task and start a second run while the first handler is still alive and about to finish.
- **Impact:** The database stays consistent, because only one attempt can pass `lock_owned` (apart from M-2 and M-4). But work outside the database (OCR CPU, external vehicle lookups, browser automation) is done twice, and the first attempt's nearly finished work is thrown away.
- **Suggested fix:** Set the lease to `timeout + grace` (for example +30 s), so the owning worker always times out and records the result first. `requeue_expired` then only rescues crashed workers.

### L-8: The timeout test depends on sleep timing and can pass without testing anything

- **File:** `backend/tests/test_background_tasks.py:208-231`
- **What is wrong:** The test sleeps 1.5 s and assumes the abandoned 2 s handler has reached its commit attempt by then. On a slow CI runner the thread may not have finished. The test then passes without exercising the ownership check, and the thread may write after the fixture's `TRUNCATE`. The suite also uses only `fakeredis`, which hid M-3.
- **Suggested fix:** Synchronise with events. Let the handler `set()` an event after `lock_owned` returns (for example by wrapping `repo.lock_owned`), and wait for it with a timeout before asserting. Add one test with a client that raises `redis.TimeoutError` from `xreadgroup`.

### L-9: Redundant index on `status`

- **File:** `backend/app/models/background_task.py:57` (`index=True`), `backend/migrations/versions/b9c0d1e2f3a4_background_tasks.py:87-89`
- **What is wrong:** `ix_background_tasks_status` duplicates the leading column of `ix_background_tasks_due (status, run_after)`.
- **Suggested fix:** Drop `index=True` in a later migration (do not edit this one if it has been applied anywhere).

## Accepted gaps

- **Contract drift:** the three `/api/v1/tasks` routes and four schemas are not in `docs/contracts/openapi.json`. The change is additive only (no route, field or status removed or changed). The owner must review, regenerate and commit the contract (`backend/AGENTS.md`, "API Contract").
- **At-least-once execution:** a handler can run more than once (after a crash, a lease expiry or a retry). Only one attempt's database changes are committed (apart from M-2 and M-4), but external side effects can repeat. Handlers must be idempotent. This is consistent with decisions.md §10 ("handled safely through idempotency") and should be written into the handler rules for 2.3.
- **Admin API shows `payload`, `result` and `idempotency_key` verbatim.** Today these hold only ids, and only admins can read them. 2.3 must keep OCR text and identity numbers (Aadhaar, PAN, chassis, engine) out of `result` and out of `TaskError` messages, or mask them in `TaskRead`. Otherwise the task list becomes an unmasked list view.
- **No production code enqueues tasks yet, and no handlers are registered.** Any task enqueued before 2.3 fails permanently with "No handler for this task type." Admins can retry it after 2.3 lands.
- **One task at a time per worker process;** throughput scales by adding processes. A long task delays `requeue_expired` in that process only.
- **Graceful shutdown waits for the running task** (up to its timeout) before exiting. With Docker's default 10 s grace period, a long task is killed and recovered through lease expiry. This is acceptable once L-7's lease grace is in place.

## Checked and correct

- **Claim atomicity:** `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`. The scalar subquery is evaluated once, and a row changed concurrently is re-checked under READ COMMITTED before it is locked. The 4-worker test shows exactly-once claiming (the branch notes that it fails without `SKIP LOCKED`).
- **Success commit vs lease expiry:** `lock_owned` takes `FOR UPDATE` on the task row before the commit. A concurrent `requeue_expired` waits, re-evaluates `status = 'running'` and skips the row, and a takeover that has already happened makes `lock_owned` return `None`. Two attempts cannot both commit, except through the token reuse in M-2 and committing handlers in M-4.
- **Clocks:** `run_after`, lease, backoff (`now() + backoff` computed from the DB timestamp) and requeue all use the database clock. The Python timeout only measures an interval. No wall-clock values are mixed in, apart from L-3's use of transaction-start time.
- **Idempotent enqueue under concurrency:** `ON CONFLICT DO NOTHING` plus re-select. A concurrent duplicate waits for the first insert and then reads the committed row. If the first transaction rolls back, the second inserts.
- **Rollback drops pending notifications:** `after_soft_rollback` with `parent is None` (test plus code). A wake-up for a rolled-back savepoint is harmless, because messages are only hints.
- **Redis is optional:** a down Redis on publish is logged and polling finds the task (test). The consumer group is re-created when missing (BUSYGROUP handled), and messages are acknowledged immediately, so the pending list does not grow in normal operation. The stream is capped at ~10,000 entries.
- **Failure recording:** only `TaskError` messages are stored, and other exceptions keep only their type (test with an Aadhaar-like message). `last_error` is capped at 500 characters. `PermanentTaskError` and a missing handler fail at once, and the backoff is 30 s·2^(n−1) capped at 15 min (tests).
- **Admin API:** all three routes use the `AdminUser` dependency, and employees get 403 (test). Path id is bounded to `MAX_DB_INT`, and filters are enum-validated (422). Retry locks the row, refuses non-failed tasks with 422, records an audit entry with `task_type` and `last_error`, and wakes workers after commit. No stack traces or SQL are returned.
- **Migration:** matches the model (columns, CHECK constraints, unique key, FK to `users.id`, indexes), uses integer ids (rule 11), and the downgrade drops exactly what the upgrade creates. Static review only; see Checks run.
- **Payload rule:** the model and docstrings require ids only, and the tests use ids only.
- **Dependencies:** `redis` (required for decisions.md §10 Redis Streams) and dev-only `fakeredis` (so CI needs no Redis server). Both are justified, and `uv lock --check` passes.
- **Ownership:** every changed file is under `backend/`. No contract, `AGENTS.md`, `compose.yaml`, script or `.env*` file is touched.

## Verdict

**CHANGES REQUESTED.** H-1 must be fixed before merge. M-1, M-2 and M-4 should be fixed before the 2.3 OCR handler is registered, because they decide whether timed-out or duplicate OCR runs can change documents. M-3 should be fixed before the worker is deployed.
