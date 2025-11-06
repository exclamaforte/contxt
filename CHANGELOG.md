# Changelog

## Recent Updates

### Fixed Issues

1. **Auto-open in VS Code on creation** - The `contxt create` command now automatically opens the new worktree in VS Code after creation, in addition to creating the worktree and venv.

2. **Smart worktree context detection** - All commands (`edit`, `delete`, `merge`) now intelligently detect when you're running from within a worktree directory and automatically use the correct project name. This fixes the error when running `contxt merge <name>` from within a worktree.

3. **Safe deletion with recovery** - The `contxt delete` command now moves worktrees to `/tmp/contxt_deleted/<gitname>_<name>_<timestamp>` instead of permanently deleting them. This allows recovery if needed, and the system will automatically clean up these directories on reboot.

### Technical Details

- Added `_find_worktree_info()` method to detect if current directory is inside a tracked worktree
- Updated `edit()`, `delete()`, and `merge()` methods to check current directory context
- Modified deletion to use `shutil.move()` with timestamped backup paths
- Enhanced `create()` to call VS Code after successful worktree setup
