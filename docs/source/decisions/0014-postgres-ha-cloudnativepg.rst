======================================================
ADR 0014: Highly available Postgres with CloudNativePG
======================================================

:Status: Accepted

Context
=======

The Kubernetes manifests ran Postgres as a single ``Deployment`` (one pod, one
``ReadWriteOnce`` volume). Losing the pod or its node takes the database, and
so every scoring call, down until Kubernetes reschedules the pod and
reattaches the volume: usually minutes, and indefinitely if the volume stays
attached to a dead node. A new production Kubernetes cluster is being set up
from scratch, with no data to carry over. The API already fails fast with ``503`` and recovers
on its own (see :doc:`/operations`, "Database outages"), so database downtime
is the remaining availability gap.

Options considered: hardening the single instance (still minutes of downtime
per failure), a managed cloud database (depends on the hosting provider), and
running Postgres with an operator inside the cluster.

Decision
========

Run Postgres as a CloudNativePG ``Cluster`` (``kubernetes/cnpg/``): one primary
and two streaming replicas spread over nodes, quorum synchronous replication
(``any 1``, ``dataDurability: preferred``) and automatic failover. The API
connects through the ``<cluster>-rw`` Service, which always points at the
current primary. The cluster starts with an empty database owned by the
API's user; the API's Alembic migrations create the schema.

Consequences
============

* A pod or node failure costs a failover (tens of seconds of ``503``), not
  minutes, and no acknowledged write is lost while a replica is healthy.
* A cluster-wide operator (CRDs) must be installed and upgraded.
* Three instances triple the storage and memory of the database.
* The same operator provides WAL archiving and point-in-time recovery to
  object storage, which is the planned backup mechanism.
* An old primary's connections fail with shutdown or read-only errors during
  a failover; the API treats them as outages (``503``, pool reset), so it
  reconnects to the new primary.
