# Plan-level checks with mocked providers (no AWS access): terraform test
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_eks_cluster" {
    defaults = { identity = [{ oidc = [{ issuer = "https://oidc.eks.us-east-1.amazonaws.com/id/ABC" }] }] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:111122223333:key/k" }
  }
}
mock_provider "random" {}

variables {
  region                   = "us-east-1"
  environment              = "prod"
  eks_cluster_name         = "eks"
  ebs_csi_driver_role_name = "ebs-csi"
}

run "defaults" {
  command = plan
  assert {
    condition     = aws_secretsmanager_secret.db_app.name == "game/prod/db/app"
    error_message = "secret name"
  }
  assert {
    condition     = length(aws_s3_bucket.backups) == 1 && aws_s3_bucket.backups[0].bucket == "game-prod-db-backups-111122223333"
    error_message = "bucket"
  }
  assert {
    condition     = length(aws_iam_role_policy.ebs_csi_kms) == 1
    error_message = "ebs csi policy"
  }
  assert {
    condition     = local.oidc_arn == "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABC"
    error_message = "oidc arn"
  }
}

run "no_backups_no_csi" {
  command = plan
  variables {
    create_backup_bucket     = false
    ebs_csi_driver_role_name = ""
  }
  assert {
    condition     = length(aws_s3_bucket.backups) == 0 && length(aws_iam_role.cnpg_backup) == 0 && length(aws_iam_role_policy.ebs_csi_kms) == 0
    error_message = "optional resources"
  }
}

run "reserved_user_rejected" {
  command = plan
  variables {
    db_app_username = "postgres"
  }
  expect_failures = [var.db_app_username]
}
