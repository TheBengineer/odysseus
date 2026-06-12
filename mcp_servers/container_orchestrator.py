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
import base64
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

__all__: list[str] = [
    'validate_hostname',
    'validate_port',
    'validate_name',
    'validate_key_path',
    'sanitize_for_subprocess',
    'validate_host_config',
    'ConfigManager',
    'SshManager',
    'OpencodeError',
    'OpencodeClient',
    'HostConfig',
    'ConnectionState',
    'SessionInfo',
]

# ── Dataclass types ───────────────────────────────────────────────────────────


@dataclass
class HostConfig:
    name: str
    host: str
    port: int
    user: str
    key_path: str = "data/ssh/id_ed25519"
    opencode_port: int = 4096
    local_tunnel_port: int | None = None  # None = auto-assign
    description: str = ""

    def __post_init__(self):
        errors = validate_host_config({
            "name": self.name, "host": self.host, "port": self.port,
            "user": self.user, "key_path": self.key_path,
            "opencode_port": self.opencode_port,
            "local_tunnel_port": self.local_tunnel_port,
            "description": self.description,
        })
        if errors:
            raise ValueError(f"Invalid HostConfig: {'; '.join(errors)}")


@dataclass
class ConnectionState:
    name: str
    local_port: int
    remote_host: str
    pid: int = 0
    status: str = "disconnected"  # connected|disconnected|error
    error_message: str = ""


@dataclass
class SessionInfo:
    session_id: str
    title: str = ""
    created_at: str = ""
    status: str = "active"  # active|completed|failed


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


class SshManager:
    """Manage async SSH tunnel lifecycle for remote OpenCode hosts.

    Connects to remote hosts via SSH, maintains persistent tunnels,
    and provides health checks. All subprocess invocations use
    :func:`asyncio.create_subprocess_exec` with a list of arguments
    (never a shell string) to prevent injection.
    """

    def __init__(self, config_dir: str) -> None:
        #: Path to the known_hosts file for SSH connections.
        self._known_hosts = str(Path(config_dir) / "known_hosts")
        #: In-memory store of active tunnel connections.
        #: ``{name: {"process": ..., "local_port": ..., ...}}``
        self._connections: dict[str, dict[str, Any]] = {}

    # ── public API ────────────────────────────────────────────────────────────

    async def connect(self, config: dict) -> dict:
        """Establish an SSH tunnel to the remote host defined in *config*.

        Validates the configuration, starts the SSH process, waits for the
        tunnel to become live (via ``ss -tlnp``), and verifies that OpenCode
        responds on the tunneled port.

        Parameters
        ----------
        config : dict
            Host configuration with keys: ``name``, ``host``, ``port``,
            ``user``, ``key_path``, ``opencode_port``, ``local_tunnel_port``.

        Returns
        -------
        dict
            ``{"success": True, "name": str, "local_port": int, "message": str}``
            on success, or ``{"success": False, "name": str, "message": str}``
            on failure.
        """
        # Validate config first
        errors = validate_host_config(config)
        if errors:
            return {
                "success": False,
                "name": config.get("name", "?"),
                "message": f"Invalid config: {'; '.join(errors)}",
            }

        name = config["name"]
        host = config["host"]
        port = config["port"]
        user = config["user"]
        key_path = config.get("key_path", "data/ssh/id_ed25519")
        opencode_port = config.get("opencode_port", 4096)
        local_port = config.get("local_tunnel_port", 0)

        # Reject duplicate connection name
        if name in self._connections:
            return {
                "success": False,
                "name": name,
                "message": f"Connection '{name}' already exists",
            }

        # Sanitize user-supplied values for subprocess safety
        safe_host = sanitize_for_subprocess(host)
        safe_user = sanitize_for_subprocess(user)
        safe_key = sanitize_for_subprocess(key_path)
        safe_known_hosts = sanitize_for_subprocess(self._known_hosts)

        # Build SSH command as a list (never a shell string)
        cmd = [
            "ssh", "-N",
            "-L", f"127.0.0.1:{local_port}:127.0.0.1:{opencode_port}",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={safe_known_hosts}",
            "-i", safe_key,
            "-p", str(port),
            f"{safe_user}@{safe_host}",
        ]

        # Launch the SSH process
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            return {
                "success": False,
                "name": name,
                "message": f"Failed to start SSH process: {exc}",
            }

        # Track immediately so we can clean up on failure
        self._connections[name] = {
            "process": proc,
            "local_port": local_port,
            "host": host,
            "user": user,
            "opencode_port": opencode_port,
        }

        # Wait for the tunnel port to appear as listening
        tunnel_ok = await self._wait_for_tunnel(local_port, timeout=5)
        if not tunnel_ok:
            # Capture stderr for diagnostics
            stderr_text = ""
            try:
                _, stderr_data = await asyncio.wait_for(
                    proc.communicate(), timeout=3.0
                )
                stderr_text = stderr_data.decode(errors="replace")
            except asyncio.TimeoutError:
                pass

            await self._kill_process(proc)

            # Map common SSH errors to user-friendly messages
            msg = "SSH tunnel did not establish within 5s"
            if not stderr_text:
                msg += " (no error output from SSH)"
            else:
                lower = stderr_text.lower()
                if "permission denied" in lower:
                    msg = "SSH permission denied -- check key_path and user"
                elif "connection refused" in lower:
                    msg = "SSH connection refused -- check host and port"
                elif "name or service not known" in lower:
                    msg = "Hostname resolution failed -- check host"
                else:
                    msg = f"SSH error: {stderr_text[:200].strip()}"

            del self._connections[name]
            return {"success": False, "name": name, "message": msg}

        # Verify OpenCode responds through the tunnel
        opencode_ok = await self._check_opencode_ready(local_port, timeout=10.0)
        if not opencode_ok:
            await self._kill_process(proc)
            del self._connections[name]
            return {
                "success": False,
                "name": name,
                "message": (
                    f"Tunnel is up but OpenCode did not respond on "
                    f"http://127.0.0.1:{local_port}/doc"
                ),
            }

        return {
            "success": True,
            "name": name,
            "local_port": local_port,
            "message": (
                f"Connected to {host}:{opencode_port} via "
                f"127.0.0.1:{local_port}"
            ),
        }

    async def disconnect(self, name: str) -> bool:
        """Kill the SSH process for *name* and verify the port is freed.

        Returns ``True`` if the connection was found and terminated,
        ``False`` if *name* is not tracked.
        """
        conn = self._connections.get(name)
        if conn is None:
            return False

        proc = conn["process"]
        local_port = conn["local_port"]

        await self._kill_process(proc)
        await self._wait_for_port_free(local_port, timeout=3)

        del self._connections[name]
        return True

    async def disconnect_all(self) -> None:
        """Terminate all active SSH tunnels."""
        for name in list(self._connections):
            await self.disconnect(name)

    async def status(self, name: str) -> dict:
        """Return a status dictionary for the tunnel named *name*.

        Checks both the process liveness and whether the tunnel port is
        still listening.
        """
        conn = self._connections.get(name)
        if conn is None:
            return {"name": name, "connected": False, "error": "not found"}

        proc = conn["process"]
        process_alive = proc.returncode is None
        port_open = await self._is_port_listening(conn["local_port"])

        return {
            "name": name,
            "connected": process_alive and port_open,
            "process_alive": process_alive,
            "port_open": port_open,
            "local_port": conn["local_port"],
            "host": conn["host"],
            "user": conn["user"],
            "remote_port": conn["opencode_port"],
        }

    def list_connections(self) -> list[str]:
        """Return the names of all currently tracked connections."""
        return list(self._connections.keys())

    # ── internal helpers ──────────────────────────────────────────────────────

    async def _wait_for_tunnel(self, local_port: int, timeout: int = 5) -> bool:
        """Poll ``ss -tlnp`` until *local_port* appears as listening.

        Returns ``True`` if the port is detected within *timeout* seconds.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if await self._is_port_listening(local_port):
                return True
            await asyncio.sleep(0.3)
        return False

    async def _wait_for_port_free(self, local_port: int, timeout: int = 3) -> bool:
        """Poll ``ss -tlnp`` until *local_port* is no longer listening.

        Returns ``True`` if the port is freed within *timeout* seconds.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if not await self._is_port_listening(local_port):
                return True
            await asyncio.sleep(0.3)
        return False

    async def _is_port_listening(self, local_port: int) -> bool:
        """Return ``True`` if *local_port* appears in ``ss -tlnp`` output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ss", "-tlnp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            output = stdout.decode(errors="replace")
            return f":{local_port}" in output
        except (OSError, asyncio.TimeoutError):
            return False

    async def _check_opencode_ready(self, port: int, timeout: float = 10.0) -> bool:
        """Send ``GET /doc`` to verify OpenCode responds through the tunnel."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=timeout,
            )
            request = (
                b"GET /doc HTTP/1.0\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(1024), timeout=5.0)
            writer.close()
            await writer.wait_closed()
            return response.startswith(b"HTTP/1.")
        except (OSError, asyncio.TimeoutError, ConnectionError):
            return False

    async def _kill_process(self, proc: asyncio.subprocess.Process) -> None:
        """Send SIGTERM, wait 2 s, then SIGKILL if still alive.

        No-op if the process has already exited.
        """
        if proc.returncode is not None:
            return

        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                proc.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()


class OpencodeError(Exception):
    """Raised when an OpenCode API request fails at the HTTP or transport layer.

    Carries a human-readable ``message`` and the originating exception as
    ``__cause__`` when applicable.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class OpencodeClient:
    """HTTP client for a remote OpenCode *serve* instance.

    Communicates with an OpenCode server exposed via SSH tunnel or direct
    network access.  All public methods are async and use ``httpx`` under
    the hood.

    Parameters
    ----------
    base_url : str
        Root URL of the remote OpenCode instance, e.g. ``http://127.0.0.1:14096``.
    password : str | None
        Password for ``Authorization: Basic`` header.  Sent as
        ``opencode:<password>`` base64-encoded.  When ``None``, no auth header
        is added.
    """

    def __init__(self, base_url: str, password: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._password = password
        self._auth_header: dict[str, str] = {}
        if password:
            encoded = base64.b64encode(f"opencode:{password}".encode()).decode()
            self._auth_header = {"Authorization": f"Basic {encoded}"}

    # ── public API ────────────────────────────────────────────────────────────

    async def health(self) -> bool:
        """Return ``True`` if the remote OpenCode instance is reachable.

        Sends ``GET /doc`` and asserts the response status is 200.
        """
        try:
            async with self._client() as client:
                resp = await client.get("/doc", timeout=httpx.Timeout(10.0, connect=10.0))
                return resp.status_code == 200
        except httpx.ConnectError:
            return False
        except httpx.TimeoutException:
            return False

    async def list_sessions(self) -> list[dict]:
        """Return all active sessions from the remote instance.

        Returns
        -------
        list[dict]
            List of session dictionaries, or a list containing a single
            ``{"error": "..."}`` dict on failure.
        """
        return await self._request("GET", "/sessions")

    async def create_session(self, title: str, project: str) -> dict:
        """Create a new session on the remote instance.

        Parameters
        ----------
        title : str
            Session title.
        project : str
            Project path the session belongs to.

        Returns
        -------
        dict
            The created session object, or ``{"error": "..."}`` on failure.
        """
        return await self._request("POST", "/sessions", json={"title": title, "project": project})

    async def send_prompt(self, session_id: str, text: str) -> str:
        """Send a prompt to an existing session and return the response text.

        Parameters
        ----------
        session_id : str
            ID of the target session.
        text : str
            Prompt text to send.

        Returns
        -------
        str
            The response body as a string, or a JSON string
            ``{"error": "..."}`` on failure.
        """
        try:
            async with self._client() as client:
                url = f"/sessions/{session_id}/prompt"
                resp = await client.post(
                    url,
                    json={"text": text},
                    headers=self._auth_header,
                    timeout=httpx.Timeout(120.0, connect=10.0),
                )
                resp.raise_for_status()
                return resp.text
        except httpx.ConnectError:
            return '{"error": "Connection refused — is the remote OpenCode running?"}'
        except httpx.TimeoutException:
            return '{"error": "Request timed out"}'
        except httpx.HTTPStatusError as exc:
            return self._handle_http_error(exc)

    async def stop_session(self, session_id: str) -> bool:
        """Cancel a running prompt on the remote session.

        Returns ``True`` if the cancellation was accepted (HTTP 2xx).
        """
        result = await self._request("POST", f"/sessions/{session_id}/cancel")
        if isinstance(result, dict) and "error" in result:
            return False
        return True

    async def get_session(self, session_id: str) -> dict:
        """Retrieve a single session by ID.

        Returns
        -------
        dict
            The session object, or ``{"error": "..."}`` on failure.
        """
        return await self._request("GET", f"/sessions/{session_id}")

    # ── internal helpers ──────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        """Return an ``httpx.AsyncClient`` configured with auth headers."""
        return httpx.AsyncClient(base_url=self.base_url, headers=self._auth_header)

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        """Low-level HTTP request helper with consistent error handling.

        Parameters
        ----------
        method : str
            HTTP method (``GET``, ``POST``, …).
        path : str
            URL path relative to ``base_url``.
        **kwargs
            Extra arguments forwarded to ``httpx.AsyncClient.request``.

        Returns
        -------
        Any
            Parsed JSON response, or ``{"error": "..."}`` on failure.
        """
        try:
            async with self._client() as client:
                # Set a sensible default timeout if not overridden
                kwargs.setdefault("timeout", httpx.Timeout(30.0, connect=10.0))
                resp = await client.request(method, path, **kwargs)
                resp.raise_for_status()
                return resp.json()
        except httpx.ConnectError:
            return {"error": "Connection refused — is the remote OpenCode running?"}
        except httpx.TimeoutException:
            return {"error": "Request timed out"}
        except httpx.HTTPStatusError as exc:
            return self._handle_http_error(exc)

    def _handle_http_error(self, exc: httpx.HTTPStatusError) -> dict:
        """Map HTTP status codes to user-facing error messages.

        Parameters
        ----------
        exc : httpx.HTTPStatusError
            The caught HTTP error with ``response`` and ``request``.

        Returns
        -------
        dict
            An ``{"error": "..."}`` dict appropriate for the status code.
        """
        status = exc.response.status_code
        if status in (401, 403):
            return {"error": "Authentication failed — check password"}
        if status == 404:
            return {"error": "Session not found"}
        if status >= 500:
            return {"error": f"Remote server error (HTTP {status})"}
        return {"error": f"HTTP {status}: {exc.response.text[:200]}"}
