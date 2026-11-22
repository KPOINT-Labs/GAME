variable "region" {
  description = "AWS region of the EKS cluster."
  type        = string
}

variable "environment" {
  description = "Environment name, used in resource and secret names (e.g. prod, staging)."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names and the Secrets Manager path."
  type        = string
  default     = "game"
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}

# --- Existing EKS cluster (created outside this module) ---------------------

variable "eks_cluster_name" {
  description = "Name of the existing EKS cluster; its OIDC provider must already be registered in IAM (IRSA)."
  type        = string
}

variable "ebs_csi_driver_role_name" {
  description = "IAM role name of the EBS CSI driver (EKS add-on). It is granted use of the KMS key so it can create encrypted volumes. Empty to skip and grant it yourself."
  type        = string
  default     = ""
}

# --- Kubernetes side (must match .env / kubernetes/aws manifests) ----------

variable "k8s_namespace" {
  description = "Kubernetes namespace of GAME (GAMIFICATIONENGINE_NAMESPACE)."
  type        = string
  default     = "gamificationengine"
}

variable "secrets_reader_service_account" {
  description = "Service account the External Secrets SecretStore uses to read Secrets Manager (GAMIFICATIONENGINE_AWS_SECRETS_SERVICE_ACCOUNT)."
  type        = string
  default     = "game-secrets-reader"
}

variable "cnpg_cluster_name" {
  description = "CloudNativePG Cluster name (GAMIFICATIONENGINE_CNPG_CLUSTER_NAME). Its pods run as a service account of the same name, which gets the backup role."
  type        = string
  default     = "game-db"
}

variable "cnpg_restore_cluster_names" {
  description = "CloudNativePG clusters that restore from the backups (scripts/cnpg-restore.sh): they may assume the backup role to read the bucket. Add the name of a production restore target (e.g. game-db-2) before restoring into it."
  type        = list(string)
  default     = ["game-db-restore-test"]
}

# --- Database credentials ---------------------------------------------------

variable "db_app_username" {
  description = "Application database user (GAMIFICATIONENGINE_DB_USER). Must not be postgres or streaming_replica."
  type        = string
  default     = "game_app"

  validation {
    condition     = !contains(["postgres", "streaming_replica"], var.db_app_username)
    error_message = "postgres and streaming_replica are reserved by CloudNativePG."
  }
}

variable "db_app_password_version" {
  description = "Increase to generate and store a new application password (rotation)."
  type        = number
  default     = 1
}

variable "secret_recovery_window_days" {
  description = "Days a deleted secret can be restored (0 deletes immediately; use 0 only for throwaway environments)."
  type        = number
  default     = 30
}

# --- Encryption -------------------------------------------------------------

variable "kms_deletion_window_days" {
  description = "Waiting period before the KMS key is deleted after destroy."
  type        = number
  default     = 30
}

# --- Backups (used by the upcoming CloudNativePG backup configuration) ------

variable "create_backup_bucket" {
  description = "Create the S3 bucket and IAM role for CloudNativePG backups (WAL archive and base backups)."
  type        = bool
  default     = true
}

variable "backup_noncurrent_version_days" {
  description = "Days to keep overwritten or deleted backup objects (S3 versioning safety net). Backup retention itself is set in CloudNativePG."
  type        = number
  default     = 30
}
