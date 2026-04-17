import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import MethodType

import pytest
from textual.containers import ScrollableContainer, VerticalScroll
from textual.widgets import DataTable, Input, LoadingIndicator

from contxt_config import ContxtConfig
from contxt_review_tui import (
    ConfirmDialog,
    ReviewLoopTUI,
    ReviewServerClient,
    card_width_for_entity,
    pack_entities,
)
from contxt_sessions import SessionManager, TmuxBackend


class FakeReviewClient:
    async def send_command(self, payload):
        if payload["cmd"] in {"get_overview", "refresh_overview"}:
            return {
                "status": "ok",
                "overview": {
                    "summary": {
                        "entity_count": 2,
                        "queue_count": 1,
                        "ready_to_merge_count": 0,
                    },
                    "queue": [
                        {
                            "id": "fix_ci:pr:acme/demo#1",
                            "entity_id": "pr:acme/demo#1",
                            "owner": "agent",
                            "title": "Fix CI",
                            "status": "pending",
                            "kind": "fix_ci",
                        }
                    ],
                    "entities": [
                        {
                            "id": "pr:acme/demo#1",
                            "title": "alpha",
                            "subtitle": "acme/demo#1",
                            "worktree_key": "demo/alpha",
                            "reviews_state": "yellow",
                            "ci_state": "red",
                            "status_state": "green",
                            "ready_to_merge": False,
                            "workflow_state": "review_loop",
                            "reviews_summary": "Waiting on review",
                            "ci_summary": "CI failed",
                            "status_summary": "PR submitted",
                            "worktree_path": "/tmp/alpha",
                            "lifecycle_state": "active",
                            "current_remediation": None,
                        },
                        {
                            "id": "wt:demo/orphaned",
                            "title": "orphaned",
                            "subtitle": "demo/orphaned",
                            "worktree_key": "demo/orphaned",
                            "reviews_state": "grey",
                            "ci_state": "grey",
                            "status_state": "grey",
                            "ready_to_merge": False,
                            "workflow_state": "working",
                            "reviews_summary": "No PR attached",
                            "ci_summary": "No PR attached",
                            "status_summary": "Delete or archive this worktree",
                            "worktree_path": "/tmp/orphaned",
                            "lifecycle_state": "orphaned",
                            "current_remediation": None,
                        },
                    ],
                },
            }
        if payload["cmd"] == "capture_session":
            return {"status": "error", "message": "no session"}
        if payload["cmd"] == "get_queue_item_detail":
            if payload["queue_id"] == "resolve_review_thread:pr:acme/demo#1:thread-1":
                return {
                    "status": "ok",
                    "detail": "Kind: resolve_review_thread\nOwner: agent\nAction: Address agent review.",
                }
            return {
                "status": "ok",
                "detail": "Kind: fix_ci\nOwner: agent\nAction: Ask agent to fix CI.",
            }
        return {"status": "ok"}


class SlowWorkflowClient(FakeReviewClient):
    async def send_command(self, payload):
        if payload["cmd"] == "set_workflow_state":
            await asyncio.sleep(0.3)
            return {
                "status": "ok",
                "entity_id": payload["entity_id"],
                "workflow_state": payload["state"],
            }
        return await super().send_command(payload)


class SlowQueueOwnerClient(FakeReviewClient):
    async def send_command(self, payload):
        if payload["cmd"] == "set_queue_owner":
            await asyncio.sleep(0.3)
            return {
                "status": "ok",
                "queue_id": payload["queue_id"],
                "owner": payload["owner"],
            }
        return await super().send_command(payload)


class SlowCaptureClient(FakeReviewClient):
    async def send_command(self, payload):
        if payload["cmd"] in {"get_overview", "refresh_overview"}:
            response = await super().send_command(payload)
            response["overview"]["queue"][0]["status"] = "running"
            response["overview"]["entities"][0]["current_remediation"] = {
                "session_name": "contxt-review-pr-acme-demo-1",
                "status": "running",
            }
            return response
        if payload["cmd"] == "capture_entity_session":
            await asyncio.sleep(0.3)
            return {"status": "ok", "output": "live remediation output"}
        if payload["cmd"] == "capture_session":
            await asyncio.sleep(0.3)
            return {"status": "ok", "output": "live remediation output"}
        return await super().send_command(payload)


class LiveEntityCaptureClient(FakeReviewClient):
    async def send_command(self, payload):
        if payload["cmd"] in {"get_overview", "refresh_overview"}:
            response = await super().send_command(payload)
            response["overview"]["queue"][0]["status"] = "running"
            response["overview"]["entities"][0]["current_remediation"] = {
                "session_name": "contxt-review-pr-acme-demo-1",
                "status": "running",
            }
            return response
        if payload["cmd"] == "capture_entity_session":
            return {"status": "ok", "output": "zellij session output"}
        return await super().send_command(payload)


class HumanQueueClient(FakeReviewClient):
    async def send_command(self, payload):
        if payload["cmd"] in {"get_overview", "refresh_overview"}:
            response = await super().send_command(payload)
            response["overview"]["queue"][0] = {
                "id": "resolve_review_thread:pr:acme/demo#1:thread-human",
                "entity_id": "pr:acme/demo#1",
                "owner": "human",
                "title": "Address human review",
                "status": "pending",
                "kind": "resolve_review_thread",
            }
            return response
        if payload["cmd"] == "get_queue_item_detail":
            return {
                "status": "ok",
                "detail": "Kind: resolve_review_thread\nOwner: human\nAction: Wait for manual remediation.",
            }
        if payload["cmd"] == "capture_session":
            raise AssertionError("human queue item should not request session capture")
        return await super().send_command(payload)


class MultiQueueClient(FakeReviewClient):
    async def send_command(self, payload):
        if payload["cmd"] in {"get_overview", "refresh_overview"}:
            response = await super().send_command(payload)
            response["overview"]["summary"]["queue_count"] = 2
            response["overview"]["queue"] = [
                {
                    "id": "fix_ci:pr:acme/demo#1",
                    "entity_id": "pr:acme/demo#1",
                    "owner": "agent",
                    "title": "Fix CI",
                    "status": "pending",
                    "kind": "fix_ci",
                },
                {
                    "id": "resolve_review_thread:pr:acme/demo#1:thread-1",
                    "entity_id": "pr:acme/demo#1",
                    "owner": "agent",
                    "title": "Address agent review",
                    "status": "pending",
                    "kind": "resolve_review_thread",
                },
            ]
            return response
        return await super().send_command(payload)


class NoDetailClient(FakeReviewClient):
    async def send_command(self, payload):
        if payload["cmd"] == "get_queue_item_detail":
            return {"status": "error", "message": "stale server"}
        return await super().send_command(payload)


class RateLimitedRefreshClient(FakeReviewClient):
    async def send_command(self, payload):
        if payload["cmd"] == "refresh_overview":
            return {"status": "error", "message": "GraphQL: API rate limit already exceeded for user ID 123."}
        return await super().send_command(payload)


@pytest.mark.asyncio
async def test_review_tui_renders_left_cards(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        card_list = app.query_one("#card-list")
        assert len(card_list.children) >= 1
        card_count = sum(len(row.children) for row in card_list.children)
        assert card_count == 2
        assert "entities=2" in str(app.query_one("#status-summary").content)
        assert isinstance(app.query_one("#output-scroll"), VerticalScroll)
        assert isinstance(app.query_one("#entity-search", Input), Input)


@pytest.mark.asyncio
async def test_left_search_filters_cards(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        search = app.query_one("#entity-search", Input)
        search.focus()
        await pilot.pause()
        await pilot.press("o", "r", "p", "h", "a", "n")
        await pilot.pause()
        card_list = app.query_one("#card-list")
        card_count = sum(len(row.children) for row in card_list.children)
        assert card_count == 1
        assert card_list.children[0].children[0].entity["title"] == "orphaned"


@pytest.mark.asyncio
async def test_confirm_dialog_accepts_y(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ConfirmDialog("Delete worktree demo?"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_attach_action_confirms_modal_when_open(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(ConfirmDialog("Delete worktree demo?"))
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        await app.action_attach_session()
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_review_tui_uses_cached_overview_when_refresh_is_rate_limited(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = RateLimitedRefreshClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._has_loaded_snapshot is True
        assert "entities=2" in str(app.query_one("#status-summary").content)


@pytest.mark.asyncio
async def test_status_bar_shows_rate_limit_warning(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.snapshot["summary"]["rate_limited"] = True
        app.snapshot["summary"]["rate_limit_retry_in_seconds"] = 123
        await app.render_right_pane()
        await pilot.pause()
        assert "GH_RATE_LIMIT 123s" in str(app.query_one("#status-summary").content)


@pytest.mark.asyncio
async def test_review_tui_skips_card_rebuild_when_snapshot_is_unchanged(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        card_list = app.query_one("#card-list")
        await app.render_cards()
        first_row = card_list.children[0]
        await app.render_cards()

        assert card_list.children[0] is first_row


@pytest.mark.asyncio
async def test_review_tui_hotkeys_target_contxt_commands(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()
    calls = []

    async def fake_ensure_server():
        return None

    async def fake_run_contxt_command(self, *args, wait=True):
        calls.append((args, wait))
        return True

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)
    async def fake_get_delete_preflight(project, name):
        return {"project": project, "name": name, "has_changes": True, "status_lines": [" M foo.py"]}
    monkeypatch.setattr(app, "get_delete_preflight", fake_get_delete_preflight)
    def fake_push_screen(screen, callback=None):
        if callback:
            callback(True)
    monkeypatch.setattr(app, "push_screen", fake_push_screen)
    app.run_contxt_command = MethodType(fake_run_contxt_command, app)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_entity_id = "pr:acme/demo#1"
        assert app.selected_worktree_identifiers() == ("demo", "alpha")

        await app.action_edit_worktree()
        await asyncio.wait_for(app.action_delete_worktree(), timeout=0.1)
        await asyncio.wait_for(app.action_merge_worktree(), timeout=0.1)
        await asyncio.sleep(0.05)

        assert calls == [
            (("edit", "alpha", "-p", "demo"), False),
            (("delete", "alpha", "-p", "demo", "--yes"), True),
            (("merge", "alpha", "-p", "demo"), True),
        ]


@pytest.mark.asyncio
async def test_worktree_actions_can_use_selected_queue_entity(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()
    calls = []

    async def fake_ensure_server():
        return None

    async def fake_run_contxt_command(self, *args, wait=True):
        calls.append((args, wait))
        return True

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)
    async def fake_get_delete_preflight(project, name):
        return {"project": project, "name": name, "has_changes": False, "status_lines": []}
    monkeypatch.setattr(app, "get_delete_preflight", fake_get_delete_preflight)
    def fake_push_screen(screen, callback=None):
        if callback:
            callback(True)
    monkeypatch.setattr(app, "push_screen", fake_push_screen)
    app.run_contxt_command = MethodType(fake_run_contxt_command, app)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_entity_id = None
        app.selected_queue_id = "fix_ci:pr:acme/demo#1"

        await app.action_edit_worktree()
        await asyncio.wait_for(app.action_delete_worktree(), timeout=0.1)
        await asyncio.wait_for(app.action_merge_worktree(), timeout=0.1)
        await asyncio.sleep(0.05)

        assert calls == [
            (("edit", "alpha", "-p", "demo"), False),
            (("delete", "alpha", "-p", "demo", "--yes"), True),
            (("merge", "alpha", "-p", "demo"), True),
        ]


@pytest.mark.asyncio
async def test_review_tui_can_toggle_log_view_with_markup_like_content(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)
    crashy_log = (
        '{"status": "ok", "overview": {"queue": [{"id": "fix_ci:pr:reflectionai/olympus#23942", '
        '"title": "Fix CI", "description": "Run <span class=\\"text-foreground shrink-0 w-16\\">GPUs</span>", '
        '"url": "https://github.com/reflectionai/olympus/pull/23942#discussion_r2928561550"}]}}'
    )
    monkeypatch.setattr(app.logger, "tail_text", lambda lines=200: crashy_log)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.show_logs is False

        await pilot.press("l")
        await pilot.pause()

        session_output = app.query_one("#session-output")
        assert app.show_logs is True
        assert "text-foreground shrink-0 w-16" in str(session_output.content)


@pytest.mark.asyncio
async def test_review_tui_emits_popups_for_actions(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()
    notifications = []

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, severity="information", **kwargs: notifications.append((message, severity)),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_entity_id = "pr:acme/demo#1"
        app.selected_queue_id = "fix_ci:pr:acme/demo#1"

        await app.action_toggle_logs()
        await asyncio.wait_for(app.action_dispatch_selected(), timeout=0.1)
        await asyncio.wait_for(app.action_toggle_workflow(), timeout=0.1)

        assert ("Logs on", "information") in notifications
        assert ("Dispatching queue item", "information") in notifications
        assert ("alpha: workflow -> working", "information") in notifications


@pytest.mark.asyncio
async def test_review_tui_emits_warning_popup_when_edit_without_worktree(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()
    notifications = []

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(app, "selected_worktree_identifiers", lambda: None)
    monkeypatch.setattr(
        app,
        "notify",
        lambda message, severity="information", **kwargs: notifications.append((message, severity)),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_edit_worktree()

        assert ("No worktree for selected branch", "warning") in notifications


@pytest.mark.asyncio
async def test_toggle_workflow_runs_in_background_and_logs_immediately(monkeypatch, tmp_path):
    config = ContxtConfig()
    config.config["review_log_path"] = str(tmp_path / "review.log")
    app = ReviewLoopTUI(config)
    app.client = SlowWorkflowClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_entity_id = "pr:acme/demo#1"

        await asyncio.wait_for(app.action_toggle_workflow(), timeout=0.1)
        assert app.selected_entity()["workflow_state"] == "working"

        await pilot.press("l")
        await pilot.pause()

        session_output = app.query_one("#session-output")
        assert "toggle workflow requested" in str(session_output.content)

        await asyncio.sleep(0.35)
        await pilot.pause()
        assert "toggle workflow response" in str(app.query_one("#session-output").content)


@pytest.mark.asyncio
async def test_assign_agent_runs_in_background(monkeypatch, tmp_path):
    config = ContxtConfig()
    config.config["review_log_path"] = str(tmp_path / "review.log")
    app = ReviewLoopTUI(config)
    app.client = SlowQueueOwnerClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_queue_id = "fix_ci:pr:acme/demo#1"

        await asyncio.wait_for(app.action_assign_agent(), timeout=0.1)

        await pilot.press("l")
        await pilot.pause()
        assert "queue owner change requested" in str(app.query_one("#session-output").content)


@pytest.mark.asyncio
async def test_session_output_loads_in_background(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = SlowCaptureClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()

        await asyncio.wait_for(app.render_right_pane(), timeout=0.1)
        initial_output = str(app.query_one("#session-output").content)
        assert (
            "Streaming live agent session output..." in initial_output
            or "live remediation output" in initial_output
        )

        await asyncio.sleep(0.35)
        await pilot.pause()
        assert "live remediation output" in str(app.query_one("#session-output").content)


@pytest.mark.asyncio
async def test_running_agent_item_uses_entity_session_output(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = LiveEntityCaptureClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        await asyncio.wait_for(app.render_right_pane(), timeout=0.1)
        await asyncio.sleep(0.05)
        await pilot.pause()

        output = str(app.query_one("#session-output").content)
        assert "zellij session output" in output


@pytest.mark.asyncio
async def test_human_queue_item_never_blanks_output_or_fetches_session(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = HumanQueueClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_queue_id = "resolve_review_thread:pr:acme/demo#1:thread-human"
        await asyncio.wait_for(app.render_right_pane(), timeout=0.1)
        await pilot.pause()

        output = str(app.query_one("#session-output").content)
        detail = str(app.query_one("#queue-detail").content)
        assert "pending human action" in output
        assert "manual remediation" in output
        assert "Kind: resolve_review_thread" in detail
        assert "Select a queue item." not in output


@pytest.mark.asyncio
async def test_attach_session_uses_session_manager_command(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = LiveEntityCaptureClient()
    calls = []

    async def fake_ensure_server():
        return None

    @contextmanager
    def fake_suspend(_self):
        yield

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(app.session_manager, "attach_command", lambda session_name: ["zellij", "attach", session_name])
    monkeypatch.setattr("contxt_review_tui.subprocess.call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr("contxt_review_tui.App.suspend", fake_suspend)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_queue_id = "fix_ci:pr:acme/demo#1"
        await app.render_right_pane()
        await pilot.pause()

        await app.action_attach_session()

        assert calls == [["zellij", "attach", "contxt-review-pr-acme-demo-1"]]


@pytest.mark.asyncio
async def test_enter_key_attaches_to_selected_session(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = LiveEntityCaptureClient()
    calls = []

    async def fake_ensure_server():
        return None

    @contextmanager
    def fake_suspend(_self):
        yield

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)
    monkeypatch.setattr(app.session_manager, "attach_command", lambda session_name: ["zellij", "attach", session_name])
    monkeypatch.setattr("contxt_review_tui.subprocess.call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr("contxt_review_tui.App.suspend", fake_suspend)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_queue_id = "fix_ci:pr:acme/demo#1"
        await app.render_right_pane()
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert calls == [["zellij", "attach", "contxt-review-pr-acme-demo-1"]]


@pytest.mark.asyncio
async def test_queue_item_selection_loads_detail_panel(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()

        app.selected_queue_id = "fix_ci:pr:acme/demo#1"
        await app.render_right_pane()
        await asyncio.sleep(0.05)
        await pilot.pause()

        assert "Kind: fix_ci" in str(app.query_one("#queue-detail").content)


@pytest.mark.asyncio
async def test_queue_selection_is_preserved(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = MultiQueueClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_queue_id = "resolve_review_thread:pr:acme/demo#1:thread-1"
        await app.render_right_pane()
        await pilot.pause()

        assert app.selected_queue_id == "resolve_review_thread:pr:acme/demo#1:thread-1"
        assert "Address agent review" in str(app.query_one("#queue-detail").content)


@pytest.mark.asyncio
async def test_queue_selection_summary_shows_branch_name(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_entity_id = None
        app.selected_queue_id = "fix_ci:pr:acme/demo#1"
        await app.render_right_pane()
        await pilot.pause()

        summary = str(app.query_one("#status-summary").content)
        assert "queue | entities=2 | items=1 | ready=0" in summary
        assert "acme/demo#1" in summary or "alpha" in summary


@pytest.mark.asyncio
async def test_double_selecting_queue_item_selects_entity(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    class RowKey:
        value = "fix_ci:pr:acme/demo#1"

    class Event:
        row_key = RowKey()

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.on_data_table_row_selected(Event())
        await app.on_data_table_row_selected(Event())
        await pilot.pause()

        assert app.selected_entity_id == "pr:acme/demo#1"
        card = app.query_one("#card-pr-acme-demo-1")
        assert "selected" in card.classes


@pytest.mark.asyncio
async def test_double_selecting_queue_item_scrolls_selected_card_into_view(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()
    scroll_calls = []

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    class RowKey:
        value = "fix_ci:pr:acme/demo#1"

    class Event:
        row_key = RowKey()

    original = ScrollableContainer.scroll_to_widget

    def fake_scroll_to_widget(self, widget, **kwargs):
        scroll_calls.append((widget.id, kwargs))
        return original(self, widget, **kwargs)

    monkeypatch.setattr(ScrollableContainer, "scroll_to_widget", fake_scroll_to_widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.on_data_table_row_selected(Event())
        await app.on_data_table_row_selected(Event())
        await pilot.pause()
        await pilot.pause()

        assert any(
            widget_id == "card-pr-acme-demo-1"
            and kwargs.get("top") is True
            and kwargs.get("force") is True
            and kwargs.get("immediate") is True
            for widget_id, kwargs in scroll_calls
        )


@pytest.mark.asyncio
async def test_queue_detail_has_local_fallback_when_server_detail_is_missing(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = NoDetailClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_queue_id = "fix_ci:pr:acme/demo#1"
        await app.render_right_pane()
        await asyncio.sleep(0.05)
        await pilot.pause()

        detail = str(app.query_one("#queue-detail").content)
        assert "Kind: fix_ci" in detail
        assert "Action: run agent remediation against the failing CI state." in detail


@pytest.mark.asyncio
async def test_pending_queue_item_shows_status_not_no_active_remediation(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.selected_queue_id = "fix_ci:pr:acme/demo#1"
        await app.render_right_pane()
        await pilot.pause()

        output = str(app.query_one("#session-output").content)
        assert "Status: pending agent action" in output
        assert "No active remediation" not in output


@pytest.mark.asyncio
async def test_review_server_client_handles_large_single_line_responses(tmp_path):
    socket_path = tmp_path / "review.sock"

    async def handle_client(reader, writer):
        await reader.readline()
        payload = {"status": "ok", "blob": "x" * 150000}
        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle_client, path=str(socket_path))
    async with server:
        client = ReviewServerClient(str(socket_path))
        response = await client.send_command({"cmd": "ping"})
        assert response is not None
        assert response["status"] == "ok"
        assert len(response["blob"]) == 150000


def test_pack_entities_wraps_compact_cards_by_available_width():
    entities = [
        {"title": "alpha", "subtitle": "repo#1"},
        {"title": "beta", "subtitle": "repo#2"},
        {"title": "gamma", "subtitle": "repo#3"},
    ]

    widths = [card_width_for_entity(entity) for entity in entities]
    rows = pack_entities(entities, widths[0] + widths[1] + 1)

    assert len(rows) == 2
    assert [entity["title"] for entity in rows[0]] == ["alpha", "beta"]
    assert [entity["title"] for entity in rows[1]] == ["gamma"]


def test_render_entity_detail_is_compact_single_line():
    config = ContxtConfig()
    app = ReviewLoopTUI(config)

    detail = app.render_entity_detail(
        {
            "title": "alpha",
            "subtitle": "acme/demo#1",
            "workflow_state": "review_loop",
            "reviews_state": "yellow",
            "reviews_summary": "Waiting on review",
            "ci_state": "red",
            "ci_summary": "CI failed",
            "status_state": "green",
            "status_summary": "PR submitted",
            "worktree_path": "/tmp/alpha",
            "pr_url": "https://github.com/acme/demo/pull/1",
            "ready_to_merge": True,
            "current_remediation": {
                "session_name": "contxt-review-alpha",
                "started_at": "2026-03-18T00:00:00Z",
            },
        }
    )

    assert "\n" not in detail
    assert "loop=review_loop" in detail
    assert "merge=ready" in detail
    assert "rev=" in detail
    assert "ci=" in detail
    assert "pr=" in detail
    assert "run=contxt-review-alpha" in detail
    assert "https://github.com/acme/demo/pull/1" not in detail


def test_render_queue_summary_is_compact_single_line():
    config = ContxtConfig()
    app = ReviewLoopTUI(config)

    summary = app.render_queue_summary(89, 111, 1)

    assert summary == "queue | entities=89 | items=111 | ready=1"


def test_session_manager_auto_prefers_tmux_over_zellij(monkeypatch):
    monkeypatch.setattr("contxt_sessions.shutil.which", lambda name: "/usr/bin/" + name if name in {"tmux", "zellij"} else None)
    manager = SessionManager("auto", "claude", "contxt-review")
    assert isinstance(manager.backend, TmuxBackend)


@pytest.mark.asyncio
async def test_queue_supports_vim_movement_keys_when_focused(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = MultiQueueClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        queue_table = app.query_one("#queue-table", DataTable)
        queue_table.focus()
        await pilot.pause()

        assert app.selected_queue_id == "fix_ci:pr:acme/demo#1"
        await pilot.press("j")
        await pilot.pause()
        assert app.selected_queue_id == "resolve_review_thread:pr:acme/demo#1:thread-1"

        await pilot.press("k")
        await pilot.pause()
        assert app.selected_queue_id == "fix_ci:pr:acme/demo#1"


@pytest.mark.asyncio
async def test_loading_screen_has_visible_indicator_and_label(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = FakeReviewClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.query_one("#loading-indicator", LoadingIndicator), LoadingIndicator)
        assert str(app.query_one("#loading-label").content) == "Loading review loop..."


@pytest.mark.asyncio
async def test_queue_selection_is_preserved_when_rows_rebuild(monkeypatch):
    config = ContxtConfig()
    app = ReviewLoopTUI(config)
    app.client = MultiQueueClient()

    async def fake_ensure_server():
        return None

    monkeypatch.setattr(app, "ensure_server", fake_ensure_server)

    async with app.run_test() as pilot:
        await pilot.pause()
        queue_table = app.query_one("#queue-table", DataTable)
        queue_table.focus()
        await pilot.press("j")
        await pilot.pause()
        assert app.selected_queue_id == "resolve_review_thread:pr:acme/demo#1:thread-1"

        app.snapshot["queue"][0]["status"] = "completed"
        await app.render_right_pane()
        await pilot.pause()

        assert app.selected_queue_id == "resolve_review_thread:pr:acme/demo#1:thread-1"
