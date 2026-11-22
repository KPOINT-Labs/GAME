# Placeholder for CloudNativePG backups (WAL archive + base backups through
# the Barman Cloud plugin): the bucket and the role its pods assume. The
# Cluster's backup configuration comes with the backups task.
resource "aws_s3_bucket" "backups" {
  count = var.create_backup_bucket ? 1 : 0

  bucket = "${local.name}-db-backups-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "backups" {
  count = var.create_backup_bucket ? 1 : 0

  bucket                  = aws_s3_bucket.backups[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  count = var.create_backup_bucket ? 1 : 0

  bucket = aws_s3_bucket.backups[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  count = var.create_backup_bucket ? 1 : 0

  bucket = aws_s3_bucket.backups[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  count = var.create_backup_bucket ? 1 : 0

  bucket = aws_s3_bucket.backups[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.db.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  count = var.create_backup_bucket ? 1 : 0

  bucket = aws_s3_bucket.backups[0].id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.backup_noncurrent_version_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "backups_bucket" {
  count = var.create_backup_bucket ? 1 : 0

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.backups[0].arn,
      "${aws_s3_bucket.backups[0].arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "backups" {
  count = var.create_backup_bucket ? 1 : 0

  bucket = aws_s3_bucket.backups[0].id
  policy = data.aws_iam_policy_document.backups_bucket[0].json

  depends_on = [aws_s3_bucket_public_access_block.backups]
}

resource "aws_iam_role" "cnpg_backup" {
  count = var.create_backup_bucket ? 1 : 0

  name               = "${local.name}-db-backup"
  assume_role_policy = data.aws_iam_policy_document.irsa_trust["cnpg_backup"].json
}

data "aws_iam_policy_document" "cnpg_backup" {
  count = var.create_backup_bucket ? 1 : 0

  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.backups[0].arn]
  }

  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.backups[0].arn}/*"]
  }

  statement {
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
    resources = [aws_kms_key.db.arn]
  }
}

resource "aws_iam_role_policy" "cnpg_backup" {
  count = var.create_backup_bucket ? 1 : 0

  name   = "db-backups"
  role   = aws_iam_role.cnpg_backup[0].id
  policy = data.aws_iam_policy_document.cnpg_backup[0].json
}
