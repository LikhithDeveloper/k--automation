"""Run ordered Kasm maintenance from an env0 deployment agent."""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field

ROLES = (
    ("proxy", re.compile(r"PROXY_?([1-9][0-9]*)")),
    ("web app", re.compile(r"WEB_APP_([1-9][0-9]*)")),
    ("database", re.compile(r"DB(?:_?([1-9][0-9]*))?")),
)
DOCKER_UNITS = ("docker.service", "docker.socket")
STOP_POLL_SECONDS = 20
START_POLL_SECONDS = 30
REBOOT_POLL_SECONDS = 10
SSH_TRANSPORT_ERROR = 255
HOSTNAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*")
USERNAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.-]*")


class MaintenanceError(RuntimeError):
    pass


class ConnectionError(MaintenanceError):
    pass


@dataclass
class Server:
    role: str
    hostname: str
    expected: set[str] = field(default_factory=set)
    recovery_needed: bool = False

    def run(self, *command):
        result = subprocess.run(
            [
                "ssh", "-T", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10",
                "-o", "ServerAliveCountMax=2",
                "-l", os.environ["SSH_USER"], "--", self.hostname,
                shlex.join(("sudo", "-n", "--", *command)),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode:
            error = ConnectionError if result.returncode == SSH_TRANSPORT_ERROR else MaintenanceError
            detail = result.stderr.strip() or result.stdout.strip()
            raise error(f"{self.hostname}: {command[0]} exited {result.returncode}. {detail}")
        return result.stdout.strip()

    def log(self, message):
        print(f"[{self.role} {self.hostname}] {message}", flush=True)


def inventory():
    seen = set()
    groups = []
    for role, pattern in ROLES:
        indexed = {}
        for name, value in os.environ.items():
            match = pattern.fullmatch(name)
            if not match:
                continue
            hostname = value.strip()
            if not HOSTNAME.fullmatch(hostname):
                raise MaintenanceError(f"{name} must contain one SSH hostname or IP address")
            index = int(match[1] or 0)
            identity = hostname.lower().rstrip(".")
            if index in indexed or identity in seen:
                raise MaintenanceError(f"Duplicate server or index in {name}")
            indexed[index] = Server(role, hostname)
            seen.add(identity)
        if not indexed:
            raise MaintenanceError(f"Configure at least one {role} server")
        groups.append([indexed[i] for i in sorted(indexed)])
    return groups


def timeout_seconds(name, default):
    value = os.environ.get(name, default)
    try:
        seconds = int(value)
    except ValueError:
        raise MaintenanceError(f"{name} must be a positive integer") from None
    if seconds <= 0:
        raise MaintenanceError(f"{name} must be a positive integer")
    return seconds


def poll(server, check, interval, timeout, operation):
    deadline = time.monotonic() + timeout
    while True:
        pending = check()
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MaintenanceError(f"{server.hostname}: timed out waiting for {operation}. {pending}")
        server.log(pending)
        time.sleep(min(interval, remaining))


def containers(server):
    ids = server.run("docker", "container", "ls", "--all", "--quiet").splitlines()
    if not ids:
        return {}
    records = server.run(
        "docker", "container", "inspect", "--format",
        '{"name":{{json .Name}},"state":{{json .State}}}', *ids,
    )
    return {
        record["name"].lstrip("/"): record["state"]
        for record in map(json.loads, records.splitlines())
    }


def preflight(server):
    for path in ("/opt/kasm/bin/stop", "/opt/kasm/bin/start"):
        server.run("test", "-x", path)
    for unit in DOCKER_UNITS:
        status = server.run("systemctl", "show", "--property=LoadState,UnitFileState", unit)
        if "LoadState=loaded" not in status.splitlines() or "UnitFileState=masked" in status:
            raise MaintenanceError(f"{server.hostname}: {unit} must be installed and unmasked")
    names = set(containers(server))
    if not names:
        raise MaintenanceError(f"{server.hostname}: Docker has no containers to verify")
    server.log(f"Preflight passed for {len(names)} containers")


def stop_and_reboot(server, stop_timeout, reboot_timeout):
    # Capture each host's inventory immediately before stopping Kasm.
    server.expected = set(containers(server))
    if not server.expected:
        raise MaintenanceError(f"{server.hostname}: Docker has no containers to verify")
    old_boot = server.run("cat", "/proc/sys/kernel/random/boot_id")
    server.recovery_needed = True
    server.run("/opt/kasm/bin/stop")

    def still_running():
        running = [
            name for name, state in containers(server).items()
            if state["Status"] not in {"created", "exited", "dead"}
        ]
        if running:
            return f"Waiting for {len(running)} running containers to stop"

    poll(server, still_running, STOP_POLL_SECONDS, stop_timeout, "Docker containers to stop")

    # Persistent masks prevent Docker and its socket from activating at boot.
    server.run("systemctl", "mask", *DOCKER_UNITS)
    server.log("Rebooting")
    try:
        server.run("systemctl", "--no-block", "reboot")
    except (ConnectionError, subprocess.TimeoutExpired):
        # SSH can disconnect before returning the reboot command's status.
        pass

    def same_boot():
        try:
            new_boot = server.run("cat", "/proc/sys/kernel/random/boot_id")
        except (ConnectionError, subprocess.TimeoutExpired):
            return "Waiting for SSH after reboot"
        if not new_boot or new_boot == old_boot:
            return "Waiting for a new boot ID"

    poll(server, same_boot, REBOOT_POLL_SECONDS, reboot_timeout, "a completed reboot")
    active_state = server.run("systemctl", "show", "--property=ActiveState", "--value", "docker.service")
    if active_state != "inactive":
        raise MaintenanceError(f"{server.hostname}: Docker is {active_state} after reboot")
    server.log("Reboot verified with Docker inactive")


def start(server, start_timeout):
    server.run("systemctl", "unmask", *DOCKER_UNITS)
    server.run("systemctl", "start", "docker")
    server.run("/opt/kasm/bin/start")

    def unhealthy():
        states = containers(server)
        waiting = [f"{name} (missing)" for name in sorted(server.expected - states.keys())]
        for name, state in sorted(states.items()):
            status = state["Status"]
            health = state.get("Health", {}).get("Status")
            if status != "running":
                waiting.append(f"{name} ({status})")
            elif health is not None and health != "healthy":
                waiting.append(f"{name} ({health})")
        if waiting:
            return "Waiting for " + ", ".join(waiting)

    poll(server, unhealthy, START_POLL_SECONDS, start_timeout, "Docker containers to become ready")
    server.recovery_needed = False
    server.log("All containers are ready")


def main():
    if not USERNAME.fullmatch(os.environ.get("SSH_USER", "")):
        raise MaintenanceError("Set SSH_USER to the remote account with noninteractive sudo")
    groups = inventory()
    stop_timeout = timeout_seconds("KASM_STOP_TIMEOUT_SECONDS", 600)
    reboot_timeout = timeout_seconds("KASM_REBOOT_TIMEOUT_SECONDS", 600)
    start_timeout = timeout_seconds("KASM_START_TIMEOUT_SECONDS", 900)
    servers = [server for group in groups for server in group]

    try:
        for server in servers:
            preflight(server)
        for server in servers:
            stop_and_reboot(server, stop_timeout, reboot_timeout)
        for group in reversed(groups):
            for server in group:
                start(server, start_timeout)
    except (MaintenanceError, subprocess.TimeoutExpired, OSError, ValueError) as error:
        print(f"Maintenance failed: {error}", file=sys.stderr, flush=True)
        affected = [server.hostname for group in reversed(groups)
                    for server in group if server.recovery_needed]
        if affected:
            print("Check these hosts in recovery order: " + ", ".join(affected),
                  file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MaintenanceError as error:
        raise SystemExit(str(error)) from None
