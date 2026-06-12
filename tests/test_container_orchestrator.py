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
