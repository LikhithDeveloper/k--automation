# Kasm maintenance in env0

Point a Terraform template named **Kasm maintenance PowerShell runner** at the root of the `powershell-maintenance` branch using Terraform 1.5 or later. The template changes a timestamp in Terraform state so Apply runs each time. To use an env0 Workflow template, point it at the same branch and directory. The workflow runs the maintenance script during Apply.

Set these env0 environment variables. Each numbered variable contains one hostname or IP address.

| Variable | Example |
| --- | --- |
| `SSH_USER` | `maintenance` |
| `PROXY1`, `PROXY2` | `proxy-01`, `proxy-02` |
| `WEB_APP_1`, `WEB_APP_2` | `web-01`, `web-02` |
| `DB_1`, `DB_2` | `db-01`, `db-02` |

An unnumbered `DB` also works and runs before numbered database servers. The env0 agent needs PowerShell, OpenSSH, a trusted host key for each server, and an SSH key for `SSH_USER`. Every remote command runs as root through passwordless sudo.

The script stops and reboots proxies, web apps, and databases in that order, one host at a time. Docker is masked across reboot so it stays off until its startup turn. Then the script starts Docker and Kasm in database, web app, and proxy order. It polls every 20 seconds on shutdown and every 30 seconds on startup. Containers with a Docker health check must be healthy. Other containers must be running.
