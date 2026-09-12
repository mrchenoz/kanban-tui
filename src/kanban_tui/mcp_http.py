"""HTTP(S) transport for `ktui mcp`: pycli-mcp's Streamable-HTTP server behind a bearer token.

`ktui mcp --start-server` pipes pycli-mcp's low-level MCP server through stdio, which suits one
agent on the same machine. For several agents sharing one board over the LAN this module serves
the *same* pycli-mcp server over Streamable HTTP (endpoint `/mcp`) and adds what a shared
endpoint needs:

* a bearer token checked on every request (constant-time compare; token from a 0600 file),
* optional TLS via uvicorn's `ssl_certfile` / `ssl_keyfile`,
* an `--exclude` regex so a deployment can hide e.g. `board delete` from agents,
* serialised tool calls, so agents writing at the same moment queue for milliseconds instead
  of racing on sqlite locks or on config.toml,
* resolution of the `ktui` binary the tool calls run (each call is a `ktui ...` subprocess, the
  same way the stdio server works).

Everything here needs the `[mcp]` extra; import it lazily.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pycli_mcp import CommandMCPServer, CommandQuery
from starlette.responses import JSONResponse

from kanban_tui.constants import CONFIG_DIR

if TYPE_CHECKING:
    import click

log = logging.getLogger("kanban_tui.mcp_http")

TOKEN_ENV_VAR = "KTUI_MCP_TOKEN"
DEFAULT_TOKEN_FILE = CONFIG_DIR / "mcp-token"
DEFAULT_INCLUDE = r"task|board|column|category"  # what `ktui mcp` has always exposed
MIN_TOKEN_LEN = 16
MCP_PATH = "/mcp"


# --------------------------------------------------------------------------- token
class TokenError(RuntimeError):
    """The bearer token could not be loaded safely."""


def load_token(token_file: Path | None) -> str:
    """Read the token from a 0600 file, or from $KTUI_MCP_TOKEN when `token_file` is None."""
    if token_file is not None:
        if not token_file.is_file():
            raise TokenError(
                f"token file not found: {token_file} (create one with `ktui mcp --gen-token`)"
            )
        mode = stat.S_IMODE(token_file.stat().st_mode)
        if mode & 0o077:
            raise TokenError(
                f"token file {token_file} is mode {mode:04o}; it must not be group/world "
                f"readable (chmod 600 {token_file})"
            )
        token = token_file.read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get(TOKEN_ENV_VAR, "").strip()
        if not token:
            raise TokenError(f"no token file given and ${TOKEN_ENV_VAR} is empty")
    if len(token) < MIN_TOKEN_LEN:
        raise TokenError(f"token is shorter than {MIN_TOKEN_LEN} characters")
    if any(c.isspace() for c in token):
        raise TokenError("token must not contain whitespace")
    return token


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def write_token(path: Path, token: str) -> None:
    """Write `token` to `path` with mode 0600 (parent dir created, 0700 when new)."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    path.chmod(0o600)


# --------------------------------------------------------------------------- auth
class BearerAuth:
    """Pure ASGI middleware: every HTTP/WebSocket request needs `Authorization: Bearer <token>`.

    Lifespan events pass through. Nothing else is served without the token, on any path, so an
    unauthenticated caller learns nothing about the endpoint.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token.encode("utf-8")

    def authorised(self, scope: dict[str, Any]) -> bool:
        header = b""
        for name, value in scope.get("headers", ()):
            if name == b"authorization":
                header = value
                break
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != b"bearer":
            return False
        return hmac.compare_digest(parts[1].strip(), self._token)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan" or self.authorised(scope):
            await self.app(scope, receive, send)
            return
        log.warning(
            "rejected unauthenticated %s from %s", scope["type"], scope.get("client")
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="ktui-mcp"'},
        )
        await response(scope, receive, send)


# --------------------------------------------------------------------------- ktui binary
def resolve_ktui(explicit: str | None = None) -> Path:
    """The `ktui` executable that tool calls will run.

    Order: explicit path, the script we were started as (`ktui mcp ...`), the `ktui` next to
    this interpreter, then PATH.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name in {"ktui", "kanban-tui"}:
        candidates.append(argv0)
    candidates.append(Path(sys.executable).resolve().parent / "ktui")
    found = shutil.which("ktui")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise FileNotFoundError(
        "could not find a `ktui` executable for tool calls; pass --ktui-bin"
    )


def put_on_path(ktui: Path) -> None:
    """Make `ktui` resolve to this binary in the subprocesses pycli-mcp spawns."""
    path = os.environ.get("PATH", "")
    os.environ["PATH"] = (
        f"{ktui.parent}{os.pathsep}{path}" if path else str(ktui.parent)
    )


def ktui_version(ktui: Path) -> str:
    out = subprocess.run(
        [str(ktui), "--version"], capture_output=True, text=True, check=False
    )
    return (out.stdout or out.stderr).strip()


# --------------------------------------------------------------------------- server
def subcommand_path(root: click.Command, argv: list[str]) -> str | None:
    """Resolve `argv` (without the executable) to the click subcommand it would run.

    Returns e.g. "task create", or None when argv does not reach a leaf command (bare root,
    `--web`, `--version`, a group with no subcommand, an unknown name).
    """
    import click as _click

    current: _click.Command = root
    path: list[str] = []
    for token in argv:
        if not isinstance(current, _click.Group):
            break
        sub = current.commands.get(token) if not token.startswith("-") else None
        if sub is None:
            if token.startswith("-"):
                continue  # a group-level option such as --web; never reaches a command by itself
            return None
        path.append(sub.name or token)
        current = sub
    if isinstance(current, _click.Group) or not path:
        return None
    return " ".join(path)


class SerialisingCommandMCPServer(CommandMCPServer):
    """CommandMCPServer that enforces the tool surface on every call and can serialise calls.

    pycli-mcp's `root` aggregate exposes one `ktui` tool taking a raw `args` array, and the
    include/exclude filters only shape the tool *description*. Here every call is resolved
    to the click subcommand it would run and refused unless that subcommand is on the exposed
    surface, so `--exclude` (and the include filter) hold for every tool shape, and things
    like `ktui --web` or `ktui mcp` cannot be started through the endpoint.
    """

    def __init__(
        self,
        *args: Any,
        root: click.Command,
        allowed: frozenset[str],
        serialise: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._root = root
        self._allowed = allowed
        self._lock: asyncio.Lock | None = asyncio.Lock() if serialise else None

    def refusal(self, req: Any) -> str | None:
        """Why this call must not run, or None if it is on the exposed surface."""
        command = self.commands[req.params.name].metadata.construct(
            req.params.arguments
        )
        path = subcommand_path(self._root, list(command[1:]))
        if path is None:
            return (
                "refused: arguments do not name one of the exposed ktui subcommands "
                f"({', '.join(sorted(self._allowed))})"
            )
        if path not in self._allowed:
            return f"refused: `ktui {path}` is not exposed by this server"
        return None

    async def call_tool_handler(self, req: Any) -> Any:
        from mcp.types import CallToolResult, ServerResult, TextContent

        if req.params.name not in self.commands:
            reason: str | None = f"refused: unknown tool `{req.params.name}`"
        else:
            try:
                reason = self.refusal(req)
            except Exception as e:  # malformed arguments
                reason = f"refused: {e}"
        if reason is not None:
            log.warning("%s", reason)
            return ServerResult(
                CallToolResult(
                    content=[TextContent(type="text", text=reason)], isError=True
                )
            )
        if self._lock is None:
            return await super().call_tool_handler(req)
        async with self._lock:
            return await super().call_tool_handler(req)


def exposed_subcommands(
    root: click.Command, *, include: str, exclude: str | None
) -> frozenset[str]:
    """Leaf subcommand paths ("task create", ...) that pass the include/exclude filters."""
    leaves = CommandQuery(
        command=root,
        name="ktui",
        include=include,
        exclude=exclude or None,
        aggregate="none",
    )
    return frozenset(" ".join(m.path.split()[1:]) for m in leaves)


def build_mcp_server(
    root: click.Command,
    *,
    include: str = DEFAULT_INCLUDE,
    exclude: str | None = None,
    aggregate: str = "root",
    serialise: bool = True,
) -> SerialisingCommandMCPServer:
    """The pycli-mcp server for ktui's click root, with the given tool filters.

    `aggregate="root"` is the shape `ktui mcp` has always had: one `ktui` tool taking an
    `args` array. `"none"` gives one typed tool per subcommand (`ktui.task.create` ...).
    """
    query = CommandQuery(
        command=root,
        name="ktui",
        include=include,
        exclude=exclude or None,
        aggregate=aggregate,  # type: ignore[arg-type]
    )
    return SerialisingCommandMCPServer(
        commands=[query],
        stateless=True,
        root=root,
        allowed=exposed_subcommands(root, include=include, exclude=exclude),
        serialise=serialise,
    )


class MCPEndpoint:
    """Minimal ASGI app: `/mcp` (with or without trailing slash) goes to the session manager,
    the session manager's lifetime follows the server lifespan, everything else is 404.

    Serving the endpoint directly rather than through a Starlette `Mount` avoids the 307
    redirect from `/mcp` to `/mcp/` that some MCP clients will not follow.
    """

    def __init__(self, server: CommandMCPServer) -> None:
        self.server = server
        self._running: Any = None

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self._running = self.server.session_manager.run()
                await self._running.__aenter__()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._running is not None:
                    await self._running.__aexit__(None, None, None)
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] == "http" and scope["path"].rstrip("/") == MCP_PATH:
            await self.server.session_manager.handle_request(scope, receive, send)
            return
        await JSONResponse({"error": "not found"}, status_code=404)(
            scope, receive, send
        )


def build_app(
    root: click.Command,
    token: str,
    *,
    include: str = DEFAULT_INCLUDE,
    exclude: str | None = None,
    aggregate: str = "root",
    serialise: bool = True,
) -> tuple[BearerAuth, SerialisingCommandMCPServer]:
    """(ASGI app, mcp server). The MCP endpoint is served at `/mcp`."""
    server = build_mcp_server(
        root, include=include, exclude=exclude, aggregate=aggregate, serialise=serialise
    )
    return BearerAuth(MCPEndpoint(server), token), server


def serve(
    root: click.Command,
    *,
    host: str,
    port: int,
    token: str,
    ktui_bin: str | None = None,
    include: str = DEFAULT_INCLUDE,
    exclude: str | None = None,
    aggregate: str = "root",
    serialise: bool = True,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    log_level: str = "info",
) -> None:
    """Run the HTTP(S) MCP server until interrupted."""
    import uvicorn

    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ktui = resolve_ktui(ktui_bin)
    put_on_path(ktui)
    app, server = build_app(
        root,
        token,
        include=include,
        exclude=exclude,
        aggregate=aggregate,
        serialise=serialise,
    )
    scheme = "https" if ssl_certfile else "http"
    log.info("ktui binary: %s (%s)", ktui, ktui_version(ktui))
    log.info(
        "board: config=%s database=%s",
        os.environ.get("KANBAN_TUI_CONFIG_FILE", "(default)"),
        os.environ.get("KANBAN_TUI_DATABASE_FILE", "(from config)"),
    )
    log.info("tools: %s", ", ".join(sorted(server.commands)))
    log.info(
        "listening on %s://%s:%d%s (bearer token required, calls %s)",
        scheme,
        host,
        port,
        MCP_PATH,
        "serialised" if serialise else "concurrent",
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        proxy_headers=False,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
