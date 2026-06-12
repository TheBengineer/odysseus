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

import sys
from pathlib import Path

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
