=============================================================
ADR 0015: Database credentials in AWS Secrets Manager via ESO
=============================================================

:Status: Accepted

Context
=======

Production runs on AWS (EKS). The application database password was rendered
from ``.env`` into the ``env-prod`` ConfigMap, in plain text, twice
(``DB_PASSWORD`` and inside ``DATABASE_URL``), and the API's start-up script
printed the URL, password included, to its log.

Decision
========

* The application user's credentials live in AWS Secrets Manager
  (``<prefix>/<env>/db/app``), encrypted with a customer-managed KMS key and
  created by Terraform (``infra/aws/db``) with a generated password that never
  enters Terraform state.
* The External Secrets Operator copies them into one Kubernetes Secret
  (``basic-auth``), authenticating with an IAM role for its service account
  that can read only that environment's database secrets.
* That Secret is the single source for the API (``DB_USER``/``DB_PASSWORD``),
  the CloudNativePG owner role and its managed password. The ConfigMap holds
  no credentials; ``DATABASE_URL`` is gone (the app builds its URL from the
  parts, percent-encoding the credentials).
* Outside AWS the same Secret is rendered from ``.env``
  (``GAMIFICATIONENGINE_DB_SECRETS_SOURCE=env``).
* Database volumes use an EBS StorageClass encrypted with the same KMS key.

Consequences
============

* Rotation: change the secret; External Secrets and CloudNativePG apply it to
  the database, and the API uses it after a restart. Secrets Manager's
  built-in rotation is RDS-only and is not used.
* Two operators to install on the cluster (External Secrets, CloudNativePG).
* Deleting the KMS key makes volumes, secrets and backups encrypted with it
  unreadable.
