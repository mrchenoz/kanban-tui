"""Hand a card's note link to the user.

The board usually runs on a headless host over SSH, where nothing can open Obsidian.
Instead of reaching into the user's desktop, the link is sent as an `ntfy <https://ntfy.sh>`_
notification; the user taps it on whichever device is in hand and Obsidian opens there.
The host only connects outward and holds no credential for any other machine.

Console scripts installed with the package (``uv tool install``):

``ktui-board``
    Launch the TUI with the board-host defaults (vault name, logs root).
``ktui-notify-note <obsidian-uri>``
    What the TUI does on ``o`` when headless; handy for testing the ntfy setup.
``ktui-notify-tap``
    Desktop-side hook for ``ntfy subscribe``: shows the link as a notification and opens
    it only when the user clicks.
``ktui-open-uri <uri>``
    Open a URI in this machine's desktop session (used by ``ktui-notify-tap``).

Configuration (environment, all optional):

``KTUI_NTFY_URL``         server base URL      (default ``https://ntfy.jeremychen.au``)
``KTUI_NTFY_TOPIC``       topic                (default ``ktui-notes``)
``KTUI_NTFY_TOKEN_FILE``  access-token file    (default ``~/.config/ntfy/ktui-token``)
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

DEFAULT_NTFY_URL = "https://ntfy.jeremychen.au"
DEFAULT_NTFY_TOPIC = "ktui-notes"
OBSIDIAN_SCHEME = "obsidian://"


class NoteLinkError(Exception):
    """Sending or opening the note link failed; the message says why."""


# ---------------------------------------------------------------- ntfy sender


def ntfy_token_file() -> Path:
    return Path(
        os.getenv("KTUI_NTFY_TOKEN_FILE") or Path.home() / ".config/ntfy/ktui-token"
    ).expanduser()


def ntfy_configured() -> bool:
    """True when a token file exists, i.e. the host can send note links via ntfy."""
    return ntfy_token_file().is_file()


def note_name_from_uri(uri: str) -> str:
    """The ``file=`` part of an ``obsidian://open`` URI, decoded; ``note`` if absent."""
    query = parse_qs(urlparse(uri).query)
    return unquote(query.get("file", ["note"])[0])


def send_note_link(uri: str) -> None:
    """Publish ``uri`` to the ntfy topic as a tappable notification.

    Raises :class:`NoteLinkError` on a bad URI, a missing token, an HTTP error or an
    unreachable server. Uses the JSON publish endpoint (POST to the server root) so
    non-ASCII note names survive; the ``Title:``/``Click:`` headers would not.
    """
    if not uri.startswith(OBSIDIAN_SCHEME):
        raise NoteLinkError(f"refusing non-obsidian URI: {uri[:40]}")
    try:
        token = ntfy_token_file().read_text().strip()
    except OSError as exc:
        raise NoteLinkError(f"token file: {exc}") from exc
    server = os.getenv("KTUI_NTFY_URL", DEFAULT_NTFY_URL).rstrip("/")
    topic = os.getenv("KTUI_NTFY_TOPIC", DEFAULT_NTFY_TOPIC)
    body = json.dumps(
        {
            "topic": topic,
            "title": note_name_from_uri(uri),
            "message": "Tap to open in Obsidian",
            "click": uri,
            "tags": ["spiral_notepad"],
        }
    ).encode()
    request = urllib.request.Request(
        server,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5):
            pass
    except urllib.error.HTTPError as exc:
        raise NoteLinkError(f"ntfy {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NoteLinkError(f"ntfy unreachable: {exc}") from exc


def notify_note_main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: ktui-notify-note <obsidian-uri>", file=sys.stderr)
        return 2
    try:
        send_note_link(args[0])
    except NoteLinkError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------ desktop opener


def open_uri_command(uri: str, env: dict[str, str]) -> list[str]:
    """Command that opens ``uri`` in this machine's desktop session.

    ``env`` is updated in place with what ``xdg-open`` needs when called from outside
    the session (the ntfy subscriber service): ``XDG_RUNTIME_DIR``, the first Wayland
    socket found there, and a default ``DISPLAY``.
    """
    if platform.system() == "Darwin":
        return ["open", uri]
    runtime_dir = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not env.get("WAYLAND_DISPLAY"):
        for socket in sorted(Path(runtime_dir).glob("wayland-*")):
            if socket.suffix != ".lock" and socket.is_socket():
                env["WAYLAND_DISPLAY"] = socket.name
                break
    env.setdefault("DISPLAY", ":0")
    return ["xdg-open", uri]


def open_uri_here(uri: str) -> None:
    env = dict(os.environ)
    command = open_uri_command(uri, env)
    try:
        subprocess.run(command, env=env, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NoteLinkError(f"{command[0]}: {exc}") from exc


def open_uri_main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: ktui-open-uri <uri>", file=sys.stderr)
        return 2
    try:
        open_uri_here(args[0])
    except NoteLinkError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------- ntfy subscriber hook


def notify_tap_main() -> int:
    """``ntfy subscribe`` command hook: notify, and open the link only on click.

    ntfy passes the message in ``NTFY_TITLE``/``NTFY_MESSAGE`` and the full JSON in
    ``NTFY_RAW``. Only ``obsidian://`` links are honoured. Configure with::

        subscribe:
          - topic: ktui-notes
            command: ktui-notify-tap
    """
    try:
        click = str(json.loads(os.getenv("NTFY_RAW", "{}")).get("click", ""))
    except ValueError:
        return 0
    if not click.startswith(OBSIDIAN_SCHEME):
        return 0
    title = os.getenv("NTFY_TITLE") or "Note"
    message = os.getenv("NTFY_MESSAGE") or "Tap to open in Obsidian"
    try:
        if platform.system() == "Darwin":
            # terminal-notifier opens -open's URL itself when the notification is clicked.
            subprocess.run(
                [
                    "terminal-notifier",
                    "-title",
                    title,
                    "-message",
                    message,
                    "-open",
                    click,
                ],
                check=True,
                timeout=15,
            )
            return 0
        if shutil.which("omarchy-notification-send"):
            # Omarchy stores the click command with the toast, so a click from the
            # notification history still opens the note, with nothing left waiting.
            subprocess.run(
                [
                    "omarchy-notification-send",
                    "--app-name",
                    "kanban-tui",
                    title,
                    message,
                    "--exec",
                    "ktui-open-uri",
                    click,
                ],
                check=True,
                timeout=15,
            )
            return 0
        # notify-send prints the action key when the user picks it, nothing on
        # timeout. The key must be "default": daemons that draw no buttons
        # (mako, dunst) fire only the default action, on a click of the toast.
        chosen = subprocess.run(
            ["notify-send", "--action=default=Open", "-t", "20000", title, message],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        ).stdout.strip()
        if chosen == "default":
            open_uri_here(click)
    except (OSError, subprocess.SubprocessError, NoteLinkError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------- board launcher

BOARD_DEFAULTS = {
    "KANBAN_TUI_NOTE_VAULT": "JCNotes",
    "KANBAN_TUI_LOGS_ROOT": "~/Documents/PKM/JCNotes/JCNotes",
}


def board_main() -> None:
    """Launch the TUI with the board-host defaults; anything already set wins.

    Launch from a client::

        ssh -4 jeremy@p5jc -t tmux new-session -A -s kanban ktui-board
    """
    for key, value in BOARD_DEFAULTS.items():
        os.environ.setdefault(key, os.path.expanduser(value))
    from kanban_tui.cli import cli

    cli()
