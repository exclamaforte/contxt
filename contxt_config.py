#!/usr/bin/env python3
"""
Configuration management for contxt
"""

import json
from pathlib import Path
from typing import Dict, Any


class ContxtConfig:
    """Manages contxt configuration settings"""

    DEFAULT_CONFIG = {
        "agent_command": "claude",
        "editor": "code",  # Editor command for opening worktrees
        "use_multiplexer": True,  # Use screen for detachable sessions
        "navigation_mode": "default",  # default, vim, emacs
        "confirm_kill": True,
        "preview_lines": 1,
        "server_host": "localhost",
        "server_port": 9876,
        "socket_path": str(Path.home() / ".contxt" / "server.sock"),
    }

    def __init__(self):
        self.config_dir = Path.home() / ".contxt"
        self.config_file = self.config_dir / "config.json"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from file or create default"""
        if self.config_file.exists():
            try:
                with open(self.config_file, "r") as f:
                    loaded = json.load(f)
                    # Merge with defaults to handle new config keys
                    config = self.DEFAULT_CONFIG.copy()
                    config.update(loaded)
                    return config
            except json.JSONDecodeError:
                return self.DEFAULT_CONFIG.copy()
        return self.DEFAULT_CONFIG.copy()

    def save(self):
        """Save configuration to file"""
        with open(self.config_file, "w") as f:
            json.dump(self.config, f, indent=2)

    def get(self, key: str, default=None):
        """Get a configuration value"""
        return self.config.get(key, default)

    def set(self, key: str, value: Any):
        """Set a configuration value"""
        self.config[key] = value
        self.save()

    def update(self, updates: Dict[str, Any]):
        """Update multiple configuration values"""
        self.config.update(updates)
        self.save()
