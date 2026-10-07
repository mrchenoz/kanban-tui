from datetime import UTC, datetime, timedelta

import pytest

from kanban_tui.backends.forgejo.backend import (
    ForgejoBackend,
    load_token,
    missing_setup,
)
from kanban_tui.backends.forgejo.forgejo_api import ForgejoError
from kanban_tui.config import ForgejoBackendSettings, ForgejoRepoEntry

LABELS = {"backlog": 19, "ready": 20, "doing": 21, "review": 22, "blocked": 23}
EXCLUSIVE = {f"status/{name}" for name in ("backlog", "ready", "doing", "review")}


def _iso(delta_days: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=delta_days)).isoformat()


class FakeForgejo:
    """In-memory stand-in for ForgejoClient with exclusive status labels."""

    def __init__(self):
        self.labels = [
            {"id": label_id, "name": f"status/{name}"}
            for name, label_id in LABELS.items()
        ] + [{"id": 99, "name": "bug"}]
        self.issues: dict[int, dict] = {}
        self.calls: list[tuple] = []

    def add(self, number, state="open", labels=(), closed_days_ago=None):
        self.issues[number] = {
            "number": number,
            "title": f"Issue {number}",
            "body": "",
            "state": state,
            "labels": [lab for lab in self.labels if lab["name"] in labels],
            "created_at": _iso(3),
            "updated_at": _iso(1),
            "closed_at": _iso(closed_days_ago) if closed_days_ago is not None else None,
            "html_url": f"https://forge.example/o/r/issues/{number}",
            "due_date": None,
        }

    def list_labels(self, owner, repo):
        return self.labels

    def list_issues(self, owner, repo, state, since=None):
        return [i for i in self.issues.values() if i["state"] == state]

    def get_issue(self, owner, repo, number):
        return self.issues[number]

    def create_issue(self, owner, repo, fields):
        number = max(self.issues, default=0) + 1
        self.add(number)
        issue = self.issues[number]
        issue["title"] = fields["title"]
        issue["labels"] = [
            lab for lab in self.labels if lab["id"] in fields.get("labels", [])
        ]
        self.calls.append(("create", fields))
        return issue

    def edit_issue(self, owner, repo, number, fields):
        self.calls.append(("edit", number, fields))
        self.issues[number].update(
            {k: v for k, v in fields.items() if k in ("state", "title", "body")}
        )
        return self.issues[number]

    def add_labels(self, owner, repo, number, label_ids):
        self.calls.append(("add_labels", number, label_ids))
        issue = self.issues[number]
        new = [lab for lab in self.labels if lab["id"] in label_ids]
        # Like Forgejo: an exclusive label replaces the other exclusive labels of
        # its scope; non-exclusive ones (status/blocked) stay.
        issue["labels"] = [
            lab for lab in issue["labels"] if lab["name"] not in EXCLUSIVE
        ] + new
        return issue["labels"]

    def remove_label(self, owner, repo, number, label_id):
        self.calls.append(("remove_label", number, label_id))
        issue = self.issues[number]
        issue["labels"] = [lab for lab in issue["labels"] if lab["id"] != label_id]


@pytest.fixture
def settings(monkeypatch) -> ForgejoBackendSettings:
    monkeypatch.setenv("KTUI_FORGEJO_TOKEN", "test-token")
    return ForgejoBackendSettings(
        base_url="https://forge.example",
        repos=[
            ForgejoRepoEntry(id=1, name="Test", owner="o", repo="r"),
            ForgejoRepoEntry(id=2, name="Other", owner="o", repo="other"),
        ],
    )


@pytest.fixture
def fake() -> FakeForgejo:
    fake = FakeForgejo()
    fake.add(1)
    fake.add(2, labels=["status/backlog"])
    fake.add(3, labels=["status/ready", "bug"])
    fake.add(4, labels=["status/doing"])
    fake.add(5, labels=["status/review"])
    fake.add(6, state="closed", closed_days_ago=2)
    fake.add(7, state="closed", closed_days_ago=30)
    return fake


@pytest.fixture
def backend(settings, fake) -> ForgejoBackend:
    backend = ForgejoBackend(settings)
    backend.client = fake
    return backend


def test_token_from_env_then_file(settings, tmp_path, monkeypatch):
    assert load_token(settings) == "test-token"
    monkeypatch.delenv("KTUI_FORGEJO_TOKEN")
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n")
    settings.token_file = token_file.as_posix()
    assert load_token(settings) == "file-token"


def test_missing_setup(settings, monkeypatch):
    assert missing_setup(settings) is None
    monkeypatch.delenv("KTUI_FORGEJO_TOKEN")
    assert "token" in missing_setup(settings)
    assert "base_url" in missing_setup(ForgejoBackendSettings())


def test_boards_and_columns(backend):
    assert [b.name for b in backend.get_boards()] == ["Test", "Other"]
    assert backend.active_board.board_id == 1
    assert [c.name for c in backend.get_columns()] == [
        "Backlog",
        "Ready",
        "Doing",
        "Review",
        "Done",
    ]


def test_issues_map_to_columns(backend):
    columns = {t.task_id: t.column for t in backend.get_tasks_on_active_board()}
    # Unlabelled open issue -> Backlog; closed 30 days ago drops off the board.
    assert columns == {1: 1, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}


def test_task_metadata(backend):
    task = backend.get_task_by_id(3)
    assert task.metadata["issue_number"] == 3
    assert task.metadata["labels"] == ["status/ready", "bug"]
    assert task.metadata["url"].endswith("/issues/3")
    assert task.creation_date.tzinfo is None


def _moved(backend, number, column):
    task = backend.get_task_by_id(number)
    task.column = column
    return backend.update_task_status(task)


def test_move_swaps_status_label(backend, fake):
    result = _moved(backend, 3, 3)
    assert result["success"]
    names = [lab["name"] for lab in fake.issues[3]["labels"]]
    assert names == ["bug", "status/doing"]
    assert ("remove_label", 3, LABELS["ready"]) in fake.calls


def test_move_to_done_closes_and_drops_status_label(backend, fake):
    fake.add(5, labels=["status/review", "bug"])
    assert _moved(backend, 5, 5)["success"]
    assert fake.issues[5]["state"] == "closed"
    assert [lab["name"] for lab in fake.issues[5]["labels"]] == ["bug"]


def test_reopened_issue_without_label_lands_in_backlog(backend, fake):
    _moved(backend, 5, 5)
    fake.issues[5]["state"] = "open"  # reopened in the Forgejo web UI
    assert backend.get_task_by_id(5).column == 1


def test_move_out_of_done_reopens(backend, fake):
    assert _moved(backend, 6, 4)["success"]
    assert fake.issues[6]["state"] == "open"
    assert [lab["name"] for lab in fake.issues[6]["labels"]] == ["status/review"]


def test_move_fails_without_label(backend, fake):
    fake.labels = [lab for lab in fake.labels if lab["name"] != "status/review"]
    result = _moved(backend, 4, 4)
    assert not result["success"]
    assert "status/review" in result["message"]


def test_move_reports_api_error(backend, fake):
    def boom(*args, **kwargs):
        raise ForgejoError("PATCH /repos/o/r/issues/5: 403")

    fake.edit_issue = boom
    result = _moved(backend, 5, 5)
    assert not result["success"]
    assert "403" in result["message"]


def test_create_task_in_column(backend, fake):
    task = backend.create_new_task(title="New", description="", column=2)
    assert task.column == 2
    assert fake.calls[-1] == ("create", {"title": "New", "body": "", "labels": [20]})


def test_update_task_clears_due_date(backend, fake):
    backend.update_task_entry(task_id=2, title="Renamed", description="x")
    assert fake.calls[-1] == (
        "edit",
        2,
        {"title": "Renamed", "body": "x", "unset_due_date": True},
    )
    assert fake.issues[2]["title"] == "Renamed"


def test_fetch_error_gives_empty_board(backend, fake):
    def boom(*args, **kwargs):
        raise ForgejoError("unreachable")

    fake.list_issues = boom
    assert backend.get_tasks_on_active_board() == []


def test_blocked_label_is_a_flag_not_a_column(backend, fake):
    fake.add(4, labels=["status/doing", "status/blocked"])
    task = backend.get_task_by_id(4)
    assert task.column == 3
    assert task.metadata["blocked"] is True
    assert backend.get_task_by_id(3).metadata["blocked"] is False


def test_moves_keep_status_blocked(backend, fake):
    fake.add(4, labels=["status/doing", "status/blocked", "bug"])
    assert _moved(backend, 4, 4)["success"]
    assert [lab["name"] for lab in fake.issues[4]["labels"]] == [
        "status/blocked",
        "bug",
        "status/review",
    ]
    assert ("remove_label", 4, LABELS["blocked"]) not in fake.calls


def test_done_keeps_status_blocked(backend, fake):
    fake.add(5, labels=["status/review", "status/blocked"])
    assert _moved(backend, 5, 5)["success"]
    assert [lab["name"] for lab in fake.issues[5]["labels"]] == ["status/blocked"]
