// Where the containers run. One environment per deploymentEnvironment block.
// There is exactly one environment: one Azure region plus Cloudflare's edge.
// Local development runs the same image from compose.yml against a throwaway
// state file and is described in deployment/environments.md, not modelled here.
//
// The region is the system's failure domain: the jobs and their state share
// it. Recovery copies sit in a separate storage account and resource group in
// the same region. See reliability/disaster-recovery.md.

production = deploymentEnvironment "Production" {

    azure = deploymentNode "Azure West Europe" "One region in one subscription. No second region." "Microsoft Azure" {

        jobs = deploymentNode "Container Apps environment" "Nine scheduled jobs start one-shot runs. A Blob lease admits one run at a time, so two never share the Telegram session." "Azure Container Apps Jobs" {
            runnerInstance = containerInstance notificationDigest.runner
        }

        deploymentNode "Runtime storage account" "Holds the SQLite bundle every run restores and checkpoints. Shared-key access is disabled." "Azure Blob Storage" {
            containerInstance notificationDigest.state
        }

        backupStorage = infrastructureNode "Backup storage account" "Daily recovery copies of the state, in a separate resource group and written by a separate identity." "Azure Blob Storage"
    }

    // Hierarchical identifiers: deployment elements are addressed by full path.
    production.azure.jobs.runnerInstance -> production.azure.backupStorage "Copies a daily recovery bundle to" "HTTPS, backup-writer identity"

    deploymentNode "Cloudflare" "Global edge. Operated by Cloudflare; no owner-managed hosts." "Cloudflare" {

        deploymentNode "Workers" "Serves the public archive." "Cloudflare Workers" {
            containerInstance notificationDigest.site
        }

        deploymentNode "D1" "Rendering copy only; Azure holds the system of record." "Cloudflare D1" {
            containerInstance notificationDigest.siteDb
        }
    }
}
