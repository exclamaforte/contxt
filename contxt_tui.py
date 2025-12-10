#!/usr/bin/env python3
"""
Textual TUI for contxt - Terminal interface for managing worktrees
"""

import os
import sys
import json
import socket
import asyncio
import subprocess
from pathlib import Path
from typing import Optional, Dict, List

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, Horizontal, ScrollableContainer
from textual.widgets import Header, Footer, Static, Label, Button, Input, Select, DataTable
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

    def __init__(self, worktree: Dict, preview_lines: int = 1):
        super().__init__()
        self.worktree = worktree
        self.preview_lines = preview_lines
        self.selected = False

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

        # Selection indicator
        selection = "▶ " if self.selected else "  "

        # Preview text
        preview = ""
        if output and self.preview_lines > 0:
            preview_text = "\n".join(output[-self.preview_lines :])
            if preview_text:
                preview = f"\n    {preview_text}"

        display = f"{selection}[{color}]{indicator}[/] {git_name}/{name}{preview}"
        self.update(display)

    def set_selected(self, selected: bool):
        """Set the selection state"""
        self.selected = selected
        self.update_display()


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
  c           - Create new worktree
  d           - Delete worktree
  m           - Merge worktree to main
  e           - Edit worktree in VS Code

[yellow]Other[/yellow]
  s           - Settings
  r           - Restart server
  ?           - Show this help
  q           - Quit

Press ESC or q to close this help.""",
                id="help-content",
            )

    def action_dismiss(self):
        """Dismiss the help screen"""
        self.app.pop_screen()


class SettingsScreen(ModalScreen):
    """Settings screen for configuration"""

    def __init__(self, config: ContxtConfig):
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        """Create the settings screen layout"""
        with Container(id="settings-dialog"):
            yield Static("[bold]Settings[/bold]", id="settings-title")

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

            yield Label("Confirm Kill:")
            yield Select(
                [("Yes", True), ("No", False)],
                value=self.config.get("confirm_kill", True),
                id="confirm_kill",
            )

            with Horizontal(id="settings-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", variant="default", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses"""
        if event.button.id == "save":
            # Save settings
            agent_cmd = self.query_one("#agent_command", Input).value
            editor = self.query_one("#editor", Input).value
            nav_mode = self.query_one("#nav_mode", Select).value
            preview_lines = self.query_one("#preview_lines", Input).value
            confirm_kill = self.query_one("#confirm_kill", Select).value

            self.config.set("agent_command", agent_cmd)
            self.config.set("editor", editor)
            self.config.set("navigation_mode", nav_mode)
            self.config.set("preview_lines", int(preview_lines) if preview_lines.isdigit() else 1)
            self.config.set("confirm_kill", confirm_kill)

            self.app.pop_screen()
        elif event.button.id == "cancel":
            self.app.pop_screen()


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

    #help-dialog, #settings-dialog, #confirm-dialog {
        background: $surface;
        border: thick $primary;
        padding: 2;
        width: 60%;
        height: auto;
        max-height: 80%;
    }

    #help-content {
        padding: 1;
    }

    #settings-title {
        text-align: center;
        margin-bottom: 1;
    }

    Label {
        margin-top: 1;
    }

    Input, Select {
        margin-bottom: 1;
    }

    #settings-buttons {
        margin-top: 2;
        align: center middle;
        height: auto;
    }

    Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("s", "settings", "Settings"),
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
        Binding("enter", "attach_session", "Attach", show=False),
    ]

    def __init__(self, config: ContxtConfig):
        super().__init__()
        self.config = config
        self.client: Optional[ServerClient] = None
        self.worktree_items: List[WorktreeItem] = []
        self.selected_index = 0
        self.update_timer = None

    def compose(self) -> ComposeResult:
        """Create the layout"""
        yield Header()
        with ScrollableContainer(id="worktree-list"):
            pass  # Will be populated dynamically
        yield Footer()

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
        """Refresh the list of worktrees"""
        if not self.client:
            return

        response = self.client.send_command({"cmd": "list_worktrees"})
        if not response or response.get("status") != "ok":
            return

        worktrees = response.get("worktrees", [])

        # Clear existing items
        container = self.query_one("#worktree-list")
        container.remove_children()
        self.worktree_items.clear()

        # Create new items
        preview_lines = self.config.get("preview_lines", 1)
        for i, wt in enumerate(worktrees):
            item = WorktreeItem(wt, preview_lines)
            item.set_selected(i == self.selected_index)
            self.worktree_items.append(item)
            container.mount(item)

        # Ensure selection is valid
        if self.worktree_items:
            self.selected_index = min(self.selected_index, len(self.worktree_items) - 1)
            self.update_selection()

    def update_worktree_status(self):
        """Update the status and output of all worktrees"""
        if not self.client or not self.worktree_items:
            return

        preview_lines = self.config.get("preview_lines", 1)
        for item in self.worktree_items:
            key = item.worktree["key"]
            response = self.client.send_command({"cmd": "get_output", "key": key, "lines": preview_lines})

            if response and response.get("status") == "ok":
                output = response.get("output", [])
                status = response.get("status_color", "stopped")
                item.update_display(output, status)

    def update_selection(self):
        """Update which item is selected"""
        for i, item in enumerate(self.worktree_items):
            item.set_selected(i == self.selected_index)

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

    def action_attach_session(self):
        """Attach to the selected worktree's terminal"""
        if not self.worktree_items or self.selected_index >= len(self.worktree_items):
            return

        item = self.worktree_items[self.selected_index]
        worktree = item.worktree

        # Start session if not already started
        if worktree.get("status") == "stopped":
            response = self.client.send_command(
                {"cmd": "start_session", "key": worktree["key"], "path": worktree["path"]}
            )

        # Get worktree details
        path = worktree["path"]
        agent_cmd = self.config.get("agent_command", "bash")

        # Suspend the TUI and attach to terminal
        # Use App's suspend() method via super() to avoid recursion
        with App.suspend(self):
            # Launch shell in the worktree directory
            os.chdir(path)
            subprocess.call([agent_cmd])

        # Refresh when returning
        self.refresh_worktrees()

    def action_kill_session(self):
        """Kill the selected worktree's terminal session"""
        if not self.worktree_items or self.selected_index >= len(self.worktree_items):
            return

        item = self.worktree_items[self.selected_index]
        worktree = item.worktree

        if worktree.get("status") == "stopped":
            self.notify("No active session to kill", severity="warning")
            return

        def do_kill():
            response = self.client.send_command({"cmd": "stop_session", "key": worktree["key"]})
            if response and response.get("status") == "ok":
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
            import time

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
    import time

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
    app.run()


if __name__ == "__main__":
    main()
