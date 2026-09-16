# Spec – `task move` must work on any board, non-interactively

| | |
|---|---|
| status | ready-to-build |
| created | 2026-09-16 |
| for | Neo (builds + tests, ships on `main`) |
| owner | Hunter (reinstalls on p5jc, updates the agent rule) |
| target version | `0.21.2+jc.5` |

## Goal

An agent using the shared MCP endpoint (`ktui mcp --transport http`, see README "MCP over
HTTP(S)") can move a card on **any** board, not only the one Jeremy last opened in the TUI. Today
`task move` aborts under the service unless the card is on the active board, so agents are told to
report "move blocked" and stop. That rule goes away with this change.

## Background (verified 2026-09-16 against `main` = `0.21.2+jc.4`)

`src/kanban_tui/cli/task_commands.py`, `move_task()` (around lines 479–535):

1. **Two interactive prompts.** When the card's current column or the target column is not on
   `app.backend.active_board`, the command calls `click.confirm(..., abort=True)`. The HTTP MCP
   service runs each tool call as a `ktui ...` subprocess with no TTY, so the prompt reads EOF and
   the call ends with `Aborted!`. `task delete` already has the precedent for this situation: a
   `--no-confirm` flag (line ~541).
2. **The wrong board's columns are used for the move itself.** After the prompts, the code takes
   `active_board.start_column` for the dependency check (`task.can_move_to_column`) and
   `active_board.reset_column / start_column / finish_column` for `task.update_task_status`. On a
   non-active board those ids belong to a different board, so:
   - a card moved into that board's *Doing* never gets its start date;
   - a card moved into that board's *Done* never gets its finish date, so `task.finished` stays
     false and **every card that depends on it stays blocked** (dependency check reads
     `t.finished`);
   - the dependency check itself compares the target against the wrong column id, so blocking is
     skipped or applied to the wrong column.
   The prompts have been hiding this: nobody moves cards on a non-active board interactively
   without answering "y", and the TUI itself only moves cards on the active board.

Boards in production: DT(1) Coder(2) SD26(3) Maths(4) edbase(5); each has its own
reset/start/finish column ids (`ktui board list --json`).

## The ask

### 1. Resolve the board from the columns, not from the TUI's active board

In `move_task()`:

- `old_board` = board owning `task.column`; `new_board` = board owning `target_column`
  (`app.backend.get_column_by_id(...).board_id` → board record).
- Use **`new_board`**'s `reset_column`, `start_column`, `finish_column` for both
  `can_move_to_column(start_column=...)` and `update_task_status(update_column_dict=...)`.
  The target column's board is the one whose workflow the card is entering; this is also what
  the existing cross-board test (`test_task_move_to_other_board_finish_column_uses_append_mode`)
  expects semantically (moving to another board's finish column should behave as a finish).
- `active_board` is no longer read anywhere in `move_task()` except for the prompt decision below.

### 2. `--no-confirm` on `task move`

Same shape and help text style as `task delete --no-confirm`. With it, both "not on the active
board" prompts are skipped and the move proceeds. Without it, behaviour is unchanged (prompts
still appear, `abort=True` on "n"), so interactive users and the existing tests keep working.

Do **not** silently skip the prompt when stdin is not a TTY; the flag must be explicit so an
agent's intent is visible in the tool call.

### 3. Nothing changes in the MCP layer

`mcp_http.py` and `--exclude` stay as they are. Agents will simply pass `--no-confirm` in the
`args` array: `["task","move","<task_id>","<column_id>","--no-confirm"]`. Do not have the bridge
inject the flag; keep the tool honest to the CLI.

### 4. Tests (`tests/cli/test_task_commands.py`)

Keep every existing move test green (including the confirm/abort ones). Add:

- `--no-confirm` moves a card whose column is not on the active board **without any prompt text
  in the output**; exit code 0; card lands in the target column.
- On a non-active board, moving into that board's **finish column sets the finish date** (and
  `finished` is true afterwards); moving into its **start column sets the start date**.
- On a non-active board, a card with an unfinished dependency **is blocked** when moving into
  that board's start column and **not blocked** when moving into a non-start column (this is the
  case the old code got wrong).
- `task delete --no-confirm` tests unchanged.

### 4b. Bonus fix in the same file: `task list --board N --column M` crashes

Found by neo 2026-09-16, reproduced on p5jc: `ktui task list --json --board 2 --column 6` →
`UnboundLocalError: cannot access local variable 'board_present'` at `task_commands.py:184`.
Cause: in `list_tasks()` the `if column:` branch wins and never sets `board_present`, but the
later `elif board and not board_present:` still evaluates it whenever `board` was given and the
result is non-empty. Fix: compute `board_present` whenever `board` is given (before choosing the
task source), or default it to `True`; while there, when both are given, validate that the column
belongs to that board and say so if not. Add a test: `--board 2 --column <col on board 2>` with
tasks → exit 0 and the JSON list; `--board 2 --column <col on another board>` → a clear message,
not a traceback. Ship with the move fix in `0.21.2+jc.5`.

### 5. Housekeeping

- Bump `pyproject.toml` to `0.21.2+jc.5`; add a CHANGELOG entry (the file already has a
  `0.21.2+jc.4 — 2026-09-14` section to copy the shape of).
- README, section "MCP over HTTP(S) for several agents": one line telling agents to pass
  `--no-confirm` on `task move`, and that moves work on any board.
- Ship on `main` (Hunter installs from `main`).

## Acceptance (Hunter runs this on p5jc after reinstall)

1. `ktui --version` → `0.21.2+jc.5`; `journalctl -u ktui-mcp -n 5` shows the same version.
2. Through the live endpoint (`http://192.168.0.222:5057/mcp`), with the active board = 3:
   `["task","move","<a Coder-board card>","<Coder Done column id>","--no-confirm"]` → exit 0,
   "Moved task ..." in the output, and the card shows in *Done* with a finish date in the TUI.
3. Same call **without** `--no-confirm` still returns `Aborted!` (proves nothing was silently
   skipped).
4. Agent rule in `setup-agent-kanban.sh` and the vault note "Agent tools block" becomes:
   always pass `--no-confirm` on `task move`; the "active board" paragraph is deleted.

## Out of scope

Board/column deletion (still excluded server-side), `board activate` (agents still never call
it), the TUI's own move path (unchanged; it only ever moves on the active board).
