# Application database credentials: {"username": ..., "password": ...}.
# External Secrets copies them into the Kubernetes Secret that both the
# CloudNativePG cluster (owner role) and the API read.
#
# To rotate, bump db_app_password_version (or put a new password in the
# secret yourself): External Secrets syncs it, CloudNativePG applies it to
# the role, and API pods pick it up on restart.
# Ephemeral + write-only: the generated password never enters the Terraform
# plan or state. Bump db_app_password_version to make Terraform write a new
# one (a rotation through Terraform).
ephemeral "random_password" "db_app" {
  length = 40
  # URL-safe: the password is also embedded in connection URLs.
  special          = true
  override_special = "-_"
}

resource "aws_secretsmanager_secret" "db_app" {
  name                    = "${local.secret_prefix}/app"
  description             = "${local.name} application database user (CloudNativePG owner role)"
  kms_key_id              = aws_kms_key.db.arn
  recovery_window_in_days = var.secret_recovery_window_days
}

resource "aws_secretsmanager_secret_version" "db_app" {
  secret_id = aws_secretsmanager_secret.db_app.id
  secret_string_wo = jsonencode({
    username = var.db_app_username
    password = ephemeral.random_password.db_app.result
  })
  secret_string_wo_version = var.db_app_password_version
}

# Read-only access to this environment's database secrets, for the service
# account External Secrets uses (SecretStore in kubernetes/aws/).
resource "aws_iam_role" "secrets_reader" {
  name               = "${local.name}-db-secrets-reader"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust["secrets_reader"].json
}

data "aws_iam_policy_document" "secrets_reader" {
  statement {
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = ["arn:${local.partition}:secretsmanager:${var.region}:${local.account_id}:secret:${local.secret_prefix}/*"]
  }

  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.db.arn]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "secrets_reader" {
  name   = "read-db-secrets"
  role   = aws_iam_role.secrets_reader.id
  policy = data.aws_iam_policy_document.secrets_reader.json
}
