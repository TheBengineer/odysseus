"""
container_orchestrator.py

MCP server for remote OpenCode control via SSH tunnels. Provides SSH
tunnel management to bootstrap and control remote OpenCode serve
instances, enabling Odysseus agents to orchestrate workloads across a
cluster of machines.

Configuration is read from data/container_orch.yaml and persisted
atomically using core/atomic_io.py patterns.

Config schema (data/container_orch.yaml):
    hosts:
      - name: "dev-box"              # Friendly host alias
        host: "192.168.1.100"        # SSH hostname or IP
        port: 22                     # SSH port (default: 22)
        user: "dev"                  # SSH user
        key_path: "data/ssh/id_ed25519"  # SSH identity file (default if omitted)
        opencode_port: 4096          # Remote opencode serve port to tunnel to
        local_tunnel_port: 14096     # Local tunnel mapping (auto-assigned if omitted)
        description: "Development server"
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__: list[str] = [
    'validate_hostname',
    'validate_port',
    'validate_name',
    'validate_key_path',
    'sanitize_for_subprocess',
    'validate_host_config',
    'ConfigManager',
]

# ── Input validation ──────────────────────────────────────────────────────────

# Shell metacharacters that must be rejected in most inputs
_SHELL_META = re.compile(r'[;|&$`(){}\n\r<>]')

# DNS hostname pattern (RFC 952 relaxed)
_HOSTNAME_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$')

# Friendly-name pattern: alphanumeric, underscore, hyphen, space
_NAME_RE = re.compile(r'^[a-zA-Z0-9_\-\s]{2,64}$')


def _has_shell_metachars(value: str) -> bool:
    """Return True if *value* contains any shell metacharacter."""
    return bool(_SHELL_META.search(value))


def validate_hostname(name: str) -> bool:
    """Return True if *name* is a valid hostname or IP address.

    Accepts RFC 952 DNS names, IPv4, and IPv6 addresses.
    Rejects empty strings, non-strings, and shell metacharacters.
    """
    if not isinstance(name, str) or not name:
        return False
    if _has_shell_metachars(name):
        return False
    # Check DNS hostname pattern
    if _HOSTNAME_RE.match(name):
        return True
    # Fall back to IPv4 / IPv6 parsing
    import ipaddress
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def validate_port(port: int) -> bool:
    """Return True if *port* is a valid TCP/UDP port (1-65535)."""
    return isinstance(port, int) and 1 <= port <= 65535


def validate_name(name: str) -> bool:
    """Return True if *name* is a valid friendly name (2-64 chars).

    Allowed characters: alphanumeric, underscore, hyphen, space.
    Rejects shell metacharacters and empty strings.
    """
    if not isinstance(name, str) or not name:
        return False
    if _has_shell_metachars(name):
        return False
    return bool(_NAME_RE.match(name))


def validate_key_path(path: str) -> bool:
    """Return True if *path* is a valid filesystem path (no shell meta)."""
    if not isinstance(path, str) or not path:
        return False
    if _has_shell_metachars(path):
        return False
    if '\x00' in path:
        return False
    if not path.strip():
        return False
    return True


def sanitize_for_subprocess(value: str) -> str:
    """Strip control characters and shell metacharacters from *value*.

    Removes ASCII control chars (0x00-0x1F except tab 0x09),
    DEL (0x7F), and all shell metacharacters.
    """
    if not isinstance(value, str):
        return ''
    result = re.sub(r'[\x00-\x08\x0a-\x1f\x7f]', '', value)
    result = _SHELL_META.sub('', result)
    return result


def validate_host_config(config: dict) -> list[str]:
    """Validate a full host configuration dictionary.

    Returns a list of error messages (empty list = valid).

    Required fields: ``name``, ``host``, ``port``, ``user``
    Optional fields: ``key_path``, ``opencode_port``, ``local_tunnel_port``, ``description``
    """
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["config must be a dictionary"]

    # -- name (required) --
    name = config.get('name')
    if name is None:
        errors.append("'name' is required")
    elif not isinstance(name, str):
        errors.append("'name' must be a string")
    elif not validate_name(name):
        errors.append("'name' must be 2-64 characters (alphanumeric, underscore, hyphen, space)")

    # -- host (required) --
    host = config.get('host')
    if host is None:
        errors.append("'host' is required")
    elif not isinstance(host, str):
        errors.append("'host' must be a string")
    elif not validate_hostname(host):
        errors.append("'host' is not a valid hostname or IP address")

    # -- port (required) --
    port = config.get('port')
    if port is None:
        errors.append("'port' is required")
    elif not validate_port(port):
        errors.append("'port' must be an integer between 1 and 65535")

    # -- user (required) --
    user = config.get('user')
    if user is None:
        errors.append("'user' is required")
    elif not isinstance(user, str):
        errors.append("'user' must be a string")
    elif not user.strip():
        errors.append("'user' must not be empty")
    elif _has_shell_metachars(user):
        errors.append("'user' contains shell metacharacters")

    # -- key_path (optional) --
    key_path = config.get('key_path')
    if key_path is not None:
        if not isinstance(key_path, str):
            errors.append("'key_path' must be a string")
        elif not validate_key_path(key_path):
            errors.append("'key_path' is not a valid file path")

    # -- opencode_port (optional) --
    opencode_port = config.get('opencode_port')
    if opencode_port is not None and not validate_port(opencode_port):
        errors.append("'opencode_port' must be an integer between 1 and 65535")

    # -- local_tunnel_port (optional) --
    local_tunnel_port = config.get('local_tunnel_port')
    if local_tunnel_port is not None and not validate_port(local_tunnel_port):
        errors.append("'local_tunnel_port' must be an integer between 1 and 65535")

    # -- description (optional) --
    description = config.get('description')
    if description is not None:
        if not isinstance(description, str):
            errors.append("'description' must be a string")
        elif _has_shell_metachars(description):
            errors.append("'description' contains shell metacharacters")

    return errors


class ConfigManager:
    """Read/write YAML host configuration files atomically.

    Configuration is persisted as ``data/container_orch.yaml`` (by default)
    with the schema documented at the top of this module.
    """

    def __init__(self, path: str | Path = "data/container_orch.yaml") -> None:
        self.path = Path(path)

    # ── internal helpers ──────────────────────────────────────────────────

    def _ensure_config(self) -> dict:
        """Load and return the parsed config dict.

        If the file does not exist, create it with ``{"hosts": []}`` and
        return that default.
        """
        try:
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if "hosts" not in data:
                    data["hosts"] = []
                return data
        except FileNotFoundError:
            self._write({"hosts": []})
            return {"hosts": []}

    def _write(self, data: dict) -> None:
        """Atomically write *data* to the YAML file.

        Writes to a ``.tmp`` sibling first, fsyncs, then ``os.rename`` into
        place so a crash mid-write never leaves a truncated file.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, str(self.path))

    # ── public API ────────────────────────────────────────────────────────

    def load(self) -> dict:
        """Parse YAML, validate every host entry, and return the config dict.

        Raises ``ValueError`` if any host fails validation.
        """
        data = self._ensure_config()
        hosts = data.get("hosts", [])
        if not isinstance(hosts, list):
            raise ValueError("'hosts' must be a list")
        for entry in hosts:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"Each host must be a dictionary, got {type(entry).__name__}"
                )
            errors = validate_host_config(entry)
            if errors:
                raise ValueError(
                    f"Validation failed for host '{entry.get('name', '?')}': "
                    f"{'; '.join(errors)}"
                )
        return data

    def save(self, hosts: list[dict]) -> None:
        for entry in hosts:
            errors = validate_host_config(entry)
            if errors:
                raise ValueError(
                    f"Validation failed for host '{entry.get('name', '?')}': "
                    f"{'; '.join(errors)}"
                )
        self._write({"hosts": hosts})

    def add_host(
        self,
        name: str,
        host: str,
        port: int,
        user: str,
        key_path: str | None = None,
        opencode_port: int = 4096,
        local_tunnel_port: int | None = None,
        description: str = "",
    ) -> None:
        """Validate and add a new host entry, then persist.

        Defaults
        --------
        * ``key_path`` → ``data/ssh/id_ed25519`` when omitted.
        * ``local_tunnel_port`` → auto-assigned as ``14096 + current_host_count``
          when omitted (so the first host gets 14096, the second 14097, …).
        """
        if key_path is None:
            key_path = "data/ssh/id_ed25519"
        if local_tunnel_port is None:
            existing = self.list_hosts()
            local_tunnel_port = 14096 + len(existing)

        config: dict[str, Any] = {
            "name": name,
            "host": host,
            "port": port,
            "user": user,
            "key_path": key_path,
            "opencode_port": opencode_port,
            "local_tunnel_port": local_tunnel_port,
            "description": description,
        }

        errors = validate_host_config(config)
        if errors:
            raise ValueError(f"Invalid host config: {'; '.join(errors)}")

        existing = self.list_hosts()
        if any(h.get("name") == name for h in existing):
            raise ValueError(f"Host '{name}' already exists")

        existing.append(config)
        self.save(existing)

    def remove_host(self, name: str) -> bool:
        """Remove a host by *name*.

        Returns ``True`` if the host was found and removed, ``False`` if no
        host with that name existed.
        """
        hosts = self.list_hosts()
        filtered = [h for h in hosts if h.get("name") != name]
        if len(filtered) == len(hosts):
            return False
        self.save(filtered)
        return True

    def list_hosts(self) -> list[dict]:
        return self._ensure_config().get("hosts", [])

    def get_host(self, name: str) -> dict | None:
        for host in self.list_hosts():
            if host.get("name") == name:
                return host
        return None
