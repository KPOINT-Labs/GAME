# GAME database infrastructure on AWS

Terraform for the AWS resources the highly available Postgres
(CloudNativePG, `kubernetes/cnpg/`) needs on an **existing EKS cluster**.
The VPC, the EKS cluster and its node groups are managed elsewhere.

| Resource | Purpose |
| --- | --- |
| KMS key `alias/<prefix>-<env>-db` | Encrypts the database EBS volumes, the Secrets Manager secrets and the backup bucket. Yearly rotation on. |
| Secrets Manager `<prefix>/<env>/db/app` | Application database user: `{"username", "password"}`. The password is generated as an ephemeral value and written with a write-only argument, so it is never in the plan or state. |
| IAM role `<prefix>-<env>-db-secrets-reader` | Assumed by the External Secrets service account (IRSA); may read only `<prefix>/<env>/db/*`. |
| IAM policy on the EBS CSI driver role | Lets the driver create and attach volumes encrypted with the key (`ebs_csi_driver_role_name`). |
| S3 bucket `<prefix>-<env>-db-backups-<account>` and role `<prefix>-<env>-db-backup` | For CloudNativePG backups (WAL archive and base backups): versioned, KMS-encrypted, public access blocked, TLS only. Used once the backup configuration is added. |

## Prerequisites

- An EKS cluster with its IAM OIDC provider registered (IRSA).
- The EBS CSI driver add-on, with its own IAM role.
- Node groups in at least three availability zones (one database instance per zone).
- An S3 bucket for Terraform state.
- Optional but recommended: EKS envelope encryption of Kubernetes Secrets with a KMS key (cluster setting).

## Usage

```bash
cd infra/aws/db
cp backend.hcl.example backend.hcl            # state bucket, key, region
cp terraform.tfvars.example terraform.tfvars  # fill in the CHANGE-ME values
terraform init -backend-config=backend.hcl
terraform plan
terraform apply
terraform output
```

Copy the outputs into `.env`, then follow "Deploying on AWS" in
`docs/source/operations.rst`:

| Output | `.env` variable |
| --- | --- |
| `kms_key_arn` | `GAMIFICATIONENGINE_AWS_DB_KMS_KEY_ARN` |
| `db_app_secret_name` | `GAMIFICATIONENGINE_AWS_DB_APP_SECRET` |
| `secrets_reader_role_arn` | `GAMIFICATIONENGINE_AWS_SECRETS_ROLE_ARN` |
| `backup_role_arn` | `GAMIFICATIONENGINE_AWS_DB_BACKUP_ROLE_ARN` |

Clusters restored from the backups (`scripts/cnpg-restore.sh`) read the
bucket with the backup role, so their names must be in
`cnpg_restore_cluster_names` (default: the restore check's
`game-db-restore-test`). Add a production restore target such as `game-db-2`
and apply before restoring into it.

`db_app_username`, `k8s_namespace`, `cnpg_cluster_name` and
`secrets_reader_service_account` must match `GAMIFICATIONENGINE_DB_USER`,
`GAMIFICATIONENGINE_NAMESPACE`, `GAMIFICATIONENGINE_CNPG_CLUSTER_NAME` and
`GAMIFICATIONENGINE_AWS_SECRETS_SERVICE_ACCOUNT`.

## Rotating the application password

Increase `db_app_password_version` and apply. Alternatively, put a new
`password` in the secret yourself; Terraform does not track the value. Then
see "Rotating the password" in `docs/source/operations.rst`: the
password reaches the database through External Secrets and CloudNativePG,
and the API picks it up when it restarts. Secrets Manager's built-in
rotation (Lambda) is for RDS and does not apply to this database.

## Notes

- `terraform destroy` schedules the KMS key for deletion and deletes the
  secret after its recovery window. Once the key is deleted, volumes and
  backups encrypted with it are unreadable.
- The StorageClass uses `reclaimPolicy: Retain`: deleting a PVC leaves its
  EBS volume behind. Delete released volumes by hand.
