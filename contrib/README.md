# community-scripts submission

These are the files for submitting `proxmox-autosnap` to the
[Proxmox VE Helper-Scripts](https://community-scripts.org) project.

Per their contribution rules, **new scripts go to the testing repo first**:
[`community-scripts/ProxmoxVED`](https://github.com/community-scripts/ProxmoxVED).
Once reviewed and verified there, maintainers promote the script to the main
`ProxmoxVE` repository.

| File | Destination in the ProxmoxVED fork |
| :--- | :--- |
| `ct/proxmox-autosnap.sh` | `ct/proxmox-autosnap.sh` |
| `install/proxmox-autosnap-install.sh` | `install/proxmox-autosnap-install.sh` |
| `json/proxmox-autosnap.json` | `json/proxmox-autosnap.json` |

The `ct/` script sources the framework's `build.func` (which provides the
standard whiptail wizard: CT ID, hostname, resources, bridge, IPv4 DHCP/static,
gateway, DNS, …). Both install and update deploy the app via the framework's
`fetch_and_deploy_gh_release` / `check_for_gh_release` helpers (GitHub release
`v1.0.0+` of this repo), per the ProxmoxVED AGENTS.md standard.

The `install/` script sets the app up **without** creating any host-side API
token; the container's app shows a **first-run setup wizard** on first web
access where the user enters the Proxmox host and an API token — this keeps the
helper script from modifying the host beyond creating the container.
