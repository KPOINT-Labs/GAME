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
* **Postgres** runs as a highly available CloudNativePG cluster
  (``kubernetes/cnpg/``, ``deploy-kubernetes.sh --cnpg``); see
  `High availability (CloudNativePG)`_. The single-instance Deployment
  (``--postgres``) has no failover; use it only outside production.
  Development (Docker Compose) and tests use a single Postgres instance.
* Postgres images are **pinned** to the major version of the existing data
  (18; Docker Compose uses 16 for fresh volumes). Upgrade major versions
  deliberately (``pg_upgrade`` or dump/restore); an unpinned tag can pull a
  new major on restart that cannot open the existing data directory.
* Provide configuration via ``ConfigMap`` (non-secret) and ``Secret``
  (``SECRET_KEY``, DB password, ``KEYCLOAK_CLIENT_SECRET``).
* Roll back with ``kubectl rollout undo deployment/<name>`` - Kubernetes keeps
  the deployment history.

High availability (CloudNativePG)
=================================

Production Postgres is a CloudNativePG ``Cluster``
(``kubernetes/cnpg/postgres-cluster.yaml``; decision record
:doc:`decisions/0014-postgres-ha-cloudnativepg`):

* **Three instances** (``GAMIFICATIONENGINE_CNPG_INSTANCES``): one primary
  and two streaming replicas, spread over nodes (preferred pod
  anti-affinity, so it still schedules on fewer nodes, without the node-loss
  protection). With nodes in several zones, set ``topologyKey`` to
  ``topology.kubernetes.io/zone``.
* **Automatic failover**: when the primary's pod or node fails, the operator
  promotes the most advanced replica and repoints the ``<cluster>-rw``
  Service. The API connects to ``<cluster>-rw``; during a failover it
  answers ``503`` with ``Retry-After`` (see `Database outages`_) and
  reconnects on its own once the new primary is up.
* **Synchronous replication** (``method: any``, ``number: 1``) with
  ``failoverQuorum``: a commit is acknowledged only once a replica has it
  too, and the operator promotes a replica only when it is sure to hold every
  acknowledged commit, so a failover loses no acknowledged write. The cost:
  if the primary **and** a replica fail together, the last replica is not
  promoted automatically (it may lack commits only the lost replica had) and
  the database stays down until an instance returns. As a last resort,
  promote it by hand (``kubectl cnpg promote``), accepting that recent
  commits may be lost. With ``dataDurability: preferred``, writes continue
  asynchronously if no replica is healthy.
* **Rolling updates** (minor image versions, parameters, resources) restart
  replicas first and then switch over, so the primary changes once.
* Each instance has a data volume (``GAMIFICATIONENGINE_CNPG_STORAGE_SIZE``)
  and a WAL volume (``GAMIFICATIONENGINE_CNPG_WAL_STORAGE_SIZE``) from the
  default StorageClass. Volumes can grow (edit the size and re-apply) but
  never shrink, and the WAL volume cannot be added or removed later.

Day-to-day commands (the optional ``kubectl cnpg`` plugin gives a richer
view):

.. code-block:: bash

   kubectl -n $NS get cluster game-db             # phase, instances, primary
   kubectl -n $NS get pods -l cnpg.io/cluster=game-db -L role
   kubectl cnpg -n $NS status game-db             # replication lag, sync state
   kubectl cnpg -n $NS promote game-db game-db-2  # planned switchover

Before draining a node that runs the primary, switch over to a replica on
another node, so the drain causes one brief switchover instead of waiting on
the primary's disruption budget.

Installing the operator
-----------------------

Once per Kubernetes cluster, by someone with cluster-admin rights (installs
CRDs and the controller into ``cnpg-system``):

.. code-block:: bash

   kubectl apply --server-side -f \
     https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.30/releases/cnpg-1.30.2.yaml
   kubectl rollout status deployment -n cnpg-system cnpg-controller-manager

Creating the cluster
--------------------

**Before you start**

* ``GAMIFICATIONENGINE_DB_USER`` must not be ``postgres`` or
  ``streaming_replica`` (reserved by CloudNativePG).
* At least three schedulable nodes, for the anti-affinity to protect against
  node loss.
* Choose ``GAMIFICATIONENGINE_CNPG_STORAGE_SIZE`` and
  ``GAMIFICATIONENGINE_CNPG_WAL_STORAGE_SIZE`` (per instance; the WAL volume
  is fixed at creation), ``GAMIFICATIONENGINE_CNPG_STORAGE_CLASS`` and
  ``GAMIFICATIONENGINE_CNPG_TOPOLOGY_KEY`` (``topology.kubernetes.io/zone``
  to spread over availability zones, ``kubernetes.io/hostname`` over nodes).
* On AWS, first create the AWS resources and the credentials Secret: see
  `Deploying on AWS`_.

**Steps**

1. Install the operator (above).
2. In ``.env`` set ``GAMIFICATIONENGINE_DB_HOST=game-db-rw``.
3. Create the cluster and wait for it:

   .. code-block:: bash

      ./deploy-kubernetes.sh --cnpg
      kubectl -n $NS wait --for=condition=Ready cluster/game-db --timeout=15m

4. Deploy the API: ``./deploy-kubernetes.sh --api``. It runs
   ``alembic upgrade head`` on start-up, creating the schema.
5. Before taking traffic, test a failover: delete the primary pod while
   sending requests, and check that a replica takes over and the API recovers
   after a few ``503`` responses.

   .. code-block:: bash

      kubectl -n $NS delete pod "$(kubectl -n $NS get cluster game-db -o jsonpath='{.status.currentPrimary}')"
      kubectl -n $NS get cluster game-db -w

Database credentials
--------------------

The application user's name and password live in one Kubernetes Secret
(``GAMIFICATIONENGINE_DB_CREDENTIALS_SECRET``, type ``basic-auth``). The API
reads ``DB_USER``/``DB_PASSWORD`` from it, the cluster creates its owner role
from it, and the cluster's managed role keeps the database password in step
with it (the Secret carries ``cnpg.io/reload``). The ConfigMap holds no
credentials. ``GAMIFICATIONENGINE_DB_SECRETS_SOURCE`` selects where the Secret
comes from:

* ``env``: rendered from ``GAMIFICATIONENGINE_DB_USER`` /
  ``GAMIFICATIONENGINE_DB_PASSWORD`` in ``.env`` (applied by ``--cnpg``,
  ``--postgres`` and ``--api``).
* ``aws``: synced from AWS Secrets Manager by External Secrets (``--aws``).

**Rotating the password**: change it at the source (``.env`` and
``--cnpg``, or the Secrets Manager secret), let the Secret update (External
Secrets syncs within an hour; force it with
``kubectl -n $NS annotate externalsecret game-db-app-credentials force-sync=$(date +%s) --overwrite``),
then restart the API (``kubectl -n $NS rollout restart deployment/gamificationengine``).
Between the database change and the restart, new API connections fail.

Deploying on AWS
----------------

Terraform in ``infra/aws/db`` (see its ``README.md``) creates, for an
existing EKS cluster:

* a customer-managed **KMS key** that encrypts the database volumes, the
  secrets and the backups;
* the **Secrets Manager** secret ``<prefix>/<env>/db/app``
  (``{"username", "password"}``), with a generated password that never enters
  Terraform state;
* an **IAM role** for the service account External Secrets uses, allowed to
  read only that environment's database secrets;
* the **S3 bucket** and IAM role for CloudNativePG backups (used by the
  backup configuration).

Then, on the cluster:

1. Install the External Secrets Operator (Helm chart ``external-secrets``)
   and the CloudNativePG operator.
2. Copy the Terraform outputs into ``.env`` (``GAMIFICATIONENGINE_AWS_*``),
   set ``GAMIFICATIONENGINE_DB_SECRETS_SOURCE=aws``,
   ``GAMIFICATIONENGINE_CNPG_STORAGE_CLASS=gp3-encrypted`` and
   ``GAMIFICATIONENGINE_DB_USER`` to the Terraform ``db_app_username``.
3. ``./deploy-kubernetes.sh --aws``: the ``gp3-encrypted`` StorageClass (EBS,
   KMS, ``WaitForFirstConsumer``, ``Retain``), the service account, the
   ``SecretStore`` and the ``ExternalSecret``. Check the sync:
   ``kubectl -n $NS get externalsecret game-db-app-credentials`` shows
   ``SecretSynced``.
4. Continue with `Creating the cluster`_ (``--cnpg``, then ``--api``).

Importing an existing database (optional)
-----------------------------------------

Not needed for a fresh install. To copy the application database from a
single-instance Postgres (the ``--postgres`` Deployment) into the cluster,
``scripts/cnpg-import-legacy.sh`` runs a one-off pod from the cluster's own
image that streams ``pg_dump`` into ``<cluster>-rw`` (``pg_restore`` in one
transaction, objects owned by the API's user), runs ``ANALYZE`` and compares
the row count of every table. Passwords travel on the pod's stdin, not in its
spec.

.. code-block:: bash

   kubectl -n $NS scale deployment/gamificationengine --replicas=0  # stop writes
   scripts/cnpg-import-legacy.sh --replace
   kubectl -n $NS scale deployment/gamificationengine --replicas=1

* The source defaults to the ``GAMIFICATIONENGINE_SERVICE_NAME_POSTGRES``
  Service with the same database name and credentials as the cluster;
  override with ``LEGACY_DB_HOST``, ``LEGACY_DB_PORT``, ``LEGACY_DB_NAME``,
  ``LEGACY_DB_USER`` and ``LEGACY_DB_PASSWORD``. The source's major version
  must not be newer than the cluster's.
* The target must have no tables. ``--replace`` drops its ``public`` schema
  first, for example the empty schema the API's migrations created. If the
  restore fails nothing is committed; if row counts differ afterwards
  (writes still reached the source), the script empties the target's
  ``public`` schema again and fails, so stop writes and re-run.

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
  healthcheck. In Kubernetes both use ``restartPolicy: Always``, and a
  failed Postgres primary is replaced by a replica (see
  `High availability (CloudNativePG)`_).
* **Failover**: connections to an old primary fail as it shuts down or turns
  read-only; those errors are outages too (``503``, pool reset), so the next
  request connects to the new primary.
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

Redis outages
=============

Redis is optional (``APIKEY_CACHE_BACKEND`` / ``ABUSE_PREVENTION_BACKEND``),
and losing it at runtime does not fail requests
(``app/services/redis_resilience.py``):

* **API key cache**: lookups use a per-process in-memory cache instead, i.e.
  the ``memory`` backend's behaviour. Authentication keeps working; a key
  revoked during the outage is evicted locally at once and reaches other
  workers within ``API_KEY_HEADER_CACHE_TTL_SECONDS`` (default 5 s).
* **Rate limiting**: counters are kept in the database
  (``abuse_limit_counter``) instead, so limits stay enforced. Counts start
  afresh there, so a window that spans the switch can admit up to twice its
  limit once.
* **Fail fast**: calls time out after ``REDIS_SOCKET_TIMEOUT_MS`` (500 ms)
  and are not retried. After a failure Redis is skipped for
  ``REDIS_FAILURE_COOLDOWN_SECONDS`` (5 s), so only the first request (and
  then one request per cool-down, which probes Redis) pays the timeout; the
  rest use the fallback immediately.
* **Recovery is automatic** on the first successful probe; no restart.
* Each outage logs one warning per worker and feature ("Redis unavailable
  for ...") and one "available again" line. Metrics:
  ``redis_unavailable_total`` and ``redis_fallback_total`` (by
  ``component``); alert ``RedisUnavailableFallbackActive``.
* In tests against a real Redis: frozen (``docker pause``), the first
  request took about 1 s (two 500 ms timeouts) and following requests about
  0.2 ms; stopped (connection refused), requests fell back instantly; both
  recovered on the next probe.

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
