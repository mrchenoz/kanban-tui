from collections.abc import Iterable

from textual import on
from textual.events import ScreenResume
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Header

from kanban_tui.widgets.custom_widgets import KanbanTuiFooter
from kanban_tui.widgets.overview_widgets import OverView


class OverViewScreen(Screen):
    def compose(self) -> Iterable[Widget]:
        yield Header()
        yield OverView()
        yield KanbanTuiFooter()

    @on(ScreenResume)
    async def refresh_page(self):
        await self.query_one(OverView).recompose()
        self.app.action_focus_next()
