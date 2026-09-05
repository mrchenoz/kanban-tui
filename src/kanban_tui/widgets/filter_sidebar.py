"""Category filter for the board: show every task, one category, or the
tasks without a category. The choice is remembered per board in the config."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kanban_tui.app import KanbanTui

from textual import on
from textual.binding import Binding
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from kanban_tui.constants import UNCATEGORIZED_FILTER_ID

ALL_OPTION_ID = "filter_all"
OPTION_PREFIX = "filter_category_"


def option_id_for(category_id: int | None) -> str:
    return ALL_OPTION_ID if category_id is None else f"{OPTION_PREFIX}{category_id}"


def category_id_from(option_id: str) -> int | None:
    if option_id == ALL_OPTION_ID:
        return None
    return int(option_id.removeprefix(OPTION_PREFIX))


class CategoryOptionList(OptionList):
    app: KanbanTui

    BINDINGS = [
        Binding("up,k", "cursor_up", "Cursor Up", show=False),
        Binding("down,j", "cursor_down", "Cursor Down", show=False),
    ]

    def rebuild(self) -> None:
        """List All / each category / No category with task counts, and put the
        cursor on the filter that is active right now."""
        counts = Counter(task.category for task in self.app.task_list)
        total = len(self.app.task_list)
        options = [Option(f"All tasks ({total})", id=ALL_OPTION_ID)]
        for category in self.app.backend.get_all_categories():
            label = (
                f"[black on {category.color}] {category.name} [/]"
                f" ({counts.get(category.category_id, 0)})"
            )
            options.append(Option(label, id=option_id_for(category.category_id)))
        options.append(
            Option(
                f"No category ({counts.get(None, 0)})",
                id=option_id_for(UNCATEGORIZED_FILTER_ID),
            )
        )
        self.clear_options()
        self.add_options(options)
        current = option_id_for(self.app.category_filter)
        for index, option in enumerate(options):
            if option.id == current:
                self.highlighted = index
                break
        else:
            self.highlighted = 0


class CategoryFilterOverlay(Vertical):
    """Sidebar docked left of the board, toggled with ``f``."""

    app: KanbanTui

    BINDINGS = [
        Binding("escape,f", "hide", "Close Filter", show=True),
    ]

    def __init__(self) -> None:
        super().__init__(id="overlay_category_filter")
        self.border_title = "Category Filter"
        self.display = False

    def compose(self) -> Iterable[Widget]:
        yield Label("Show tasks of one category", id="label_filter_hint")
        yield CategoryOptionList(id="category_filter_options")

    def toggle(self) -> None:
        if self.display:
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        options = self.query_one(CategoryOptionList)
        options.rebuild()
        self.display = True
        options.focus()

    def hide(self) -> None:
        self.display = False
        self.screen.query_one("KanbanBoard").get_first_card()  # type: ignore[attr-defined]

    def action_hide(self) -> None:
        self.hide()

    @on(OptionList.OptionSelected)
    async def apply_filter(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        assert event.option.id is not None
        self.app.category_filter = category_id_from(event.option.id)
        board = self.screen.query_one("KanbanBoard")
        await board.refresh_columns()  # type: ignore[attr-defined]
        self.hide()
