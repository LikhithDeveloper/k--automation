# Kasm maintenance in env0

The workflow takes the fleet offline during a maintenance window. Every stage waits for its own **Approve** action in env0 before running any server commands. Within a stage, hosts run one at a time in numeric order.

| Order | env0 sub-environment alias | Action |
| --- | --- | --- |
| 1 | `proxy-stop` | Stop and reboot proxies |
| 2 | `webapp-stop` | Stop and reboot web apps |
| 3 | `db-stop` | Stop and reboot databases |
| 4 | `db-start` | Start databases |
| 5 | `webapp-start` | Start web apps |
| 6 | `proxy-start` | Start proxies |

## env0 setup

1. Create a Terraform template named **Kasm maintenance runner** with this repository's root as its template directory. Select Terraform 1.5 or later. The plan changes a timestamp in Terraform state so env0 has an Apply to approve on every run. The template provisions no infrastructure.
2. Create an **env0 Workflow** template pointing to the same repository root. The workflow declares the six dependencies and sets `requiresApproval: true` on each stage.
3. In the workflow template's variables, set a separate `KASM_STAGE` **environment variable** for each sub-environment. Its value must exactly match the sub-environment alias from the table. Add the shared server and credential variables below at workflow scope so all six stages receive them.
4. Use an env0 agent with Python 3.9+, OpenSSH, network access to the hosts, and verified host keys in its `known_hosts`. Install `sshpass` on the agent if using SSH passwords. The servers need systemd-managed `docker.service` and `docker.socket`, plus sudo access for the configured SSH account.
5. Disable continuous deployment and automatic remediation for the maintenance environment. Start the workflow manually and keep manual approval enabled for all six sub-environments in the deployment settings. Review the stage name before approving it. Use one maintenance run at a time for the same hosts.

## Environment variables

| Variable | Example value |
| --- | --- |
| `SSH_USER` | `maintenance` |
| `PROXY1` | `proxy-01.internal` |
| `PROXY2` | `proxy-02.internal` |
| `WEB_APP_1` | `web-01.internal` |
| `WEB_APP_2` | `web-02.internal` |
| `DB_1` | `db-01.internal` |
| `DB_2` | `db-02.internal` |

Use at least one unique host in each tier. Proxies also accept `PROXY_1`. Databases accept `DB1` or an unnumbered `DB`, which sorts first. For a host variable such as `PROXY1`, add credentials using the same prefix.

- `PROXY1_PASSWORD` is the SSH password. Mark it **Sensitive** in env0. Leave it unset to use the agent's SSH key.
- `PROXY1_SUDO_PASSWORD` is optional. Mark it **Sensitive** if supplied. It defaults to the SSH password. With neither password set, the runner uses noninteractive, passwordless sudo.
- `PROXY1_USER` is optional and overrides `SSH_USER` for that host.

Use the same pattern for `PROXY2_PASSWORD`, `WEB_APP_1_PASSWORD`, `DB_1_PASSWORD`, and the rest of the inventory. All remote maintenance commands use sudo. The runner passes SSH passwords by file descriptor and sudo passwords through SSH stdin. Passwords belong in env0's sensitive variables.

## Logs, checks, and recovery

The Apply log shows timestamped stage and host progress, each command's outcome, service command output, retry reasons, and failures. A failure exits the stage with an error and prevents dependent stages from running. Plan-only and destroy runs do not run maintenance.

The stop stage records container names and the host's boot ID in the root-only `/var/lib/kasm-env0-maintenance/state.json`, runs `/opt/kasm/bin/stop`, and checks for stopped containers every 20 seconds. It then masks the Docker service and socket, reboots, and confirms a new boot ID with both units inactive and masked. The mask persists while env0 waits for later approvals.

The start stage reads the saved inventory, unmasks Docker, starts Docker and Kasm, and checks every 30 seconds for all expected containers. Every container must be running, and containers with health checks must be healthy. Optional per-host limits are `KASM_STOP_TIMEOUT_SECONDS` (600), `KASM_REBOOT_TIMEOUT_SECONDS` (600), and `KASM_START_TIMEOUT_SECONDS` (900).

If a stage fails, inspect its log and the state of the hosts it already touched. After a verified reboot, a retry of the stop stage can recognize hosts that are still masked. A retry of the start stage rechecks hosts whose startup already completed. If a host was interrupted before reboot, inspect and recover it before retrying. Check any pending reboot before changing its Docker units.

If abandoning the run, recover affected hosts in database, web app, then proxy order. On each affected host, run the following and verify container health before proceeding to the next host.

```sh
sudo systemctl unmask docker.service docker.socket
sudo systemctl start docker
sudo /opt/kasm/bin/start
sudo docker ps -a
```

After confirming manual recovery, remove that host's stale `/var/lib/kasm-env0-maintenance/state.json` with sudo before starting a new maintenance run.
