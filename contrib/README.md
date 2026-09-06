# contrib — glue for running the board over SSH

The board database lives on one headless host (the "board host"). Other machines run the TUI
over SSH. Pressing `o` on a card there cannot open Obsidian on the host, so the link is sent
as a notification instead; the user taps it on whatever device is in hand and Obsidian opens
**there**. The host only ever connects outward (to an [ntfy](https://ntfy.sh) server); it holds
no credentials for any desktop. `v` (view log) just needs the vault path.

| Script | Lives on | Does |
|---|---|---|
| `ktui-board` | board host, `~/.local/bin/` | Launches `kanban-tui` with `KANBAN_TUI_NOTE_OPEN_CMD` set to `ktui-notify-note {uri}` (unless already set) and `KANBAN_TUI_LOGS_ROOT` pointing at the vault so `v` finds logs. |
| `ktui-notify-note` | board host, `~/.local/bin/` | Posts the `obsidian://` URI to an ntfy topic as a notification (title = note name, click = URI). Python stdlib only. Token in `~/.config/ntfy/ktui-token`; server/topic via `KTUI_NTFY_URL` / `KTUI_NTFY_TOPIC`. Non-zero exit on failure, which the TUI shows. |
| `ktui-notify-tap` | each desktop, `~/.local/bin/` | Hook for the ntfy CLI subscriber: shows the link as a desktop notification and opens it only when clicked (`notify-send --action` + `open-uri` on Linux, `terminal-notifier -open` on macOS). Ignores anything that is not `obsidian://`. Phones use the ntfy app instead; it follows the click URL natively. |
| `open-uri` | each desktop, `~/.local/bin/` | Opens a URI in that machine's desktop session: `open` on macOS, `xdg-open` with the Wayland socket found on Linux. |

Launch from a client:

```sh
ssh -4 <user>@<board-host> -t tmux new-session -A -s kanban ~/.local/bin/ktui-board
```

Server side: any ntfy server (self-hosted or ntfy.sh) with a topic the host's token may write
to and the devices' tokens may read. Desktop subscriber config (`~/.config/ntfy/client.yml`
on Linux, `~/Library/Application Support/ntfy/client.yml` on macOS):

```yaml
default-host: https://ntfy.example.com
default-token: tk_…
subscribe:
  - topic: ktui-notes
    command: ~/.local/bin/ktui-notify-tap
```

Run it in the desktop session (`systemctl --user enable --now ntfy-client` on Linux). Edit the
vault path and server URL in the scripts for your setup. `-4` on the launch command only matters
if the client's firewall treats IPv4 and IPv6 differently.

Test the sender on the board host exactly as `o` runs it (use a URI with `&file=`):

```sh
~/.local/bin/ktui-notify-note 'obsidian://open?vault=JCNotes&file=Some%20Note'
```
