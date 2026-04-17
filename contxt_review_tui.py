#!/usr/bin/env python3
"""
Textual dashboard for the async review loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import webbrowser
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Dict, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical, VerticalScroll
from textual import events
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, LoadingIndicator, Static

from contxt_config import ContxtConfig
from contxt_review_logging import ReviewLogger
from contxt_sessions import SessionManager


STATE_COLORS = {
    "green": "#5fb86b",
    "yellow": "#e0b34a",
    "red": "#d25b57",
    "grey": "#707b84",
}


def card_width_for_entity(entity: Dict[str, Any]) -> int:
    subtitle = entity.get("subtitle", "")
    title = entity.get("title", "")
    longest = max(len(title), len(subtitle), len("REV CI PR"))
    return max(18, min(34, longest + 4))


def pack_entities(entities: list[Dict[str, Any]], available_width: int) -> list[list[Dict[str, Any]]]:
    if available_width <= 0:
        return [entities] if entities else []

    rows: list[list[Dict[str, Any]]] = []
    current_row: list[Dict[str, Any]] = []
    current_width = 0
    gap = 1

    for entity in entities:
        width = card_width_for_entity(entity)
        projected = width if not current_row else current_width + gap + width
        if current_row and projected > available_width:
            rows.append(current_row)
            current_row = [entity]
            current_width = width
        else:
            current_row.append(entity)
            current_width = projected

    if current_row:
        rows.append(current_row)
    return rows


def safe_widget_id(value: str) -> str:
    cleaned = []
    for char in value:
        if char.isalnum() or char in {"_", "-"}:
            cleaned.append(char)
        else:
            cleaned.append("-")
    result = "".join(cleaned).strip("-")
    if not result or result[0].isdigit():
        result = f"item-{result}"
    return result


class ReviewServerClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    async def send_command(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            reader, writer = await asyncio.open_unix_connection(
                self.socket_path,
                limit=1024 * 1024,
            )
        except OSError:
            return None

        writer.write((json.dumps(payload) + "\n").encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        if not line:
            return None
        return json.loads(line.decode("utf-8"))


class BranchCard(Static):
    class Selected(Message):
        def __init__(self, card: "BranchCard") -> None:
            self.card = card
            super().__init__()

    def __init__(self, entity: Dict[str, Any], selected: bool = False):
        self.entity = entity
        label = self._render_label(selected)
        super().__init__(
            label,
            id=f"card-{safe_widget_id(entity['id'])}",
            classes=self._classes(selected),
        )

    def _chip(self, label: str, state: str) -> str:
        color = STATE_COLORS.get(state, STATE_COLORS["grey"])
        return f"[black on {color}] {label} [/]"

    def _classes(self, selected: bool) -> str:
        classes = ["branch-card"]
        if self.entity["lifecycle_state"] == "orphaned":
            classes.append("orphaned")
        if selected:
            classes.append("selected")
        return " ".join(classes)

    def _render_label(self, selected: bool) -> str:
        status_line = " ".join(
            [
                self._chip("REV", self.entity["reviews_state"]),
                self._chip("CI", self.entity["ci_state"]),
                self._chip("PR", self.entity["status_state"]),
            ]
        )
        heading = f"[b]{self.entity['title']}[/b]"
        subtitle = self.entity["subtitle"]
        if self.entity["ready_to_merge"]:
            subtitle += " [green](ready)[/]"
        if selected:
            heading = f"[reverse]{heading}[/reverse]"
        return f"{heading}\n{subtitle}\n{status_line}"

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Selected(self))


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter", "confirm", show=False, priority=True),
        Binding("escape", "cancel", show=False, priority=True),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="confirm-dialog"):
            yield Static(self.message, markup=False)
            with Horizontal():
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", variant="default", id="no")

    def on_mount(self) -> None:
        self.query_one("#yes", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def key_enter(self) -> None:
        self.dismiss(True)

    def key_escape(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes":
            self.dismiss(True)
        else:
            self.dismiss(False)

    async def on_key(self, event: events.Key) -> None:
        if event.key == "y":
            self.dismiss(True)
        elif event.key == "n":
            self.dismiss(False)


class ReviewLoopTUI(App):
    CSS = """
    Screen {
        background: #efe7d5;
        color: #1f252a;
    }
    #body {
        layout: horizontal;
        height: 1fr;
    }
    #left-pane {
        width: 1fr;
        padding: 0;
        border-right: solid #bfae8b;
    }
    #left-column {
        width: 1fr;
        padding: 0;
    }
    #entity-search {
        height: 1;
        min-height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: #f6efe2;
        color: #1f252a;
    }
    #left-scroll {
        width: 1fr;
        height: 1fr;
        padding: 0;
    }
    #right-pane {
        width: 1fr;
        padding: 0;
    }
    #loading-screen {
        width: 1fr;
        height: 1fr;
        align: center middle;
        background: #f6efe2;
    }
    #loading-panel {
        width: auto;
        height: auto;
        align: center middle;
    }
    #loading-indicator {
        width: auto;
        height: auto;
        color: #aa4126;
    }
    #loading-label {
        width: auto;
        height: auto;
        color: #5d4d36;
    }
    #status-bar {
        height: 1;
        margin: 0;
    }
    #status-spacer {
        width: 1fr;
    }
    #status-summary {
        width: auto;
        color: #5d4d36;
        padding: 0;
    }
    #right-main {
        height: 1fr;
    }
    #queue-table {
        width: 1fr;
        height: 1fr;
        margin: 0;
    }
    #inspector-pane {
        width: 1fr;
        height: 1fr;
    }
    #queue-detail-scroll {
        height: 12;
        margin: 0;
    }
    #queue-detail {
        height: auto;
        padding: 0;
        background: #f6efe2;
        color: #1f252a;
        border-left: solid #bfae8b;
        border-bottom: solid #bfae8b;
    }
    #card-list {
        height: auto;
    }
    .card-row {
        height: auto;
        margin-bottom: 0;
    }
    .branch-card {
        height: auto;
        width: auto;
        min-width: 18;
        padding: 0;
        margin: 0 1 0 0;
        content-align: left top;
        background: #f6efe2;
        color: #1f252a;
        border: solid #bfae8b;
    }
    .branch-card.orphaned {
        background: #d6d6d2;
        color: #46505a;
        border: solid #9ba5ad;
    }
    .branch-card.selected {
        border: heavy #aa4126;
        background: #f2ddbe;
    }
    #output-scroll {
        height: 1fr;
        margin: 0;
    }
    #session-output {
        height: auto;
        padding: 0;
        background: #212a2f;
        color: #e6eee3;
        border-left: solid #58636b;
    }
    #confirm-dialog {
        width: 60%;
        height: auto;
        padding: 2;
        background: #f6efe2;
        border: heavy #aa4126;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "clear_selection", "Queue"),
        Binding("v", "toggle_workflow", "Toggle Loop"),
        Binding("a", "dispatch_selected", "Dispatch"),
        Binding("g", "assign_agent", "Assign Agent"),
        Binding("h", "assign_human", "Assign Human"),
        Binding("c", "create_worktree", "Create WT"),
        Binding("e", "edit_worktree", "Edit WT"),
        Binding("d", "delete_worktree", "Delete WT"),
        Binding("l", "toggle_logs", "Logs"),
        Binding("m", "merge_worktree", "Merge WT"),
        Binding("o", "open_github", "Open GitHub"),
        Binding("/", "focus_search", "Search", show=False),
        Binding("j", "queue_down", "Queue Down", show=False),
        Binding("k", "queue_up", "Queue Up", show=False),
        Binding("enter", "attach_session", "Attach Session", show=False, priority=True),
    ]

    def __init__(self, config: ContxtConfig):
        super().__init__()
        self.config = config
        self.client = ReviewServerClient(config.get("review_socket_path"))
        self.session_manager = SessionManager(
            config.get("session_backend", "auto"),
            config.get("review_agent_command") or config.get("agent_command", "claude"),
            config.get("review_session_prefix", "contxt-review"),
        )
        self.snapshot: Dict[str, Any] = {"entities": [], "queue": [], "summary": {}}
        self.selected_entity_id: Optional[str] = None
        self.selected_queue_id: Optional[str] = None
        self.show_logs = False
        self._server_process: Optional[subprocess.Popen[Any]] = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._contxt_entrypoint = Path(__file__).resolve().with_name("contxt")
        self.logger = ReviewLogger(config.get("review_log_path"), "tui")
        self._queue_detail_queue_id: Optional[str] = None
        self._queue_detail_loading_queue_id: Optional[str] = None
        self._queue_detail_text = "Select a queue item."
        self._session_output_queue_id: Optional[str] = None
        self._session_output_loading_queue_id: Optional[str] = None
        self._session_output_text = "Queue empty."
        self._cards_signature: Optional[tuple[Any, ...]] = None
        self._queue_signature: Optional[tuple[Any, ...]] = None
        self._status_summary_text = ""
        self._display_mode: Optional[str] = None
        self._detail_panel_height = "12"
        self._session_panel_height = "1fr"
        self._last_queue_detail_text = "Select a queue item."
        self._last_session_output_text = "No active remediation."
        self._last_queue_select_id: Optional[str] = None
        self._last_queue_select_at = 0.0
        self._snapshot_refresh_in_flight = False
        self._has_loaded_snapshot = False
        self._suspend_queue_highlight_events = False
        self._entity_search = ""

    def popup(self, message: str, severity: str = "information") -> None:
        self.notify(message, severity=severity)

    def selected_queue_item(self, queue_items: Optional[list[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        items = queue_items if queue_items is not None else self.snapshot.get("queue", [])
        if not self.selected_queue_id:
            return None
        return next((item for item in items if item["id"] == self.selected_queue_id), None)

    def selected_queue_entity(self, queue_item: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        item = queue_item or self.selected_queue_item()
        if not item:
            return None
        return next(
            (entity for entity in self.snapshot.get("entities", []) if entity["id"] == item["entity_id"]),
            None,
        )

    def run_in_background(self, label: str, coro: Awaitable[Any]) -> None:
        async def runner() -> None:
            try:
                await coro
            except Exception as exc:
                self.logger.error("background task failed", label=label, error=str(exc))

        task = asyncio.create_task(runner(), name=f"review-tui:{label}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="left-pane"):
                with Vertical(id="left-column"):
                    yield Input(placeholder="search", id="entity-search")
                    with ScrollableContainer(id="left-scroll"):
                        yield Vertical(id="card-list")
            with Vertical(id="right-pane"):
                with Vertical(id="loading-screen"):
                    with Vertical(id="loading-panel"):
                        yield LoadingIndicator(id="loading-indicator")
                        yield Static("Loading review loop...", id="loading-label", markup=False)
                with Horizontal(id="status-bar"):
                    yield Static("", id="status-spacer")
                    yield Static("Loading review loop...", id="status-summary")
                with Horizontal(id="right-main"):
                    yield DataTable(id="queue-table")
                    with Vertical(id="inspector-pane"):
                        with VerticalScroll(id="queue-detail-scroll", can_focus=True):
                            yield Static("Select a queue item.", id="queue-detail", markup=False)
                        with VerticalScroll(id="output-scroll", can_focus=True):
                            yield Static("No active remediation.", id="session-output", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.logger.info("review tui mounted")
        await self.ensure_server()
        queue_table = self.query_one("#queue-table", DataTable)
        queue_table.add_columns("Owner", "Item", "Status")
        queue_table.cursor_type = "row"
        self.set_interval(10, self.schedule_snapshot_refresh)
        self.set_interval(2, self.refresh_logs)
        overview = await self.client.send_command({"cmd": "get_overview"})
        if overview and overview.get("status") == "ok":
            self.snapshot = overview["overview"]
            self._has_loaded_snapshot = True
            await self.render_cards()
            await self.render_right_pane()
            queue_table.focus()
        self.run_in_background("initial_refresh", self.refresh_snapshot())

    def schedule_snapshot_refresh(self) -> None:
        if self._snapshot_refresh_in_flight:
            return
        self.run_in_background("refresh", self.refresh_snapshot())

    async def ensure_server(self) -> None:
        response = await self.client.send_command({"cmd": "get_overview"})
        if response:
            self.logger.info("connected to existing review server")
            return

        self.logger.info("starting review server")
        entrypoint = Path(__file__).resolve().with_name("contxt")
        self._server_process = subprocess.Popen(
            [sys.executable, str(entrypoint), "review-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(60):
            await asyncio.sleep(0.25)
            response = await self.client.send_command({"cmd": "get_overview"})
            if response:
                self.logger.info("review server became available")
                return
        self.logger.error("review server did not start")
        raise RuntimeError("review server did not start")

    async def refresh_snapshot(self) -> None:
        self._snapshot_refresh_in_flight = True
        try:
            response = await self.client.send_command({"cmd": "refresh_overview"})
            if not response or response.get("status") != "ok":
                self.logger.error("refresh overview failed", response=response)
                if not self._has_loaded_snapshot:
                    overview = await self.client.send_command({"cmd": "get_overview"})
                    if overview and overview.get("status") == "ok":
                        self.snapshot = overview["overview"]
                        self._has_loaded_snapshot = True
                        await self.render_cards()
                        await self.render_right_pane()
                return
            self.snapshot = response["overview"]
            self._has_loaded_snapshot = True
            self.logger.info(
                "overview refreshed",
                entity_count=self.snapshot.get("summary", {}).get("entity_count", 0),
                queue_count=self.snapshot.get("summary", {}).get("queue_count", 0),
            )
            await self.render_cards()
            await self.render_right_pane()
        finally:
            self._snapshot_refresh_in_flight = False

    async def refresh_logs(self) -> None:
        if self.show_logs:
            await self.render_right_pane()

    def filtered_entities(self) -> list[Dict[str, Any]]:
        entities = self.snapshot.get("entities", [])
        query = self._entity_search.strip().lower()
        if not query:
            return entities
        filtered = []
        for entity in entities:
            haystack = " ".join(
                str(value or "")
                for value in [
                    entity.get("title"),
                    entity.get("subtitle"),
                    entity.get("branch"),
                    entity.get("worktree_key"),
                    entity.get("worktree_path"),
                ]
            ).lower()
            if query in haystack:
                filtered.append(entity)
        return filtered

    def request_session_output(self, queue_id: str, entity_id: Optional[str] = None) -> None:
        if self.show_logs or self._session_output_loading_queue_id == queue_id:
            return
        self._session_output_loading_queue_id = queue_id
        self.run_in_background(
            "session_output",
            self._load_session_output(queue_id, entity_id),
        )

    def request_queue_detail(self, queue_id: str) -> None:
        if self.show_logs or self._queue_detail_loading_queue_id == queue_id:
            return
        self._queue_detail_loading_queue_id = queue_id
        self.run_in_background(
            "queue_detail",
            self._load_queue_detail(queue_id),
        )

    async def _load_queue_detail(self, queue_id: str) -> None:
        try:
            text = await self.fetch_queue_item_detail(queue_id)
            if self.selected_queue_id == queue_id:
                self._queue_detail_queue_id = queue_id
                if text:
                    self._queue_detail_text = text
                if not self.show_logs and self._last_queue_detail_text != self._queue_detail_text:
                    self.query_one("#queue-detail", Static).update(Text(self._queue_detail_text))
                    self._last_queue_detail_text = self._queue_detail_text
        finally:
            if self._queue_detail_loading_queue_id == queue_id:
                self._queue_detail_loading_queue_id = None

    async def _load_session_output(self, queue_id: str, entity_id: Optional[str]) -> None:
        try:
            output = await self.fetch_session_output(queue_id, entity_id)
            text = output if output else self.render_local_session_status(
                self.selected_queue_item(),
                self.selected_queue_entity(),
            )
            if self.selected_queue_id == queue_id:
                self._session_output_queue_id = queue_id
                self._session_output_text = text
                if not self.show_logs:
                    if self._last_session_output_text != text:
                        self.query_one("#session-output", Static).update(Text(text))
                        self._last_session_output_text = text
        finally:
            if self._session_output_loading_queue_id == queue_id:
                self._session_output_loading_queue_id = None

    async def render_cards(self) -> None:
        card_list = self.query_one("#card-list", Vertical)
        entities = self.filtered_entities()
        available_width = max(20, self.query_one("#left-scroll").size.width - 2)
        signature = (
            available_width,
            tuple(
                (
                    entity["id"],
                    entity["title"],
                    entity["subtitle"],
                    entity["reviews_state"],
                    entity["ci_state"],
                    entity["status_state"],
                    entity["ready_to_merge"],
                    entity["lifecycle_state"],
                    entity["id"] == self.selected_entity_id,
                )
                for entity in entities
            ),
        )
        if signature == self._cards_signature:
            return

        async with card_list.batch():
            await card_list.remove_children()
            for row_entities in pack_entities(entities, available_width):
                cards = []
                for entity in row_entities:
                    card = BranchCard(
                        entity,
                        selected=entity["id"] == self.selected_entity_id,
                    )
                    card.styles.width = card_width_for_entity(entity)
                    cards.append(card)
                row = Horizontal(*cards, classes="card-row")
                await card_list.mount(row)
        self._cards_signature = signature
        self.schedule_scroll_selected_card_into_view()

    def schedule_scroll_selected_card_into_view(self) -> None:
        if self.selected_entity_id:
            self.call_after_refresh(self.scroll_selected_card_into_view)

    def scroll_selected_card_into_view(self) -> None:
        if not self.selected_entity_id:
            return
        left_pane = self.query_one("#left-scroll", ScrollableContainer)
        card_id = f"#card-{safe_widget_id(self.selected_entity_id)}"
        card = self.query(card_id).first()
        if card is not None:
            left_pane.scroll_to_widget(
                card,
                animate=False,
                top=True,
                force=True,
                immediate=True,
            )

    def selected_entity(self) -> Optional[Dict[str, Any]]:
        if not self.selected_entity_id:
            return None
        return next(
            (item for item in self.snapshot.get("entities", []) if item["id"] == self.selected_entity_id),
            None,
        )

    def selected_worktree_identifiers(self) -> Optional[tuple[str, str]]:
        entity = self.selected_entity() or self.selected_queue_entity()
        if not entity:
            return None
        worktree_key = entity.get("worktree_key")
        if not worktree_key or "/" not in worktree_key:
            return None
        project, name = worktree_key.split("/", 1)
        return project, name

    def selected_session_name(self) -> Optional[str]:
        entity = self.selected_entity() or self.selected_queue_entity()
        if not entity:
            return None
        remediation = entity.get("current_remediation") or {}
        return remediation.get("session_name")

    async def run_contxt_command(self, *args: str, wait: bool = True) -> bool:
        if wait:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self._contxt_entrypoint),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.communicate()
            return process.returncode == 0

        subprocess.Popen(
            [sys.executable, str(self._contxt_entrypoint), *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True

    async def get_delete_preflight(self, project: str, name: str) -> Optional[Dict[str, Any]]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self._contxt_entrypoint),
            "delete-check",
            name,
            "-p",
            project,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode != 0:
            return None
        try:
            return json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def format_delete_warning(self, preflight: Dict[str, Any]) -> str:
        lines = [f"Delete worktree {preflight['project']}/{preflight['name']}?"]
        if preflight.get("has_changes"):
            lines.append("This worktree has staged/unstaged changes:")
            for line in preflight.get("status_lines", [])[:12]:
                lines.append(f"  {line}")
            if len(preflight.get("status_lines", [])) > 12:
                lines.append(f"  ... and {len(preflight['status_lines']) - 12} more")
        return "\n".join(lines)

    async def render_right_pane(self) -> None:
        loading_screen = self.query_one("#loading-screen", Vertical)
        status_bar = self.query_one("#status-bar", Horizontal)
        status_summary = self.query_one("#status-summary", Static)
        right_main = self.query_one("#right-main", Horizontal)
        queue_table = self.query_one("#queue-table", DataTable)
        queue_detail_scroll = self.query_one("#queue-detail-scroll", VerticalScroll)
        queue_detail = self.query_one("#queue-detail", Static)
        output_scroll = self.query_one("#output-scroll", VerticalScroll)
        session_output = self.query_one("#session-output", Static)
        right_pane = self.query_one("#right-pane", Vertical)

        if not self._has_loaded_snapshot:
            loading_screen.display = True
            status_bar.display = False
            right_main.display = False
            return

        loading_screen.display = False
        status_bar.display = True
        right_main.display = True

        if self.show_logs:
            log_text = self.logger.tail_text(250)
            async with right_pane.batch():
                if self._display_mode != "logs":
                    queue_detail_scroll.display = False
                if self._session_panel_height != "1fr":
                    output_scroll.styles.height = "1fr"
                    self._session_panel_height = "1fr"
                if self._last_session_output_text != log_text:
                    session_output.update(Text(log_text))
                    self._last_session_output_text = log_text
                    output_scroll.scroll_end(animate=False, immediate=True)
            self._display_mode = "logs"
            return

        entities = {entity["id"]: entity for entity in self.snapshot.get("entities", [])}
        selected_entity = entities.get(self.selected_entity_id)

        if selected_entity:
            summary_text = self.render_entity_detail(selected_entity)
            queue_items = [item for item in self.snapshot.get("queue", []) if item["entity_id"] == selected_entity["id"]]
        else:
            queue_items = self.snapshot.get("queue", [])

        queue_signature = tuple(
            (item["id"], item["owner"], item["title"], item.get("status", "pending"))
            for item in queue_items
        )

        selected_index = None
        if queue_items:
            if self.selected_queue_id not in {item["id"] for item in queue_items}:
                self.selected_queue_id = queue_items[0]["id"]
            selected_index = next(
                (
                    index
                    for index, item in enumerate(queue_items)
                    if item["id"] == self.selected_queue_id
                ),
                0,
            )
        else:
            self.selected_queue_id = None
            self._queue_detail_queue_id = None
            self._queue_detail_loading_queue_id = None
            self._session_output_queue_id = None
            self._session_output_loading_queue_id = None

        selected_queue_item = self.selected_queue_item(queue_items)
        selected_queue_entity = self.selected_queue_entity(selected_queue_item)
        if not selected_entity:
            summary = self.snapshot.get("summary", {})
            summary_text = self.render_queue_selection_summary(
                summary.get("entity_count", 0),
                summary.get("queue_count", 0),
                summary.get("ready_to_merge_count", 0),
                selected_queue_item,
                selected_queue_entity,
            )

        if self.selected_queue_id:
            if self._queue_detail_queue_id != self.selected_queue_id:
                self._queue_detail_text = self.render_local_queue_item_detail(
                    selected_queue_item,
                    selected_queue_entity,
                )
                self.request_queue_detail(self.selected_queue_id)
        else:
            self._queue_detail_text = "Select a queue item."

        if self.selected_queue_id:
            if selected_queue_item and selected_queue_item.get("status") == "running":
                if self._session_output_queue_id != self.selected_queue_id:
                    self._session_output_text = self.render_local_session_status(
                        selected_queue_item,
                        selected_queue_entity,
                    )
                    self.request_session_output(
                        self.selected_queue_id,
                        selected_queue_entity["id"] if selected_queue_entity else None,
                    )
            else:
                self._session_output_queue_id = self.selected_queue_id
                self._session_output_loading_queue_id = None
                self._session_output_text = self.render_local_session_status(
                    selected_queue_item,
                    selected_queue_entity,
                )
        else:
            self._session_output_queue_id = None
            self._session_output_loading_queue_id = None
            self._session_output_text = "Select a queue item."

        async with right_pane.batch():
            if self._display_mode != "main":
                queue_detail_scroll.display = True
            if self._detail_panel_height != "12":
                queue_detail_scroll.styles.height = 12
                self._detail_panel_height = "12"
            if self._session_panel_height != "1fr":
                output_scroll.styles.height = "1fr"
                self._session_panel_height = "1fr"
            if self._status_summary_text != summary_text:
                status_summary.update(summary_text)
                self._status_summary_text = summary_text
            self._suspend_queue_highlight_events = True
            try:
                if self._queue_signature != queue_signature:
                    queue_table.clear(columns=False)
                    for item in queue_items:
                        queue_table.add_row(
                            item["owner"],
                            item["title"],
                            item.get("status", "pending"),
                            key=item["id"],
                        )
                    self._queue_signature = queue_signature
                if selected_index is not None:
                    queue_table.move_cursor(row=selected_index)
            finally:
                self._suspend_queue_highlight_events = False
            if self._last_queue_detail_text != self._queue_detail_text:
                queue_detail.update(Text(self._queue_detail_text))
                self._last_queue_detail_text = self._queue_detail_text
            if self._last_session_output_text != self._session_output_text:
                session_output.update(Text(self._session_output_text))
                self._last_session_output_text = self._session_output_text
                output_scroll.scroll_end(animate=False, immediate=True)
        self._display_mode = "main"

    def render_queue_summary(self, entity_count: int, queue_count: int, ready_to_merge_count: int) -> str:
        parts = [
            "queue",
            f"entities={entity_count}",
            f"items={queue_count}",
            f"ready={ready_to_merge_count}",
        ]
        summary = self.snapshot.get("summary", {})
        if summary.get("rate_limited"):
            retry = summary.get("rate_limit_retry_in_seconds")
            if retry is not None:
                parts.append(f"GH_RATE_LIMIT {retry}s")
            else:
                parts.append("GH_RATE_LIMIT")
        return " | ".join(parts)

    def render_queue_selection_summary(
        self,
        entity_count: int,
        queue_count: int,
        ready_to_merge_count: int,
        queue_item: Optional[Dict[str, Any]],
        entity: Optional[Dict[str, Any]],
    ) -> str:
        parts = [self.render_queue_summary(entity_count, queue_count, ready_to_merge_count)]
        if entity:
            parts.append(entity.get("branch") or entity.get("subtitle") or "unknown-branch")
        if queue_item:
            parts.append(queue_item["title"])
            parts.append(queue_item.get("status", "pending"))
        return " | ".join(parts)

    def render_local_queue_item_detail(
        self,
        queue_item: Optional[Dict[str, Any]],
        entity: Optional[Dict[str, Any]],
    ) -> str:
        if not queue_item:
            return "Select a queue item."

        lines = [
            f"Title: {queue_item['title']}",
            f"Kind: {queue_item.get('kind', 'unknown')}",
            f"Owner: {queue_item.get('owner', 'unknown')}",
            f"Status: {queue_item.get('status', 'pending')}",
            f"Target: {queue_item.get('entity_id', 'unknown')}",
        ]

        if entity:
            lines.extend(
                [
                    f"Branch: {entity.get('branch') or entity.get('subtitle') or 'unknown'}",
                    f"Worktree: {entity.get('worktree_path') or 'missing'}",
                    f"Reviews: {entity.get('reviews_summary', 'unknown')}",
                    f"CI: {entity.get('ci_summary', 'unknown')}",
                    f"PR: {entity.get('status_summary', 'unknown')}",
                ]
            )

        kind = queue_item.get("kind", "")
        if kind == "create_worktree":
            lines.append("Action: create the missing worktree so agent remediation can start.")
        elif kind == "delete_worktree":
            lines.append("Action: delete the stale worktree.")
        elif kind == "fix_ci":
            lines.append("Action: run agent remediation against the failing CI state.")
        elif kind == "resolve_review_thread":
            lines.append("Action: respond to and resolve the outstanding review thread.")
        elif kind == "request_reviewers":
            lines.append("Action: ask humans for final review.")
        elif kind == "ready_to_merge":
            lines.append("Action: PR is ready for merge review.")
        elif kind == "submit_pr":
            lines.append("Action: submit the PR for this worktree.")
        elif kind == "rebase_branch":
            lines.append("Action: do the final rebase onto main once blockers are clear.")

        return "\n".join(lines)

    def render_local_session_status(
        self,
        queue_item: Optional[Dict[str, Any]],
        entity: Optional[Dict[str, Any]],
    ) -> str:
        if not queue_item:
            return "Select a queue item."

        status = queue_item.get("status", "pending")
        owner = queue_item.get("owner", "unknown")
        kind = queue_item.get("kind", "unknown")

        if owner == "human":
            return "\n".join(
                [
                    "Status: pending human action",
                    f"Kind: {kind}",
                    "This queue item is waiting for manual remediation.",
                    "Use the queue detail pane for the review context and required action.",
                ]
            )
        if status == "running":
            remediation = (entity or {}).get("current_remediation") or {}
            session_name = remediation.get("session_name")
            lines = [
                "Status: running",
                f"Owner: {owner}",
                f"Kind: {kind}",
            ]
            if session_name:
                lines.append(f"Session: {session_name}")
                attach_cmd = self.session_manager.attach_command(session_name)
                if attach_cmd:
                    lines.append(f"Attach: {' '.join(attach_cmd)}")
                    lines.append("Press Enter to attach. Detach to return to the review TUI.")
            lines.append("Streaming live agent session output...")
            lines.append("If this stays empty, attach directly to the session.")
            return "\n".join(lines)
        if status == "completed":
            return f"Status: completed\nOwner: {owner}\nKind: {kind}\nNo live session is attached."
        if kind == "create_worktree":
            return "Status: pending agent action\nWaiting to create the missing worktree."
        if entity and not entity.get("worktree_path"):
            return "Status: blocked\nA worktree is required before agent remediation can run."
        return f"Status: pending agent action\nKind: {kind}\nNo live remediation session has started yet."

    def render_entity_detail(self, entity: Dict[str, Any]) -> str:
        worktree = "wt" if entity.get("worktree_path") else "no-wt"
        parts = [
            f"[b]{entity['title']}[/b]",
            entity["subtitle"],
            f"loop={entity['workflow_state']}",
            f"rev={entity['reviews_state']}",
            f"ci={entity['ci_state']}",
            f"pr={entity['status_state']}",
            worktree,
        ]
        if entity["ready_to_merge"]:
            parts.append("merge=ready")
        remediation = entity.get("current_remediation")
        if remediation:
            parts.append(f"run={remediation.get('session_name', 'agent')}")
        return " | ".join(parts)

    async def fetch_queue_item_detail(self, queue_id: str) -> Optional[str]:
        response = await self.client.send_command({"cmd": "get_queue_item_detail", "queue_id": queue_id})
        if not response or response.get("status") != "ok":
            return None
        return response.get("detail")

    async def fetch_session_output(self, queue_id: str, entity_id: Optional[str]) -> Optional[str]:
        if entity_id:
            response = await self.client.send_command(
                {"cmd": "capture_entity_session", "entity_id": entity_id, "lines": 80}
            )
            if response and response.get("status") == "ok":
                return response.get("output")
        response = await self.client.send_command({"cmd": "capture_session", "queue_id": queue_id, "lines": 80})
        if not response or response.get("status") != "ok":
            return None
        return response.get("output")

    async def on_branch_card_selected(self, event: BranchCard.Selected) -> None:
        self.selected_entity_id = event.card.entity["id"]
        self.logger.info("entity selected", entity_id=self.selected_entity_id)
        self.popup(f"Selected {event.card.entity['title']}")
        await self.render_cards()
        self.run_in_background("render_right_pane", self.render_right_pane())

    async def _apply_queue_selection(self, queue_id: str, *, allow_double_select: bool, show_popup: bool) -> None:
        now = time.monotonic()
        is_double_select = False
        if allow_double_select:
            is_double_select = queue_id == self._last_queue_select_id and (now - self._last_queue_select_at) <= 0.45
            self._last_queue_select_id = queue_id
            self._last_queue_select_at = now
        self.selected_queue_id = queue_id
        self.logger.info("queue item selected", queue_id=self.selected_queue_id, double_select=is_double_select)
        queue_item = self.selected_queue_item()
        title = queue_item["title"] if queue_item else self.selected_queue_id
        if show_popup:
            self.popup(f"Selected queue item: {title}")
        if is_double_select:
            entity = self.selected_queue_entity(queue_item)
            if entity:
                self.selected_entity_id = entity["id"]
                self.logger.info("queue item activated entity", queue_id=queue_id, entity_id=entity["id"])
                await self.render_cards()
        self._queue_detail_queue_id = None
        self._queue_detail_text = self.render_local_queue_item_detail(
            queue_item,
            self.selected_queue_entity(queue_item),
        )
        self._session_output_queue_id = None
        self._session_output_text = self.render_local_session_status(
            queue_item,
            self.selected_queue_entity(queue_item),
        )
        self.run_in_background("render_right_pane", self.render_right_pane())

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._suspend_queue_highlight_events:
            return
        queue_id = (
            str(event.row_key.value)
            if hasattr(event.row_key, "value")
            else str(event.row_key)
        )
        await self._apply_queue_selection(queue_id, allow_double_select=False, show_popup=False)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        queue_id = (
            str(event.row_key.value)
            if hasattr(event.row_key, "value")
            else str(event.row_key)
        )
        await self._apply_queue_selection(queue_id, allow_double_select=True, show_popup=True)

    async def on_key(self, event: events.Key) -> None:
        if isinstance(self.screen, ConfirmDialog):
            if event.key == "enter":
                event.stop()
                self.screen.dismiss(True)
                return
            if event.key == "escape":
                event.stop()
                self.screen.dismiss(False)
                return
        if event.key != "enter":
            return
        if not self.selected_session_name():
            return
        event.stop()
        await self.action_attach_session()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "entity-search":
            return
        self._entity_search = event.value
        await self.render_cards()

    async def action_focus_search(self) -> None:
        self.query_one("#entity-search", Input).focus()

    async def action_queue_down(self) -> None:
        queue_table = self.query_one("#queue-table", DataTable)
        if not queue_table.has_focus:
            return
        queue_table.action_cursor_down()

    async def action_queue_up(self) -> None:
        queue_table = self.query_one("#queue-table", DataTable)
        if not queue_table.has_focus:
            return
        queue_table.action_cursor_up()

    async def action_refresh(self) -> None:
        self.logger.info("manual refresh requested")
        self.popup("Refreshing review loop")
        self.schedule_snapshot_refresh()

    async def action_clear_selection(self) -> None:
        self.selected_entity_id = None
        self.logger.info("selection cleared")
        self.popup("Showing universal queue")
        await self.render_cards()
        self.run_in_background("render_right_pane", self.render_right_pane())

    async def action_toggle_workflow(self) -> None:
        if not self.selected_entity_id:
            self.logger.info("toggle workflow ignored", reason="no selected entity")
            self.popup("Select a branch first", severity="warning")
            return
        entity = next((item for item in self.snapshot["entities"] if item["id"] == self.selected_entity_id), None)
        if not entity:
            self.logger.error("toggle workflow failed", entity_id=self.selected_entity_id, reason="entity missing")
            self.popup("Selected branch is missing", severity="error")
            return
        state = "working" if entity["workflow_state"] == "review_loop" else "review_loop"
        self.logger.info(
            "toggle workflow requested",
            entity_id=entity["id"],
            previous_state=entity["workflow_state"],
            requested_state=state,
        )
        entity["workflow_state"] = state
        self.popup(f"{entity['title']}: workflow -> {state}")
        await self.render_cards()
        self.run_in_background("render_right_pane", self.render_right_pane())
        self.run_in_background(
            "toggle_workflow",
            self._toggle_workflow_background(entity["id"], state),
        )

    async def _toggle_workflow_background(self, entity_id: str, state: str) -> None:
        try:
            response = await self.client.send_command({"cmd": "set_workflow_state", "entity_id": entity_id, "state": state})
            self.logger.info(
                "toggle workflow response",
                entity_id=entity_id,
                status=response.get("status") if response else None,
                workflow_state=response.get("workflow_state") if response else None,
            )
            await self.refresh_snapshot()
            updated = self.selected_entity()
            self.logger.info(
                "toggle workflow post-refresh",
                entity_id=entity_id,
                workflow_state=updated["workflow_state"] if updated else None,
            )
        except Exception as exc:
            self.logger.error("toggle workflow crashed", entity_id=entity_id, error=str(exc))

    async def _reassign_selected(self, owner: str) -> None:
        if not self.selected_queue_id:
            self.logger.info("queue owner change ignored", owner=owner, reason="no selected queue")
            self.popup("Select a queue item first", severity="warning")
            return
        self.logger.info("queue owner change requested", queue_id=self.selected_queue_id, owner=owner)
        self.popup(f"Queue owner -> {owner}")
        self.run_in_background(
            "set_queue_owner",
            self._reassign_selected_background(self.selected_queue_id, owner),
        )

    async def _reassign_selected_background(self, queue_id: str, owner: str) -> None:
        try:
            await self.client.send_command({"cmd": "set_queue_owner", "queue_id": queue_id, "owner": owner})
            await self.refresh_snapshot()
        except Exception as exc:
            self.logger.error("queue owner change crashed", queue_id=queue_id, owner=owner, error=str(exc))

    async def action_assign_agent(self) -> None:
        await self._reassign_selected("agent")

    async def action_assign_human(self) -> None:
        await self._reassign_selected("human")

    async def action_dispatch_selected(self) -> None:
        if not self.selected_queue_id:
            self.logger.info("dispatch ignored", reason="no selected queue")
            self.popup("Select a queue item first", severity="warning")
            return
        self.logger.info("dispatch requested", queue_id=self.selected_queue_id)
        self.popup("Dispatching queue item")
        self.run_in_background(
            "dispatch_queue_item",
            self._dispatch_selected_background(self.selected_queue_id),
        )

    async def _dispatch_selected_background(self, queue_id: str) -> None:
        try:
            await self.client.send_command({"cmd": "dispatch_queue_item", "queue_id": queue_id})
            await self.refresh_snapshot()
        except Exception as exc:
            self.logger.error("dispatch crashed", queue_id=queue_id, error=str(exc))

    async def action_create_worktree(self) -> None:
        if not self.selected_entity_id:
            self.logger.info("create worktree ignored", reason="no selected entity")
            self.popup("Select a branch first", severity="warning")
            return
        self.logger.info("create worktree requested", entity_id=self.selected_entity_id)
        entity = self.selected_entity()
        label = entity["title"] if entity else self.selected_entity_id
        self.popup(f"Creating worktree for {label}")
        entity_id = self.selected_entity_id
        self.run_in_background(
            "create_worktree",
            self._create_worktree_background(entity_id),
        )

    async def _create_worktree_background(self, entity_id: str) -> None:
        try:
            await self.client.send_command({"cmd": "dispatch_queue_item", "queue_id": f"create_worktree:{entity_id}"})
            await self.refresh_snapshot()
        except Exception as exc:
            self.logger.error("create worktree crashed", entity_id=entity_id, error=str(exc))

    async def action_edit_worktree(self) -> None:
        worktree = self.selected_worktree_identifiers()
        if not worktree:
            self.logger.info("edit worktree ignored", reason="no selected worktree")
            self.popup("No worktree for selected branch", severity="warning")
            return
        project, name = worktree
        self.logger.info("edit worktree requested", project=project, name=name)
        self.popup(f"Opening worktree {project}/{name}")
        await self.run_contxt_command("edit", name, "-p", project, wait=False)

    async def action_delete_worktree(self) -> None:
        worktree = self.selected_worktree_identifiers()
        if not worktree:
            self.logger.info("delete worktree ignored", reason="no selected worktree")
            self.popup("No worktree for selected branch", severity="warning")
            return
        project, name = worktree
        preflight = await self.get_delete_preflight(project, name)
        if preflight is None:
            self.popup("Failed to inspect worktree before delete", severity="error")
            return
        def handle_confirm(confirmed: bool) -> None:
            if not confirmed:
                self.popup("Delete cancelled")
                return
            self.logger.info("delete worktree requested", project=project, name=name)
            self.popup(f"Deleting worktree {project}/{name}")
            self.run_in_background(
                "delete_worktree",
                self._run_contxt_and_refresh("delete", name, "-p", project, "--yes"),
            )

        self.push_screen(ConfirmDialog(self.format_delete_warning(preflight)), handle_confirm)

    async def action_toggle_logs(self) -> None:
        self.show_logs = not self.show_logs
        self.logger.info("log mode toggled", enabled=self.show_logs)
        self.popup("Logs on" if self.show_logs else "Logs off")
        await self.render_right_pane()

    async def action_merge_worktree(self) -> None:
        worktree = self.selected_worktree_identifiers()
        if not worktree:
            self.logger.info("merge worktree ignored", reason="no selected worktree")
            self.popup("No worktree for selected branch", severity="warning")
            return
        project, name = worktree
        self.logger.info("merge worktree requested", project=project, name=name)
        self.popup(f"Merging worktree {project}/{name}")
        self.run_in_background(
            "merge_worktree",
            self._run_contxt_and_refresh("merge", name, "-p", project),
        )

    async def _run_contxt_and_refresh(self, *args: str) -> None:
        try:
            if await self.run_contxt_command(*args):
                await self.refresh_snapshot()
        except Exception as exc:
            self.logger.error("contxt command crashed", args=list(args), error=str(exc))

    async def action_open_github(self) -> None:
        if not self.selected_entity_id:
            self.logger.info("open github ignored", reason="no selected entity")
            self.popup("Select a branch first", severity="warning")
            return
        entity = next((item for item in self.snapshot["entities"] if item["id"] == self.selected_entity_id), None)
        if entity and entity.get("pr_url"):
            self.logger.info("open github requested", entity_id=entity["id"], pr_url=entity["pr_url"])
            self.popup(f"Opening GitHub for {entity['title']}")
            webbrowser.open(entity["pr_url"])
            return
        self.popup("No GitHub URL for selected branch", severity="warning")

    async def action_attach_session(self) -> None:
        if isinstance(self.screen, ConfirmDialog):
            self.screen.dismiss(True)
            return
        session_name = self.selected_session_name()
        if not session_name:
            self.popup("No active agent session for this selection", severity="warning")
            return
        attach_cmd = self.session_manager.attach_command(session_name)
        if not attach_cmd:
            self.popup("Current session backend does not support attach", severity="warning")
            return
        self.logger.info("attach session requested", session_name=session_name, command=attach_cmd)
        self.popup(f"Attaching to {session_name}")
        original_cwd = os.getcwd()
        worktree_path = (self.selected_entity() or self.selected_queue_entity() or {}).get("worktree_path")
        try:
            with App.suspend(self):
                if worktree_path and Path(worktree_path).exists():
                    os.chdir(worktree_path)
                subprocess.call(attach_cmd)
        finally:
            os.chdir(original_cwd)
        self.schedule_snapshot_refresh()


def main() -> None:
    config = ContxtConfig()
    ReviewLoopTUI(config).run()


if __name__ == "__main__":
    main()
