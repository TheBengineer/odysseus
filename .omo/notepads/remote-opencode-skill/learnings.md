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

## OpencodeClient Implementation (2024-06-12)

Implemented `OpencodeClient` class in `mcp_servers/container_orchestrator.py`:

- **Architecture**: `httpx.AsyncClient`-based HTTP client for remote OpenCode serve instances
- **Auth**: `Authorization: Basic` with base64-encoded `opencode:<password>` header; omitted when password is `None`
- **Constructor**: `OpencodeClient(base_url, password=None)` — base_url like `http://127.0.0.1:14096`
- **Methods**:
  - `health() -> bool` — `GET /doc`, asserts 200
  - `list_sessions() -> list[dict]` — `GET /sessions`
  - `create_session(title, project) -> dict` — `POST /sessions` with `{"title", "project"}`
  - `send_prompt(session_id, text) -> str` — `POST /sessions/{id}/prompt` with `{"text"}`; returns `resp.text` (not JSON); longer timeout (120s read, 10s connect)
  - `stop_session(session_id) -> bool` — `POST /sessions/{id}/cancel`
  - `get_session(session_id) -> dict` — `GET /sessions/{id}`
- **Error handling**: All public methods wrap in try/except; returns `{"error": "..."}` dicts or error JSON strings. `_handle_http_error()` maps 401/403 → auth failure, 404 → session not found, 5xx → remote server error
- **Timeouts**: `httpx.Timeout(120.0, connect=10.0)` for `send_prompt` (long prompts); `httpx.Timeout(30.0, connect=10.0)` for other requests via `_request()` default
- **Key constraint**: No URL-param credential leakage; no request/response body logging; no hardcoded endpoint paths beyond verified OpenCode API patterns

### References
- SSH command pattern: `src/builtin_actions.py:339` (existing subprocess SSH usage)
- Config defaults: `ConfigManager.add_host()` defaults for key_path and port assignment

## Dataclass Types Added (2024-06-12)

Added three stdlib dataclass types to `mcp_servers/container_orchestrator.py`:

- **`HostConfig`**: Structured host configuration with `__post_init__` validation via `validate_host_config()`. Fields: `name`, `host`, `port`, `user`, `key_path` (default `data/ssh/id_ed25519`), `opencode_port` (4096), `local_tunnel_port` (None=auto-assign), `description`. Raises `ValueError` on invalid construction.
- **`ConnectionState`**: SSH tunnel state tracking. Fields: `name`, `local_port`, `remote_host`, `pid`, `status` (connected|disconnected|error), `error_message`.
- **`SessionInfo`**: Remote session metadata. Fields: `session_id`, `title`, `created_at`, `status` (active|completed|failed).
- All types use `@dataclass` (stdlib, not pydantic) and are JSON-serializable via `dataclasses.asdict()`.
- `from __future__ import annotations` enables `int | None` syntax on `local_tunnel_port`.

## Task 11 - MCP server main entry point (container_orchestrator.py)

- Added `from mcp.server.stdio import stdio_server` import
- Added `async def run()` using `stdio_server()` context manager + `server.run()` with `create_initialization_options()`
- Added `if __name__ == "__main__": asyncio.run(run())` entry point
- Verified server starts standalone (timeout exit code 124 = blocking on stdio = correct)
- Pattern matches `email_server.py` lines 2189-2197 exactly
- Unknown tool name handling already existed in `call_tool` (else clause returning `Error: Unknown tool: {name}` as TextContent)
- Global try/except already wrapped `call_tool` dispatch
- No modification needed to existing 10 tool definitions or handlers

## SKILL.md Creation (2026-06-12)

Created `data/skills/system/container-orchestrator/SKILL.md`:

- **Frontmatter**: name, description, version (1.0.0), category (system), tags (remote/container/ssh/opencode/deploy/orchestration), platforms (linux), requires_toolsets (mcp), status (published), confidence (0.9), source (taught), created ISO timestamp, when_to_use (trigger keywords), procedure (7 steps), pitfalls (4 items), verification (3 items)
- **Body sections**: Overview, When to Use, Quick Start, Procedure, Tools Reference, Security Best Practices, Troubleshooting, Pitfalls, Verification
- **10 tools documented**: container_orch_list_hosts, container_orch_add_host, container_orch_remove_host, container_orch_connect, container_orch_disconnect, container_orch_status, container_orch_create_session, container_orch_send_prompt, container_orch_list_sessions, container_orch_stop_session
- Verified parseable by custom YAML frontmatter parser in `skill_format.py`

## OpencodeClient Unit Tests (2026-06-12)

Added `TestOpencodeClient` class (19 tests) to `tests/test_container_orchestrator.py`:

- **Mocking strategy**: Uses `patch.object(_orch.httpx, "AsyncClient")` with `autouse` fixture so **no test ever connects to a real server**. The fixture also yields the inner async-context-manager instance for per-test configuration.
- **Key patterns discovered**:
  - `_request()` calls `resp.json()` **synchronously** (no `await`) — so `json` on response mocks must be `Mock(return_value=...)`, **not** `AsyncMock`.
  - `send_prompt()` calls `resp.raise_for_status()` **synchronously** — same constraint: use `Mock` (not `AsyncMock`) for response mocks.
  - `httpx.Response(status_code=N)` without `request=` raises `RuntimeError` in httpx ≥0.28. Always pass `request=httpx.Request("METHOD", "http://...")` when testing via real `httpx.Response` objects.
  - `httpx.ConnectError` and `httpx.TimeoutException` can be used as `side_effect` directly on any mock attribute.
- **Coverage**: All 6 public methods (`health`, `create_session`, `send_prompt`, `list_sessions`, `stop_session`, `get_session`) plus error paths (ConnectError, TimeoutException, 401, 404, 5xx) and auth header construction.
- **Result**: 19/19 tests pass, 0 warnings, no real connections.
