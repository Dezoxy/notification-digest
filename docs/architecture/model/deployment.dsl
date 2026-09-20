// Where the containers run. One environment per deploymentEnvironment block.
// There is exactly one environment: the owner's homelab plus Cloudflare's edge.
// Local development runs the same image from compose.yml against a throwaway
// state file and is described in deployment/environments.md, not modelled here.
//
// The single Proxmox node is the system's failure domain: the runner, its state
// and the backups all sit on it. See reliability/disaster-recovery.md.

production = deploymentEnvironment "Production" {

    deploymentNode "Homelab" "Single Proxmox node in the owner's flat. No second site." "Proxmox VE" {

        deploymentNode "01-myapps-vm" "Debian VM on the LAN, reachable only from inside it." "Debian VM" {

            deploymentNode "systemd timers + Docker" "Nine timers start one-shot runs; a flock wrapper serializes every run so two never share the Telegram session." "systemd, Docker Engine" {
                containerInstance notificationDigest.runner
            }

            deploymentNode "/srv/appdata/digest" "Local ext4. SQLite is never placed on the virtiofs share, where it fails to open." "ext4 filesystem" {
                containerInstance notificationDigest.state
            }
        }
    }

    deploymentNode "Cloudflare" "Global edge. Operated by Cloudflare; no owner-managed hosts." "Cloudflare" {

        deploymentNode "Workers" "Serves the public archive." "Cloudflare Workers" {
            containerInstance notificationDigest.site
        }

        deploymentNode "D1" "Rendering copy only; the VM holds the system of record." "Cloudflare D1" {
            containerInstance notificationDigest.siteDb
        }
    }
}
