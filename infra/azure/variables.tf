variable "resource_group_name" {
  type    = string
  default = "notification-digest-westeurope"
}

variable "location" {
  type    = string
  default = "westeurope"
  validation {
    condition     = var.location == "westeurope"
    error_message = "This reviewed migration targets West Europe. Review residency and pricing before changing region."
  }
}

variable "storage_account_name" {
  description = "Globally unique, lowercase alphanumeric name; set during deployment preparation."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.storage_account_name))
    error_message = "Storage account names require 3-24 lowercase letters/digits."
  }
}

variable "image" {
  description = "Exact GHCR release tag or digest. Replace the VM baseline with the tested migration release before executing any job."
  type        = string
  default     = "ghcr.io/dezoxy/notification-digest:0.28.0"
  validation {
    condition     = can(regex("^ghcr\\.io/dezoxy/notification-digest(:[0-9]+\\.[0-9]+\\.[0-9]+|@sha256:[0-9a-f]{64})$", var.image))
    error_message = "Use an exact release version or sha256 digest; floating tags are forbidden."
  }
}

variable "schedules_enabled" {
  description = "False creates Manual jobs. True replaces them with Scheduled jobs after the state handoff."
  type        = bool
  default     = false
}

variable "migration_release_verified" {
  description = "Operator attestation that the selected image contains the tested cloud runner and state/delivery safeguards."
  type        = bool
  default     = false
}

variable "namespace" {
  type    = string
  default = "production"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.namespace))
    error_message = "Use a simple lowercase state namespace."
  }
}

variable "ghcr_username" {
  description = "GitHub username matching the read:packages PAT; no token values here."
  type        = string
}

variable "key_vault_resource_id" {
  description = "Existing RBAC-enabled vault, managed outside this stack."
  type        = string
  validation {
    condition     = can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.KeyVault/vaults/[^/]+$", var.key_vault_resource_id))
    error_message = "Supply the existing vault ARM resource ID."
  }
}

variable "secret_refs" {
  description = "Runtime environment name -> existing versionless Key Vault secret URL. Includes GHCR_PULL_TOKEN (registry only). Never supply secret values."
  type        = map(string)
  validation {
    condition = alltrue([
      for name, url in var.secret_refs :
      can(regex("^[A-Z][A-Z0-9_]*$", name)) && can(regex("^https://[a-zA-Z0-9-]+\\.vault\\.azure\\.net/secrets/[a-zA-Z0-9-]+$", url))
    ])
    error_message = "Secret references must be uppercase environment names and versionless Key Vault URLs."
  }
  validation {
    condition = alltrue([
      for name in ["GHCR_PULL_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "TG_API_ID", "TG_API_HASH", "TG_SESSION"] : contains(keys(var.secret_refs), name)
    ])
    error_message = "Registry, subscription OAuth and Telegram session secret references are required."
  }
}

variable "app_env" {
  description = "Reviewed nonsecret application configuration. Keep credentials/cookies and signed URLs in secret_refs."
  type        = map(string)
  validation {
    condition = alltrue([
      for name in keys(var.app_env) :
      !can(regex("(TOKEN|SECRET|PASSWORD|COOKIE|SESSION|API_KEY|INGEST_KEY|API_HASH|PROXY_KEY)", name)) &&
      !startswith(name, "DIGEST_CLOUD_") && !contains(["STATE_DB_PATH", "ARCHIVE_DIR", "X_COOKIES_PATH"], name)
    ])
    error_message = "Do not put credentials or cloud-managed paths in app_env; use secret_refs for credentials."
  }
}

variable "alert_email" {
  type        = string
  description = "Owner email for Azure failures, missed schedules and budget notifications."
  validation {
    condition     = can(regex("^[^@ ]+@[^@ ]+\\.[^@ ]+$", var.alert_email))
    error_message = "Supply an alert recipient email."
  }
}

variable "monthly_budget" {
  description = "Budget in the Azure subscription billing currency, not necessarily USD. Alerts do not cap spending."
  type        = number
  default     = 5
  validation {
    condition     = var.monthly_budget >= 1
    error_message = "A monthly budget must be at least 1 billing-currency unit."
  }
}

variable "budget_start_date" {
  description = "First day of the deployment month, e.g. 2026-10-01T00:00:00Z."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-01T00:00:00Z$", var.budget_start_date))
    error_message = "Use the first day of the deployment month at UTC midnight."
  }
}
