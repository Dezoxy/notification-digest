locals {
  tags = { application = "notification-digest", owner = "toom", managed_by = "terraform" }
  jobs = {
    daytime   = "0 6,12 * * *"
    overnight = "0 0 * * *"
    evening   = "0 18 * * *"
    daily     = "30 18,19 * * *"
    weekly    = "45 19,20 * * 0"
    positions = "25 1,5,9,13,17,21 * * *"
    patreon   = "50 * * * *"
    relay     = "40 * * * *"
    backup    = "15 4 * * *"
  }
  secrets = { for name, url in var.secret_refs : name => {
    name = lower(replace(name, "_", "-"))
    url  = url
    key  = basename(url)
  } }
  cloud_env = {
    DIGEST_CLOUD_ACCOUNT_URL        = azurerm_storage_account.state.primary_blob_endpoint
    DIGEST_CLOUD_CONTAINER          = azurerm_storage_container.state.name
    DIGEST_CLOUD_NAMESPACE          = var.namespace
    DIGEST_CLOUD_DATA_DIR           = "/data"
    DIGEST_CLOUD_IDENTITY_CLIENT_ID = azurerm_user_assigned_identity.runner.client_id
    STATE_DB_PATH                   = "/data/state.db"
    ARCHIVE_DIR                     = "/data/archive"
    X_COOKIES_PATH                  = "/data/x-cookies.json"
  }
}

resource "azurerm_resource_group" "digest" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
  lifecycle { prevent_destroy = true }
}

resource "azurerm_storage_account" "state" {
  name                            = var.storage_account_name
  resource_group_name             = azurerm_resource_group.digest.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  tags                            = local.tags
  blob_properties {
    delete_retention_policy { days = 1 }
    container_delete_retention_policy { days = 7 }
  }
  # The runner prunes immutable checkpoints by reference under the shared
  # manifest lease; an age-only lifecycle rule would delete live state.
  lifecycle { prevent_destroy = true }
}

resource "azurerm_storage_container" "state" {
  name                  = "digest-state"
  storage_account_id    = azurerm_storage_account.state.id
  container_access_type = "private"
  lifecycle { prevent_destroy = true }
}

resource "azurerm_log_analytics_workspace" "digest" {
  name                = "notification-digest-logs"
  resource_group_name = azurerm_resource_group.digest.name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.1
  tags                = local.tags
}

resource "azurerm_container_app_environment" "digest" {
  name                       = "notification-digest"
  resource_group_name        = azurerm_resource_group.digest.name
  location                   = var.location
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.digest.id
  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
  tags = local.tags
}

resource "azurerm_user_assigned_identity" "runner" {
  name                = "notification-digest-runner"
  resource_group_name = azurerm_resource_group.digest.name
  location            = var.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "state" {
  scope                = azurerm_storage_container.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.runner.principal_id
}

resource "azurerm_role_assignment" "secrets" {
  for_each             = toset([for secret in values(local.secrets) : secret.key])
  scope                = "${var.key_vault_resource_id}/secrets/${each.key}"
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.runner.principal_id
}

resource "azurerm_container_app_job" "digest" {
  for_each                     = local.jobs
  name                         = "digest-${each.key}"
  resource_group_name          = azurerm_resource_group.digest.name
  location                     = var.location
  container_app_environment_id = azurerm_container_app_environment.digest.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 3000
  replica_retry_limit          = 0
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runner.id]
  }
  dynamic "manual_trigger_config" {
    for_each = var.schedules_enabled ? [] : [1]
    content {
      parallelism              = 1
      replica_completion_count = 1
    }
  }
  dynamic "schedule_trigger_config" {
    for_each = var.schedules_enabled ? [1] : []
    content {
      cron_expression          = each.value
      parallelism              = 1
      replica_completion_count = 1
    }
  }
  dynamic "secret" {
    for_each = local.secrets
    content {
      name                = secret.value.name
      identity            = azurerm_user_assigned_identity.runner.id
      key_vault_secret_id = secret.value.url
    }
  }
  registry {
    server               = "ghcr.io"
    username             = var.ghcr_username
    password_secret_name = local.secrets["GHCR_PULL_TOKEN"].name
  }
  template {
    container {
      name    = "digest"
      image   = var.image
      cpu     = 0.5
      memory  = "1Gi"
      command = ["python", "-m", "digest.cloud_run"]
      args    = [each.key]
      dynamic "env" {
        for_each = merge(var.app_env, local.cloud_env)
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = { for name, secret in local.secrets : name => secret if name != "GHCR_PULL_TOKEN" }
        content {
          name        = env.key
          secret_name = env.value.name
        }
      }
    }
  }
  lifecycle {
    precondition {
      condition     = !var.schedules_enabled || (var.migration_release_verified && var.image != "ghcr.io/dezoxy/notification-digest:0.28.0")
      error_message = "Scheduled activation requires a verified migration image; baseline 0.28.0 has no cloud runner."
    }
    precondition {
      condition     = length(setintersection(toset(keys(var.app_env)), toset(keys(var.secret_refs)))) == 0
      error_message = "An environment variable must have exactly one nonsecret value or secret reference."
    }
    precondition {
      condition = alltrue([
        for secret in values(local.secrets) :
        split(".", split("/", secret.url)[2])[0] == basename(var.key_vault_resource_id)
      ])
      error_message = "All secret URLs must belong to the specified shared vault."
    }
  }
  tags       = local.tags
  depends_on = [azurerm_role_assignment.state, azurerm_role_assignment.secrets]
}
