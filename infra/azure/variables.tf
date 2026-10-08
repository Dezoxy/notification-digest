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

variable "backup_resource_group_name" {
  description = "Separate resource group for recovery copies outside the runtime account."
  type        = string
  default     = "notification-digest-backups-westeurope"
  validation {
    condition     = lower(var.backup_resource_group_name) != lower(var.resource_group_name)
    error_message = "Recovery copies require a separate resource group from the runtime."
  }
}

variable "backup_storage_account_name" {
  description = "Globally unique recovery storage name, different from the runtime account."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.backup_storage_account_name))
    error_message = "Backup storage names require 3-24 lowercase letters/digits."
  }
  validation {
    condition     = var.backup_storage_account_name != var.storage_account_name
    error_message = "Recovery copies must use a different account from runtime state."
  }
}

variable "image" {
  description = "Exact GHCR release tag or digest, pinned in the tracked image.auto.tfvars.json. The default is the VM baseline, which has no cloud runner."
  type        = string
  default     = "ghcr.io/dezoxy/notification-digest:0.28.0"
  validation {
    condition     = can(regex("^ghcr\\.io/dezoxy/notification-digest(:[0-9]+\\.[0-9]+\\.[0-9]+|@sha256:[0-9a-f]{64})$", var.image))
    error_message = "Use an exact release version or sha256 digest; floating tags are forbidden."
  }
}

variable "jobs_enabled" {
  description = "False prepares the foundation and empty vault. Enable only after every required secret has been copied and verified."
  type        = bool
  default     = false
}

variable "schedules_enabled" {
  description = "False creates Manual jobs. True replaces them with Scheduled jobs after the state handoff."
  type        = bool
  default     = false
  validation {
    condition     = !var.schedules_enabled || var.jobs_enabled
    error_message = "Schedule activation requires jobs_enabled after the dedicated vault secrets are copied and verified."
  }
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

variable "key_vault_name" {
  description = "Globally unique name of the dedicated digest vault created by this stack."
  type        = string
  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$", var.key_vault_name)) && !strcontains(var.key_vault_name, "--")
    error_message = "Vault names require 3-24 letters/digits/hyphens, start with a letter and cannot contain consecutive hyphens."
  }
}

variable "secret_names" {
  description = "Runtime environment name -> secret name in the dedicated digest vault. GHCR_PULL_TOKEN is registry-only. Secret values are copied separately and never managed by Terraform."
  type        = map(string)
  validation {
    condition = alltrue([
      for name, secret_name in var.secret_names :
      can(regex("^[A-Z][A-Z0-9_]*$", name)) && can(regex("^[a-zA-Z0-9-]{1,127}$", secret_name))
    ])
    error_message = "Use uppercase environment names and Azure Key Vault secret names containing only letters, digits and hyphens."
  }
  validation {
    condition = alltrue([
      for name in ["GHCR_PULL_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "TG_API_ID", "TG_API_HASH", "TG_SESSION"] : contains(keys(var.secret_names), name)
    ])
    error_message = "Registry, subscription OAuth and Telegram session secret names are required."
  }
  validation {
    condition     = !contains(keys(var.secret_names), "X_COOKIES") && !contains(keys(var.secret_names), "X_COOKIES_PATH")
    error_message = "X cookies are seeded into Blob runtime state, not injected as job environment variables."
  }
}

variable "app_env" {
  description = "Reviewed nonsecret application configuration. Keep credentials/cookies and signed URLs in secret_names."
  type        = map(string)
  validation {
    condition = alltrue([
      for name in keys(var.app_env) :
      !can(regex("(TOKEN|SECRET|PASSWORD|COOKIE|SESSION|API_KEY|INGEST_KEY|API_HASH|PROXY_KEY)", name)) &&
      !startswith(name, "DIGEST_CLOUD_") && !contains(["STATE_DB_PATH", "ARCHIVE_DIR", "X_COOKIES_PATH"], name)
    ])
    error_message = "Do not put credentials or cloud-managed paths in app_env; use secret_names for credentials."
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
