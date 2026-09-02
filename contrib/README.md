# contrib — glue for running the board over SSH

The board database lives on one host (the "board host"). Other machines run the TUI over
SSH. Two small scripts make the `o` (open note) and `v` (view log) keybinds work in that setup.

| Script | Lives on | Does |
|---|---|---|
| `ktui-board` | the board host, `~/.local/bin/` | Launches `kanban-tui`. When started over SSH it sets `KANBAN_TUI_NOTE_OPEN_CMD` to `ssh <user>@<ssh client> ~/.local/bin/open-uri {uri}`, so `o` opens the note on the machine you are sitting at. Sets `KANBAN_TUI_LOGS_ROOT` to the vault so `v` finds logs. Started locally it leaves the opener alone. |
| `open-uri` | every client (Linux or macOS), `~/.local/bin/` | Opens a URI in that machine's desktop session: `open` on macOS, `xdg-open` with the Wayland socket found on Linux. |

Launch from a client:

```sh
ssh -4 <user>@<board-host> -t tmux new-session -A -s kanban ~/.local/bin/ktui-board
```

Client prerequisites: sshd on (macOS: System Settings → General → Sharing → Remote Login), the board
host's public key in `~/.ssh/authorized_keys`, and `open-uri` installed. `-4` only matters if the
client's firewall allows port 22 for IPv4 but not IPv6. Edit the user name and vault path in
`ktui-board` for your setup.
