from kanban_tui.backends.sqlite.backend import SqliteBackend
from kanban_tui.config import Settings


def test_sqlite_backend(test_config: Settings, test_app):
    settings = test_config.backend.sqlite_settings
    boards = SqliteBackend(settings).get_boards()

    assert len(boards) == 1
