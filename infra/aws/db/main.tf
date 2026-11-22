data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_eks_cluster" "this" {
  name = var.eks_cluster_name
}

locals {
  name        = "${var.name_prefix}-${var.environment}"
  account_id  = data.aws_caller_identity.current.account_id
  partition   = data.aws_partition.current.partition
  oidc_issuer = trimprefix(data.aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://")
  oidc_arn    = "arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.oidc_issuer}"

  # Secrets Manager path for everything this environment's database needs.
  secret_prefix = "${var.name_prefix}/${var.environment}/db"

  # Kubernetes service accounts allowed to assume each IRSA role.
  irsa_trust_subjects = {
    secrets_reader = ["system:serviceaccount:${var.k8s_namespace}:${var.secrets_reader_service_account}"]
    # The database cluster, plus clusters restored from its backups (each
    # CloudNativePG cluster runs as a service account named after it).
    cnpg_backup = [
      for sa in concat([var.cnpg_cluster_name], var.cnpg_restore_cluster_names) :
      "system:serviceaccount:${var.k8s_namespace}:${sa}"
    ]
  }
}

# Trust policy for an IAM role assumed by one Kubernetes service account
# (IAM Roles for Service Accounts).
data "aws_iam_policy_document" "irsa_trust" {
  for_each = local.irsa_trust_subjects

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:sub"
      values   = each.value
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}
