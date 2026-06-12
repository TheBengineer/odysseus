# Learnings: remote-opencode-skill

## SshManager Implementation (2024-06-12)

Implemented `SshManager` class in `mcp_servers/container_orchestrator.py`:

- **Architecture**: Async SSH tunnel lifecycle manager using `asyncio.create_subprocess_exec` (list args, no shell injection)
- **Tunnel health**: Two-phase verification: (1) `ss -tlnp` polling for port up to 5s, (2) raw HTTP `GET /doc` via `asyncio.open_connection` to verify OpenCode responds
- **Graceful shutdown**: SIGTERM → 2s wait → SIGKILL escalation pattern
- **Port safety**: All tunnels bind to `127.0.0.1` only; `StrictHostKeyChecking=accept-new` (not `no`)
- **Known hosts**: Path set to `<config_dir>/known_hosts` via `UserKnownHostsFile`
- **Error mapping**: Parses SSH stderr for permission denied, connection refused, hostname resolution failures
- **Validation**: Reuses existing `validate_host_config()` and `sanitize_for_subprocess()` from the module
- **Connection store**: In-memory dict `{name: {process, local_port, host, user, opencode_port}}` — process handle never exposed outside class
- **Key design constraint**: Avoids `httpx` import to prevent cycles; uses stdlib `asyncio.open_connection` for health checks

### References
- SSH command pattern: `src/builtin_actions.py:339` (existing subprocess SSH usage)
- Config defaults: `ConfigManager.add_host()` defaults for key_path and port assignment
