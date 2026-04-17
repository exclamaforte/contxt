#!/usr/bin/env python3
"""
Session backends for long-lived agent terminals.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Optional


class SessionError(RuntimeError):
    """Raised when a session backend command fails."""


async def _run_command(*args: str, cwd: Optional[str] = None) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise SessionError(message or f"command failed: {' '.join(args)}")
    return stdout.decode("utf-8", errors="replace")


@dataclass
class LocalSession:
    process: asyncio.subprocess.Process
    cwd: str
    output: Deque[str] = field(default_factory=lambda: deque(maxlen=500))
    booted: bool = False
    reader_task: Optional[asyncio.Task] = None


class SessionBackend:
    """Common interface for multiplexed or local agent sessions."""

    backend_name = "base"

    async def ensure_session(self, session_name: str, cwd: str, boot_command: str) -> None:
        raise NotImplementedError

    async def send_input(self, session_name: str, text: str, press_enter: bool = True) -> None:
        raise NotImplementedError

    async def capture_output(self, session_name: str, lines: int = 80) -> str:
        raise NotImplementedError

    async def close_session(self, session_name: str) -> None:
        raise NotImplementedError

    def attach_command(self, session_name: str) -> Optional[list[str]]:
        return None

    async def session_exists(self, session_name: str) -> bool:
        return False


class LocalPtyBackend(SessionBackend):
    """Fallback backend that keeps a local shell subprocess alive."""

    backend_name = "subprocess"

    def __init__(self, shell: str = "/bin/bash"):
        self.shell = shell
        self.sessions: Dict[str, LocalSession] = {}

    async def _reader(self, session: LocalSession) -> None:
        assert session.process.stdout is not None
        while True:
            line = await session.process.stdout.readline()
            if not line:
                break
            session.output.append(line.decode("utf-8", errors="replace").rstrip())

    async def ensure_session(self, session_name: str, cwd: str, boot_command: str) -> None:
        existing = self.sessions.get(session_name)
        if existing and existing.process.returncode is None:
            return

        process = await asyncio.create_subprocess_exec(
            self.shell,
            "-lc",
            "exec bash",
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        session = LocalSession(process=process, cwd=cwd)
        session.reader_task = asyncio.create_task(self._reader(session))
        self.sessions[session_name] = session
        if boot_command:
            await asyncio.sleep(0.2)
            await self.send_input(session_name, boot_command)
            session.booted = True

    async def send_input(self, session_name: str, text: str, press_enter: bool = True) -> None:
        session = self.sessions[session_name]
        if session.process.stdin is None:
            raise SessionError(f"session {session_name} has no stdin")
        payload = text
        if press_enter:
            payload += "\n"
        session.process.stdin.write(payload.encode("utf-8"))
        await session.process.stdin.drain()

    async def capture_output(self, session_name: str, lines: int = 80) -> str:
        session = self.sessions[session_name]
        recent = list(session.output)[-lines:]
        return "\n".join(recent)

    async def close_session(self, session_name: str) -> None:
        session = self.sessions.pop(session_name, None)
        if session is None:
            return
        if session.process.returncode is None:
            session.process.terminate()
            await session.process.wait()
        if session.reader_task:
            session.reader_task.cancel()

    async def session_exists(self, session_name: str) -> bool:
        session = self.sessions.get(session_name)
        return bool(session and session.process.returncode is None)


class TmuxBackend(SessionBackend):
    """Manage one command-focused tmux session per PR."""

    backend_name = "tmux"

    async def _exists(self, session_name: str) -> bool:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            "has-session",
            "-t",
            session_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait() == 0

    async def ensure_session(self, session_name: str, cwd: str, boot_command: str) -> None:
        if not await self._exists(session_name):
            await _run_command("tmux", "new-session", "-d", "-s", session_name, "-c", cwd)
            if boot_command:
                await asyncio.sleep(0.2)
                await self.send_input(session_name, boot_command)

    async def send_input(self, session_name: str, text: str, press_enter: bool = True) -> None:
        await _run_command("tmux", "send-keys", "-t", session_name, "-l", "--", text)
        if press_enter:
            await _run_command("tmux", "send-keys", "-t", session_name, "Enter")

    async def capture_output(self, session_name: str, lines: int = 80) -> str:
        return await _run_command(
            "tmux",
            "capture-pane",
            "-p",
            "-S",
            f"-{max(lines, 10)}",
            "-t",
            session_name,
        )

    async def close_session(self, session_name: str) -> None:
        if await self._exists(session_name):
            await _run_command("tmux", "kill-session", "-t", session_name)

    def attach_command(self, session_name: str) -> Optional[list[str]]:
        return ["tmux", "attach-session", "-t", session_name]

    async def session_exists(self, session_name: str) -> bool:
        return await self._exists(session_name)


class ZellijBackend(SessionBackend):
    """Manage detached zellij sessions with a single focused pane."""

    backend_name = "zellij"

    async def _exists(self, session_name: str) -> bool:
        output = await _run_command("zellij", "list-sessions", "--short")
        return session_name in {line.strip() for line in output.splitlines() if line.strip()}

    async def ensure_session(self, session_name: str, cwd: str, boot_command: str) -> None:
        if not await self._exists(session_name):
            await _run_command("zellij", "attach", "--create-background", session_name)
            await asyncio.sleep(0.4)
            command = boot_command or os.environ.get("SHELL", "/bin/bash")
            await _run_command(
                "zellij",
                "--session",
                session_name,
                "run",
                "--cwd",
                cwd,
                "--",
                "bash",
                "-lc",
                f"{command}; exec bash",
            )

    async def send_input(self, session_name: str, text: str, press_enter: bool = True) -> None:
        raise SessionError(
            "Detached zellij sessions do not support reliable prompt injection; use tmux or attach interactively."
        )

    async def capture_output(self, session_name: str, lines: int = 80) -> str:
        with tempfile.NamedTemporaryFile(prefix="contxt-zellij-", delete=False) as handle:
            capture_path = Path(handle.name)
        try:
            await _run_command(
                "zellij",
                "--session",
                session_name,
                "action",
                "dump-screen",
                "--full",
                str(capture_path),
            )
            output = capture_path.read_text(errors="replace")
            return "\n".join(output.splitlines()[-lines:])
        finally:
            capture_path.unlink(missing_ok=True)

    async def close_session(self, session_name: str) -> None:
        if await self._exists(session_name):
            await _run_command("zellij", "kill-session", session_name)

    def attach_command(self, session_name: str) -> Optional[list[str]]:
        return ["zellij", "attach", session_name]

    async def session_exists(self, session_name: str) -> bool:
        return await self._exists(session_name)


class SessionManager:
    """Backend selection plus a tiny naming/bootstrapping layer."""

    def __init__(self, backend_name: str, boot_command: str, prefix: str):
        self.boot_command = boot_command
        self.prefix = prefix
        self.backend = self._select_backend(backend_name)

    def _select_backend(self, backend_name: str) -> SessionBackend:
        if backend_name == "auto":
            if shutil.which("tmux"):
                return TmuxBackend()
            if shutil.which("zellij"):
                return ZellijBackend()
            return LocalPtyBackend()
        if backend_name == "zellij":
            return ZellijBackend()
        if backend_name == "tmux":
            return TmuxBackend()
        return LocalPtyBackend()

    def build_session_name(self, raw_key: str) -> str:
        safe = []
        for char in raw_key:
            if char.isalnum():
                safe.append(char)
            else:
                safe.append("-")
        compact = "".join(safe).strip("-")
        return f"{self.prefix}-{compact}"[:80]

    async def ensure_agent_session(self, raw_key: str, cwd: str) -> str:
        session_name = self.build_session_name(raw_key)
        await self.backend.ensure_session(session_name, cwd, self.boot_command)
        return session_name

    async def send_prompt(self, session_name: str, prompt: str) -> None:
        await self.backend.send_input(session_name, prompt)

    async def capture_output(self, session_name: str, lines: int = 80) -> str:
        return await self.backend.capture_output(session_name, lines)

    async def close_session(self, session_name: str) -> None:
        await self.backend.close_session(session_name)

    def attach_command(self, session_name: str) -> Optional[list[str]]:
        return self.backend.attach_command(session_name)

    async def session_exists(self, session_name: str) -> bool:
        return await self.backend.session_exists(session_name)
