"""Proxy entry point and common SSH, Docker, and maintenance functions."""

import json
import logging
import os
import re
import shlex
import sys
import time
from pathlib import Path

from fabric import Config, Connection
from invoke.exceptions import AuthFailure, CommandTimedOut, ThreadException
from paramiko import RejectPolicy, SSHException

HOST_PATTERNS = {
    "proxy": re.compile(r"PROXY_?([1-9][0-9]*)"),
    "webapp": re.compile(r"WEB_APP_([1-9][0-9]*)"),
    "db": re.compile(r"DB(?:_?([1-9][0-9]*))?"),
}
HOSTNAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*")
USERNAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.-]*")
BOOT_ID = re.compile(r"[0-9a-f-]{36}")
DOCKER_UNITS = ("docker.service", "docker.socket")
KASM_BIN = "/opt/kasm/bin"
STATE_DIR = "/var/lib/kasm-env0-maintenance"
STATE_FILE = f"{STATE_DIR}/state.json"
SUDO_PROMPT = "[kasm-maintenance] sudo password: "

# Pass state as a quoted argument so sudo can use standard input for its password.
WRITE_STATE = r"""
set -eu
umask 077
temporary=$(mktemp "$2/state.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
printf '%s\n' "$1" > "$temporary"
mv -f -- "$temporary" "$3"
"""


def log(level, context, message):
    passwords = {
        value for name, value in os.environ.items()
        if name.endswith("_PASSWORD") and value
    }
    for password in sorted(passwords, key=len, reverse=True):
        message = message.replace(password, "[redacted]")
    logging.log(level, "[%s] %s", context, message)


def inventory():
    groups = {}
    seen = set()
    for role, pattern in HOST_PATTERNS.items():
        indexed = {}
        for name, value in os.environ.items():
            match = pattern.fullmatch(name)
            if not match:
                continue
            host = value.strip()
            user = os.environ.get(f"{name}_USER", os.environ.get("SSH_USER", ""))
            if not HOSTNAME.fullmatch(host) or not USERNAME.fullmatch(user):
                raise ValueError(f"Check the hostname and SSH user for {name}")
            index = int(match[1] or 0)
            host_key = host.lower().rstrip(".")
            if index in indexed or host_key in seen:
                raise ValueError(f"Duplicate host or index in {name}")
            password = os.environ.get(f"{name}_PASSWORD") or None
            sudo_password = os.environ.get(f"{name}_SUDO_PASSWORD", password) or None
            if any(len(secret.encode()) > 1024 or "\r" in secret or "\n" in secret
                   for secret in (password, sudo_password) if secret):
                raise ValueError(f"Invalid password length or newline in credentials for {name}")
            indexed[index] = (host, user, password, sudo_password)
            seen.add(host_key)
        if not indexed:
            raise ValueError(f"Configure at least one {role} server")
        groups[role] = [server for _, server in sorted(indexed.items())]
    return groups


def run_ssh(server, command):
    host, user, password, sudo_password = server
    connect_kwargs = {"banner_timeout": 10, "auth_timeout": 10, "channel_timeout": 10}
    if password is not None:
        connect_kwargs.update(password=password, allow_agent=False, look_for_keys=False)
    config = Config(overrides={"sudo": {"prompt": SUDO_PROMPT}})
    options = dict(hide=True, warn=True, echo=False, pty=False, in_stream=False, timeout=180)

    with Connection(host, user=user, config=config, connect_timeout=10,
                    connect_kwargs=connect_kwargs, forward_agent=False) as connection:
        # Fabric accepts unknown keys by default. Require a verified key instead.
        connection.client.set_missing_host_key_policy(RejectPolicy())
        connection.client.load_system_host_keys()
        system_hosts = Path("/etc/ssh/ssh_known_hosts")
        if system_hosts.is_file():
            connection.client.load_system_host_keys(str(system_hosts))
        try:
            connection.open()
        except Exception:
            connection.client.close()
            raise
        connection.transport.set_keepalive(10)
        quoted_command = shlex.join(command)
        if sudo_password is None:
            return connection.run(f"sudo -n -u root -- {quoted_command}", **options)
        return connection.sudo(f"-- {quoted_command}", password=sudo_password, user="root", **options)


def remote(server, label, *command, capture=False, reconnect=False, allowed_exit=()):
    host = server[0]
    log(logging.INFO, host, label)
    try:
        result = run_ssh(server, command)
    except AuthFailure:
        raise RuntimeError(f"{label}: sudo rejected the password") from None
    except (CommandTimedOut, EOFError, OSError, SSHException, ThreadException) as error:
        detail = str(error) if not isinstance(error, ThreadException) else ", ".join(
            f"{item.type.__name__}: {item.value}" for item in error.exceptions
        )
        message = f"{label}: {type(error).__name__}: {detail}"
        if reconnect and is_reboot_disconnect(error):
            log(logging.WARNING, host, message)
            return None
        raise RuntimeError(message) from None

    stderr = result.stderr.replace(SUDO_PROMPT, "").strip()
    stdout = result.stdout.strip()
    if reconnect and result.exited == -1:
        log(logging.WARNING, host, f"{label}: SSH closed before returning an exit status. {stderr}")
        return None
    if stderr:
        log(logging.ERROR if result.exited else logging.WARNING, host, stderr)
    if stdout and (not capture or result.exited):
        log(logging.ERROR if result.exited else logging.INFO, host, stdout)
    if result.exited and result.exited not in allowed_exit:
        raise RuntimeError(f"{label}: exit {result.exited}")
    log(logging.INFO, host, f"{label}: completed (exit {result.exited})")
    return None if result.exited else stdout


def is_reboot_disconnect(error):
    if isinstance(error, (CommandTimedOut, EOFError, OSError)):
        return True
    if type(error) is SSHException:
        return str(error).startswith(("Error reading SSH protocol banner", "SSH session not active",
                                      "Channel closed."))
    if isinstance(error, ThreadException):
        return all(is_reboot_disconnect(item.value) for item in error.exceptions)
    return False


def timeout_seconds(name, default):
    try:
        seconds = int(os.environ.get(name, default))
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if seconds <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return seconds


def poll(server, check, interval, timeout, operation):
    deadline = time.monotonic() + timeout
    while True:
        pending = check()
        if not pending:
            log(logging.INFO, server[0], f"Verified {operation}")
            return
        delay = min(interval, deadline - time.monotonic())
        if delay <= 0:
            raise RuntimeError(f"Timed out waiting for {operation}. {pending}")
        log(logging.INFO, server[0], f"{pending}. Checking again in {delay:g}s")
        time.sleep(delay)


def containers(server):
    output = remote(server, "List all Docker containers",
                    "docker", "container", "ls", "--all", "--quiet", capture=True)
    ids = output.splitlines()
    if not ids:
        return {}
    records = remote(
        server, "Inspect Docker container states",
        "docker", "container", "inspect", "--format",
        '{"name":{{json .Name}},"state":{{json .State}}}', *ids, capture=True,
    )
    return {
        record["name"].lstrip("/"): record["state"]
        for record in map(json.loads, records.splitlines())
    }


def read_boot_id(server, reconnect=False):
    boot_id = remote(server, "Read boot ID", "cat", "/proc/sys/kernel/random/boot_id",
                     capture=True, reconnect=reconnect)
    if boot_id is not None and not BOOT_ID.fullmatch(boot_id):
        raise RuntimeError("Host returned an invalid boot ID")
    return boot_id


def read_state(server):
    present = remote(server, "Check for a saved maintenance state", "test", "-f", STATE_FILE,
                     allowed_exit=(1,))
    if present is None:
        return None
    raw = remote(server, "Read saved maintenance state", "cat", STATE_FILE, capture=True)
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError("Invalid saved maintenance state")
    boot_id = state.get("boot_id")
    names = state.get("containers")
    if not (isinstance(boot_id, str) and BOOT_ID.fullmatch(boot_id)
            and isinstance(names, list) and names
            and all(isinstance(name, str) and name for name in names)
            and isinstance(state.get("complete"), bool)):
        raise ValueError("Invalid saved maintenance state")
    return state


def save_state(server, state):
    remote(server, "Prepare maintenance state directory",
           "install", "-d", "-m", "0700", STATE_DIR)
    remote(server, "Save maintenance state", "sh", "-c", WRITE_STATE, "write-state",
           json.dumps(state), STATE_DIR, STATE_FILE)


def verify_docker_inactive(server):
    for unit in DOCKER_UNITS:
        status = remote(
            server, f"Verify {unit} is inactive and masked",
            "systemctl", "show", "--property=ActiveState,UnitFileState", unit, capture=True,
        )
        if not {"ActiveState=inactive", "UnitFileState=masked"}.issubset(status.splitlines()):
            raise RuntimeError(f"{unit} must be inactive and masked after reboot")


def stop(server, stop_timeout, reboot_timeout):
    current_boot = read_boot_id(server)
    previous = read_state(server)
    if previous and not previous["complete"]:
        if current_boot == previous["boot_id"]:
            raise RuntimeError("An interrupted shutdown needs recovery before retrying")
        verify_docker_inactive(server)
        log(logging.INFO, server[0], "Earlier shutdown already rebooted this host")
        return

    for action in ("stop", "start"):
        remote(server, f"Check Kasm {action} script", "test", "-x", f"{KASM_BIN}/{action}")
    for unit in DOCKER_UNITS:
        status = remote(server, f"Check {unit}", "systemctl", "show",
                        "--property=LoadState,UnitFileState", unit, capture=True)
        if "LoadState=loaded" not in status.splitlines() or "UnitFileState=masked" in status:
            raise RuntimeError(f"{unit} must be installed and unmasked")
    names = sorted(containers(server))
    if not names:
        raise RuntimeError("Docker has no containers to verify")
    save_state(server, {"boot_id": current_boot, "containers": names, "complete": False})
    remote(server, "Stop Kasm", f"{KASM_BIN}/stop")

    def still_running():
        active = [
            f"{name} ({state['Status']})" for name, state in sorted(containers(server).items())
            if state["Status"] not in {"created", "exited", "dead"}
        ]
        return "Containers still active: " + ", ".join(active) if active else None

    poll(server, still_running, 20, stop_timeout, "all containers have stopped")
    remote(server, "Mask Docker across reboot", "systemctl", "mask", *DOCKER_UNITS)
    remote(server, "Request reboot", "systemctl", "--no-block", "reboot", reconnect=True)

    def waiting_for_reboot():
        boot_id = read_boot_id(server, reconnect=True)
        if boot_id is None:
            return "Waiting for SSH after reboot"
        if boot_id == current_boot:
            return "Waiting for the boot ID to change"
        return None

    poll(server, waiting_for_reboot, 10, reboot_timeout, "host has rebooted")
    verify_docker_inactive(server)


def start(server, start_timeout):
    state = read_state(server)
    if state is None or read_boot_id(server) == state["boot_id"]:
        raise RuntimeError("Startup requires a saved shutdown and a verified reboot")
    expected = set(state["containers"])
    if not state["complete"]:
        remote(server, "Unmask Docker", "systemctl", "unmask", *DOCKER_UNITS)
        remote(server, "Start Docker", "systemctl", "start", "docker")
        remote(server, "Start Kasm", f"{KASM_BIN}/start")

    def unhealthy():
        states = containers(server)
        waiting = [f"{name} (missing)" for name in sorted(expected - states.keys())]
        for name, status in sorted(states.items()):
            health = status.get("Health", {}).get("Status")
            if status["Status"] != "running":
                waiting.append(f"{name} ({status['Status']})")
            elif health is not None and health != "healthy":
                waiting.append(f"{name} ({health})")
        return "Containers not ready: " + ", ".join(waiting) if waiting else None

    poll(server, unhealthy, 30, start_timeout, "all containers are ready")
    if not state["complete"]:
        state["complete"] = True
        save_state(server, state)


def main(role):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)
    context = role
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"stop", "start"}:
            raise ValueError(f"Usage: python3 {sys.argv[0]} stop|start")
        action = sys.argv[1]
        context = f"{role} {action}"
        servers = inventory()[role]
        stop_timeout = timeout_seconds("KASM_STOP_TIMEOUT_SECONDS", 600)
        reboot_timeout = timeout_seconds("KASM_REBOOT_TIMEOUT_SECONDS", 600)
        start_timeout = timeout_seconds("KASM_START_TIMEOUT_SECONDS", 900)
        log(logging.INFO, context, f"Stage started for {len(servers)} hosts")
        for server in servers:
            context = f"{role} {action} {server[0]}"
            log(logging.INFO, context, "Host started")
            if action == "stop":
                stop(server, stop_timeout, reboot_timeout)
            else:
                start(server, start_timeout)
            log(logging.INFO, context, "Host completed")
        log(logging.INFO, f"{role} {action}", "Stage completed")
        return 0
    except KeyboardInterrupt:
        log(logging.ERROR, context, "Stage interrupted. Check maintenance state before retrying.")
        return 130
    except Exception as error:
        log(logging.ERROR, context, f"{type(error).__name__}: {error}")
        log(logging.ERROR, context, "Stage failed. Check the affected host before retrying.")
        return 1


if __name__ == "__main__":
    sys.exit(main("proxy"))
