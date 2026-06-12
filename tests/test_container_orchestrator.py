"""Tests for ``mcp_servers.container_orchestrator.ConfigManager``.

``ConfigManager`` manages a YAML host configuration file with atomic writes.
These tests exercise all public methods (``add_host``, ``remove_host``,
``list_hosts``, ``get_host``, ``load``, ``save``) against real temp files.

Schema reference (data/container_orch.yaml):
    hosts:
      - name: "dev-box"              # Friendly host alias
        host: "192.168.1.100"        # SSH hostname or IP
        port: 22                     # SSH port
        user: "dev"                  # SSH user
        key_path: "data/ssh/id_ed25519"  # SSH identity file
        opencode_port: 4096          # Remote opencode serve port
        local_tunnel_port: 14096     # Local tunnel mapping
        description: "Development server"
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock, patch

import httpx
import pytest
import yaml

# Load container_orchestrator.py directly to avoid pulling in the full
# mcp_servers package and its heavy MCP/httpx startup dependencies.
ROOT = Path(__file__).resolve().parents[1]
ORCH_PATH = ROOT / "mcp_servers" / "container_orchestrator.py"

import importlib.util  # noqa: E402 (import after path def)

# Register the module in sys.modules BEFORE exec_module so that @dataclass
# (which looks up cls.__module__ in sys.modules) does not fail.
MODNAME = "_container_orch_under_test"
_spec = importlib.util.spec_from_file_location(MODNAME, ORCH_PATH)
_orch = importlib.util.module_from_spec(_spec)
sys.modules[MODNAME] = _orch
_spec.loader.exec_module(_orch)

ConfigManager = _orch.ConfigManager
OpencodeClient = _orch.OpencodeClient
OpencodeError = _orch.OpencodeError
SshManager = _orch.SshManager
validate_host_config = _orch.validate_host_config


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def cfg(tmp_path: Path) -> ConfigManager:
    """Return a ``ConfigManager`` backed by a temp YAML file."""
    return ConfigManager(path=str(tmp_path / "container_orch.yaml"))


def _read_yaml(path: Path) -> dict:
    """Read the YAML file at *path* and return the parsed dict."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_A_VALID_HOST = dict(
    name="dev-box",
    host="192.168.1.100",
    port=22,
    user="dev",
    key_path="data/ssh/id_ed25519",
    opencode_port=4096,
    local_tunnel_port=14096,
    description="Development server",
)

# ── Auto-create ───────────────────────────────────────────────────────────────


class TestAutoCreate:
    """``ConfigManager`` creates the config file on first access."""

    def test_auto_create_on_list(self, tmp_path: Path) -> None:
        """``list_hosts`` on a missing file creates it with an empty host list."""
        path = tmp_path / "does_not_exist.yaml"
        assert not path.exists()

        cm = ConfigManager(path=str(path))
        hosts = cm.list_hosts()

        assert hosts == []
        assert path.exists()
        data = _read_yaml(path)
        assert data == {"hosts": []}

    def test_auto_create_on_add(self, tmp_path: Path) -> None:
        """``add_host`` creates the file when it does not exist."""
        path = tmp_path / "fresh.yaml"
        cm = ConfigManager(path=str(path))

        cm.add_host(**_A_VALID_HOST)
        hosts = cm.list_hosts()
        assert len(hosts) == 1
        assert hosts[0]["name"] == "dev-box"


# ── add_host ──────────────────────────────────────────────────────────────────


class TestAddHost:
    """Adding hosts — success and error paths."""

    def test_add_host(self, cfg: ConfigManager) -> None:
        """A valid host is persisted and retrievable."""
        cfg.add_host(**_A_VALID_HOST)

        hosts = cfg.list_hosts()
        assert len(hosts) == 1
        entry = hosts[0]
        assert entry["name"] == "dev-box"
        assert entry["host"] == "192.168.1.100"
        assert entry["port"] == 22
        assert entry["user"] == "dev"
        assert entry["key_path"] == "data/ssh/id_ed25519"
        assert entry["opencode_port"] == 4096
        assert entry["local_tunnel_port"] == 14096
        assert entry["description"] == "Development server"

    def test_add_host_invalid_name(self, cfg: ConfigManager) -> None:
        """A host with an invalid name raises ``ValueError``."""
        with pytest.raises(ValueError, match="Invalid host config"):
            cfg.add_host(
                **{**_A_VALID_HOST, "name": "x"}  # too short (1 char, min 2)
            )

    def test_add_host_invalid_port(self, cfg: ConfigManager) -> None:
        """A host with an out-of-range port raises ``ValueError``."""
        with pytest.raises(ValueError, match="Invalid host config"):
            cfg.add_host(
                **{**_A_VALID_HOST, "port": 0}  # 0 is not a valid port
            )

    def test_add_host_invalid_hostname(self, cfg: ConfigManager) -> None:
        """A host with shell metacharacters in hostname raises ``ValueError``."""
        with pytest.raises(ValueError, match="Invalid host config"):
            cfg.add_host(
                **{**_A_VALID_HOST, "host": "foo; rm -rf /"}
            )

    def test_duplicate_host(self, cfg: ConfigManager) -> None:
        """Adding a host with the same name twice raises ``ValueError``."""
        cfg.add_host(**_A_VALID_HOST)
        with pytest.raises(ValueError, match="already exists"):
            cfg.add_host(**_A_VALID_HOST)

    def test_duplicate_host_different_case(self, cfg: ConfigManager) -> None:
        """Host name matching is case-sensitive, so ``Dev-Box`` is distinct."""
        cfg.add_host(**_A_VALID_HOST)
        different = {**_A_VALID_HOST, "name": "Dev-Box"}
        cfg.add_host(**different)  # should not raise
        assert len(cfg.list_hosts()) == 2


# ── remove_host ───────────────────────────────────────────────────────────────


class TestRemoveHost:
    """Removing hosts — success and error paths."""

    def test_remove_host(self, cfg: ConfigManager) -> None:
        """An existing host can be removed."""
        cfg.add_host(**_A_VALID_HOST)
        cfg.add_host(name="backup", host="10.0.0.2", port=22, user="ops")

        result = cfg.remove_host("dev-box")

        assert result is True
        hosts = cfg.list_hosts()
        assert len(hosts) == 1
        assert hosts[0]["name"] == "backup"

    def test_remove_host_nonexistent(self, cfg: ConfigManager) -> None:
        """Removing a non-existent host returns ``False``."""
        result = cfg.remove_host("ghost")
        assert result is False

    def test_remove_host_from_empty(self, cfg: ConfigManager) -> None:
        """Removing from an empty config returns ``False``."""
        result = cfg.remove_host("nobody")
        assert result is False

    def test_remove_host_persists(self, cfg: ConfigManager) -> None:
        """After removal the YAML file reflects the change on re-read."""
        cfg.add_host(**_A_VALID_HOST)
        cfg.remove_host("dev-box")

        fresh = ConfigManager(path=str(cfg.path))
        assert fresh.list_hosts() == []


# ── list_hosts ────────────────────────────────────────────────────────────────


class TestListHosts:
    """Listing hosts."""

    def test_list_hosts_empty(self, cfg: ConfigManager) -> None:
        """``list_hosts`` returns an empty list when no hosts exist."""
        assert cfg.list_hosts() == []

    def test_list_hosts_multiple(self, cfg: ConfigManager) -> None:
        """``list_hosts`` returns all added hosts."""
        cfg.add_host(**_A_VALID_HOST)
        cfg.add_host(name="backup", host="10.0.0.2", port=22, user="ops")

        hosts = cfg.list_hosts()
        assert len(hosts) == 2
        names = [h["name"] for h in hosts]
        assert names == ["dev-box", "backup"]


# ── get_host ──────────────────────────────────────────────────────────────────


class TestGetHost:
    """Retrieving a single host by name."""

    def test_get_host(self, cfg: ConfigManager) -> None:
        """``get_host`` returns the host dict for a known name."""
        cfg.add_host(**_A_VALID_HOST)
        host = cfg.get_host("dev-box")
        assert host is not None
        assert host["name"] == "dev-box"
        assert host["host"] == "192.168.1.100"

    def test_get_host_nonexistent(self, cfg: ConfigManager) -> None:
        """``get_host`` returns ``None`` for an unknown name."""
        host = cfg.get_host("nope")
        assert host is None

    def test_get_host_empty_config(self, cfg: ConfigManager) -> None:
        """``get_host`` returns ``None`` when no hosts exist."""
        assert cfg.get_host("anything") is None


# ── Default values / auto-assignment ─────────────────────────────────────────


class TestDefaults:
    """Default key_path and auto-assigned ``local_tunnel_port``."""

    def test_default_key_path(self, cfg: ConfigManager) -> None:
        """``add_host`` assigns ``data/ssh/id_ed25519`` when key_path is omitted."""
        cfg.add_host(
            name="dev-box",
            host="192.168.1.100",
            port=22,
            user="dev",
            # key_path not passed
        )
        host = cfg.get_host("dev-box")
        assert host is not None
        assert host["key_path"] == "data/ssh/id_ed25519"

    def test_default_key_path_explicit_none(self, cfg: ConfigManager) -> None:
        """``add_host`` assigns default when key_path is None."""
        cfg.add_host(
            name="dev-box",
            host="192.168.1.100",
            port=22,
            user="dev",
            key_path=None,
        )
        host = cfg.get_host("dev-box")
        assert host is not None
        assert host["key_path"] == "data/ssh/id_ed25519"

    def test_auto_port_assignment_first(self, cfg: ConfigManager) -> None:
        """First host gets ``local_tunnel_port`` 14096."""
        cfg.add_host(
            name="dev-box",
            host="192.168.1.100",
            port=22,
            user="dev",
            # local_tunnel_port not passed
        )
        host = cfg.get_host("dev-box")
        assert host is not None
        assert host["local_tunnel_port"] == 14096

    def test_auto_port_assignment_second(self, cfg: ConfigManager) -> None:
        """Second host gets ``local_tunnel_port`` 14097."""
        cfg.add_host(
            name="dev-box",
            host="192.168.1.100",
            port=22,
            user="dev",
        )
        cfg.add_host(
            name="backup",
            host="10.0.0.2",
            port=22,
            user="ops",
        )
        host = cfg.get_host("backup")
        assert host is not None
        assert host["local_tunnel_port"] == 14097

    def test_explicit_tunnel_port(self, cfg: ConfigManager) -> None:
        """An explicit ``local_tunnel_port`` is preserved, not auto-assigned."""
        cfg.add_host(
            name="dev-box",
            host="192.168.1.100",
            port=22,
            user="dev",
            local_tunnel_port=9999,
        )
        host = cfg.get_host("dev-box")
        assert host is not None
        assert host["local_tunnel_port"] == 9999

    def test_default_opencode_port(self, cfg: ConfigManager) -> None:
        """``opencode_port`` defaults to 4096 when omitted."""
        cfg.add_host(
            name="dev-box",
            host="192.168.1.100",
            port=22,
            user="dev",
            # opencode_port not passed
        )
        host = cfg.get_host("dev-box")
        assert host is not None
        assert host["opencode_port"] == 4096


# ── Atomic write (crash safety) ───────────────────────────────────────────────


class TestAtomicWrite:
    """``_write`` uses an atomic write pattern (tmp + rename)."""

    def test_atomic_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Parent directories are created before writing."""
        path = tmp_path / "deep" / "nested" / "config.yaml"
        cm = ConfigManager(path=str(path))

        cm.add_host(**_A_VALID_HOST)

        assert path.exists()
        data = _read_yaml(path)
        assert len(data["hosts"]) == 1

    def test_atomic_write_leaves_no_tmp_file(self, tmp_path: Path) -> None:
        """No ``.tmp`` sibling remains after a successful write."""
        path = tmp_path / "config.yaml"
        cm = ConfigManager(path=str(path))

        cm.add_host(**_A_VALID_HOST)

        tmp_siblings = list(tmp_path.glob("*.tmp"))
        assert tmp_siblings == []

    def test_atomic_write_content_is_valid_yaml(self, tmp_path: Path) -> None:
        """The written file is parseable YAML with expected structure."""
        path = tmp_path / "config.yaml"
        cm = ConfigManager(path=str(path))
        cm.add_host(**_A_VALID_HOST)

        raw = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        assert parsed == {"hosts": [{**_A_VALID_HOST}]}

    def test_atomic_write_overwrites_on_second_add(self, tmp_path: Path) -> None:
        """A second write replaces the file content cleanly."""
        path = tmp_path / "config.yaml"
        cm = ConfigManager(path=str(path))
        cm.add_host(**_A_VALID_HOST)
        cm.add_host(name="second", host="10.0.0.2", port=22, user="ops")

        data = _read_yaml(path)
        assert len(data["hosts"]) == 2

        # Remove the first host and verify the file shrinks cleanly.
        cm.remove_host("dev-box")
        data_after = _read_yaml(path)
        assert len(data_after["hosts"]) == 1
        assert data_after["hosts"][0]["name"] == "second"


# ── load / save ───────────────────────────────────────────────────────────────


class TestLoadSave:
    """Direct ``load`` and ``save`` methods."""

    def test_save_and_load(self, tmp_path: Path) -> None:
        """``save`` writes hosts, ``load`` reads them back."""
        path = tmp_path / "config.yaml"
        cm = ConfigManager(path=str(path))

        hosts = [
            {**_A_VALID_HOST},
        ]
        cm.save(hosts)
        data = cm.load()
        assert len(data["hosts"]) == 1
        assert data["hosts"][0]["name"] == "dev-box"

    def test_save_validates(self, cfg: ConfigManager) -> None:
        """``save`` raises ``ValueError`` for invalid host entries."""
        with pytest.raises(ValueError, match="Validation failed"):
            cfg.save([{"name": "bad", "host": "", "port": 0, "user": ""}])

    def test_load_raises_on_corrupt(self, tmp_path: Path) -> None:
        """``load`` raises ``ValueError`` for invalid host data in the file."""
        path = tmp_path / "corrupt.yaml"
        path.write_text("hosts: not_a_list\n", encoding="utf-8")
        cm = ConfigManager(path=str(path))
        with pytest.raises(ValueError, match="'hosts' must be a list"):
            cm.load()

    def test_load_raises_on_bad_entry(self, tmp_path: Path) -> None:
        """``load`` raises ``ValueError`` when an entry fails validation."""
        path = tmp_path / "bad_entry.yaml"
        data = {"hosts": [{"name": "x", "host": "ok", "port": 22, "user": "u"}]}
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)
        cm = ConfigManager(path=str(path))
        with pytest.raises(ValueError, match="Validation failed"):
            cm.load()


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Boundary conditions and missing required fields."""

    def test_missing_required_name(self, cfg: ConfigManager) -> None:
        """Missing 'name' raises ``ValueError``."""
        with pytest.raises(ValueError, match="Invalid host config"):
            cfg.add_host(
                name="",  # empty string is invalid
                host="10.0.0.1",
                port=22,
                user="dev",
            )

    def test_missing_required_host(self, cfg: ConfigManager) -> None:
        """Missing 'host' raises ``ValueError`` (empty string)."""
        with pytest.raises(ValueError, match="Invalid host config"):
            cfg.add_host(
                name="test-host",
                host="",
                port=22,
                user="dev",
            )

    def test_missing_required_port(self, cfg: ConfigManager) -> None:
        """Missing 'port' raises ``ValueError``."""
        with pytest.raises(ValueError, match="Invalid host config"):
            cfg.add_host(
                name="test-host",
                host="10.0.0.1",
                port=-1,  # negative port is invalid
                user="dev",
            )

    def test_empty_description(self, cfg: ConfigManager) -> None:
        """An empty description is stored correctly."""
        cfg.add_host(
            **{**_A_VALID_HOST, "description": ""}
        )
        host = cfg.get_host("dev-box")
        assert host is not None
        assert host["description"] == ""

    def test_description_with_shell_meta(self, cfg: ConfigManager) -> None:
        """Description with shell metacharacters raises ``ValueError``."""
        with pytest.raises(ValueError, match="Invalid host config"):
            cfg.add_host(
                **{**_A_VALID_HOST, "description": "some `backtick` desc"}
            )

    def test_config_file_detects_external_changes(self, tmp_path: Path) -> None:
        """``list_hosts`` re-reads the file, so external edits are visible."""
        path = tmp_path / "shared.yaml"
        cm = ConfigManager(path=str(path))
        cm.add_host(**_A_VALID_HOST)

        # External edit: add another host directly to the YAML.
        data = _read_yaml(path)
        data["hosts"].append({"name": "ext", "host": "10.0.0.9", "port": 22, "user": "ext"})
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        hosts = cm.list_hosts()
        assert len(hosts) == 2
        assert hosts[1]["name"] == "ext"


# ── SshManager tests ─────────────────────────────────────────────────────────────


class TestSshManager:
    """Mocked-subprocess tests for SSH tunnel lifecycle.

    All ``asyncio.create_subprocess_exec`` and network calls are mocked —
    no actual SSH connections are ever established.
    """

    _A_VALID_CONFIG: dict = {
        "name": "dev-box",
        "host": "192.168.1.100",
        "port": 22,
        "user": "dev",
        "key_path": "data/ssh/id_ed25519",
        "opencode_port": 4096,
        "local_tunnel_port": 14096,
        "description": "Development server",
    }

    # ── fixtures ────────────────────────────────────────────────────────────────

    @pytest.fixture
    def ssh(self, tmp_path: Path) -> SshManager:
        """Return an ``SshManager`` backed by a temp config directory."""
        return SshManager(config_dir=str(tmp_path / "ssh"))

    @pytest.fixture
    def mock_proc(self) -> AsyncMock:
        """Return a mock subprocess ``Process`` that appears alive.

        ``returncode`` is ``None`` (still running), ``communicate`` returns
        empty bytes, ``wait`` returns 0 immediately, and ``send_signal``
        is a no-op.
        """
        proc = AsyncMock()
        proc.returncode = None
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        proc.send_signal = Mock()
        proc.pid = 12345
        return proc

    @pytest.fixture
    def happy_path_mocks(
        self, ssh: SshManager, mock_proc: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
        """Patch ``asyncio.create_subprocess_exec`` and internal tunnel/OpenCode
        checks so a ``connect()`` call succeeds immediately.

        Returns ``(mock_create_subprocess_exec, mock_wait_for_tunnel,
        mock_check_opencode)`` for further per-test overrides.
        """
        mock_create = AsyncMock(return_value=mock_proc)
        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_create)

        mock_tunnel = AsyncMock(return_value=True)
        monkeypatch.setattr(ssh, "_wait_for_tunnel", mock_tunnel)

        mock_opencode = AsyncMock(return_value=True)
        monkeypatch.setattr(ssh, "_check_opencode_ready", mock_opencode)

        return mock_create, mock_tunnel, mock_opencode

    # ── connect — command construction ─────────────────────────────────────────

    async def test_build_command(
        self, ssh: SshManager, happy_path_mocks: tuple,
    ) -> None:
        """``connect`` builds an SSH command with all expected flags."""
        mock_create, _, _ = happy_path_mocks

        await ssh.connect(self._A_VALID_CONFIG)

        mock_create.assert_called_once()
        cmd = mock_create.call_args[0]  # positional args

        assert cmd[0] == "ssh"
        assert "-N" in cmd
        assert "-L" in cmd
        port_arg = "127.0.0.1:14096:127.0.0.1:4096"
        assert any(port_arg in arg for arg in cmd)
        assert "-i" in cmd
        assert "data/ssh/id_ed25519" in cmd
        assert "-p" in cmd
        assert "22" in cmd
        assert "dev@192.168.1.100" in cmd
        assert any("StrictHostKeyChecking=accept-new" in arg for arg in cmd)
        # All arguments are plain strings (no nested lists)
        assert all(isinstance(a, str) for a in cmd)

    async def test_connect_with_auth_key(
        self, ssh: SshManager, happy_path_mocks: tuple,
    ) -> None:
        """``connect`` passes ``-i`` with the custom key path when provided."""
        mock_create, _, _ = happy_path_mocks
        config = {**self._A_VALID_CONFIG, "key_path": "data/ssh/custom_ed25519"}

        await ssh.connect(config)

        cmd = mock_create.call_args[0]
        i_idx = cmd.index("-i")
        assert cmd[i_idx + 1] == "data/ssh/custom_ed25519"

    # ── connect — success path ─────────────────────────────────────────────────

    async def test_connect(
        self, ssh: SshManager, happy_path_mocks: tuple,
    ) -> None:
        """``connect`` returns ``success=True`` when the tunnel and OpenCode
        verification both pass."""
        result = await ssh.connect(self._A_VALID_CONFIG)

        assert result["success"] is True
        assert result["name"] == "dev-box"
        assert result["local_port"] == 14096
        # Connection is tracked in _connections
        assert "dev-box" in ssh._connections

    async def test_connect_duplicate(
        self, ssh: SshManager, happy_path_mocks: tuple,
    ) -> None:
        """``connect`` rejects a duplicate connection name."""
        await ssh.connect(self._A_VALID_CONFIG)
        result = await ssh.connect(self._A_VALID_CONFIG)

        assert result["success"] is False
        assert "already exists" in result["message"]

    # ── connect — failure paths ────────────────────────────────────────────────

    async def test_connect_no_opencode(
        self, ssh: SshManager, mock_proc: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``connect`` fails when the tunnel is up but OpenCode does not respond."""
        monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc))
        monkeypatch.setattr(ssh, "_wait_for_tunnel", AsyncMock(return_value=True))
        monkeypatch.setattr(ssh, "_check_opencode_ready", AsyncMock(return_value=False))

        result = await ssh.connect(self._A_VALID_CONFIG)

        assert result["success"] is False
        assert "OpenCode did not respond" in result["message"]
        # Connection is cleaned up on failure
        assert "dev-box" not in ssh._connections

    async def test_connect_refused(
        self, ssh: SshManager, mock_proc: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``connect`` reports a user-friendly message on SSH connection refused."""
        mock_proc.communicate = AsyncMock(
            return_value=(
                b"",
                b"ssh: connect to host 192.168.1.100 port 22: Connection refused",
            ),
        )
        monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc))
        monkeypatch.setattr(ssh, "_wait_for_tunnel", AsyncMock(return_value=False))

        result = await ssh.connect(self._A_VALID_CONFIG)

        assert result["success"] is False
        assert "connection refused" in result["message"].lower()
        assert "dev-box" not in ssh._connections

    # ── injection prevention ───────────────────────────────────────────────────

    async def test_injection_prevention(
        self, ssh: SshManager, happy_path_mocks: tuple,
    ) -> None:
        """Shell metacharacters in config are caught by validation — subprocess
        is never invoked with malicious values."""
        mock_create, _, _ = happy_path_mocks

        malicious = {
            **self._A_VALID_CONFIG,
            "host": "192.168.1.100; rm -rf /",
            "user": "dev$(whoami)",
        }
        result = await ssh.connect(malicious)

        # Validation rejects shell metacharacters BEFORE subprocess exec
        assert result["success"] is False
        assert "Invalid config" in result["message"]
        mock_create.assert_not_called()

    # ── sanitize_for_subprocess (defence-in-depth) ─────────────────────────────

    def test_sanitize_removes_shell_meta(self) -> None:
        """``sanitize_for_subprocess`` strips shell metacharacters."""
        assert _orch.sanitize_for_subprocess("foo;bar") == "foobar"
        assert _orch.sanitize_for_subprocess("foo$(bar)") == "foobar"
        assert _orch.sanitize_for_subprocess("foo|bar") == "foobar"
        assert _orch.sanitize_for_subprocess("normal") == "normal"
        assert _orch.sanitize_for_subprocess("") == ""

    # ── disconnect ─────────────────────────────────────────────────────────────

    async def test_disconnect(
        self, ssh: SshManager, mock_proc: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``disconnect`` kills the SSH process and removes the connection."""
        ssh._connections["dev-box"] = {
            "process": mock_proc,
            "local_port": 14096,
            "host": "192.168.1.100",
            "user": "dev",
            "opencode_port": 4096,
        }
        monkeypatch.setattr(ssh, "_wait_for_port_free", AsyncMock(return_value=True))

        result = await ssh.disconnect("dev-box")

        assert result is True
        mock_proc.send_signal.assert_called_once_with(_orch.signal.SIGTERM)
        assert "dev-box" not in ssh._connections

    async def test_disconnect_not_found(
        self, ssh: SshManager,
    ) -> None:
        """``disconnect`` returns ``False`` for an unknown host."""
        result = await ssh.disconnect("ghost")
        assert result is False

    async def test_disconnect_all(
        self, ssh: SshManager, mock_proc: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``disconnect_all`` terminates every active tunnel and clears state."""
        proc_a = mock_proc
        proc_b = AsyncMock()
        proc_b.returncode = None
        proc_b.communicate = AsyncMock(return_value=(b"", b""))
        proc_b.wait = AsyncMock(return_value=0)
        proc_b.send_signal = Mock()
        proc_b.pid = 23456

        ssh._connections["host-a"] = {
            "process": proc_a, "local_port": 14096,
        }
        ssh._connections["host-b"] = {
            "process": proc_b, "local_port": 14097,
        }
        monkeypatch.setattr(ssh, "_wait_for_port_free", AsyncMock(return_value=True))

        await ssh.disconnect_all()

        assert ssh._connections == {}
        proc_a.send_signal.assert_called_once_with(_orch.signal.SIGTERM)
        proc_b.send_signal.assert_called_once_with(_orch.signal.SIGTERM)

    # ── status ─────────────────────────────────────────────────────────────────

    async def test_status_connected(
        self, ssh: SshManager, mock_proc: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``status`` reports connected when the process is alive and port open."""
        ssh._connections["dev-box"] = {
            "process": mock_proc,
            "local_port": 14096,
            "host": "192.168.1.100",
            "user": "dev",
            "opencode_port": 4096,
        }
        monkeypatch.setattr(ssh, "_is_port_listening", AsyncMock(return_value=True))

        state = await ssh.status("dev-box")

        assert state["connected"] is True
        assert state["process_alive"] is True
        assert state["port_open"] is True

    async def test_status_disconnected(
        self, ssh: SshManager, mock_proc: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``status`` reports disconnected when the process has exited."""
        mock_proc.returncode = 0  # process exited
        ssh._connections["dev-box"] = {
            "process": mock_proc,
            "local_port": 14096,
            "host": "192.168.1.100",
            "user": "dev",
            "opencode_port": 4096,
        }
        monkeypatch.setattr(ssh, "_is_port_listening", AsyncMock(return_value=False))

        state = await ssh.status("dev-box")

        assert state["connected"] is False
        assert state["process_alive"] is False
        assert state["port_open"] is False

    async def test_status_not_found(
        self, ssh: SshManager,
    ) -> None:
        """``status`` returns not-found for an unknown host."""
        state = await ssh.status("ghost")
        assert state["connected"] is False
        assert state["error"] == "not found"

    # ── _wait_for_tunnel ───────────────────────────────────────────────────────

    async def test_wait_for_tunnel_success(
        self, ssh: SshManager, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_wait_for_tunnel`` returns ``True`` when the port is detected immediately."""
        monkeypatch.setattr(ssh, "_is_port_listening", AsyncMock(return_value=True))

        ok = await ssh._wait_for_tunnel(local_port=14096, timeout=1)
        assert ok is True

    async def test_wait_for_tunnel_timeout(
        self, ssh: SshManager, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_wait_for_tunnel`` returns ``False`` when the port never appears."""
        monkeypatch.setattr(ssh, "_is_port_listening", AsyncMock(return_value=False))

        ok = await ssh._wait_for_tunnel(local_port=14096, timeout=0.1)
        assert ok is False


# ── OpencodeClient tests ─────────────────────────────────────────────────────────


class TestOpencodeClient:
    """Mocked HTTP tests for ``OpencodeClient``.

    All ``httpx.AsyncClient`` calls are mocked — no real network
    connections are ever made.
    """

    @pytest.fixture
    def client(self) -> OpencodeClient:
        """Return an ``OpencodeClient`` pointing at a local tunnel (no auth)."""
        return OpencodeClient(base_url="http://127.0.0.1:14096")

    @pytest.fixture
    def authed_client(self) -> OpencodeClient:
        """Return a client with a password for auth header tests."""
        return OpencodeClient(base_url="http://127.0.0.1:14096", password="secret123")

    @pytest.fixture(autouse=True)
    def mock_httpx_client(self) -> AsyncMock:
        """Patch ``_orch.httpx.AsyncClient`` — connections are never real.

        Returns the mock *instance* (what the ``async with`` block yields)
        so each test can configure ``request``, ``get``, ``post``, etc.
        """
        with patch.object(_orch.httpx, "AsyncClient") as mock_cls:
            instance = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = instance
            yield instance

    # ── Auth header ─────────────────────────────────────────────────────────────

    def test_auth_header(self) -> None:
        """``Authorization`` header is ``Basic base64(opencode:password)``."""
        client = OpencodeClient("http://127.0.0.1:14096", password="hunter2")
        expected = base64.b64encode(b"opencode:hunter2").decode()
        assert client._auth_header == {"Authorization": f"Basic {expected}"}

    def test_no_auth_header(self) -> None:
        """When password is ``None``, no auth header is set."""
        client = OpencodeClient("http://127.0.0.1:14096")
        assert client._auth_header == {}

    # ── health ──────────────────────────────────────────────────────────────────

    async def test_health_success(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``GET /doc`` with 200 returns ``True``."""
        mock_httpx_client.get.return_value = Mock(status_code=200)
        assert await client.health() is True
        mock_httpx_client.get.assert_called_once_with("/doc", timeout=ANY)

    async def test_health_failure(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """Non-200 status returns ``False``."""
        mock_httpx_client.get.return_value = Mock(status_code=500)
        assert await client.health() is False

    async def test_health_connection_refused(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``ConnectError`` from ``GET /doc`` returns ``False``."""
        mock_httpx_client.get.side_effect = httpx.ConnectError("No connection")
        assert await client.health() is False

    async def test_health_timeout(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``TimeoutException`` from ``GET /doc`` returns ``False``."""
        mock_httpx_client.get.side_effect = httpx.TimeoutException("Timed out")
        assert await client.health() is False

    # ── create_session ──────────────────────────────────────────────────────────

    async def test_create_session(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``POST /sessions`` with title/project payload returns session data."""
        expected = {"session_id": "sess_001", "title": "Test", "status": "active"}
        mock_httpx_client.request.return_value = Mock(
            status_code=200,
            json=Mock(return_value=expected),
        )
        result = await client.create_session(title="Test", project="/my-project")
        assert result == expected
        mock_httpx_client.request.assert_called_once_with(
            "POST", "/sessions",
            json={"title": "Test", "project": "/my-project"},
            timeout=ANY,
        )

    # ── send_prompt ─────────────────────────────────────────────────────────────

    async def test_send_prompt(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``POST /sessions/{id}/prompt`` with text returns response string."""
        expected = "Hello! How can I help?"
        mock_httpx_client.post.return_value = Mock(
            status_code=200,
            text=expected,
        )
        result = await client.send_prompt(session_id="sess_001", text="Hi there")
        assert result == expected
        mock_httpx_client.post.assert_called_once()
        args, kwargs = mock_httpx_client.post.call_args
        assert args[0] == "/sessions/sess_001/prompt"
        assert kwargs["json"] == {"text": "Hi there"}
        assert kwargs["headers"] == {}

    async def test_send_prompt_connection_refused(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``ConnectError`` during prompt returns error JSON string."""
        mock_httpx_client.post.side_effect = httpx.ConnectError("No connection")
        result = await client.send_prompt(session_id="sess_001", text="Hi")
        assert json.loads(result) == {
            "error": "Connection refused — is the remote OpenCode running?"
        }

    async def test_send_prompt_timeout(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``TimeoutException`` during prompt returns error JSON string."""
        mock_httpx_client.post.side_effect = httpx.TimeoutException("Timed out")
        result = await client.send_prompt(session_id="sess_001", text="Hi")
        assert json.loads(result) == {"error": "Request timed out"}

    # ── list_sessions ───────────────────────────────────────────────────────────

    async def test_list_sessions(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``GET /sessions`` returns a list of session dicts."""
        expected = [
            {"session_id": "sess_001", "title": "A", "status": "active"},
            {"session_id": "sess_002", "title": "B", "status": "active"},
        ]
        mock_httpx_client.request.return_value = Mock(
            status_code=200,
            json=Mock(return_value=expected),
        )
        result = await client.list_sessions()
        assert result == expected
        mock_httpx_client.request.assert_called_once_with(
            "GET", "/sessions", timeout=ANY,
        )

    # ── stop_session ────────────────────────────────────────────────────────────

    async def test_stop_session(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``POST /sessions/{id}/cancel`` returns ``True`` on 2xx."""
        mock_httpx_client.request.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"result": "cancelled"}),
        )
        result = await client.stop_session(session_id="sess_001")
        assert result is True
        mock_httpx_client.request.assert_called_once_with(
            "POST", "/sessions/sess_001/cancel", timeout=ANY,
        )

    async def test_stop_session_failure(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """When the API returns an error dict, ``stop_session`` returns ``False``."""
        mock_httpx_client.request.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"error": "already stopped"}),
        )
        result = await client.stop_session(session_id="sess_001")
        assert result is False

    # ── get_session ─────────────────────────────────────────────────────────────

    async def test_get_session(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``GET /sessions/{id}`` returns the session dict."""
        expected = {"session_id": "sess_001", "title": "My Session", "status": "active"}
        mock_httpx_client.request.return_value = Mock(
            status_code=200,
            json=Mock(return_value=expected),
        )
        result = await client.get_session(session_id="sess_001")
        assert result == expected
        mock_httpx_client.request.assert_called_once_with(
            "GET", "/sessions/sess_001", timeout=ANY,
        )

    # ── Error paths (via ``_request``) ──────────────────────────────────────────

    async def test_connection_refused(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``ConnectError`` from ``_request`` returns error dict."""
        mock_httpx_client.request.side_effect = httpx.ConnectError("No connection")
        result = await client.list_sessions()
        assert result == {"error": "Connection refused — is the remote OpenCode running?"}

    async def test_timeout(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """``TimeoutException`` from ``_request`` returns error dict."""
        mock_httpx_client.request.side_effect = httpx.TimeoutException("Timed out")
        result = await client.list_sessions()
        assert result == {"error": "Request timed out"}

    async def test_auth_failure(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """401 response from ``_request`` returns auth error dict."""
        mock_httpx_client.request.return_value = httpx.Response(
            status_code=401,
            request=httpx.Request("GET", "http://127.0.0.1:14096/"),
        )
        result = await client.list_sessions()
        assert result == {"error": "Authentication failed — check password"}

    async def test_session_not_found(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """404 response from ``_request`` returns not-found error dict."""
        mock_httpx_client.request.return_value = httpx.Response(
            status_code=404,
            request=httpx.Request("GET", "http://127.0.0.1:14096/"),
        )
        result = await client.get_session(session_id="ghost")
        assert result == {"error": "Session not found"}

    async def test_server_error(
        self, client: OpencodeClient, mock_httpx_client: AsyncMock,
    ) -> None:
        """500+ response from ``_request`` returns server-error dict."""
        mock_httpx_client.request.return_value = httpx.Response(
            status_code=502,
            request=httpx.Request("GET", "http://127.0.0.1:14096/"),
        )
        result = await client.list_sessions()
        assert result == {"error": "Remote server error (HTTP 502)"}


# ── MCP dispatch tests ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_config() -> Mock:
    """Mock ``_get_config`` to return a controlled ``ConfigManager``."""
    config = Mock(spec=_orch.ConfigManager)
    config.list_hosts.return_value = []
    with patch.object(_orch, '_get_config', return_value=config):
        yield config


@pytest.fixture
def mock_ssh() -> AsyncMock:
    """Mock ``_get_ssh_manager`` to return a controlled ``SshManager``."""
    ssh = AsyncMock(spec=_orch.SshManager)
    with patch.object(_orch, '_get_ssh_manager', return_value=ssh):
        yield ssh


@pytest.fixture
def mock_connected() -> set:
    """Mock ``_get_connected`` to return a controlled set."""
    connected: set = set()
    with patch.object(_orch, '_get_connected', return_value=connected):
        yield connected


@pytest.fixture
def mock_connection_info() -> dict:
    """Mock ``_get_connection_info`` to return a controlled dict."""
    info: dict = {}
    with patch.object(_orch, '_get_connection_info', return_value=info):
        yield info


@pytest.fixture
def mock_opencode_client() -> AsyncMock:
    """Mock ``OpencodeClient`` constructor to return a controlled instance."""
    client = AsyncMock(spec=_orch.OpencodeClient)
    with patch.object(_orch, 'OpencodeClient', return_value=client):
        yield client


class TestListTools:
    """``list_tools`` returns all 10 container orchestrator tools."""

    async def test_list_tools(self) -> None:
        """All 10 tools are registered with expected names."""
        tools = await _orch.list_tools()
        assert len(tools) == 10
        names = {t.name for t in tools}
        expected = {
            "container_orch_list_hosts",
            "container_orch_add_host",
            "container_orch_remove_host",
            "container_orch_connect",
            "container_orch_disconnect",
            "container_orch_status",
            "container_orch_create_session",
            "container_orch_send_prompt",
            "container_orch_list_sessions",
            "container_orch_stop_session",
        }
        assert names == expected


class TestUnknownTool:
    """``call_tool`` with an unknown tool name returns an error."""

    async def test_unknown_tool(self) -> None:
        """An unrecognized tool name produces ``TextContent`` with error text."""
        result = await _orch.call_tool("nonexistent_tool", {})
        assert len(result) == 1
        assert result[0].type == "text"
        assert "Unknown tool" in result[0].text
        assert "nonexistent_tool" in result[0].text

    async def test_unknown_tool_empty_name(self) -> None:
        """An empty tool name also returns unknown-tool error."""
        result = await _orch.call_tool("", {})
        assert len(result) == 1
        assert "Unknown tool" in result[0].text


class TestDispatchConfigTools:
    """Config management tools (list/add/remove) dispatch to correct handlers."""

    async def test_list_hosts_empty(self, mock_config: Mock) -> None:
        """``container_orch_list_hosts`` returns 'No hosts configured' when empty."""
        mock_config.list_hosts.return_value = []
        result = await _orch.call_tool("container_orch_list_hosts", {})
        mock_config.list_hosts.assert_called_once()
        assert result[0].text == "No hosts configured"

    async def test_list_hosts_with_hosts(
        self, mock_config: Mock,
    ) -> None:
        """``container_orch_list_hosts`` formats host entries with status."""
        mock_config.list_hosts.return_value = [
            {"name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
             "local_tunnel_port": 14096, "opencode_port": 4096},
        ]
        result = await _orch.call_tool("container_orch_list_hosts", {})
        assert "dev-box" in result[0].text
        assert "Configured hosts (1)" in result[0].text
        assert "disconnected" in result[0].text

    async def test_list_hosts_connected(
        self, mock_config: Mock, mock_connected: set,
    ) -> None:
        """A connected host shows 'connected' status."""
        mock_connected.add("dev-box")
        mock_config.list_hosts.return_value = [
            {"name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
             "local_tunnel_port": 14096, "opencode_port": 4096},
        ]
        result = await _orch.call_tool("container_orch_list_hosts", {})
        assert "connected" in result[0].text.lower()

    async def test_list_hosts_with_description(
        self, mock_config: Mock,
    ) -> None:
        """Host descriptions are included in the output when present."""
        mock_config.list_hosts.return_value = [
            {"name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
             "local_tunnel_port": 14096, "opencode_port": 4096,
             "description": "Dev server"},
        ]
        result = await _orch.call_tool("container_orch_list_hosts", {})
        assert "Dev server" in result[0].text

    async def test_list_hosts_error(self, mock_config: Mock) -> None:
        """When ``list_hosts`` raises, the error is returned as ``TextContent``."""
        mock_config.list_hosts.side_effect = OSError("Permission denied")
        result = await _orch.call_tool("container_orch_list_hosts", {})
        assert "Error reading host configuration" in result[0].text

    async def test_add_host_dispatch(
        self, mock_config: Mock,
    ) -> None:
        """``container_orch_add_host`` calls ``ConfigManager.add_host``."""
        mock_config.list_hosts.return_value = []
        result = await _orch.call_tool(
            "container_orch_add_host",
            {"name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev"},
        )
        mock_config.add_host.assert_called_once()
        _, kwargs = mock_config.add_host.call_args
        assert kwargs["name"] == "dev-box"
        assert kwargs["host"] == "10.0.0.1"
        assert kwargs["port"] == 22
        assert kwargs["user"] == "dev"
        assert "added successfully" in result[0].text

    async def test_add_host_all_fields(
        self, mock_config: Mock,
    ) -> None:
        """All optional fields are forwarded to ``add_host``."""
        mock_config.list_hosts.return_value = []
        result = await _orch.call_tool(
            "container_orch_add_host",
            {
                "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
                "key_path": "data/ssh/custom", "opencode_port": 4097,
                "local_tunnel_port": 9999, "description": "Custom box",
            },
        )
        mock_config.add_host.assert_called_once()
        _, kwargs = mock_config.add_host.call_args
        assert kwargs["key_path"] == "data/ssh/custom"
        assert kwargs["opencode_port"] == 4097
        assert kwargs["local_tunnel_port"] == 9999
        assert kwargs["description"] == "Custom box"

    async def test_add_host_missing_fields(self) -> None:
        """Missing required fields return a 'Missing required' error."""
        result = await _orch.call_tool("container_orch_add_host", {})
        assert "Missing required" in result[0].text

    async def test_add_host_partial_missing(self) -> None:
        """Only the missing fields are reported."""
        result = await _orch.call_tool(
            "container_orch_add_host",
            {"name": "dev-box"},  # missing host, port, user
        )
        assert "Missing required" in result[0].text

    async def test_add_host_validation_error(
        self, mock_config: Mock,
    ) -> None:
        """When ``add_host`` raises ``ValueError``, the error text is returned."""
        mock_config.add_host.side_effect = ValueError("Invalid host config: bad port")
        mock_config.list_hosts.return_value = []
        result = await _orch.call_tool(
            "container_orch_add_host",
            {"name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev"},
        )
        assert "Invalid host config" in result[0].text

    async def test_remove_host_dispatch(
        self, mock_config: Mock, mock_connected: set,
    ) -> None:
        """``container_orch_remove_host`` calls ``ConfigManager.remove_host``."""
        mock_config.remove_host.return_value = True
        result = await _orch.call_tool(
            "container_orch_remove_host",
            {"name": "dev-box"},
        )
        mock_config.remove_host.assert_called_once_with("dev-box")
        assert "removed successfully" in result[0].text

    async def test_remove_host_not_found(
        self, mock_config: Mock, mock_connected: set,
    ) -> None:
        """Removing a non-existent host returns 'not found'."""
        mock_config.remove_host.return_value = False
        result = await _orch.call_tool(
            "container_orch_remove_host",
            {"name": "ghost"},
        )
        assert "not found" in result[0].text

    async def test_remove_host_connected(
        self, mock_connected: set,
    ) -> None:
        """Removing a connected host returns an error (disconnect first)."""
        mock_connected.add("dev-box")
        result = await _orch.call_tool(
            "container_orch_remove_host",
            {"name": "dev-box"},
        )
        assert "currently connected" in result[0].text

    async def test_remove_host_missing_name(self) -> None:
        """Missing 'name' argument returns an error."""
        result = await _orch.call_tool("container_orch_remove_host", {})
        assert "'name' is required" in result[0].text


class TestDispatchSshTools:
    """SSH tunnel tools (connect/disconnect/status) dispatch to correct handlers."""

    async def test_connect_dispatch(
        self, mock_config: Mock, mock_connected: set, mock_ssh: AsyncMock,
    ) -> None:
        """``container_orch_connect`` calls ``SshManager.connect``."""
        mock_config.get_host.return_value = {
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
            "key_path": "data/ssh/id_ed25519", "opencode_port": 4096,
            "local_tunnel_port": 14096, "description": "",
        }
        mock_ssh.connect.return_value = {
            "success": True, "name": "dev-box", "local_port": 14096,
            "message": "Connected to 10.0.0.1:4096 via 127.0.0.1:14096",
        }
        result = await _orch.call_tool(
            "container_orch_connect",
            {"name": "dev-box"},
        )
        mock_config.get_host.assert_called_once_with("dev-box")
        mock_ssh.connect.assert_called_once()
        assert "Connected" in result[0].text
        assert "dev-box" in mock_connected

    async def test_connect_host_not_found(
        self, mock_config: Mock,
    ) -> None:
        """Connecting to a non-configured host returns 'not found'."""
        mock_config.get_host.return_value = None
        result = await _orch.call_tool(
            "container_orch_connect",
            {"name": "ghost"},
        )
        assert "not found" in result[0].text

    async def test_connect_already_connected(
        self, mock_config: Mock, mock_connected: set,
    ) -> None:
        """Connecting an already-connected host returns error."""
        mock_config.get_host.return_value = {"name": "dev-box", "host": "10.0.0.1",
            "port": 22, "user": "dev"}
        mock_connected.add("dev-box")
        result = await _orch.call_tool(
            "container_orch_connect",
            {"name": "dev-box"},
        )
        assert "already connected" in result[0].text

    async def test_connect_missing_name(self) -> None:
        """Missing 'name' argument returns error."""
        result = await _orch.call_tool("container_orch_connect", {})
        assert "'name' is required" in result[0].text

    async def test_connect_failure(
        self, mock_config: Mock, mock_ssh: AsyncMock,
    ) -> None:
        """When SSH connection fails, the error message is returned."""
        mock_config.get_host.return_value = {
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
        }
        mock_ssh.connect.return_value = {
            "success": False, "name": "dev-box",
            "message": "SSH connection refused — check host and port",
        }
        result = await _orch.call_tool(
            "container_orch_connect",
            {"name": "dev-box"},
        )
        assert "refused" in result[0].text

    async def test_disconnect_dispatch(
        self, mock_connected: set, mock_ssh: AsyncMock,
    ) -> None:
        """``container_orch_disconnect`` calls ``SshManager.disconnect``."""
        mock_connected.add("dev-box")
        mock_ssh.disconnect.return_value = True
        result = await _orch.call_tool(
            "container_orch_disconnect",
            {"name": "dev-box"},
        )
        mock_ssh.disconnect.assert_called_once_with("dev-box")
        assert "Disconnected" in result[0].text
        assert "dev-box" not in mock_connected

    async def test_disconnect_not_connected(
        self, mock_connected: set,
    ) -> None:
        """Disconnecting a non-connected host returns 'not currently connected'."""
        result = await _orch.call_tool(
            "container_orch_disconnect",
            {"name": "dev-box"},
        )
        assert "not currently connected" in result[0].text.lower()

    async def test_disconnect_missing_name(self) -> None:
        """Missing 'name' argument returns error."""
        result = await _orch.call_tool("container_orch_disconnect", {})
        assert "'name' is required" in result[0].text

    async def test_status_dispatch(
        self, mock_ssh: AsyncMock,
    ) -> None:
        """``container_orch_status`` calls ``SshManager.status``."""
        mock_ssh.status.return_value = {
            "name": "dev-box", "connected": True, "process_alive": True,
            "port_open": True, "local_port": 14096, "host": "10.0.0.1",
            "user": "dev", "remote_port": 4096,
        }
        result = await _orch.call_tool(
            "container_orch_status",
            {"name": "dev-box"},
        )
        mock_ssh.status.assert_called_once_with("dev-box")
        assert "connected" in result[0].text.lower()

    async def test_status_not_found(
        self, mock_ssh: AsyncMock,
    ) -> None:
        """Status for an unknown host returns 'no active connection'."""
        mock_ssh.status.return_value = {
            "name": "ghost", "connected": False, "error": "not found",
        }
        result = await _orch.call_tool(
            "container_orch_status",
            {"name": "ghost"},
        )
        assert "no active connection" in result[0].text.lower()

    async def test_status_disconnected(
        self, mock_ssh: AsyncMock,
    ) -> None:
        """Status for a disconnected host shows 'disconnected'."""
        mock_ssh.status.return_value = {
            "name": "dev-box", "connected": False, "process_alive": False,
            "port_open": False,
        }
        result = await _orch.call_tool(
            "container_orch_status",
            {"name": "dev-box"},
        )
        assert "disconnected" in result[0].text.lower()

    async def test_status_missing_name(self) -> None:
        """Missing 'name' argument returns error."""
        result = await _orch.call_tool("container_orch_status", {})
        assert "'name' is required" in result[0].text


class TestDispatchSessionTools:
    """Session management tools dispatch to correct handlers."""

    async def test_create_session_dispatch(
        self, mock_connection_info: dict, mock_opencode_client: AsyncMock,
    ) -> None:
        """``container_orch_create_session`` creates session via ``OpencodeClient``."""
        mock_connection_info["dev-box"] = {"local_port": 14096}
        mock_opencode_client.create_session.return_value = {
            "session_id": "sess_001", "title": "Test", "status": "active",
        }
        result = await _orch.call_tool(
            "container_orch_create_session",
            {"name": "dev-box", "title": "Test Session", "project": "/project"},
        )
        mock_opencode_client.create_session.assert_called_once_with(
            title="Test Session", project="/project",
        )
        assert "sess_001" in result[0].text

    async def test_create_session_not_connected(
        self, mock_connection_info: dict,
    ) -> None:
        """Creating a session on a non-connected host returns error."""
        result = await _orch.call_tool(
            "container_orch_create_session",
            {"name": "dev-box", "title": "Test", "project": "/project"},
        )
        assert "not connected" in result[0].text.lower()

    async def test_create_session_missing_fields(self) -> None:
        """Missing title/project returns error."""
        result = await _orch.call_tool(
            "container_orch_create_session",
            {"name": "dev-box"},
        )
        assert "'title' is required" in result[0].text

    async def test_create_session_error_response(
        self, mock_connection_info: dict, mock_opencode_client: AsyncMock,
    ) -> None:
        """When OpenCode returns an error, it is passed through."""
        mock_connection_info["dev-box"] = {"local_port": 14096}
        mock_opencode_client.create_session.return_value = {
            "error": "Authentication failed",
        }
        result = await _orch.call_tool(
            "container_orch_create_session",
            {"name": "dev-box", "title": "Test", "project": "/p"},
        )
        assert "Authentication failed" in result[0].text

    async def test_send_prompt_dispatch(
        self, mock_connection_info: dict, mock_opencode_client: AsyncMock,
    ) -> None:
        """``container_orch_send_prompt`` sends prompt via ``OpencodeClient``."""
        mock_connection_info["dev-box"] = {"local_port": 14096}
        mock_opencode_client.send_prompt.return_value = "Response text"
        result = await _orch.call_tool(
            "container_orch_send_prompt",
            {"name": "dev-box", "session_id": "sess_001", "text": "Hello"},
        )
        mock_opencode_client.send_prompt.assert_called_once_with(
            session_id="sess_001", text="Hello",
        )
        assert "Response text" in result[0].text

    async def test_send_prompt_missing_fields(self) -> None:
        """Missing session_id or text returns error."""
        result = await _orch.call_tool(
            "container_orch_send_prompt",
            {"name": "dev-box"},
        )
        assert "'session_id' is required" in result[0].text

    async def test_send_prompt_not_connected(
        self, mock_connection_info: dict,
    ) -> None:
        """Sending a prompt to a non-connected host returns error."""
        result = await _orch.call_tool(
            "container_orch_send_prompt",
            {"name": "dev-box", "session_id": "sess_001", "text": "Hi"},
        )
        assert "not connected" in result[0].text.lower()

    async def test_list_sessions_dispatch(
        self, mock_connection_info: dict, mock_opencode_client: AsyncMock,
    ) -> None:
        """``container_orch_list_sessions`` lists sessions via ``OpencodeClient``."""
        mock_connection_info["dev-box"] = {"local_port": 14096}
        mock_opencode_client.list_sessions.return_value = [
            {"session_id": "sess_001", "title": "A", "status": "active"},
        ]
        result = await _orch.call_tool(
            "container_orch_list_sessions",
            {"name": "dev-box"},
        )
        mock_opencode_client.list_sessions.assert_called_once()
        assert "sess_001" in result[0].text

    async def test_list_sessions_empty(
        self, mock_connection_info: dict, mock_opencode_client: AsyncMock,
    ) -> None:
        """When no sessions exist, 'No active sessions' is returned."""
        mock_connection_info["dev-box"] = {"local_port": 14096}
        mock_opencode_client.list_sessions.return_value = []
        result = await _orch.call_tool(
            "container_orch_list_sessions",
            {"name": "dev-box"},
        )
        assert "No active sessions" in result[0].text

    async def test_list_sessions_not_connected(
        self, mock_connection_info: dict,
    ) -> None:
        """Listing sessions on a non-connected host returns error."""
        result = await _orch.call_tool(
            "container_orch_list_sessions",
            {"name": "dev-box"},
        )
        assert "not connected" in result[0].text.lower()

    async def test_stop_session_dispatch(
        self, mock_connection_info: dict, mock_opencode_client: AsyncMock,
    ) -> None:
        """``container_orch_stop_session`` stops session via ``OpencodeClient``."""
        mock_connection_info["dev-box"] = {"local_port": 14096}
        mock_opencode_client.stop_session.return_value = True
        result = await _orch.call_tool(
            "container_orch_stop_session",
            {"name": "dev-box", "session_id": "sess_001"},
        )
        mock_opencode_client.stop_session.assert_called_once_with(
            session_id="sess_001",
        )
        assert "cancelled" in result[0].text.lower()

    async def test_stop_session_failure(
        self, mock_connection_info: dict, mock_opencode_client: AsyncMock,
    ) -> None:
        """When ``stop_session`` returns ``False``, a failure message is returned."""
        mock_connection_info["dev-box"] = {"local_port": 14096}
        mock_opencode_client.stop_session.return_value = False
        result = await _orch.call_tool(
            "container_orch_stop_session",
            {"name": "dev-box", "session_id": "sess_001"},
        )
        assert "Failed" in result[0].text

    async def test_stop_session_not_connected(
        self, mock_connection_info: dict,
    ) -> None:
        """Stopping a session on a non-connected host returns error."""
        result = await _orch.call_tool(
            "container_orch_stop_session",
            {"name": "dev-box", "session_id": "sess_001"},
        )
        assert "not connected" in result[0].text.lower()

    async def test_stop_session_missing_fields(self) -> None:
        """Missing session_id returns error."""
        result = await _orch.call_tool(
            "container_orch_stop_session",
            {"name": "dev-box"},
        )
        assert "'session_id' is required" in result[0].text


# ── Input validation tests ──────────────────────────────────────────────────────


class TestValidateHostname:
    """``validate_hostname`` accepts valid hostnames/IPs, rejects everything else."""

    # ── valid cases ─────────────────────────────────────────────────────────

    def test_valid_dns_name(self) -> None:
        """Standard DNS hostname is accepted."""
        assert _orch.validate_hostname("example.com") is True

    def test_valid_dns_with_hyphen(self) -> None:
        """Hostname with hyphens is accepted."""
        assert _orch.validate_hostname("my-host") is True

    def test_valid_single_label(self) -> None:
        """Single-label hostname is accepted."""
        assert _orch.validate_hostname("localhost") is True

    def test_valid_ipv4(self) -> None:
        """IPv4 address is accepted."""
        assert _orch.validate_hostname("192.168.1.1") is True

    def test_valid_ipv6(self) -> None:
        """IPv6 loopback is accepted."""
        assert _orch.validate_hostname("::1") is True

    def test_valid_ipv6_full(self) -> None:
        """Full IPv6 address is accepted."""
        assert _orch.validate_hostname("2001:db8::1") is True

    def test_valid_numeric_label(self) -> None:
        """Numeric hostname-like string may pass DNS check."""
        assert _orch.validate_hostname("123") is True

    # ── invalid cases ─────────────────────────────────────────────────────────

    def test_invalid_empty_string(self) -> None:
        """Empty string is rejected."""
        assert _orch.validate_hostname("") is False

    def test_invalid_shell_meta_semicolon(self) -> None:
        """Hostname with semicolon (shell meta) is rejected."""
        assert _orch.validate_hostname("foo;bar") is False

    def test_invalid_shell_meta_pipe(self) -> None:
        """Hostname with pipe is rejected."""
        assert _orch.validate_hostname("foo|bar") is False

    def test_invalid_shell_meta_backtick(self) -> None:
        """Hostname with backtick is rejected."""
        assert _orch.validate_hostname("foo`bar") is False

    def test_invalid_shell_meta_dollar_brace(self) -> None:
        """Hostname with shell variable syntax is rejected."""
        assert _orch.validate_hostname("foo$(bar)") is False

    def test_invalid_shell_meta_ampersand(self) -> None:
        """Hostname with ampersand is rejected."""
        assert _orch.validate_hostname("foo&bar") is False

    def test_invalid_newline(self) -> None:
        """Hostname with newline is rejected."""
        assert _orch.validate_hostname("foo\nbar") is False

    def test_invalid_none(self) -> None:
        """``None`` is rejected (not a string)."""
        assert _orch.validate_hostname(None) is False  # type: ignore[arg-type]

    def test_invalid_non_string(self) -> None:
        """Non-string types are rejected."""
        assert _orch.validate_hostname(123) is False  # type: ignore[arg-type]

    def test_invalid_starts_with_dot(self) -> None:
        """Hostname starting with dot does not match DNS pattern."""
        assert _orch.validate_hostname(".example.com") is False

    def test_invalid_ends_with_dot(self) -> None:
        """Hostname ending with dot does not match DNS pattern."""
        assert _orch.validate_hostname("example.com.") is False

    def test_invalid_newline_in_middle(self) -> None:
        """Hostname with embedded newline is rejected."""
        assert _orch.validate_hostname("foo\nbar") is False


class TestValidateName:
    """``validate_name`` accepts valid friendly names, rejects everything else."""

    # ── valid cases ─────────────────────────────────────────────────────────

    def test_valid_min_length(self) -> None:
        """Minimum length (2 chars) is accepted."""
        assert _orch.validate_name("ab") is True

    def test_valid_max_length(self) -> None:
        """Maximum length (64 chars) is accepted."""
        assert _orch.validate_name("a" * 64) is True

    def test_valid_with_hyphen(self) -> None:
        """Hyphen in name is accepted."""
        assert _orch.validate_name("my-host") is True

    def test_valid_with_underscore(self) -> None:
        """Underscore in name is accepted."""
        assert _orch.validate_name("my_host") is True

    def test_valid_with_spaces(self) -> None:
        """Spaces in name are accepted."""
        assert _orch.validate_name("My Host") is True

    def test_valid_alphanumeric(self) -> None:
        """Plain alphanumeric name is accepted."""
        assert _orch.validate_name("Host123") is True

    def test_valid_with_trailing_space(self) -> None:
        """Name with trailing space (within regex) is accepted."""
        assert _orch.validate_name("host ") is True

    # ── invalid cases ─────────────────────────────────────────────────────────

    def test_invalid_empty_string(self) -> None:
        """Empty string is rejected."""
        assert _orch.validate_name("") is False

    def test_invalid_too_short(self) -> None:
        """Single character is rejected (min 2)."""
        assert _orch.validate_name("x") is False

    def test_invalid_too_long(self) -> None:
        """65+ characters are rejected (max 64)."""
        assert _orch.validate_name("a" * 65) is False

    def test_invalid_shell_meta_semicolon(self) -> None:
        """Name with semicolon is rejected."""
        assert _orch.validate_name("foo;bar") is False

    def test_invalid_shell_meta_pipe(self) -> None:
        """Name with pipe is rejected."""
        assert _orch.validate_name("foo|bar") is False

    def test_invalid_shell_meta_backtick(self) -> None:
        """Name with backtick is rejected."""
        assert _orch.validate_name("foo`bar") is False

    def test_invalid_shell_meta_dollar_brace(self) -> None:
        """Name with shell variable syntax is rejected."""
        assert _orch.validate_name("foo$(bar)") is False

    def test_invalid_newline(self) -> None:
        """Name with newline is rejected."""
        assert _orch.validate_name("foo\nbar") is False

    def test_invalid_none(self) -> None:
        """``None`` is rejected."""
        assert _orch.validate_name(None) is False  # type: ignore[arg-type]

    def test_invalid_with_at_sign(self) -> None:
        """Special characters like ``@`` are rejected."""
        assert _orch.validate_name("foo@bar") is False

    def test_invalid_with_dots(self) -> None:
        """Dots are not in the allowed character set."""
        assert _orch.validate_name("foo.bar") is False


class TestValidatePort:
    """``validate_port`` accepts valid ports, rejects out-of-range and non-int."""

    # ── valid cases ─────────────────────────────────────────────────────────

    def test_valid_port_min(self) -> None:
        """Minimum valid port (1) is accepted."""
        assert _orch.validate_port(1) is True

    def test_valid_port_typical(self) -> None:
        """Typical SSH port (22) is accepted."""
        assert _orch.validate_port(22) is True

    def test_valid_port_max(self) -> None:
        """Maximum valid port (65535) is accepted."""
        assert _orch.validate_port(65535) is True

    def test_valid_port_mid_range(self) -> None:
        """A mid-range port is accepted."""
        assert _orch.validate_port(8080) is True

    # ── invalid cases ─────────────────────────────────────────────────────────

    def test_invalid_port_zero(self) -> None:
        """Port 0 is rejected (reserved)."""
        assert _orch.validate_port(0) is False

    def test_invalid_port_negative(self) -> None:
        """Negative port is rejected."""
        assert _orch.validate_port(-1) is False

    def test_invalid_port_too_high(self) -> None:
        """Port > 65535 is rejected."""
        assert _orch.validate_port(65536) is False

    def test_invalid_port_float(self) -> None:
        """Float is rejected (must be ``int``)."""
        assert _orch.validate_port(22.5) is False

    def test_invalid_port_none(self) -> None:
        """``None`` is rejected."""
        assert _orch.validate_port(None) is False  # type: ignore[arg-type]

    def test_invalid_port_string(self) -> None:
        """String is rejected."""
        assert _orch.validate_port("22") is False  # type: ignore[arg-type]

    def test_invalid_port_list(self) -> None:
        """List is rejected."""
        assert _orch.validate_port([22]) is False  # type: ignore[arg-type]


class TestValidateKeyPath:
    """``validate_key_path`` accepts valid filesystem paths, rejects others."""

    def test_valid_relative_path(self) -> None:
        """A normal relative path is accepted."""
        assert _orch.validate_key_path("data/ssh/id_ed25519") is True

    def test_valid_absolute_path(self) -> None:
        """An absolute path is accepted."""
        assert _orch.validate_key_path("/home/user/.ssh/id_rsa") is True

    def test_valid_path_with_dots(self) -> None:
        """A path with dots is accepted."""
        assert _orch.validate_key_path("./config/ssh/key") is True

    def test_invalid_empty_string(self) -> None:
        """Empty string is rejected."""
        assert _orch.validate_key_path("") is False

    def test_invalid_none(self) -> None:
        """``None`` is rejected."""
        assert _orch.validate_key_path(None) is False  # type: ignore[arg-type]

    def test_invalid_shell_meta_semicolon(self) -> None:
        """Path with semicolon is rejected."""
        assert _orch.validate_key_path("foo;bar") is False

    def test_invalid_shell_meta_pipe(self) -> None:
        """Path with pipe is rejected."""
        assert _orch.validate_key_path("foo|bar") is False

    def test_invalid_shell_meta_backtick(self) -> None:
        """Path with backtick is rejected."""
        assert _orch.validate_key_path("foo`bar") is False

    def test_invalid_newline(self) -> None:
        """Path with newline is rejected."""
        assert _orch.validate_key_path("foo\nbar") is False

    def test_invalid_null_byte(self) -> None:
        """Path with null byte is rejected."""
        assert _orch.validate_key_path("foo\x00bar") is False

    def test_invalid_whitespace_only(self) -> None:
        """Whitespace-only path is rejected."""
        assert _orch.validate_key_path("   ") is False


class TestValidateHostConfig:
    """``validate_host_config`` returns all validation errors."""

    def test_valid_config_returns_empty(self) -> None:
        """A fully valid config returns an empty error list."""
        config = {
            "name": "dev-box",
            "host": "192.168.1.100",
            "port": 22,
            "user": "dev",
            "key_path": "data/ssh/id_ed25519",
            "opencode_port": 4096,
            "local_tunnel_port": 14096,
            "description": "Development server",
        }
        assert _orch.validate_host_config(config) == []

    def test_non_dict_input(self) -> None:
        """Non-dict input returns ``['config must be a dictionary']``."""
        assert _orch.validate_host_config(None) == ["config must be a dictionary"]
        assert _orch.validate_host_config("string") == ["config must be a dictionary"]
        assert _orch.validate_host_config(123) == ["config must be a dictionary"]

    def test_empty_dict_returns_all_required_errors(self) -> None:
        """Empty dict returns errors for all required fields."""
        errors = _orch.validate_host_config({})
        assert len(errors) == 4
        assert any("'name' is required" in e for e in errors)
        assert any("'host' is required" in e for e in errors)
        assert any("'port' is required" in e for e in errors)
        assert any("'user' is required" in e for e in errors)

    def test_missing_name(self) -> None:
        """Missing ``'name'`` returns name error."""
        errors = _orch.validate_host_config({
            "host": "10.0.0.1", "port": 22, "user": "dev",
        })
        assert any("'name' is required" in e for e in errors)

    def test_name_not_string(self) -> None:
        """Non-string ``'name'`` returns type error."""
        errors = _orch.validate_host_config({
            "name": 123, "host": "10.0.0.1", "port": 22, "user": "dev",
        })
        assert any("'name' must be a string" in e for e in errors)

    def test_name_invalid_format(self) -> None:
        """``'name'`` with invalid format (too short) returns format error."""
        errors = _orch.validate_host_config({
            "name": "x", "host": "10.0.0.1", "port": 22, "user": "dev",
        })
        assert any("2-64 characters" in e for e in errors)

    def test_missing_host(self) -> None:
        """Missing ``'host'`` returns host error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "port": 22, "user": "dev",
        })
        assert any("'host' is required" in e for e in errors)

    def test_host_not_string(self) -> None:
        """Non-string ``'host'`` returns type error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": 123, "port": 22, "user": "dev",
        })
        assert any("'host' must be a string" in e for e in errors)

    def test_host_invalid_hostname(self) -> None:
        """``'host'`` with shell metacharacters returns hostname error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "foo;bar", "port": 22, "user": "dev",
        })
        assert any("valid hostname" in e for e in errors)

    def test_missing_port(self) -> None:
        """Missing ``'port'`` returns port error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "user": "dev",
        })
        assert any("'port' is required" in e for e in errors)

    def test_port_not_integer(self) -> None:
        """Non-integer ``'port'`` returns type error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": "22", "user": "dev",
        })
        assert any("'port' must be an integer" in e for e in errors)

    def test_port_out_of_range(self) -> None:
        """Out-of-range ``'port'`` (0) returns range error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 0, "user": "dev",
        })
        assert any("1 and 65535" in e for e in errors)

    def test_missing_user(self) -> None:
        """Missing ``'user'`` returns user error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22,
        })
        assert any("'user' is required" in e for e in errors)

    def test_user_not_string(self) -> None:
        """Non-string ``'user'`` returns type error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": 123,
        })
        assert any("'user' must be a string" in e for e in errors)

    def test_user_empty(self) -> None:
        """Empty string ``'user'`` is rejected."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "",
        })
        assert any("'user' must not be empty" in e for e in errors)

    def test_user_shell_meta(self) -> None:
        """``'user'`` with shell metacharacters is rejected."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev;rm",
        })
        assert any("shell metacharacters" in e for e in errors)

    def test_key_path_invalid(self) -> None:
        """Invalid ``'key_path'`` returns key_path error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
            "key_path": "foo;bar",
        })
        assert any("'key_path'" in e for e in errors)

    def test_key_path_not_string(self) -> None:
        """Non-string ``'key_path'`` returns type error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
            "key_path": 123,
        })
        assert any("'key_path' must be a string" in e for e in errors)

    def test_opencode_port_invalid(self) -> None:
        """Invalid ``'opencode_port'`` returns port error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
            "opencode_port": 0,
        })
        assert any("'opencode_port'" in e for e in errors)

    def test_local_tunnel_port_invalid(self) -> None:
        """Invalid ``'local_tunnel_port'`` returns port error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
            "local_tunnel_port": 70000,
        })
        assert any("'local_tunnel_port'" in e for e in errors)

    def test_description_with_shell_meta(self) -> None:
        """``'description'`` with shell metacharacters is rejected."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
            "description": "some `code` here",
        })
        assert any("'description' contains" in e for e in errors)

    def test_description_not_string(self) -> None:
        """Non-string ``'description'`` returns type error."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
            "description": 123,
        })
        assert any("'description' must be a string" in e for e in errors)

    def test_description_empty_is_valid(self) -> None:
        """Empty description is acceptable."""
        errors = _orch.validate_host_config({
            "name": "dev-box", "host": "10.0.0.1", "port": 22, "user": "dev",
            "description": "",
        })
        assert errors == []

    def test_config_only_required_fields(self) -> None:
        """Config with only required fields (no optionals) is valid."""
        errors = _orch.validate_host_config({
            "name": "dev-box",
            "host": "10.0.0.1",
            "port": 22,
            "user": "dev",
        })
        assert errors == []

    def test_multiple_errors_returned(self) -> None:
        """Multiple validation errors are returned in a single call."""
        errors = _orch.validate_host_config({
            "name": "x",    # too short
            "host": "",     # empty - fails validate_hostname
            "port": 0,      # out of range
            "user": "",     # empty
        })
        assert len(errors) >= 3
        assert any("2-64 characters" in e for e in errors)
        assert any("valid hostname" in e for e in errors)
        assert any("1 and 65535" in e for e in errors)
        assert any("must not be empty" in e for e in errors)
