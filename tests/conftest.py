import json
import socket
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from contxt_server import ContxtServer
from contxt_tui import ServerClient


class DummyConfig:
    """Minimal config object that satisfies ContxtServer expectations in tests."""

    def __init__(self, values: Dict[str, str]):
        self._values = dict(values)

    def get(self, key: str, default=None):
        return self._values.get(key, default)


@pytest.fixture
def fake_home(tmp_path, monkeypatch) -> Path:
    """Provide an isolated HOME directory for filesystem-heavy components."""
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))  # type: ignore[arg-type]
    return home


@pytest.fixture
def worktree_layout(fake_home: Path) -> Dict[str, str]:
    """Create metadata and todo files that mimic a couple of worktrees."""
    worktrees_dir = fake_home / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    project = "demo"
    primary = "alpha"
    secondary = "beta"

    primary_path = worktrees_dir / project / primary
    secondary_path = worktrees_dir / project / secondary
    primary_path.mkdir(parents=True, exist_ok=True)
    secondary_path.mkdir(parents=True, exist_ok=True)

    metadata = {
        project: {
            primary: {"worktree_path": str(primary_path)},
            secondary: {"worktree_path": str(secondary_path)},
        }
    }
    (worktrees_dir / ".contxt_metadata.json").write_text(json.dumps(metadata, indent=2))

    todos = {
        f"{project}/{primary}": [
            {"id": "todo-alpha-1", "text": "keep server alive", "done": False},
            {"id": "todo-alpha-2", "text": "existing note", "done": True},
        ],
        f"{project}/{secondary}": [],
    }
    (worktrees_dir / ".contxt_todos.json").write_text(json.dumps(todos, indent=2))

    return {
        "project": project,
        "primary_key": f"{project}/{primary}",
        "primary_path": str(primary_path),
        "secondary_key": f"{project}/{secondary}",
        "secondary_path": str(secondary_path),
    }


@pytest.fixture
def fake_terminal_session(monkeypatch):
    """Replace TerminalSession with a deterministic fake so tests stay hermetic."""

    class FakeTerminalSession:
        instances = []

        def __init__(self, worktree_path: str, shell: str = "/bin/bash"):
            self.worktree_path = worktree_path
            self.shell = shell
            self.master_fd = None
            self.pid = None
            self.started = False
            self.status = "stopped"
            self.command_log = []
            self.output_buffer = deque(maxlen=1000)
            self.last_activity = time.time()
            FakeTerminalSession.instances.append(self)

        def start(self):
            self.started = True
            self.status = "idle"

        def read_output(self):
            return None

        def write_input(self, data: bytes):
            text = data.decode("utf-8", errors="replace")
            self.command_log.append(text)
            for line in text.splitlines():
                if line:
                    self.output_buffer.append(line)
            self.status = "working"
            self.last_activity = time.time()

        def get_recent_output(self, lines: int = 10):
            return list(self.output_buffer)[-lines:]

        def get_status(self):
            if not self.started:
                return "stopped"
            if time.time() - self.last_activity > 0.5:
                return "idle"
            return self.status

        def stop(self):
            self.status = "stopped"

    monkeypatch.setattr("contxt_server.TerminalSession", FakeTerminalSession)
    return FakeTerminalSession


@pytest.fixture
def running_server(fake_home, worktree_layout, fake_terminal_session):
    """Spin up the Contxt server in a background thread for integration tests."""
    socket_path = Path("/tmp") / f"contxt-test-{uuid.uuid4().hex}.sock"
    config = DummyConfig({"socket_path": str(socket_path)})
    server = ContxtServer(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait until the server socket starts accepting connections
    for _ in range(200):
        if socket_path.exists():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(str(socket_path))
                sock.close()
                break
            except OSError:
                time.sleep(0.01)
                continue
        else:
            time.sleep(0.01)
    else:
        raise RuntimeError("Server did not start in time")

    yield server, str(socket_path), worktree_layout

    shutdown_client = ServerClient(str(socket_path))
    if shutdown_client.connect():
        shutdown_client.send_command({"cmd": "shutdown"})
        shutdown_client.close()

    thread.join(timeout=5)
    if socket_path.exists():
        socket_path.unlink()


@pytest.fixture
def server_client_factory(running_server):
    """Provide a callable that returns connected ServerClient instances."""
    _, socket_path, _ = running_server
    clients = []

    def factory():
        client = ServerClient(socket_path)
        assert client.connect(), "Failed to connect to test server"
        clients.append(client)
        return client

    yield factory

    for client in clients:
        client.close()
