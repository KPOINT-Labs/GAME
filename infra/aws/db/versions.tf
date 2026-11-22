terraform {
  # 1.11+: ephemeral resources and write-only arguments keep the database
  # password out of plan and state.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # State lives in S3; values go in a backend config file kept out of git
  # (see backend.hcl.example): terraform init -backend-config=backend.hcl
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = merge(
      {
        Project     = var.name_prefix
        Environment = var.environment
        Component   = "database"
        ManagedBy   = "terraform"
      },
      var.tags,
    )
  }
}
