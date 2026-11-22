# One customer-managed key for the database's data at rest: EBS volumes
# (StorageClass gp3-encrypted), Secrets Manager secrets and the backup bucket.
# Access is granted by IAM policies (the key policy delegates to the account).
resource "aws_kms_key" "db" {
  description             = "${local.name} database encryption (EBS volumes, secrets, backups)"
  enable_key_rotation     = true
  deletion_window_in_days = var.kms_deletion_window_days
}

resource "aws_kms_alias" "db" {
  name          = "alias/${local.name}-db"
  target_key_id = aws_kms_key.db.key_id
}

# The EBS CSI driver creates and attaches the encrypted volumes, so it needs
# to use the key and create grants for EC2.
data "aws_iam_policy_document" "ebs_csi_kms" {
  statement {
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = [aws_kms_key.db.arn]
  }

  statement {
    actions   = ["kms:CreateGrant", "kms:ListGrants", "kms:RevokeGrant"]
    resources = [aws_kms_key.db.arn]

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "ebs_csi_kms" {
  count = var.ebs_csi_driver_role_name == "" ? 0 : 1

  name   = "${local.name}-db-kms"
  role   = var.ebs_csi_driver_role_name
  policy = data.aws_iam_policy_document.ebs_csi_kms.json
}
