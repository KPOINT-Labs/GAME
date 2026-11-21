==========
Operations
==========

.. admonition:: Who is this page for?
   :class: note

   Operators deploying and running GAME. Pairs with :doc:`configuration`
   (every variable), :doc:`security` (hardening), and :doc:`observability`
   (signals). The repository also keeps ``DEPLOYMENT.md`` and
   ``KUBERNETES_SETUP.md`` as quick references.

Deployment topology
===================

GAME is **stateless**: it holds no per-request server state beyond the
database (and optional Redis). That means you scale it by running more
identical replicas behind a load balancer; PostgreSQL and Redis are the only
shared state.

::

   ┌────────────┐     ┌──────────────┐     ┌──────────────┐
   │  Ingress / │────►│  GAME API    │────►│  PostgreSQL  │
   │  Load bal. │     │  (N replicas)│     └──────────────┘
   └────────────┘     │  gunicorn +  │────►┌──────────────┐
        │             │  uvicorn     │     │   Redis      │ (optional:
        │             └──────┬───────┘     └──────────────┘  rate-limit +
        ▼                    │                                apikey cache)
   ┌────────────┐            ▼
   │  Keycloak  │◄───── JWT validation (JWKS)
   └────────────┘

The process model in containers is **gunicorn** managing **uvicorn** workers
(``app/gunicorn_conf.py``, ``app/start-prod.sh``).

Local & dev with Docker Compose
===============================

The repository ships several Compose files and a ``Makefile`` that wraps them
(auto-detecting ``docker compose`` v2 vs ``docker-compose`` v1):

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Make target
     - What it does
   * - ``make setup``
     - First-run: installs Docker if missing, creates ``.env`` from the
       sample (interactive).
   * - ``make dev``
     - Dev stack (``docker-compose-dev.yml``): API + Postgres + Keycloak.
   * - ``make dev-nodb``
     - Dev stack without a bundled DB (bring your own).
   * - ``make integrated``
     - Integrated stack (``docker-compose.devintegrated.yml``).
   * - ``make up`` / ``make up-fg``
     - Start in background / foreground.
   * - ``make logs`` / ``make logs-api``
     - Tail logs (all services / just the API).
   * - ``make ps``
     - Show running containers.
   * - ``make shell-api`` / ``make shell-db``
     - Shell into the API container / ``psql`` into Postgres.
   * - ``make down`` / ``make clean``
     - Stop+remove containers / …**and volumes** (destructive).
   * - ``make audit``
     - Run ``pip-audit`` locally (parity with CI).

Override the compose file or command per invocation, e.g.
``make up FILE=docker-compose.yml DC="docker-compose"``.

Raw Compose, without Make:

.. code-block:: bash

   # Dev
   docker-compose -f docker-compose-dev.yml up --build
   docker-compose -f docker-compose-dev.yml down --remove-orphans

   # Production-style single host
   docker-compose up --build -d
   docker-compose logs -f
   docker-compose up --scale app=3      # horizontal scale

Production deployment
=====================

#. **Configure** the environment for ``ENV=prod`` (or ``stage``). The
   fail-fast guards will block boot on missing secrets - that is intended; see
   :doc:`configuration` and :doc:`security`.
#. **Run migrations** before serving traffic (see below).
#. **Deploy** the image with your orchestrator (Compose, Kubernetes, or a
   managed container platform), behind an ingress that terminates TLS.
#. **Set ``TRUSTED_PROXY_IPS``** to the ingress IP/CIDR so per-IP rate limits
   work and forwarding headers are trusted.
#. **Protect ``/metrics``** at the ingress, or set ``METRICS_ENABLED=false``.
#. **Externalize shared state**: point ``REDIS_URL`` and switch
   ``ABUSE_PREVENTION_BACKEND`` / ``APIKEY_CACHE_BACKEND`` to ``redis`` so
   limits and key revocations are consistent across replicas.

Kubernetes
==========

Manifests live under ``kubernetes/`` and a helper script
``deploy-kubernetes.sh`` is provided. See ``KUBERNETES_SETUP.md`` for the full
walkthrough. Operational notes:

* **Probes** are defined in the manifests: liveness on
  ``GET /api/v1/kpi/health_check`` (never touches the database). Readiness
  uses the same endpoint until the API image tag is bumped to a build that
  includes ``GET /api/v1/kpi/ready`` (requires the database); then switch the
  readiness probe to it. Postgres has ``pg_isready`` probes.
* The Postgres image is **pinned** to the major version of the existing data
  (``postgres:18-alpine``; Docker Compose uses 16 for fresh volumes). Upgrade
  major versions deliberately (``pg_upgrade`` or dump/restore); an unpinned
  tag can pull a new major on restart that cannot open the existing data
  directory.
* Provide configuration via ``ConfigMap`` (non-secret) and ``Secret``
  (``SECRET_KEY``, DB password, ``KEYCLOAK_CLIENT_SECRET``).
* Roll back with ``kubectl rollout undo deployment/<name>`` - Kubernetes keeps
  the deployment history.

Database migrations (Alembic)
=============================

Schema changes are Alembic migrations (``migrations/``, ``alembic.ini``). The
golden rule: **migrate before the new code serves traffic**, in CI/CD.

.. code-block:: bash

   # Local / Poetry
   poetry run alembic upgrade head

   # Inside a running container
   docker-compose exec app alembic upgrade head

   # Generate a new migration after a model change (review before committing!)
   poetry run alembic revision --autogenerate -m "describe change"

Health, readiness & graceful shutdown
=====================================

* **Liveness** - ``GET /api/v1/kpi/health_check``: the process is up. It does
  not touch the database, so a database outage never restarts API pods.
* **Readiness** - ``GET /api/v1/kpi/ready``: ``200`` when the database answers
  ``SELECT 1`` within ``DB_READY_TIMEOUT_SECONDS`` (default 2 s), else ``503``.
  Route traffic only to ready instances.
* **Graceful shutdown** - the FastAPI lifespan hook flushes the DSL
  execution-log queue on shutdown (``observer.aclose()``) so buffered audit
  rows aren't lost. Give the container a few seconds of termination grace so
  the flush completes.

Database outages
================

What happens when PostgreSQL is stopped, crashes or becomes unreachable:

* **Requests fail fast with** ``503`` and a ``Retry-After`` header (connection
  refused, connection lost, pool exhausted, or a connect/query timeout).
  ``DB_CONNECT_TIMEOUT_SECONDS`` (default 5) and ``DB_COMMAND_TIMEOUT_SECONDS``
  (default 30) bound how long a request waits on an unreachable or frozen
  database, so requests do not pile up; a query already running when the
  database freezes fails after the command timeout. After a failure, requests
  answer ``503`` immediately for ``DB_UNAVAILABLE_COOLDOWN_SECONDS``
  (default 2). Other errors still return ``500``.
* **Readiness** turns ``503`` within a few seconds, so load balancers and
  Kubernetes stop sending traffic; liveness stays ``200``.
* **Recovery is automatic**: an outage error discards the broken connection and
  the whole pool, so the next request opens fresh connections. In tests the
  API served awards again 2-4 seconds after Postgres came back (stop, kill or
  freeze), without a restart. If Postgres restarts while the API is idle, the
  first request may get one retryable ``503``.
* **Restarts**: in Docker Compose, ``api`` and ``postgres`` restart unless
  stopped, and ``api`` starts only once Postgres passes its ``pg_isready``
  healthcheck. In Kubernetes both use ``restartPolicy: Always``.
* **No acknowledged write is lost**: each award writes the points row, the
  wallet balance and the wallet transaction in one database transaction, and
  Postgres flushes its write-ahead log before acknowledging a commit (the
  defaults ``fsync`` and ``synchronous_commit`` are on; keep them on). An
  action and its cohort change are also one transaction.
* **Clients must retry** ``503`` responses (and timeouts) with the **same**
  ``Idempotency-Key``: if a commit succeeded but the response was lost, the
  retry is recognised instead of awarding twice. See :doc:`integrating`.
* **By design**, audit-log rows and DSL execution traces may be dropped while
  the database is unavailable (they never block scoring), and rate limiting
  is unavailable with the database when ``ABUSE_PREVENTION_BACKEND`` is
  ``database``.
* **Backups are not covered here.** A single database with no backups loses
  everything if its disk is lost; set up scheduled backups (``pg_dump`` or
  WAL archiving) and test restores.

Scaling guidance
================

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Lever
     - Guidance
   * - **Replicas / workers**
     - Scale horizontally; the app is stateless. Size gunicorn workers to CPU.
   * - **DB pool**
     - Total connections ≈ replicas × workers × (``DB_POOL_SIZE`` +
       ``DB_MAX_OVERFLOW``). Keep it under PostgreSQL's ``max_connections``;
       consider PgBouncer at high replica counts.
   * - **Rate-limit & cache backend**
     - Use ``redis`` so limits and key revocations are global, not per-worker.
   * - **DSL trace sink**
     - Watch ``dsl_execution_log_dropped_total``; if non-zero, the trace DB is
       behind - raise ``DSL_EXECUTION_LOG_QUEUE_MAXSIZE`` or lower the sample
       rate. Scoring is unaffected (:doc:`observability`).

Load & performance testing
==========================

A k6 load suite ships in ``tests/load`` with a runner:

.. code-block:: bash

   ./scripts/run_load_test.sh --mode 100        # 100 VUs
   ./scripts/run_load_test.sh --mode 1000       # stress
   ./scripts/run_load_test.sh --vus 300 \
     --mix-a 60 --mix-b 30 --mix-c 10 \
     --warmup 20s --hold 2m --ramp-down 20s

See :doc:`contributing` for the full testing story and the README for every
flag.

Runbooks
========

* **DSL strategy incidents** (a published strategy erroring, hitting limits,
  or needing rollback) → ``docs/dsl/runbook.md`` and :doc:`strategies`.
* **"Network Error" in the dashboard** → almost always a backend ``500``;
  check API logs (:doc:`observability`).
* **Boot failure in prod/stage** → a fail-fast guard tripped; the error names
  the variable (:doc:`configuration`).
