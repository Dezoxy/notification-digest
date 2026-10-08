output "resource_group_name" { value = azurerm_resource_group.digest.name }
output "job_names" { value = { for key, job in azurerm_container_app_job.digest : key => job.name } }
output "state_account_url" { value = azurerm_storage_account.state.primary_blob_endpoint }
output "state_container" { value = azurerm_storage_container.state.name }
output "runner_identity_client_id" { value = azurerm_user_assigned_identity.runner.client_id }
output "log_analytics_workspace_id" { value = azurerm_log_analytics_workspace.digest.workspace_id }
output "key_vault_name" { value = azurerm_key_vault.digest.name }
output "key_vault_id" { value = azurerm_key_vault.digest.id }
output "key_vault_uri" { value = azurerm_key_vault.digest.vault_uri }
