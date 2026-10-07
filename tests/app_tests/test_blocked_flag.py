from kanban_tui.app import KanbanTui
from kanban_tui.widgets.task_card import TaskCard

APP_SIZE = (150, 50)


async def test_blocked_metadata_flags_the_card(test_app: KanbanTui):
    async with test_app.run_test(size=APP_SIZE) as pilot:
        card = list(pilot.app.screen.query(TaskCard).results())[1]
        task = card.task_
        assert "blocked" not in str(card.border_title)
        assert not card.has_class("blocked")

        pilot.app.backend.update_task_entry(
            task_id=task.task_id,
            title=task.title,
            description=task.description,
            category=task.category,
            due_date=task.due_date,
            metadata={"blocked": True},
        )
        pilot.app.action_refresh()
        await pilot.pause()

        card = pilot.app.screen.query_exactly_one(f"#taskcard_{task.task_id}", TaskCard)
        assert str(card.border_title) == f"#{task.task_id} · ⛔ blocked"
        assert card.has_class("blocked")
