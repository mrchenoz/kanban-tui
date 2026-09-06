import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kanban_tui import notelink

URI = "obsidian://open?vault=JCNotes&file=PB%20-%20PA%20stack%20%E4%B8%AD"


@pytest.fixture
def ntfy_server():
    """Fake ntfy: records the last POST, answers with the status set on it."""
    state = {"status": 200, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            state["requests"].append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    yield state
    server.shutdown()


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "ktui-token"
    path.write_text("tk_test\n")
    monkeypatch.setenv("KTUI_NTFY_TOKEN_FILE", str(path))
    return path


def test_note_name_from_uri_decodes_file():
    assert notelink.note_name_from_uri(URI) == "PB - PA stack 中"
    assert notelink.note_name_from_uri("obsidian://open?vault=X") == "note"


def test_ntfy_configured_follows_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KTUI_NTFY_TOKEN_FILE", str(tmp_path / "missing"))
    assert not notelink.ntfy_configured()
    (tmp_path / "missing").write_text("tk")
    assert notelink.ntfy_configured()


def test_send_note_link_posts_json(ntfy_server, token_file, monkeypatch):
    monkeypatch.setenv("KTUI_NTFY_URL", ntfy_server["url"] + "/")
    monkeypatch.setenv("KTUI_NTFY_TOPIC", "t")
    notelink.send_note_link(URI)
    (request,) = ntfy_server["requests"]
    assert request["path"] == "/"
    assert request["auth"] == "Bearer tk_test"
    assert request["body"] == {
        "topic": "t",
        "title": "PB - PA stack 中",
        "message": "Tap to open in Obsidian",
        "click": URI,
        "tags": ["spiral_notepad"],
    }


def test_send_note_link_reports_http_error(ntfy_server, token_file, monkeypatch):
    monkeypatch.setenv("KTUI_NTFY_URL", ntfy_server["url"])
    ntfy_server["status"] = 403
    with pytest.raises(notelink.NoteLinkError, match="ntfy 403"):
        notelink.send_note_link(URI)


def test_send_note_link_refuses_other_schemes(token_file):
    with pytest.raises(notelink.NoteLinkError, match="non-obsidian"):
        notelink.send_note_link("https://example.com")


def test_send_note_link_needs_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KTUI_NTFY_TOKEN_FILE", str(tmp_path / "none"))
    with pytest.raises(notelink.NoteLinkError, match="token file"):
        notelink.send_note_link(URI)


def test_notify_note_main_exit_codes(ntfy_server, token_file, monkeypatch, capsys):
    monkeypatch.setenv("KTUI_NTFY_URL", ntfy_server["url"])
    assert notelink.notify_note_main([]) == 2
    assert notelink.notify_note_main([URI]) == 0
    ntfy_server["status"] = 500
    assert notelink.notify_note_main([URI]) == 1
    assert "ntfy 500" in capsys.readouterr().err


def test_open_uri_command_finds_wayland_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(notelink.platform, "system", lambda: "Linux")
    (tmp_path / "wayland-1.lock").touch()
    monkeypatch.setattr(
        notelink.Path, "is_socket", lambda self: self.name == "wayland-1"
    )
    (tmp_path / "wayland-1").touch()
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    assert notelink.open_uri_command(URI, env) == ["xdg-open", URI]
    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["DISPLAY"] == ":0"


def test_open_uri_command_on_mac(monkeypatch):
    monkeypatch.setattr(notelink.platform, "system", lambda: "Darwin")
    env = {}
    assert notelink.open_uri_command(URI, env) == ["open", URI]
    assert env == {}


def test_notify_tap_opens_only_after_click(monkeypatch):
    monkeypatch.setattr(notelink.platform, "system", lambda: "Linux")
    monkeypatch.setenv("NTFY_RAW", json.dumps({"click": URI}))
    monkeypatch.setenv("NTFY_TITLE", "PB")
    monkeypatch.setenv("NTFY_MESSAGE", "tap")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        stdout = "default\n" if args[0] == "notify-send" else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(notelink.subprocess, "run", fake_run)
    assert notelink.notify_tap_main() == 0
    assert calls[0][:2] == ["notify-send", "--action=default=Open"]
    assert calls[0][-2:] == ["PB", "tap"]
    assert calls[1] == ["xdg-open", URI]

    calls.clear()
    monkeypatch.setattr(
        notelink.subprocess,
        "run",
        lambda args, **kw: (
            calls.append(args),
            subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        )[1],
    )
    assert notelink.notify_tap_main() == 0
    assert [c[0] for c in calls] == ["notify-send"]  # timed out: nothing opened


def test_notify_tap_ignores_other_links(monkeypatch):
    monkeypatch.setenv("NTFY_RAW", json.dumps({"click": "https://evil.example"}))
    monkeypatch.setattr(
        notelink.subprocess, "run", lambda *a, **k: pytest.fail("must not run")
    )
    assert notelink.notify_tap_main() == 0


def test_board_main_sets_defaults_without_overriding(monkeypatch):
    monkeypatch.delenv("KANBAN_TUI_NOTE_VAULT", raising=False)
    monkeypatch.setenv("KANBAN_TUI_LOGS_ROOT", "/custom")
    monkeypatch.setattr("kanban_tui.cli.cli", lambda: None)
    notelink.board_main()
    import os

    assert os.environ["KANBAN_TUI_NOTE_VAULT"] == "JCNotes"
    assert os.environ["KANBAN_TUI_LOGS_ROOT"] == "/custom"
