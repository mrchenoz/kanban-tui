from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import MarkdownViewer


class ModalLogViewScreen(ModalScreen):
    """Read-only view of a task's log file, rendered in place over the board."""

    DEFAULT_CSS = """
    ModalLogViewScreen {
        align: center middle;
    }
    ModalLogViewScreen > Vertical {
        width: 80%;
        height: 80%;
        border: round $primary;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("escape,q", "dismiss_screen", description="Close", show=True),
    ]

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        super().__init__()

    def compose(self) -> ComposeResult:
        with Vertical() as container:
            container.border_title = self.log_path.name
            yield MarkdownViewer(
                markdown=self.log_path.read_text(encoding="utf-8"),
                show_table_of_contents=False,
            )

    def action_dismiss_screen(self) -> None:
        self.dismiss()
