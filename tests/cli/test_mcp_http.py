"""Tests for the HTTP transport of `ktui mcp` (kanban_tui.mcp_http)."""

import asyncio
import contextlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

pytest.importorskip("pycli_mcp")

from httpx import ASGITransport
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from kanban_tui import mcp_http
from kanban_tui.cli import cli

TOKEN = "unit-test-token-0123456789abcdef"


# --------------------------------------------------------------------------- token handling
def test_load_token_rejects_group_readable_file(tmp_path: Path):
    path = tmp_path / "token"
    path.write_text(TOKEN)
    path.chmod(0o640)
    with pytest.raises(mcp_http.TokenError, match="must not be group/world readable"):
        mcp_http.load_token(path)


def test_load_token_rejects_short_token(tmp_path: Path):
    path = tmp_path / "token"
    path.write_text("short")
    path.chmod(0o600)
    with pytest.raises(mcp_http.TokenError, match="shorter than"):
        mcp_http.load_token(path)


def test_load_token_missing_file(tmp_path: Path):
    with pytest.raises(mcp_http.TokenError, match="not found"):
        mcp_http.load_token(tmp_path / "absent")


def test_load_token_from_env(monkeypatch):
    monkeypatch.setenv(mcp_http.TOKEN_ENV_VAR, f"  {TOKEN}\n")
    assert mcp_http.load_token(None) == TOKEN
    monkeypatch.setenv(mcp_http.TOKEN_ENV_VAR, "")
    with pytest.raises(mcp_http.TokenError, match="empty"):
        mcp_http.load_token(None)


def test_write_token_creates_0600_file(tmp_path: Path):
    path = tmp_path / "sub" / "token"
    mcp_http.write_token(path, TOKEN)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert mcp_http.load_token(path) == TOKEN


# --------------------------------------------------------------------------- auth middleware
async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_bearer_auth_middleware():
    app = mcp_http.BearerAuth(_ok_app, TOKEN)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/mcp")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        assert r.json() == {"error": "unauthorized"}

        r = await client.get("/mcp", headers={"Authorization": f"Bearer {TOKEN}x"})
        assert r.status_code == 401

        r = await client.get("/mcp", headers={"Authorization": f"Basic {TOKEN}"})
        assert r.status_code == 401

        r = await client.get("/anything", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200
        assert r.text == "ok"

        r = await client.get("/mcp", headers={"Authorization": f"bearer {TOKEN}"})
        assert r.status_code == 200


# --------------------------------------------------------------------------- tool surface
def test_tool_shapes_and_exclude():
    root = mcp_http.build_mcp_server(cli)
    assert list(root.commands) == ["ktui"]
    assert "args" in root.commands["ktui"].tool.inputSchema["properties"]

    typed = mcp_http.build_mcp_server(cli, aggregate="none")
    names = sorted(typed.commands)
    assert all(re.match(r"^ktui\.(task|board|column|category)\.", n) for n in names)
    assert {"ktui.task.create", "ktui.task.move", "ktui.board.delete"} <= set(names)
    for hidden in ("ktui.demo", "ktui.mcp", "ktui.skill", "ktui.info", "ktui.clear"):
        assert not any(n.startswith(hidden) for n in names)

    locked = mcp_http.build_mcp_server(
        cli, aggregate="none", exclude=r"^(board|column) delete$"
    )
    assert "ktui.board.delete" not in locked.commands
    assert "ktui.task.delete" in locked.commands


def test_subcommand_path_resolution():
    sp = mcp_http.subcommand_path
    assert sp(cli, ["task", "create", "Title", "--column", "2"]) == "task create"
    assert sp(cli, ["--web", "task", "list", "--json"]) == "task list"
    assert sp(cli, ["board", "delete", "1"]) == "board delete"
    assert sp(cli, ["--web"]) is None
    assert sp(cli, []) is None
    assert sp(cli, ["task"]) is None
    assert sp(cli, ["nonsense", "create"]) is None
    assert sp(cli, ["--version"]) is None


def test_exposed_subcommands():
    allowed = mcp_http.exposed_subcommands(
        cli, include=mcp_http.DEFAULT_INCLUDE, exclude=r"^(board|column) delete$"
    )
    assert {"task create", "task list", "task move", "board create"} <= allowed
    assert "board delete" not in allowed
    assert not any(
        p.startswith(("demo", "mcp", "skill", "info", "clear")) for p in allowed
    )


# --------------------------------------------------------------------------- cli
def test_mcp_http_instruction(test_app):
    runner = CliRunner()
    result = runner.invoke(
        cli, args=["mcp", "--transport", "http", "--host", "10.0.0.5"], obj=test_app
    )
    assert result.exit_code == 0
    assert (
        "claude mcp add --transport http --scope user ktui http://10.0.0.5:5057/mcp"
        in result.output
    )
    assert "Bearer <TOKEN>" in result.output


def test_mcp_http_missing_token_file(test_app, tmp_path: Path, monkeypatch):
    monkeypatch.delenv(mcp_http.TOKEN_ENV_VAR, raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        args=[
            "mcp",
            "--start-server",
            "--transport",
            "http",
            "--token-file",
            str(tmp_path / "absent"),
        ],
        obj=test_app,
    )
    assert result.exit_code == 2
    assert "token file not found" in result.output


def test_mcp_ssl_options_must_pair(test_app, tmp_path: Path):
    cert = tmp_path / "cert.pem"
    cert.write_text("x")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        args=["mcp", "--transport", "http", "--ssl-certfile", str(cert)],
        obj=test_app,
    )
    assert result.exit_code == 2
    assert "must be given together" in result.output


def test_mcp_gen_token(test_app, tmp_path: Path):
    path = tmp_path / "mcp-token"
    runner = CliRunner()
    result = runner.invoke(
        cli, args=["mcp", "--gen-token", "--token-file", str(path)], obj=test_app
    )
    assert result.exit_code == 0, result.output
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    token = result.output.strip().splitlines()[-1]
    assert len(token) >= 32
    assert path.read_text().strip() == token

    result = runner.invoke(
        cli, args=["mcp", "--gen-token", "--token-file", str(path)], obj=test_app
    )
    assert result.exit_code == 2
    assert "already exists" in result.output


# --------------------------------------------------------------------------- end to end
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(test_config, tmp_path: Path):
    """`ktui mcp --start-server --transport http` on a random port against the test board."""
    ktui = Path(sys.executable).parent / "ktui"
    if not ktui.is_file():
        pytest.skip("ktui console script not installed in this environment")
    token_file = tmp_path / "mcp-token"
    mcp_http.write_token(token_file, TOKEN)
    port = _free_port()
    proc = subprocess.Popen(
        [
            str(ktui),
            "mcp",
            "--start-server",
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--token-file",
            str(token_file),
            "--exclude",
            r"^(board|column) delete$",
            "--log-level",
            "warning",
        ],
        env=dict(
            os.environ
        ),  # test_config put KANBAN_TUI_CONFIG_FILE / _DATABASE_FILE here
    )
    deadline = time.time() + 30
    try:
        while True:
            if proc.poll() is not None:
                pytest.fail(f"server exited early with {proc.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if time.time() > deadline:
                    pytest.fail("server did not start listening")
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/mcp", os.environ["KANBAN_TUI_DATABASE_FILE"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def _json(result) -> list:
    """ktui prints a header line before the JSON of `board list --json`, and a sentence instead
    of `[]` for an empty `task list --json`; take the JSON array if there is one."""
    text = _text(result)
    start = text.find("[")
    return json.loads(text[start:]) if start >= 0 else []


_tasks = _json


@contextlib.asynccontextmanager
async def _Session(url: str):
    client = httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def _ktui(session, *args: str):
    return await session.call_tool("ktui", arguments={"args": list(args)})


async def test_http_end_to_end(http_server):
    url, db_path = http_server

    # unauthenticated requests are refused
    async with httpx.AsyncClient() as client:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {"Accept": "application/json, text/event-stream"}
        assert (await client.post(url, json=body, headers=headers)).status_code == 401
        assert (
            await client.post(
                url,
                json=body,
                headers={**headers, "Authorization": "Bearer nope-000000000000"},
            )
        ).status_code == 401
        assert (await client.get(url.replace("/mcp", "/other"))).status_code == 401

    async with _Session(url) as s:
        tools = (await s.list_tools()).tools
        assert [t.name for t in tools] == ["ktui"]
        assert "board delete" not in tools[0].description
        assert "task delete" in tools[0].description

        r = await _ktui(s, "board", "create", "Smoke board")
        assert not r.isError and "board_id = 1" in _text(r)

        r = await _ktui(
            s, "task", "create", "Smoke card", "--description", "made over MCP"
        )
        assert not r.isError, _text(r)
        task_id = int(re.search(r"task_id\s*=\s*(\d+)", _text(r)).group(1))

        r = await _ktui(s, "task", "list", "--json")
        card = next(t for t in _tasks(r) if t["task_id"] == task_id)
        assert card["column"] == 1

        r = await _ktui(s, "task", "move", str(task_id), "2")
        assert not r.isError, _text(r)
        r = await _ktui(s, "task", "list", "--json")
        assert next(t for t in _tasks(r) if t["task_id"] == task_id)["column"] == 2

        r = await _ktui(
            s, "task", "update", str(task_id), "--title", "Smoke card (updated)"
        )
        assert not r.isError, _text(r)
        r = await _ktui(s, "task", "list", "--json")
        assert next(t for t in _tasks(r) if t["task_id"] == task_id)["title"] == (
            "Smoke card (updated)"
        )

        # hidden by --exclude: refused before any subprocess runs, even via the root tool
        r = await _ktui(s, "board", "delete", "1", "--no-confirm")
        assert r.isError and "not exposed" in _text(r)
        r = await _ktui(s, "board", "list", "--json")
        assert any(b["board_id"] == 1 for b in _json(r))
        for bad in (["--web"], ["mcp", "--start-server"], ["demo"], ["task"], []):
            r = await _ktui(s, *bad)
            assert r.isError and "refused" in _text(r), bad

        r = await _ktui(s, "task", "delete", str(task_id), "--no-confirm")
        assert not r.isError, _text(r)
        r = await _ktui(s, "task", "list", "--json")
        assert all(t["task_id"] != task_id for t in _tasks(r))

    # several agents writing at once
    async def agent(i: int):
        async with _Session(url) as s2:
            return await _ktui(s2, "task", "create", f"concurrent-{i}")

    results = await asyncio.gather(*(agent(i) for i in range(6)))
    assert all(not r.isError for r in results), [_text(r) for r in results]
    async with _Session(url) as s:
        titles = {t["title"] for t in _tasks(await _ktui(s, "task", "list", "--json"))}
    assert {f"concurrent-{i}" for i in range(6)} <= titles
    with sqlite3.connect(db_path) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
