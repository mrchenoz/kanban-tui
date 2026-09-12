import asyncio
import contextlib
import signal
import sys
from pathlib import Path

import click

from kanban_tui.app import KanbanTui
from kanban_tui.config import Backends
from kanban_tui.utils import print_to_console


@click.command()
@click.pass_context
@click.pass_obj
@click.option(
    "--start-server",
    is_flag=True,
    default=False,
    type=click.BOOL,
    help="Starts the actual mcp server",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
    help=(
        "stdio: one agent on this machine, started by its MCP client. "
        "http: one shared Streamable-HTTP endpoint for several agents; "
        "a bearer token is required on every request."
    ),
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="[http] Address to bind. Use this machine's LAN address, not 0.0.0.0.",
)
@click.option(
    "--port", type=int, default=5057, show_default=True, help="[http] Port to bind."
)
@click.option(
    "--token-file",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "[http] File holding the bearer token, mode 0600. "
        "Default: <config dir>/mcp-token if it exists, else $KTUI_MCP_TOKEN."
    ),
)
@click.option(
    "--gen-token",
    is_flag=True,
    default=False,
    help="Write a new random bearer token to --token-file (or its default path) and exit.",
)
@click.option(
    "--ssl-certfile",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="[http] Serve HTTPS with this certificate (PEM).",
)
@click.option(
    "--ssl-keyfile",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="[http] Private key for --ssl-certfile.",
)
@click.option(
    "--exclude",
    default=None,
    help=(
        "Regex of subcommands to hide from agents, matched against e.g. 'board delete'. "
        "Example: '^(board|column) delete$'."
    ),
)
@click.option(
    "--aggregate",
    type=click.Choice(["root", "group", "none"]),
    default="root",
    show_default=True,
    help=(
        "Tool shape. root: a single `ktui` tool taking an args array (the shape agents "
        "know from the stdio server). none: one typed tool per subcommand, e.g. ktui.task.create."
    ),
)
@click.option(
    "--serialise/--no-serialise",
    default=True,
    show_default=True,
    help="[http] Run tool calls one at a time so concurrent agents queue instead of racing.",
)
@click.option(
    "--ktui-bin",
    default=None,
    help="[http] The ktui executable tool calls run. Default: the one running this command.",
)
@click.option(
    "--log-level", default="info", show_default=True, help="[http] uvicorn log level."
)
def mcp(
    app: KanbanTui,
    ctx,
    start_server: bool,
    transport: str,
    host: str,
    port: int,
    token_file: Path | None,
    gen_token: bool,
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    exclude: str | None,
    aggregate: str,
    serialise: bool,
    ktui_bin: str | None,
    log_level: str,
):
    """
    Starts the mcp server, exposes the CLI Interface Commands
    """
    try:
        from mcp.server.stdio import stdio_server

        from kanban_tui import mcp_http
    except ImportError:
        print_to_console(
            "Please install [yellow]kanban-tui\\[mcp][/] to use kanban-tui as an mcp server."
        )
        return

    if gen_token:
        path = token_file or mcp_http.DEFAULT_TOKEN_FILE
        if path.exists():
            raise click.exceptions.UsageError(
                f"{path} already exists. Remove it first to rotate the token "
                "(then restart the server and update every agent's client config)."
            )
        token = mcp_http.generate_token()
        mcp_http.write_token(path, token)
        print_to_console(
            f"Wrote {path} (mode 0600). Give agents this value as their Bearer token:"
        )
        click.echo(token)
        return

    if app.config.backend.mode != Backends.SQLITE:
        raise click.exceptions.UsageError(
            f"""
            Currently using `{app.config.backend.mode}` backend.
            Please change the backend to `{Backends.SQLITE}` before using the `mcp` command.
            """
        )
    if (ssl_certfile is None) != (ssl_keyfile is None):
        raise click.exceptions.UsageError(
            "--ssl-certfile and --ssl-keyfile must be given together."
        )

    if not start_server:
        if transport == "http":
            scheme = "https" if ssl_certfile else "http"
            print_to_console(
                "To add this endpoint to an agent's [yellow]claude[/], run there:"
            )
            print_to_console(
                f"[blue]claude mcp add --transport http --scope user ktui "
                f"{scheme}://{host}:{port}{mcp_http.MCP_PATH} "
                f"--header 'Authorization: Bearer <TOKEN>'[/]"
            )
            print_to_console(
                "Start the server here with [blue]ktui mcp --start-server --transport http "
                f"--host {host} --port {port}[/] (token: `ktui mcp --gen-token`)."
            )
        else:
            print_to_console(
                "To add [yellow]kanban-tui[/] as an mcp-server, e.g. for `claude`, run:"
            )
            print_to_console(
                "[blue]claude mcp add kanban-tui --transport stdio --scope user -- ktui mcp --start-server[/]"
            )
        return

    if transport == "http":
        if token_file is None and mcp_http.DEFAULT_TOKEN_FILE.is_file():
            token_file = mcp_http.DEFAULT_TOKEN_FILE
        try:
            token = mcp_http.load_token(token_file)
            mcp_http.serve(
                ctx.parent.command,
                host=host,
                port=port,
                token=token,
                ktui_bin=ktui_bin,
                exclude=exclude,
                aggregate=aggregate,
                serialise=serialise,
                ssl_certfile=ssl_certfile,
                ssl_keyfile=ssl_keyfile,
                log_level=log_level,
            )
        except (mcp_http.TokenError, FileNotFoundError) as e:
            raise click.exceptions.UsageError(str(e)) from e
        return

    mcp_server = mcp_http.build_mcp_server(
        ctx.parent.command, exclude=exclude, aggregate=aggregate, serialise=False
    )

    # Create a shutdown event for graceful cleanup
    shutdown_event = asyncio.Event()

    def signal_handler(sig, frame):
        """Handle shutdown signals gracefully"""
        shutdown_event.set()

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    async def run_stdio():
        try:
            async with stdio_server() as (read_stream, write_stream):
                server_task = asyncio.create_task(
                    mcp_server.server.run(
                        read_stream,
                        write_stream,
                        mcp_server.server.create_initialization_options(),
                    )
                )

                shutdown_task = asyncio.create_task(shutdown_event.wait())

                # Wait for either the server to complete or shutdown signal
                _done, pending = await asyncio.wait(
                    {server_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel any pending tasks
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        except Exception as e:
            print_to_console(f"[red]MCP server error: {e}[/]")
            sys.exit(1)
        finally:
            pass

    try:
        asyncio.run(run_stdio())
    except KeyboardInterrupt:
        pass
    finally:
        sys.exit(0)
