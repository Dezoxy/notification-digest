terraform {
  required_version = "~> 1.16.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.4.0"
    }
  }
  # Workspace execution mode must be Local: GitHub obtains Azure OIDC tokens,
  # while HCP Terraform holds the state. No local state fallback is permitted.
  cloud {
    organization = "toomhorvath"
    workspaces {
      name = "notification-digest-azure"
    }
  }
}

provider "azurerm" {
  features {}
  storage_use_azuread             = true
  resource_provider_registrations = "none"
}
