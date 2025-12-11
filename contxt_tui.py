#!/usr/bin/env python3
"""
Textual TUI for contxt - Terminal interface for managing worktrees
"""

import os
import sys
import json
import time
import socket
import shutil
import asyncio
import shlex
import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Dict, List, Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, Horizontal, ScrollableContainer, VerticalScroll
from textual.widgets import Header, Footer, Static, Label, Button, Input, Select, DataTable, Checkbox
from textual.screen import Screen, ModalScreen
from textual import events
from textual.reactive import reactive

from contxt_config import ContxtConfig


class ServerClient:
    """Client for communicating with contxt server"""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.socket: Optional[socket.socket] = None

    def connect(self) -> bool:
        """Connect to the server"""
        try:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.connect(self.socket_path)
            return True
        except Exception:
            return False

    def send_command(self, cmd: Dict) -> Optional[Dict]:
        """Send a command to the server and get response"""
        if not self.socket:
            return None

        try:
            message = json.dumps(cmd).encode() + b"\n"
            self.socket.sendall(message)

            # Receive response
            data = self.socket.recv(8192)
            if data:
                return json.loads(data.decode().strip())
        except Exception:
            return None

    def close(self):
        """Close the connection"""
        if self.socket:
            self.socket.close()
            self.socket = None


class WorktreeItem(Static):
    """A single worktree item in the list"""

    def __init__(
        self,
        worktree: Dict,
        preview_lines: int = 1,
        on_select: Optional[Callable[["WorktreeItem"], None]] = None,
    ):
        super().__init__()
        self.worktree = worktree
        self.preview_lines = preview_lines
        self.selected = False
        self.last_output: List[str] = []
        self._render_buffer: Optional[str] = None
        self._on_select = on_select

    def on_mount(self):
        """Update the display when mounted"""
        self.update_display()

    def update_display(self, output: Optional[List[str]] = None, status: Optional[str] = None):
        """Update the worktree display"""
        if status:
            self.worktree["status"] = status

        # Determine color based on status
        status_val = self.worktree.get("status", "stopped")
        if status_val == "working":
            color = "yellow"
            indicator = "●"
        elif status_val == "idle":
            color = "green"
            indicator = "●"
        else:  # stopped
            color = "red"
            indicator = "○"

        # Build display
        name = self.worktree["name"]
        git_name = self.worktree["git_name"]
        agent = self.worktree.get("agent", "")

        # Selection indicator
        selection = "▶ " if self.selected else "  "

        # Track and normalize preview lines so items keep a steady height
        if output is not None:
            self.last_output = list(output)
        current_output = list(self.last_output)

        preview_block: List[str] = []
        if self.preview_lines > 0:
            recent = current_output[-self.preview_lines :]
            preview_block = recent + [""] * (self.preview_lines - len(recent))
        elif output is not None:
            # Even if no preview requested, remember latest output copy
            self.last_output = list(output)

        # Include agent name in display if present
        agent_display = f" [{agent}]" if agent else ""
        lines = [f"{selection}[{color}]{indicator}[/] {git_name}/{name}{agent_display}"]

        if preview_block:
            for line in preview_block:
                lines.append(f"    {line}")

        separator = "[dim]" + "-" * 60 + "[/]"
        lines.append(separator)

        display = "\n".join(lines)

        if display == self._render_buffer:
            return  # Avoid flicker by only updating when content changes

        self._render_buffer = display
        self.update(display)

    def set_selected(self, selected: bool):
        """Set the selection state"""
        self.selected = selected
        self.update_display()

    def on_click(self, event: events.Click) -> None:
        """Handle mouse clicks by notifying the parent"""
        event.stop()
        if self._on_select:
            self._on_select(self)


class HelpScreen(ModalScreen):
    """Help screen showing all hotkeys"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        """Create the help screen layout"""
        with Container(id="help-dialog"):
            yield Static(
                """[bold]Contxt Help[/bold]

[yellow]Navigation[/yellow]
  ↑/↓         - Navigate up/down (default mode)
  Tab/S-Tab   - Navigate down/up
  j/k         - Navigate down/up (vim mode)
  C-n/C-p     - Navigate down/up (emacs mode)

[yellow]Actions[/yellow]
  Enter       - Attach to terminal session
  x           - Kill terminal session
  t           - Manage todos for worktree
  c           - Create new worktree
  d           - Delete worktree
  m           - Merge worktree to main
  e           - Edit worktree in configured editor

[yellow]Other[/yellow]
  s           - Settings
  r           - Restart server
  ?           - Show this help
  q           - Quit

[yellow]In Todo List[/yellow]
  Space       - Toggle todo done/not done
  a           - Add new todo
  d           - Delete selected todo
  ↑/↓, j/k    - Navigate todos

[yellow]In screen session (when attached)[/yellow]
  C-a then d  - Detach without killing agent
  C-c         - Kill current command
  exit        - Exit session

Press ESC or q to close this help.""",
                id="help-content",
            )

    def action_dismiss(self):
        """Dismiss the help screen"""
        self.app.pop_screen()


class SettingsScreen(ModalScreen):
    """Settings screen for configuration"""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, config: ContxtConfig):
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        """Create the settings screen layout"""
        with Container(id="settings-dialog"):
            yield Static("[bold]Settings[/bold]", id="settings-title")

            with ScrollableContainer(id="settings-content"):
                yield Label("Agent Command:")
                yield Input(
                    value=self.config.get("agent_command", "claude"),
                    placeholder="claude",
                    id="agent_command",
                )

                yield Label("Editor Command:")
                yield Input(
                    value=self.config.get("editor", "code"),
                    placeholder="code",
                    id="editor",
                )

                yield Label("Navigation Mode:")
                yield Select(
                    [
                        ("Default (arrows/tab)", "default"),
                        ("Vim (j/k)", "vim"),
                        ("Emacs (C-n/C-p)", "emacs"),
                    ],
                    value=self.config.get("navigation_mode", "default"),
                    id="nav_mode",
                )

                yield Label("Preview Lines:")
                yield Input(
                    value=str(self.config.get("preview_lines", 1)),
                    placeholder="1",
                    id="preview_lines",
                )

                yield Label("Preview Skip Lines:")
                yield Input(
                    value=str(self.config.get("preview_skip_lines", 0)),
                    placeholder="0",
                    id="preview_skip_lines",
                )

                yield Label("Confirm Kill:")
                yield Select(
                    [("Yes", True), ("No", False)],
                    value=self.config.get("confirm_kill", True),
                    id="confirm_kill",
                )

            yield Static(
                "[dim]Save changes or press ESC to cancel[/dim]",
                id="settings-help"
            )

            with Horizontal(id="settings-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", variant="default", id="cancel")

    def action_cancel(self):
        """Cancel without saving"""
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses"""
        if event.button.id == "save":
            # Save settings
            agent_cmd = self.query_one("#agent_command", Input).value
            editor = self.query_one("#editor", Input).value
            nav_mode = self.query_one("#nav_mode", Select).value
            preview_lines = self.query_one("#preview_lines", Input).value
            preview_skip = self.query_one("#preview_skip_lines", Input).value
            confirm_kill = self.query_one("#confirm_kill", Select).value

            self.config.set("agent_command", agent_cmd)
            self.config.set("editor", editor)
            self.config.set("navigation_mode", nav_mode)
            self.config.set("preview_lines", int(preview_lines) if preview_lines.isdigit() else 1)
            self.config.set("preview_skip_lines", int(preview_skip) if preview_skip.isdigit() else 0)
            self.config.set("confirm_kill", confirm_kill)

            self.app.pop_screen()
        elif event.button.id == "cancel":
            self.action_cancel()


class ConfirmDialog(ModalScreen):
    """Confirmation dialog"""

    def __init__(self, message: str, on_confirm=None):
        super().__init__()
        self.message = message
        self.on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        """Create the dialog layout"""
        with Container(id="confirm-dialog"):
            yield Static(self.message)
            with Horizontal():
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", variant="default", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press"""
        if event.button.id == "yes" and self.on_confirm:
            self.on_confirm()
        self.app.pop_screen()


class AddTodoDialog(ModalScreen):
    """Dialog for adding a new todo item"""

    def __init__(self):
        super().__init__()
        self.todo_text = None

    def compose(self) -> ComposeResult:
        """Create the dialog layout"""
        with Container(id="add-todo-dialog"):
            yield Static("[bold]Add Todo[/bold]", id="add-todo-title")
            yield Input(placeholder="Enter todo text...", id="todo-input")
            with Horizontal(id="add-todo-buttons"):
                yield Button("Add", variant="primary", id="add")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self):
        """Focus the input when mounted"""
        self.query_one("#todo-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press"""
        if event.button.id == "add":
            input_widget = self.query_one("#todo-input", Input)
            self.todo_text = input_widget.value.strip()
            if self.todo_text:
                self.dismiss(self.todo_text)
            else:
                self.app.pop_screen()
        elif event.button.id == "cancel":
            self.app.pop_screen()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle enter key in input"""
        self.todo_text = event.value.strip()
        if self.todo_text:
            self.dismiss(self.todo_text)
        else:
            self.app.pop_screen()


class TodoItem(Static):
    """A single todo item"""

    def __init__(self, text: str, done: bool = False):
        super().__init__()
        self.text = text
        self.done = done

    def on_mount(self):
        """Update the display when mounted"""
        self.update_display()

    def update_display(self):
        """Update the todo item display"""
        checkbox = "[X]" if self.done else "[ ]"
        if self.done:
            self.update(f"[dim]{checkbox} {self.text}[/dim]")
        else:
            self.update(f"{checkbox} {self.text}")

    def toggle(self):
        """Toggle the done state"""
        self.done = not self.done
        self.update_display()


class TodoListScreen(ModalScreen):
    """Screen for managing todos for a worktree"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
        Binding("space", "toggle_selected", "Toggle", show=True),
        Binding("a", "add_todo", "Add", show=True),
        Binding("d", "delete_selected", "Delete", show=True),
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("down,j", "cursor_down", "Down", show=False),
    ]

    def __init__(self, worktree_key: str, todos: List[Dict]):
        super().__init__()
        self.worktree_key = worktree_key
        self.todos = todos
        self.todo_items: List[TodoItem] = []
        self.selected_index = 0

    def compose(self) -> ComposeResult:
        """Create the todo list layout"""
        with Container(id="todo-dialog"):
            yield Static(f"[bold]Todos for {self.worktree_key}[/bold]", id="todo-title")
            with ScrollableContainer(id="todo-list"):
                for todo in self.todos:
                    item = TodoItem(todo["text"], todo.get("done", False))
                    self.todo_items.append(item)
                    yield item

            yield Static(
                "[dim]Space: Toggle  •  A: Add  •  D: Delete  •  ↑/↓: Navigate  •  ESC/Q: Close[/dim]",
                id="todo-help"
            )

            with Horizontal(id="todo-buttons"):
                yield Button("Done", variant="primary", id="done")

    def on_mount(self):
        """Initialize when mounted"""
        self.update_selection()

    def update_selection(self):
        """Update which item is selected"""
        for i, item in enumerate(self.todo_items):
            if i == self.selected_index:
                item.add_class("selected")
            else:
                item.remove_class("selected")

    def action_cursor_up(self):
        """Move selection up"""
        if self.todo_items:
            self.selected_index = (self.selected_index - 1) % len(self.todo_items)
            self.update_selection()

    def action_cursor_down(self):
        """Move selection down"""
        if self.todo_items:
            self.selected_index = (self.selected_index + 1) % len(self.todo_items)
            self.update_selection()

    def action_toggle_selected(self):
        """Toggle the selected todo item"""
        if self.todo_items and self.selected_index < len(self.todo_items):
            self.todo_items[self.selected_index].toggle()

    def action_add_todo(self):
        """Add a new todo item"""
        def handle_todo(text: str):
            """Handle the returned todo text"""
            if text:
                todo = {"text": text, "done": False}
                self.todos.append(todo)
                item = TodoItem(text, False)
                self.todo_items.append(item)
                container = self.query_one("#todo-list")
                container.mount(item)
                self.selected_index = len(self.todo_items) - 1
                self.update_selection()

        self.app.push_screen(AddTodoDialog(), handle_todo)

    def action_delete_selected(self):
        """Delete the selected todo item"""
        if self.todo_items and self.selected_index < len(self.todo_items):
            item = self.todo_items[self.selected_index]
            # Find and remove from todos list
            for i, todo in enumerate(self.todos):
                if todo["text"] == item.text:
                    self.todos.pop(i)
                    break

            # Remove from UI
            item.remove()
            self.todo_items.pop(self.selected_index)

            # Adjust selection
            if self.todo_items:
                self.selected_index = min(self.selected_index, len(self.todo_items) - 1)
                self.update_selection()

    def action_dismiss(self):
        """Save and dismiss the screen"""
        # Update todos with current done states
        for i, item in enumerate(self.todo_items):
            if i < len(self.todos):
                self.todos[i]["done"] = item.done

        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button press"""
        if event.button.id == "done":
            self.action_dismiss()


class ContxtTUI(App):
    """Main Textual TUI application"""

    CSS = """
    Screen {
        background: $surface;
    }

    #worktree-list {
        height: 100%;
        border: solid $primary;
        padding: 1;
    }

    WorktreeItem {
        height: auto;
        padding: 0 1;
    }

    #help-dialog, #settings-dialog, #confirm-dialog, #todo-dialog, #add-todo-dialog {
        background: $surface;
        border: thick $primary;
        padding: 2;
        width: 60%;
        height: auto;
        max-height: 80%;
    }

    #add-todo-title {
        text-align: center;
        margin-bottom: 1;
    }

    #add-todo-buttons {
        margin-top: 2;
        align: center middle;
        height: auto;
    }

    #todo-list {
        height: auto;
        max-height: 30;
        border: solid $primary-darken-2;
        padding: 1;
        margin: 1 0;
    }

    TodoItem {
        height: auto;
        padding: 0 1;
    }

    TodoItem.selected {
        background: $primary-darken-2;
    }

    #todo-title {
        text-align: center;
        margin-bottom: 1;
    }

    #todo-help {
        text-align: center;
        margin-top: 1;
        margin-bottom: 1;
    }

    #todo-buttons {
        margin-top: 1;
        align: center middle;
        height: auto;
    }

    #help-content {
        padding: 1;
    }

    #settings-title {
        text-align: center;
        margin-bottom: 1;
    }

    #settings-content {
        height: auto;
        max-height: 20;
        min-height: 10;
    }

    #settings-help {
        text-align: center;
        padding-top: 1;
        height: auto;
    }

    Label {
        margin-top: 1;
    }

    Input, Select {
        margin-bottom: 1;
    }

    #settings-buttons {
        align: center middle;
        height: 3;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("s", "settings", "Settings"),
        Binding("t", "manage_todos", "Todos"),
        Binding("r", "restart_server", "Restart Server"),
        Binding("c", "create_worktree", "Create"),
        Binding("d", "delete_worktree", "Delete"),
        Binding("m", "merge_worktree", "Merge"),
        Binding("e", "edit_worktree", "Edit"),
        Binding("x", "kill_session", "Kill Session"),
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("down,j", "cursor_down", "Down", show=False),
        Binding("ctrl+p", "cursor_up", "Up (Emacs)", show=False),
        Binding("ctrl+n", "cursor_down", "Down (Emacs)", show=False),
        Binding("tab", "cursor_down", "Next", show=False),
        Binding("shift+tab", "cursor_up", "Previous", show=False),
        Binding("shift+up,K", "move_item_up", "Move Item Up", show=False),
        Binding("shift+down,J", "move_item_down", "Move Item Down", show=False),
        Binding("enter", "attach_session", "Attach", show=False),
    ]

    def __init__(self, config: ContxtConfig):
        super().__init__()
        self.config = config
        self.client: Optional[ServerClient] = None
        self.worktree_items: List[WorktreeItem] = []
        self.selected_index = 0
        self.update_timer = None
        self.todos_file = Path.home() / "worktrees" / ".contxt_todos.json"
        self.todos: Dict[str, List[Dict]] = self.load_todos()
        self.order_file = Path.home() / "worktrees" / ".contxt_order.json"
        self.custom_order: List[str] = self.load_order()
        # Track screen content hashes for status detection
        self.screen_hashes: Dict[str, str] = {}
        self.screen_stable_since: Dict[str, float] = {}

    def compose(self) -> ComposeResult:
        """Create the layout"""
        yield Header()
        with VerticalScroll(id="worktree-list"):
            pass  # Will be populated dynamically
        yield Footer()

    def load_todos(self) -> Dict[str, List[Dict]]:
        """Load todos from file"""
        if self.todos_file.exists():
            try:
                with open(self.todos_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {}
        return {}

    def save_todos(self):
        """Save todos to file"""
        self.todos_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.todos_file, 'w') as f:
            json.dump(self.todos, f, indent=2)

    def load_order(self) -> List[str]:
        """Load custom order from file"""
        if self.order_file.exists():
            try:
                with open(self.order_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return []
        return []

    def save_order(self):
        """Save custom order to file"""
        self.order_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.order_file, 'w') as f:
            json.dump(self.custom_order, f, indent=2)

    def on_mount(self):
        """Initialize when the app is mounted"""
        # Connect to server
        socket_path = self.config.get("socket_path")
        self.client = ServerClient(socket_path)

        if not self.client.connect():
            self.notify("Failed to connect to server", severity="error")
            self.exit()
            return

        # Load and display worktrees
        self.refresh_worktrees()

        # Start update timer
        self.set_interval(1.0, self.update_worktree_status)

    def refresh_worktrees(self):
        """Refresh the list of worktrees and their agent sessions"""
        if not self.client:
            return

        response = self.client.send_command({"cmd": "list_worktrees"})
        if not response or response.get("status") != "ok":
            return

        worktrees = response.get("worktrees", [])

        selected_key = None
        if self.worktree_items and 0 <= self.selected_index < len(self.worktree_items):
            selected_key = self.worktree_items[self.selected_index].worktree["key"]

        container = self.query_one("#worktree-list")
        container.remove_children()
        new_items: List[WorktreeItem] = []

        # Get all screen sessions if using multiplexer
        use_multiplexer = self.config.get("use_multiplexer", True)
        active_sessions = {}
        if use_multiplexer and shutil.which("screen"):
            result = subprocess.run(
                ["screen", "-ls"],
                capture_output=True,
                text=True
            )
            # Parse screen session names from output
            # Format: "12345.contxt-repo-branch-agent"
            for line in result.stdout.splitlines():
                if "contxt-" in line:
                    # Extract session name (after the PID and dot)
                    parts = line.strip().split()
                    if parts:
                        session_full = parts[0]  # e.g., "12345.contxt-repo-branch-agent"
                        if '.' in session_full:
                            session_name = session_full.split('.', 1)[1]
                            active_sessions[session_name] = True

        # Create items for each worktree and its sessions
        preview_lines = self.config.get("preview_lines", 1)
        for wt in worktrees:
            git_name = wt["git_name"]
            wt_name = wt["name"]
            base_session_prefix = f"contxt-{git_name}-{wt_name}-"

            # Find all active sessions for this worktree
            worktree_sessions = []
            for session_name in active_sessions.keys():
                if session_name.startswith(base_session_prefix):
                    # Extract agent name from session name
                    agent_name = session_name[len(base_session_prefix):]
                    worktree_sessions.append(agent_name)

            # If no active sessions, create one entry for the default agent
            if not worktree_sessions:
                agent_cmd = self.config.get("agent_command", "bash")
                agent_short = agent_cmd.split()[0].split('/')[-1]
                worktree_sessions = [agent_short]

            # Create an item for each agent session
            for agent_name in worktree_sessions:
                wt_copy = wt.copy()
                wt_copy["agent"] = agent_name
                wt_copy["key"] = f"{wt['key']}-{agent_name}"
                item = WorktreeItem(wt_copy, preview_lines, on_select=self.handle_worktree_click)
                new_items.append(item)

        if not new_items:
            self.worktree_items = []
            self.custom_order = []
            self.selected_index = 0
            return

        self._update_custom_order(new_items)
        self.worktree_items = self._order_items_by_custom(new_items)

        if selected_key:
            for idx, item in enumerate(self.worktree_items):
                if item.worktree["key"] == selected_key:
                    self.selected_index = idx
                    break
            else:
                self.selected_index = min(self.selected_index, len(self.worktree_items) - 1)
        else:
            self.selected_index = min(self.selected_index, len(self.worktree_items) - 1)

        for idx, item in enumerate(self.worktree_items):
            item.set_selected(idx == self.selected_index)
            container.mount(item)

    def update_worktree_status(self):
        """Update the status and output of all worktrees"""
        if not self.client or not self.worktree_items:
            return

        preview_lines = self.config.get("preview_lines", 1)
        skip_lines = self.config.get("preview_skip_lines", 0)
        use_multiplexer = self.config.get("use_multiplexer", True)
        with open("log.txt", "w") as log:
            for item in self.worktree_items:
                key = item.worktree["key"]
                worktree = item.worktree
                agent_name = worktree.get("agent", "")

                # Determine status based on screen session if using multiplexer
                if use_multiplexer and shutil.which("screen"):
                    session_name = f"contxt-{worktree['git_name']}-{worktree['name']}-{agent_name}"
                    log.write(f"checking {session_name}\n")
                    log.flush()
                    result = subprocess.run(
                        ["screen", "-ls", session_name],
                        capture_output=True,
                        text=True
                    )

                    if session_name in result.stdout:
                        # Screen session exists - use content hashing to detect activity
                        output = []
                        current_hash = None

                        try:
                            tmp_path = None
                            with tempfile.NamedTemporaryFile(
                                mode='w+', delete=False, dir="/tmp"
                            ) as tmp:
                                tmp_path = Path(tmp.name)

                            log.write("saving screen\n")
                            log.flush()
                            # Capture screen content to temp file
                            result = subprocess.run(
                                [
                                    "screen",
                                    "-S",
                                    session_name,
                                    "-p",
                                    "0",
                                    "-X",
                                    "hardcopy",
                                    "-h",
                                    str(tmp_path),
                                ],
                                capture_output=True,
                                timeout=1
                            )
                            log.write("screen read\n")
                            log.flush()

                            if result.returncode != 0:
                                raise RuntimeError(result.stderr)

                            # Read the content and calculate hash
                            try:
                                log.write("hashing screen\n")
                                with open(tmp_path, 'rb') as f:
                                    raw_content = f.read()

                                # Hash the raw bytes so encoding issues don't break status updates
                                current_hash = hashlib.md5(raw_content).hexdigest()
                                log.write(f"current hash {current_hash}\n")

                                # Decode with replacement for preview text
                                text_content = raw_content.decode('utf-8', errors='replace')

                                lines = text_content.splitlines()
                                output = self._prepare_preview_lines(lines, preview_lines, skip_lines)
                                log.write("screen hashed\n")
                            except FileNotFoundError:
                                log.write("temp file missing\n")
                                pass
                            finally:
                                # Clean up temp file
                                if tmp_path and tmp_path.exists():
                                    try:
                                        tmp_path.unlink()
                                    except Exception:
                                        pass
                            log.flush()
                        except Exception as exc:
                            log.write(f"screen capture failed: {exc}\n")
                            log.flush()
                            output = []

                        # Determine status based on content hash changes
                        # Use session name as key for tracking hashes
                        hash_key = session_name
                        if current_hash:
                            prev_hash = self.screen_hashes.get(hash_key)
                            current_time = time.time()
                            log.write(f"prev hash {prev_hash}\n")
                            if prev_hash != current_hash:
                                # Content changed - agent is working
                                status = "working"
                                self.screen_hashes[hash_key] = current_hash
                                self.screen_stable_since[hash_key] = current_time
                            else:
                                # Content hasn't changed
                                stable_since = self.screen_stable_since.get(hash_key, current_time)
                                stable_duration = current_time - stable_since

                                # If stable for more than 3 seconds, consider idle
                                if stable_duration > 3:
                                    status = "idle"
                                else:
                                    # Recently stopped changing, still consider working
                                    status = "working"
                        else:
                            status = "idle"
                        log.write(f"status {status}\n")
                        log.flush()
                    else:
                        status = "stopped"
                        output = []
                        log.write("session not found\n")
                        log.flush()
                else:
                    # Use server PTY status for non-multiplexer sessions
                    requested_lines = preview_lines + skip_lines if preview_lines > 0 else skip_lines
                    response = self.client.send_command({"cmd": "get_output", "key": key, "lines": max(requested_lines, 1)})

                    if response and response.get("status") == "ok":
                        raw_output = response.get("output", [])
                        output = self._prepare_preview_lines(raw_output, preview_lines, skip_lines)
                        status = response.get("status_color", "stopped")
                    else:
                        output = []
                        status = "stopped"
                    log.write(f"PTY status {status}\n")
                    log.flush()

                item.update_display(output, status)

    def _prepare_preview_lines(self, lines: List[str], preview_lines: int, skip_lines: int) -> List[str]:
        """Trim trailing blanks, skip the most recent prompt lines, and take the preview window."""
        if preview_lines <= 0:
            return []

        normalized = [line.rstrip() for line in lines]
        while normalized and not normalized[-1].strip():
            normalized.pop()

        if skip_lines > 0:
            if len(normalized) <= skip_lines:
                return []
            normalized = normalized[:-skip_lines]

        if len(normalized) > preview_lines:
            normalized = normalized[-preview_lines:]

        return normalized

    def _update_custom_order(self, items: List[WorktreeItem]):
        """Reconcile the tracked order with the current set of worktree items."""
        new_keys = [item.worktree["key"] for item in items]
        ordered_keys = [key for key in self.custom_order if key in new_keys]
        for key in new_keys:
            if key not in ordered_keys:
                ordered_keys.append(key)
        self.custom_order = ordered_keys

    def _order_items_by_custom(self, items: List[WorktreeItem]) -> List[WorktreeItem]:
        """Return items sorted by the tracked custom order."""
        order_index = {key: idx for idx, key in enumerate(self.custom_order)}
        return sorted(items, key=lambda item: order_index.get(item.worktree["key"], len(order_index)))

    def _remount_worktree_items(self):
        """Re-render the worktree list to reflect any ordering changes."""
        container = self.query_one("#worktree-list")
        # Get current children in container
        current_children = list(container.children)

        # Reorder children in the DOM to match worktree_items order
        for idx, item in enumerate(self.worktree_items):
            current_pos = current_children.index(item)
            if current_pos != idx:
                # Move this child to the correct position
                if idx == 0:
                    container.move_child(item, before=current_children[0])
                else:
                    container.move_child(item, after=self.worktree_items[idx - 1])
                # Update our tracking of current positions
                current_children.remove(item)
                current_children.insert(idx, item)
            item.set_selected(idx == self.selected_index)

    def update_selection(self):
        """Update which item is selected"""
        for i, item in enumerate(self.worktree_items):
            item.set_selected(i == self.selected_index)

    def _move_selected_item(self, delta: int):
        """Reorder the selected worktree by the specified offset."""
        if not self.worktree_items:
            return
        new_index = self.selected_index + delta
        if new_index < 0 or new_index >= len(self.worktree_items):
            return

        self.worktree_items[self.selected_index], self.worktree_items[new_index] = (
            self.worktree_items[new_index],
            self.worktree_items[self.selected_index],
        )
        self.selected_index = new_index
        self.custom_order = [item.worktree["key"] for item in self.worktree_items]
        self.save_order()
        self._remount_worktree_items()
        self.update_selection()

    def action_move_item_up(self):
        """Move the selected worktree up one slot"""
        self._move_selected_item(-1)

    def action_move_item_down(self):
        """Move the selected worktree down one slot"""
        self._move_selected_item(1)

    def handle_worktree_click(self, item: WorktreeItem):
        """Update selection when a worktree item is clicked"""
        if item in self.worktree_items:
            new_index = self.worktree_items.index(item)
            if new_index != self.selected_index:
                self.selected_index = new_index
                self.update_selection()

    def action_cursor_up(self):
        """Move selection up"""
        if self.worktree_items:
            self.selected_index = (self.selected_index - 1) % len(self.worktree_items)
            self.update_selection()

    def action_cursor_down(self):
        """Move selection down"""
        if self.worktree_items:
            self.selected_index = (self.selected_index + 1) % len(self.worktree_items)
            self.update_selection()

    def action_help(self):
        """Show help screen"""
        self.push_screen(HelpScreen())

    def action_settings(self):
        """Show settings screen"""
        self.push_screen(SettingsScreen(self.config))

    def action_manage_todos(self):
        """Manage todos for the selected worktree"""
        if not self.worktree_items or self.selected_index >= len(self.worktree_items):
            return

        item = self.worktree_items[self.selected_index]
        worktree = item.worktree
        key = worktree["key"]

        # Get or create todos for this worktree
        if key not in self.todos:
            self.todos[key] = []

        # Show todo list screen
        self.push_screen(TodoListScreen(key, self.todos[key]))
        # Save todos when returning from the screen
        self.save_todos()

    def _agent_requires_shell_on_exit(self, agent_cmd: str) -> bool:
        """Determine if the agent should leave an interactive shell running after exit."""
        try:
            parsed = shlex.split(agent_cmd)
        except ValueError:
            parsed = agent_cmd.split()
        if not parsed:
            return False
        base_name = Path(parsed[0]).name
        return base_name in {"claude", "codex"}

    def _build_agent_launch_command(self, path: str, agent_cmd: str) -> str:
        """Build the shell command used to launch the agent session."""
        quoted_path = shlex.quote(path)
        command = f"cd {quoted_path} && {agent_cmd}"
        if self._agent_requires_shell_on_exit(agent_cmd):
            command = f"{command}; exec bash"
        return command

    def action_attach_session(self):
        """Attach to the selected worktree's terminal"""
        if not self.worktree_items or self.selected_index >= len(self.worktree_items):
            return

        item = self.worktree_items[self.selected_index]
        worktree = item.worktree
        key = worktree["key"]

        # Start session if not already started
        if worktree.get("status") == "stopped":
            response = self.client.send_command(
                {"cmd": "start_session", "key": key, "path": worktree["path"]}
            )

        # Get worktree details
        path = worktree["path"]
        agent_cmd = self.config.get("agent_command", "bash")
        launch_cmd = self._build_agent_launch_command(path, agent_cmd)
        # Use the agent name stored in the worktree dict (from screen session or default)
        agent_name = worktree.get("agent", agent_cmd.split()[0].split('/')[-1])
        session_name = f"contxt-{worktree['git_name']}-{worktree['name']}-{agent_name}"

        # Suspend the TUI and attach to terminal
        # Use App's suspend() method via super() to avoid recursion
        original_cwd = os.getcwd()
        try:
            with App.suspend(self):
                os.chdir(path)

                # Check if we're using screen
                use_multiplexer = self.config.get("use_multiplexer", True)

                if use_multiplexer and shutil.which("screen"):
                    # Check if screen session already exists
                    result = subprocess.run(
                        ["screen", "-ls", session_name],
                        capture_output=True,
                        text=True
                    )

                    session_exists = session_name in result.stdout

                    if not session_exists:
                        # Session doesn't exist, create it with agent command
                        subprocess.run(
                            ["screen", "-dmS", session_name, "bash", "-c", launch_cmd]
                        )
                        # Give it a moment to start
                        time.sleep(1.0)

                        # Verify session was created and is still running
                        verify_result = subprocess.run(
                            ["screen", "-ls", session_name],
                            capture_output=True,
                            text=True
                        )
                        if session_name not in verify_result.stdout:
                            print(f"\n[ERROR] Failed to create screen session '{session_name}'")
                            print(f"The agent command '{agent_cmd}' may not exist or failed to start.")
                            print("Please check your agent_command in settings (press 's').\n")
                            input("Press Enter to continue...")
                            return

                        # Inject todos if any exist and are not done
                        if key in self.todos:
                            pending_todos = [t for t in self.todos[key] if not t.get("done", False)]
                            if pending_todos:
                                # Build todo text with each todo on a separate line
                                for todo in pending_todos:
                                    todo_line = f"- TODO {todo['text']}"
                                    # Use screen's stuff command with literal carriage return
                                    # We need to use bash to properly send the enter key
                                    subprocess.run(
                                        ["bash", "-c", f"screen -S {session_name} -X stuff $'{todo_line}\\n'"],
                                        capture_output=True
                                    )
                                    time.sleep(0.1)  # Brief pause between lines
                                # Wait a moment before attaching
                                time.sleep(0.5)

                    # Attach to the session
                    print(f"\nAttaching to screen session '{session_name}'")
                    print("Press Ctrl-A then D to detach without killing the agent\n")
                    subprocess.call(["screen", "-r", session_name])
                else:
                    # Fallback: run directly (Ctrl-C will kill it)
                    print(f"\nLaunching {agent_cmd} in {path}")
                    print("Note: Ctrl-C will only exit the agent, the shell will remain open.\n")
                    subprocess.call(["bash", "-c", launch_cmd])
        finally:
            os.chdir(original_cwd)

        # Refresh when returning
        self.refresh_worktrees()

    def action_kill_session(self):
        """Kill the selected worktree's terminal session"""
        if not self.worktree_items or self.selected_index >= len(self.worktree_items):
            return

        item = self.worktree_items[self.selected_index]
        worktree = item.worktree
        agent_cmd = self.config.get("agent_command", "bash")
        # Use the agent name stored in the worktree dict (from screen session or default)
        agent_name = worktree.get("agent", agent_cmd.split()[0].split('/')[-1])
        session_name = f"contxt-{worktree['git_name']}-{worktree['name']}-{agent_name}"

        # Check if screen session exists
        has_screen_session = False
        if shutil.which("screen"):
            result = subprocess.run(
                ["screen", "-ls", session_name],
                capture_output=True,
                text=True
            )
            has_screen_session = session_name in result.stdout

        if worktree.get("status") == "stopped" and not has_screen_session:
            self.notify("No active session to kill", severity="warning")
            return

        def do_kill():
            # Kill screen session if it exists
            if has_screen_session:
                # Use screen -S sessionname -X quit to properly terminate the session
                result = subprocess.run(
                    ["screen", "-S", session_name, "-X", "quit"],
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    self.notify(f"Warning: Failed to kill screen session", severity="warning")

            # Kill PTY session
            response = self.client.send_command({"cmd": "stop_session", "key": worktree["key"]})

            self.notify(f"Killed session for {worktree['name']}")
            self.refresh_worktrees()

        if self.config.get("confirm_kill", True):
            self.push_screen(ConfirmDialog(f"Kill session for {worktree['name']}?", on_confirm=do_kill))
        else:
            do_kill()

    def action_create_worktree(self):
        """Create a new worktree"""
        with App.suspend(self):
            name = input("Worktree name: ")
            if name:
                subprocess.call(["python3", str(Path(__file__).parent / "contxt"), "create", name])
        self.refresh_worktrees()

    def action_delete_worktree(self):
        """Delete the selected worktree"""
        if not self.worktree_items or self.selected_index >= len(self.worktree_items):
            return

        item = self.worktree_items[self.selected_index]
        worktree = item.worktree

        def do_delete():
            with App.suspend(self):
                subprocess.call(
                    [
                        "python3",
                        str(Path(__file__).parent / "contxt"),
                        "delete",
                        worktree["name"],
                        "-p",
                        worktree["git_name"],
                    ]
                )
            self.refresh_worktrees()

        self.push_screen(
            ConfirmDialog(f"Delete worktree {worktree['name']}?", on_confirm=do_delete)
        )

    def action_merge_worktree(self):
        """Merge the selected worktree"""
        if not self.worktree_items or self.selected_index >= len(self.worktree_items):
            return

        item = self.worktree_items[self.selected_index]
        worktree = item.worktree

        with App.suspend(self):
            subprocess.call(
                [
                    "python3",
                    str(Path(__file__).parent / "contxt"),
                    "merge",
                    worktree["name"],
                    "-p",
                    worktree["git_name"],
                ]
            )

    def action_edit_worktree(self):
        """Edit the selected worktree in configured editor"""
        if not self.worktree_items or self.selected_index >= len(self.worktree_items):
            return

        item = self.worktree_items[self.selected_index]
        worktree = item.worktree
        editor = self.config.get("editor", "code")

        try:
            subprocess.Popen([editor, worktree["path"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.notify(f"Opened {worktree['name']} in {editor}")
        except FileNotFoundError:
            self.notify(f"Error: '{editor}' command not found", severity="error")

    def action_restart_server(self):
        """Restart the contxt server"""

        def do_restart():
            # Send shutdown command
            if self.client:
                self.client.send_command({"cmd": "shutdown"})
                self.client.close()

            self.notify("Restarting server...")

            # Wait a moment
            time.sleep(1)

            # Start new server
            start_server(self.config)

            # Reconnect
            time.sleep(1)
            self.client = ServerClient(self.config.get("socket_path"))
            if self.client.connect():
                self.notify("Server restarted")
                self.refresh_worktrees()
            else:
                self.notify("Failed to reconnect", severity="error")

        self.push_screen(ConfirmDialog("Restart the server?", on_confirm=do_restart))


def start_server(config: ContxtConfig) -> bool:
    """Start the contxt server if not already running"""
    socket_path = Path(config.get("socket_path"))
    pid_file = Path.home() / ".contxt" / "server.pid"

    # Check if server is already running
    if socket_path.exists():
        try:
            test_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            test_socket.connect(str(socket_path))
            test_socket.close()
            return True  # Server is running
        except Exception:
            # Socket exists but not connectable, remove it
            socket_path.unlink()

    # Start server in background
    server_script = Path(__file__).parent / "contxt_server.py"
    subprocess.Popen(
        ["python3", str(server_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for server to start
    for _ in range(10):
        time.sleep(0.5)
        if socket_path.exists():
            try:
                test_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                test_socket.connect(str(socket_path))
                test_socket.close()
                return True
            except Exception:
                pass

    return False


def main():
    """Run the TUI"""
    config = ContxtConfig()

    # Ensure server is running
    if not start_server(config):
        print("Failed to start contxt server", file=sys.stderr)
        sys.exit(1)

    # Run the TUI
    app = ContxtTUI(config)
    try:
        app.run()
    finally:
        os.system("cls" if os.name == "nt" else "clear")


if __name__ == "__main__":
    main()
