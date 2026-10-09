terraform {
  required_version = "~> 1.16.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.9.0"
    }
  }
  # Bootstrap this separate backend before init. GitHub OIDC accesses state
  # without account keys; the digest runner cannot access this container.
  # Supply only nonsecret location fields through -backend-config=backend.hcl.
  backend "azurerm" {
    use_azuread_auth = true
    use_oidc         = true
  }
}

provider "azurerm" {
  features {}
  storage_use_azuread             = true
  resource_provider_registrations = "none"
}
