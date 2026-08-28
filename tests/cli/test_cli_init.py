from click.testing import CliRunner

import kanban_tui.cli as cli_module
from kanban_tui.cli import cli
from kanban_tui.constants import CONFIG_FILE, DATABASE_FILE


def test_no_subcommand_uses_env_vars(monkeypatch, test_config_path, test_database_path):
    """Regression test: running `ktui` with no subcommand must use the
    KANBAN_TUI_CONFIG_FILE / KANBAN_TUI_DATABASE_FILE env vars instead of
    always falling back to the default CONFIG_FILE / DATABASE_FILE paths."""
    monkeypatch.setenv("KANBAN_TUI_CONFIG_FILE", test_config_path)
    monkeypatch.setenv("KANBAN_TUI_DATABASE_FILE", test_database_path)

    captured = {}

    class FakeApp:
        def __init__(self, config_path, database_path, **kwargs):
            captured["config_path"] = config_path
            captured["database_path"] = database_path

        def run(self):
            pass

    monkeypatch.setattr(cli_module, "KanbanTui", FakeApp)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, args=[])

    assert result.exit_code == 0
    assert captured["config_path"] == test_config_path
    assert captured["database_path"] == test_database_path


def test_no_subcommand_falls_back_to_defaults(monkeypatch):
    monkeypatch.delenv("KANBAN_TUI_CONFIG_FILE", raising=False)
    monkeypatch.delenv("KANBAN_TUI_DATABASE_FILE", raising=False)

    captured = {}

    class FakeApp:
        def __init__(self, config_path, database_path, **kwargs):
            captured["config_path"] = config_path
            captured["database_path"] = database_path

        def run(self):
            pass

    monkeypatch.setattr(cli_module, "KanbanTui", FakeApp)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, args=[])

    assert result.exit_code == 0
    assert captured["config_path"] == CONFIG_FILE.as_posix()
    assert captured["database_path"] == DATABASE_FILE.as_posix()
