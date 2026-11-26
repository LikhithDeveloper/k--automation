from os import environ
from re import fullmatch, search
from socket import SOCK_STREAM, getaddrinfo, socket
from time import sleep

from fabric import Connection
from paramiko import RejectPolicy

BOOT_ID = "cat /proc/sys/kernel/random/boot_id"
DOCKER_UNITS = "docker.service docker.socket"


def servers(pattern):
    hosts = []
    for name, value in environ.items():
        match = fullmatch(pattern, name)
        if match:
            hosts.append((int(match[1] or 0), value.strip()))
    return [host for _, host in sorted(hosts)]


def connect(host, user):
    ssh = Connection(host, user=user, connect_timeout=10, forward_agent=False)
    ssh.client.load_system_host_keys()
    ssh.client.set_missing_host_key_policy(RejectPolicy())
    return ssh


def sudo(ssh, command):
    print(f"[{ssh.host}] {command}", flush=True)
    with ssh:
        return ssh.run(f"sudo -n -u root -- {command}", in_stream=False).stdout.strip()


def containers(ssh):
    output = sudo(ssh, "docker ps -a --format '{{.Names}}|{{.State}}|{{.Status}}'")
    states = {}
    for line in output.splitlines():
        name, state, status = line.split("|", 2)
        states[name] = (state, status)
    return states


def wait_for_reboot(ssh, old_boot):
    addresses = getaddrinfo(ssh.host, ssh.port, type=SOCK_STREAM)
    for _ in range(60):
        sleep(10)
        for family, kind, protocol, _, address in addresses:
            with socket(family, kind, protocol) as probe:
                probe.settimeout(2)
                if probe.connect_ex(address) == 0:
                    break
        else:
            continue
        boot = sudo(ssh, BOOT_ID)
        if fullmatch(r"[0-9a-f-]{36}", boot) and boot != old_boot:
            return
    raise RuntimeError(f"[{ssh.host}] Reboot did not complete in time")


def stop_and_reboot(ssh):
    expected = set(containers(ssh))
    if not expected:
        raise RuntimeError(f"[{ssh.host}] No Docker containers found")

    sudo(ssh, "/opt/kasm/bin/stop")
    while any(state not in {"created", "exited", "dead"} for state, _ in containers(ssh).values()):
        sleep(20)

    # Hold Docker off until this server's startup turn.
    sudo(ssh, f"systemctl mask {DOCKER_UNITS}")
    old_boot = sudo(ssh, BOOT_ID)
    sudo(ssh, "systemd-run --on-active=5s systemctl --no-block reboot")
    wait_for_reboot(ssh, old_boot)
    return expected


def start(ssh, expected):
    sudo(ssh, f"systemctl unmask {DOCKER_UNITS}")
    sudo(ssh, "systemctl start docker")
    sudo(ssh, "/opt/kasm/bin/start")
    while True:
        states = containers(ssh)
        ready = all(
            state == "running" and not search(r"\((unhealthy|health: [^)]+)\)", status)
            for state, status in states.values()
        )
        if ready and expected.issubset(states):
            return
        sleep(30)


def main():
    proxies = servers(r"PROXY_?([1-9][0-9]*)")
    web_apps = servers(r"WEB_APP_([1-9][0-9]*)")
    databases = servers(r"DB(?:_?([1-9][0-9]*))?")
    shutdown_order = proxies + web_apps + databases
    startup_order = databases + web_apps + proxies

    if not (proxies and web_apps and databases):
        raise ValueError("Set at least one proxy, web app, and database server variable")
    user = environ.get("SSH_USER", "")
    if not fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*", user):
        raise ValueError("Set SSH_USER to the SSH account with passwordless sudo access")
    if len(set(shutdown_order)) != len(shutdown_order) or any(
        not fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*", host) for host in shutdown_order
    ):
        raise ValueError("Use unique server hostnames or IP addresses")

    expected = {}
    for host in shutdown_order:
        expected[host] = stop_and_reboot(connect(host, user))
    for host in startup_order:
        start(connect(host, user), expected[host])


if __name__ == "__main__":
    main()
