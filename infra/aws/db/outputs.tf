# Values to copy into .env for deploy-kubernetes.sh --aws (see README.md).

output "kms_key_arn" {
  description = "GAMIFICATIONENGINE_AWS_DB_KMS_KEY_ARN: encrypts the database volumes (StorageClass), secrets and backups."
  value       = aws_kms_key.db.arn
}

output "db_app_secret_name" {
  description = "GAMIFICATIONENGINE_AWS_DB_APP_SECRET: Secrets Manager secret with the application user's username and password."
  value       = aws_secretsmanager_secret.db_app.name
}

output "secrets_reader_role_arn" {
  description = "GAMIFICATIONENGINE_AWS_SECRETS_ROLE_ARN: role for the External Secrets service account."
  value       = aws_iam_role.secrets_reader.arn
}

output "backup_bucket_name" {
  description = "S3 bucket for CloudNativePG backups (used by the backup configuration)."
  value       = try(aws_s3_bucket.backups[0].bucket, null)
}

output "backup_role_arn" {
  description = "GAMIFICATIONENGINE_AWS_DB_BACKUP_ROLE_ARN: role for the CloudNativePG pods' service account (backups, and reading them when restoring)."
  value       = try(aws_iam_role.cnpg_backup[0].arn, null)
}
