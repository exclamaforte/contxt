import pytest

from contxt_config import ContxtConfig
from contxt_tui import ContxtTUI, ServerClient, HelpScreen


@pytest.mark.asyncio
async def test_tui_navigation_and_todo_sync(running_server, monkeypatch):
    _, socket_path, layout = running_server

    config = ContxtConfig()
    config.set("socket_path", socket_path)
    config.set("use_multiplexer", False)
    config.set("preview_lines", 2)
    config.set("preview_skip_lines", 0)

    # Avoid real screen/multiplexer calls during tests.
    monkeypatch.setattr("contxt_tui.shutil.which", lambda cmd: None)

    app = ContxtTUI(config)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.worktree_items) >= 2
        assert app.worktree_items[0].selected

        await pilot.press("down")
        await pilot.pause()
        assert app.worktree_items[1].selected

        # Simulate background todo updates while the TUI is running.
        client = ServerClient(socket_path)
        assert client.connect()
        key = layout["primary_key"]
        version = app.todo_versions.get(key, 0)
        response = client.send_command(
            {
                "cmd": "update_todos",
                "key": key,
                "operations": [{"op": "add", "text": "ui-automation note"}],
                "version": version,
            }
        )
        assert response["status"] == "ok"
        client.close()

        app.refresh_all_todos()
        await pilot.pause()

        assert any(todo["text"] == "ui-automation note" for todo in app.todos[key])

        # Drive the help modal like a browser automation script.
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)

        await pilot.press("q")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
