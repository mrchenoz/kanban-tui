"""Forgejo issues as a kanban board.

Each configured repo is a board and each issue a card; the issue number is the
task id. Columns come from exclusive status labels, plus closed issues as Done:

    Backlog  status/backlog (and open issues without a status label)
    Ready    status/ready
    Doing    status/doing
    Review   status/review
    Done     closed issues, closed within the last ``done_days`` days

Moving a card swaps the status label, or closes / reopens the issue.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kanban_tui.backends.base import Backend
from kanban_tui.backends.forgejo.forgejo_api import ForgejoClient, ForgejoError
from kanban_tui.classes.board import Board
from kanban_tui.classes.category import Category
from kanban_tui.classes.column import Column
from kanban_tui.classes.task import Task
from kanban_tui.config import ForgejoBackendSettings, ForgejoRepoEntry

TOKEN_ENV = "KTUI_FORGEJO_TOKEN"

# (column id, column name, status label suffix); Done has no label, it is "closed".
COLUMNS: list[tuple[int, str, str | None]] = [
    (1, "Backlog", "backlog"),
    (2, "Ready", "ready"),
    (3, "Doing", "doing"),
    (4, "Review", "review"),
    (5, "Done", None),
]
BACKLOG_COLUMN = 1
DOING_COLUMN = 3
REVIEW_COLUMN = 4
DONE_COLUMN = 5


def _parse_datetime(value: str | None) -> datetime | None:
    """Forgejo timestamps are ISO 8601 with an offset; return local naive time."""
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone().replace(tzinfo=None)


def load_token(settings: ForgejoBackendSettings) -> str:
    token = os.getenv(TOKEN_ENV, "").strip()
    if token:
        return token
    if settings.token_file:
        path = Path(settings.token_file).expanduser()
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


def missing_setup(settings: ForgejoBackendSettings) -> str | None:
    """Say what is missing before the backend can be used, or None when ready."""
    if not settings.base_url:
        return "set backend.forgejo_settings.base_url in the config"
    if not settings.repos:
        return "add a repo under backend.forgejo_settings.repos in the config"
    if not load_token(settings):
        return f"set {TOKEN_ENV} or backend.forgejo_settings.token_file"
    return None


def _notify_error(message: str) -> None:
    """Show a fetch error in the running app, if there is one."""
    try:
        from textual._context import active_app

        active_app.get().notify(
            title="Forgejo", message=message, severity="error", timeout=8
        )
    except (ImportError, LookupError):
        pass


@dataclass
class ForgejoBackend(Backend):
    settings: ForgejoBackendSettings
    client: ForgejoClient = field(init=False)
    # Per repo ("owner/repo"): status label suffix -> label id
    _label_cache: dict[str, dict[str, int]] = field(init=False, default_factory=dict)

    def __post_init__(self):
        self.client = ForgejoClient(self.settings.base_url, load_token(self.settings))

    # Queries
    def get_boards(self) -> list[Board]:
        return [
            Board(
                board_id=entry.id,
                name=entry.name,
                icon=":package:",
                creation_date=datetime.now(),
                reset_column=BACKLOG_COLUMN,
                start_column=DOING_COLUMN,
                finish_column=DONE_COLUMN,
            )
            for entry in self.settings.repos
        ]

    def get_all_categories(self) -> list[Category]:
        return []

    def get_category_by_id(self, category_id: int) -> Category | None:
        return None

    def get_board_infos(self) -> list[dict]:
        board_infos = []
        for board in self.get_boards():
            board_tasks = self.get_tasks_by_board_id(board_id=board.board_id)
            board_infos.append(
                {
                    "board_id": board.board_id,
                    "amount_tasks": len(board_tasks),
                    "amount_columns": len(COLUMNS),
                    "next_due": min(
                        (t.due_date for t in board_tasks if t.due_date), default=None
                    ),
                }
            )
        return board_infos

    def get_columns(self, board_id: int | None = None) -> list[Column]:
        if not board_id and self.active_board:
            board_id = self.active_board.board_id
        return [
            Column(
                column_id=column_id,
                name=name,
                visible=True,
                position=column_id - 1,
                board_id=board_id or 0,
            )
            for column_id, name, _ in COLUMNS
        ]

    def get_column_by_id(self, column_id: int) -> Column | None:
        return next(
            (column for column in self.get_columns() if column.column_id == column_id),
            None,
        )

    def get_tasks_by_board_id(self, board_id: int) -> list[Task]:
        entry = self._repo_entry(board_id)
        if entry is None:
            return []
        cutoff = datetime.now(UTC) - timedelta(days=self.settings.done_days)
        try:
            issues = self.client.list_issues(entry.owner, entry.repo, state="open")
            # `since` filters on last update; closed_at is checked below.
            closed = self.client.list_issues(
                entry.owner, entry.repo, state="closed", since=cutoff.isoformat()
            )
        except ForgejoError as exc:
            _notify_error(f"Could not load {entry.owner}/{entry.repo}: {exc}")
            return []
        issues += [
            issue
            for issue in closed
            if issue.get("closed_at")
            and datetime.fromisoformat(issue["closed_at"]) >= cutoff
        ]
        return [self._issue_to_task(issue, entry) for issue in issues]

    def get_tasks_on_active_board(self) -> list[Task]:
        board = self.active_board
        return self.get_tasks_by_board_id(board.board_id) if board else []

    def get_task_by_id(self, task_id: int) -> Task | None:
        entry = self._active_entry()
        if entry is None:
            return None
        try:
            issue = self.client.get_issue(entry.owner, entry.repo, task_id)
        except ForgejoError:
            return None
        return self._issue_to_task(issue, entry)

    def get_tasks_by_ids(self, task_ids: list[int]) -> list[Task]:
        return [task for task in map(self.get_task_by_id, task_ids) if task is not None]

    @property
    def active_board(self) -> Board | None:
        boards = self.get_boards()
        for board in boards:
            if board.board_id == self.settings.active_repo:
                return board
        return boards[0] if boards else None

    # Commands
    def update_task_status(
        self,
        new_task: Task,
        target_position: int | None = None,
        append_mode=None,
    ) -> dict[str, bool | str]:
        """Move an issue to `new_task.column` by changing its status label/state."""
        # Forgejo has no card order, so position and append mode are ignored.
        _ = target_position, append_mode
        entry = self._active_entry()
        number = new_task.metadata.get("issue_number")
        if entry is None or number is None:
            return {"success": False, "message": "Task is not a Forgejo issue"}

        owner, repo = entry.owner, entry.repo
        is_closed = new_task.metadata.get("state") == "closed"
        try:
            if new_task.column == DONE_COLUMN:
                # Done carries no status label: a closed issue keeps none, so one
                # reopened in Forgejo lands in Backlog rather than its old column.
                self._remove_status_labels(entry, new_task)
                self.client.edit_issue(owner, repo, number, {"state": "closed"})
                return {"success": True, "message": f"#{number} closed"}

            suffix = self._column_suffix(new_task.column)
            label_id = self._status_label_ids(entry).get(suffix)
            if label_id is None:
                return {
                    "success": False,
                    "message": f"Label {self.settings.label_prefix}{suffix} "
                    f"does not exist in {owner}/{repo}",
                }
            if is_closed:
                self.client.edit_issue(owner, repo, number, {"state": "open"})
            # Drop other status labels first, so this also works when the labels
            # are not marked exclusive in Forgejo.
            self._remove_status_labels(entry, new_task, keep=suffix)
            self.client.add_labels(owner, repo, number, [label_id])
        except ForgejoError as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "message": f"#{number} -> {self.settings.label_prefix}{suffix}",
        }

    def create_new_task(
        self,
        title: str,
        description: str,
        column: int,
        category: int | None = None,
        due_date: datetime | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        entry = self._active_entry()
        if entry is None:
            raise ForgejoError("No Forgejo repo configured")
        fields: dict[str, Any] = {"title": title, "body": description}
        if column != DONE_COLUMN:
            label_id = self._status_label_ids(entry).get(self._column_suffix(column))
            if label_id is not None:
                fields["labels"] = [label_id]
        if due_date:
            fields["due_date"] = _due_date_iso(due_date)
        issue = self.client.create_issue(entry.owner, entry.repo, fields)
        return self._issue_to_task(issue, entry)

    def update_task_entry(
        self,
        task_id: int,
        title: str,
        description: str,
        category: int | None = None,
        due_date: datetime | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        entry = self._active_entry()
        if entry is None:
            raise ForgejoError("No Forgejo repo configured")
        fields: dict[str, Any] = {"title": title, "body": description}
        if due_date:
            fields["due_date"] = _due_date_iso(due_date)
        else:
            fields["unset_due_date"] = True
        issue = self.client.edit_issue(entry.owner, entry.repo, task_id, fields)
        return self._issue_to_task(issue, entry)

    # Helpers
    def _repo_entry(self, board_id: int | None) -> ForgejoRepoEntry | None:
        return next(
            (entry for entry in self.settings.repos if entry.id == board_id), None
        )

    def _active_entry(self) -> ForgejoRepoEntry | None:
        board = self.active_board
        return self._repo_entry(board.board_id) if board else None

    @staticmethod
    def _column_suffix(column_id: int) -> str:
        return next(
            (suffix for cid, _, suffix in COLUMNS if cid == column_id and suffix),
            "backlog",
        )

    def _status_label_ids(self, entry: ForgejoRepoEntry) -> dict[str, int]:
        key = f"{entry.owner}/{entry.repo}"
        if key not in self._label_cache:
            prefix = self.settings.label_prefix
            self._label_cache[key] = {
                label["name"][len(prefix) :]: label["id"]
                for label in self.client.list_labels(entry.owner, entry.repo)
                if label["name"].startswith(prefix)
            }
        return self._label_cache[key]

    def _status_labels_on(self, task: Task) -> list[str]:
        prefix = self.settings.label_prefix
        return [
            name[len(prefix) :]
            for name in task.metadata.get("labels", [])
            if name.startswith(prefix)
        ]

    def _remove_status_labels(
        self, entry: ForgejoRepoEntry, task: Task, keep: str | None = None
    ) -> None:
        label_ids = self._status_label_ids(entry)
        for suffix in self._status_labels_on(task):
            if suffix != keep and suffix in label_ids:
                self.client.remove_label(
                    entry.owner, entry.repo, task.task_id, label_ids[suffix]
                )

    def _issue_column(self, issue: dict) -> int:
        if issue.get("state") == "closed":
            return DONE_COLUMN
        prefix = self.settings.label_prefix
        for label in issue.get("labels") or []:
            name = label.get("name", "")
            if name.startswith(prefix):
                suffix = name[len(prefix) :]
                for column_id, _, column_suffix in COLUMNS:
                    if column_suffix == suffix:
                        return column_id
        return BACKLOG_COLUMN

    def _issue_to_task(self, issue: dict, entry: ForgejoRepoEntry) -> Task:
        column = self._issue_column(issue)
        created = _parse_datetime(issue.get("created_at")) or datetime.now()
        updated = _parse_datetime(issue.get("updated_at"))
        start_date = updated if column in (DOING_COLUMN, REVIEW_COLUMN) else None
        finish_date = _parse_datetime(issue.get("closed_at"))
        labels = [label.get("name", "") for label in issue.get("labels") or []]
        assignees = [a.get("login", "") for a in issue.get("assignees") or []]
        milestone = (issue.get("milestone") or {}).get("title")
        return Task(
            task_id=issue["number"],
            title=issue.get("title", ""),
            column=column,
            creation_date=created,
            start_date=start_date,
            finish_date=finish_date,
            due_date=_parse_datetime(issue.get("due_date")),
            description=issue.get("body") or "",
            category=None,
            metadata={
                "issue_number": issue["number"],
                "repo": f"{entry.owner}/{entry.repo}",
                "url": issue.get("html_url"),
                "state": issue.get("state"),
                "labels": labels,
                "assignees": assignees,
                "milestone": milestone,
                "backend_source": "forgejo",
            },
        )

    # Not supported: columns are fixed, categories and dependencies are not mapped,
    # and issues are never deleted from the board.
    def create_new_board(self, *args, **kwargs):
        raise NotImplementedError(
            "Add Forgejo repos under backend.forgejo_settings.repos in the config."
        )

    def update_board_entry(self, *args, **kwargs):
        raise NotImplementedError("Edit Forgejo repos in the config.")

    def delete_board(self, *args, **kwargs):
        raise NotImplementedError("Remove Forgejo repos in the config.")

    def delete_task(self, *args, **kwargs):
        raise NotImplementedError("Close the issue instead; Forgejo issues stay.")

    def create_new_column(self, *args, **kwargs):
        raise NotImplementedError("Forgejo columns are fixed status labels.")

    def update_column_visibility(self, *args, **kwargs):
        raise NotImplementedError("Forgejo columns are fixed status labels.")

    def switch_column_positions(self, *args, **kwargs):
        raise NotImplementedError("Forgejo columns are fixed status labels.")

    def update_column_name(self, *args, **kwargs):
        raise NotImplementedError("Forgejo columns are fixed status labels.")

    def delete_column(self, *args, **kwargs):
        raise NotImplementedError("Forgejo columns are fixed status labels.")

    def create_new_category(self, *args, **kwargs):
        raise NotImplementedError("Forgejo backend doesn't support categories.")

    def update_category_entry(self, *args, **kwargs):
        raise NotImplementedError("Forgejo backend doesn't support categories.")

    def delete_category(self, *args, **kwargs):
        raise NotImplementedError("Forgejo backend doesn't support categories.")

    def create_task_dependency(self, *args, **kwargs):
        raise NotImplementedError("Set issue dependencies in Forgejo directly.")

    def delete_task_dependency(self, *args, **kwargs):
        raise NotImplementedError("Set issue dependencies in Forgejo directly.")


def _due_date_iso(due_date: datetime | str) -> str:
    if isinstance(due_date, str):
        due_date = datetime.fromisoformat(due_date)
    if due_date.tzinfo is None:
        due_date = due_date.astimezone()
    return due_date.isoformat()
