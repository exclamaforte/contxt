#!/usr/bin/env python3
"""
Server component for contxt - manages persistent terminal sessions
"""

import os
import sys
import pty
import json
import time
import signal
import socket
import select
import termios
import threading
import subprocess
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from collections import deque

from contxt_config import ContxtConfig


class TerminalSession:
    """Manages a single PTY session for a worktree"""

    def __init__(self, worktree_path: str, shell: str = "/bin/bash"):
        self.worktree_path = worktree_path
        self.shell = shell
        self.master_fd: Optional[int] = None
        self.pid: Optional[int] = None
        self.output_buffer = deque(maxlen=1000)  # Keep last 1000 lines
        self.last_activity = time.time()
        self.is_active = False
        self.attached_clients = set()

    def start(self):
        """Start the PTY session"""
        if self.master_fd is not None:
            return  # Already started

        # Fork a PTY
        self.pid, self.master_fd = pty.fork()

        if self.pid == 0:
            # Child process - set up environment and exec shell
            os.chdir(self.worktree_path)
            os.environ["PS1"] = "$ "  # Simple prompt
            os.execvp(self.shell, [self.shell])
        else:
            # Parent process - configure terminal
            try:
                # Set non-blocking mode
                import fcntl

                flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
                fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

                # Get terminal attributes and set up
                attrs = termios.tcgetattr(self.master_fd)
                attrs[3] = attrs[3] & ~termios.ECHO  # Disable echo
                termios.tcsetattr(self.master_fd, termios.TCSANOW, attrs)
            except Exception:
                pass  # Best effort

    def read_output(self) -> Optional[bytes]:
        """Read available output from the PTY"""
        if self.master_fd is None:
            return None

        try:
            data = os.read(self.master_fd, 4096)
            if data:
                self.last_activity = time.time()
                # Add to buffer
                lines = data.decode("utf-8", errors="replace").split("\n")
                for line in lines:
                    if line:
                        self.output_buffer.append(line)
            return data
        except OSError:
            return None

    def write_input(self, data: bytes):
        """Write input to the PTY"""
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def get_recent_output(self, lines: int = 10) -> List[str]:
        """Get the most recent N lines of output"""
        return list(self.output_buffer)[-lines:]

    def is_process_running(self) -> bool:
        """Check if there are any child processes running in the session"""
        if self.pid is None:
            return False

        try:
            # Check if there are any child processes
            result = subprocess.run(
                ["pgrep", "-P", str(self.pid)],
                capture_output=True,
                text=True,
                timeout=1,
            )
            return result.returncode == 0
        except Exception:
            return False

    def get_status(self) -> str:
        """Get the current status: working, idle, or stopped"""
        if self.master_fd is None:
            return "stopped"

        # Check if there's been recent activity (within last 2 seconds)
        if time.time() - self.last_activity < 2:
            return "working"

        # Check if there are child processes running
        if self.is_process_running():
            return "working"

        return "idle"

    def stop(self):
        """Stop the PTY session"""
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass

        self.pid = None
        self.master_fd = None


class ContxtServer:
    """Server that manages terminal sessions for all worktrees"""

    def __init__(self, config: ContxtConfig):
        self.config = config
        self.socket_path = Path(config.get("socket_path"))
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket
        if self.socket_path.exists():
            self.socket_path.unlink()

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(str(self.socket_path))
        self.server_socket.listen(5)
        self.server_socket.setblocking(False)

        self.sessions: Dict[str, TerminalSession] = {}
        self.clients = []
        self.running = True

        # Load metadata to know about worktrees
        self.worktrees_base = Path.home() / "worktrees"
        self.metadata_file = self.worktrees_base / ".contxt_metadata.json"

    def load_worktrees(self) -> Dict:
        """Load worktree metadata"""
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, "r") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {}
        return {}

    def get_session(self, worktree_key: str) -> Optional[TerminalSession]:
        """Get or create a session for a worktree"""
        return self.sessions.get(worktree_key)

    def create_session(self, worktree_key: str, worktree_path: str) -> TerminalSession:
        """Create a new terminal session"""
        session = TerminalSession(worktree_path)
        session.start()
        self.sessions[worktree_key] = session
        return session

    def handle_client_message(self, client_socket, message: Dict):
        """Handle a message from a client"""
        cmd = message.get("cmd")
        response = {"status": "ok"}

        if cmd == "list_worktrees":
            metadata = self.load_worktrees()
            worktrees = []
            for git_name, wts in metadata.items():
                for wt_name, wt_info in wts.items():
                    key = f"{git_name}/{wt_name}"
                    session = self.get_session(key)
                    status = session.get_status() if session else "stopped"
                    worktrees.append(
                        {
                            "key": key,
                            "git_name": git_name,
                            "name": wt_name,
                            "path": wt_info["worktree_path"],
                            "status": status,
                        }
                    )
            response["worktrees"] = worktrees

        elif cmd == "get_output":
            key = message.get("key")
            lines = message.get("lines", 1)
            session = self.get_session(key)
            if session:
                response["output"] = session.get_recent_output(lines)
                response["status_color"] = session.get_status()
            else:
                response["output"] = []
                response["status_color"] = "stopped"

        elif cmd == "start_session":
            key = message.get("key")
            path = message.get("path")
            if key not in self.sessions:
                self.create_session(key, path)
                response["message"] = "Session started"
            else:
                response["message"] = "Session already exists"

        elif cmd == "stop_session":
            key = message.get("key")
            session = self.get_session(key)
            if session:
                session.stop()
                del self.sessions[key]
                response["message"] = "Session stopped"
            else:
                response["status"] = "error"
                response["message"] = "Session not found"

        elif cmd == "write_input":
            key = message.get("key")
            data = message.get("data", "")
            session = self.get_session(key)
            if session:
                session.write_input(data.encode())
                response["message"] = "Input written"
            else:
                response["status"] = "error"
                response["message"] = "Session not found"

        elif cmd == "shutdown":
            self.running = False
            response["message"] = "Server shutting down"

        # Send response
        try:
            response_data = json.dumps(response).encode() + b"\n"
            client_socket.sendall(response_data)
        except Exception:
            pass

    def run(self):
        """Main server loop"""
        print(f"Contxt server running on {self.socket_path}")

        while self.running:
            # Build list of sockets to monitor
            readable = [self.server_socket] + self.clients
            # Add PTY master fds
            for session in self.sessions.values():
                if session.master_fd is not None:
                    readable.append(session.master_fd)

            try:
                ready, _, _ = select.select(readable, [], [], 0.1)
            except (ValueError, OSError):
                # Clean up closed sockets
                self.clients = [c for c in self.clients if c.fileno() != -1]
                continue

            for sock in ready:
                if sock == self.server_socket:
                    # New client connection
                    try:
                        client, _ = self.server_socket.accept()
                        client.setblocking(False)
                        self.clients.append(client)
                    except Exception:
                        pass

                elif isinstance(sock, int):
                    # PTY output - read it
                    for session in self.sessions.values():
                        if session.master_fd == sock:
                            session.read_output()
                            break

                else:
                    # Client message
                    try:
                        data = sock.recv(4096)
                        if not data:
                            # Client disconnected
                            self.clients.remove(sock)
                            sock.close()
                        else:
                            # Parse and handle message
                            messages = data.decode().strip().split("\n")
                            for msg in messages:
                                if msg:
                                    try:
                                        message = json.loads(msg)
                                        self.handle_client_message(sock, message)
                                    except json.JSONDecodeError:
                                        pass
                    except Exception:
                        # Error with client, remove it
                        if sock in self.clients:
                            self.clients.remove(sock)
                        try:
                            sock.close()
                        except Exception:
                            pass

        # Cleanup
        for session in self.sessions.values():
            session.stop()
        self.server_socket.close()
        if self.socket_path.exists():
            self.socket_path.unlink()


def main():
    """Start the contxt server"""
    config = ContxtConfig()
    server = ContxtServer(config)

    # Write PID file
    pid_file = Path.home() / ".contxt" / "server.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    try:
        server.run()
    finally:
        if pid_file.exists():
            pid_file.unlink()


if __name__ == "__main__":
    main()
