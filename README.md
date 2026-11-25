# Kasm maintenance in env0

The workflow stops and reboots each proxy, then each web app server, then each database server. After every reboot is confirmed, it starts the database servers, then the web app servers, then the proxies. Each host must pass its readiness check before the runner moves on. Run this during a maintenance window because the sequence takes the fleet offline.

## env0 setup

1. Create a Terraform template named **Kasm maintenance runner** in env0. Point it to this repository and use the repository root as the template directory. Select Terraform 1.5 or later. The template provisions no infrastructure. Its `env0.yml` runs the maintenance script before Terraform Apply on deploys. Plan-only runs and destroys do not invoke the script.
2. Create an **env0 Workflow** template pointing to the same repository root. Its `env0.workflow.yml` invokes **Kasm maintenance runner**.
3. Set the following as environment variables on the runner template or its project. Each value is one hostname or IP address.

   | Variable | Example |
   | --- | --- |
   | `SSH_USER` | `maintenance` |
   | `PROXY1` | `proxy-01.internal` |
   | `PROXY2` | `proxy-02.internal` |
   | `WEB_APP_1` | `web-01.internal` |
   | `WEB_APP_2` | `web-02.internal` |
   | `DB_1` | `db-01.internal` |
   | `DB_2` | `db-02.internal` |

   Use unique indexes and hosts, with at least one host in each tier. Proxies also accept `PROXY_1`. Database servers accept `DB1` or an unnumbered `DB`, which sorts first. Hosts run in ascending index order within each tier in both phases.
4. Use an env0 agent with Python 3.9+, OpenSSH, network access to the hosts, an SSH key available to the SSH client, and verified server entries in its `known_hosts`. The remote account needs noninteractive sudo for the Kasm scripts, Docker, systemctl, and the preflight and boot ID checks. The hosts need systemd-managed `docker.service` and `docker.socket`.
5. Disable automatic deploys for the maintenance environment and require approval before Apply. Deploy the workflow manually. Use one maintenance run at a time for the same hosts.

## Polling and recovery

- Shutdown checks Docker immediately after running `/opt/kasm/bin/stop` and then every 20 seconds while containers remain running. The runner masks Docker and its socket across reboot and verifies a changed boot ID before proceeding.
- Startup unmasks the Docker units, starts Docker, runs `/opt/kasm/bin/start`, and checks all containers immediately and then every 30 seconds. Pre-shutdown container names must reappear. Containers with health checks must report `healthy`. Containers without health checks must be running.
- The per-host timeouts are configurable through `KASM_STOP_TIMEOUT_SECONDS` (600), `KASM_REBOOT_TIMEOUT_SECONDS` (600), and `KASM_START_TIMEOUT_SECONDS` (900). A timeout fails the deployment.

If the run fails or is interrupted, check all hosts already touched. Some may be rebooted with Docker still masked. Recover the affected hosts in database, web app, then proxy order. After confirming each host is reachable and any pending reboot has completed, run these commands on that host and check container readiness before continuing.

```sh
sudo -n systemctl unmask docker.service docker.socket
sudo -n systemctl start docker
sudo -n /opt/kasm/bin/start
sudo -n docker ps -a
```
