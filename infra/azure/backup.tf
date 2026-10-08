# Recovery copies outlive the runtime account and use a separate writer identity.
# This is account isolation, not protection from subscription administrators.
resource "azurerm_resource_group" "backup" {
  name     = var.backup_resource_group_name
  location = var.location
  tags     = local.tags
  lifecycle { prevent_destroy = true }
}

resource "azurerm_storage_account" "backup" {
  name                            = var.backup_storage_account_name
  resource_group_name             = azurerm_resource_group.backup.name
  location                        = var.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  allow_nested_items_to_be_public = false
  tags                            = local.tags
  blob_properties {
    versioning_enabled = true
    delete_retention_policy { days = 14 }
    container_delete_retention_policy { days = 14 }
  }
  lifecycle { prevent_destroy = true }
}

resource "azurerm_storage_container" "backup" {
  name                  = "digest-backups"
  storage_account_id    = azurerm_storage_account.backup.id
  container_access_type = "private"
  lifecycle { prevent_destroy = true }
}

resource "azurerm_storage_management_policy" "backup" {
  storage_account_id = azurerm_storage_account.backup.id
  rule {
    name    = "expire-daily-recovery-copies"
    enabled = true
    filters {
      prefix_match = ["${azurerm_storage_container.backup.name}/backups/"]
      blob_types   = ["blockBlob"]
    }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 30
      }
      version {
        delete_after_days_since_creation = 30
      }
    }
  }
}

resource "azurerm_user_assigned_identity" "backup_writer" {
  name                = "notification-digest-backup-writer"
  resource_group_name = azurerm_resource_group.backup.name
  location            = var.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "backup_writer" {
  scope                = azurerm_storage_container.backup.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.backup_writer.principal_id
}
