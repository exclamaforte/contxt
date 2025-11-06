# contxt - Git Worktree Manager for AI Agents

A CLI tool to manage git worktrees with Python virtual environments, designed for AI agent workflows.

## Features

- Create isolated worktrees with dedicated branches and Python venvs
- Open worktrees in VS Code with a single command
- List and manage multiple projects and worktrees
- Merge worktree branches back to main
- All worktrees stored in `~/worktrees/<project>/<name>`

## Installation

1. Make the script executable (already done):
   ```bash
   chmod +x /Users/gabrielferns/contxt/contxt
   ```

2. Add to your PATH by adding this line to your `~/.bashrc`, `~/.zshrc`, or equivalent:
   ```bash
   export PATH="$PATH:/Users/gabrielferns/contxt"
   ```

3. Reload your shell configuration:
   ```bash
   source ~/.zshrc  # or ~/.bashrc
   ```

4. Verify installation:
   ```bash
   contxt --help
   ```

## Usage

### Create a worktree

Creates a new git worktree with a branch and Python venv:

```bash
# From within a git repository
contxt create <name>

# Specify project name explicitly
contxt create <name> -p <project>
```

This creates:
- Worktree at `~/worktrees/<gitname>/<name>/`
- Branch named `worktree/<name>`
- Python venv at `~/worktrees/<gitname>/<name>/.venv`

Example:
```bash
cd ~/projects/myapp
contxt create feature-auth
# Creates ~/worktrees/myapp/feature-auth/
```

### Open worktree in VS Code

```bash
contxt edit <name>
contxt edit <name> -p <project>
```

### List worktrees

```bash
# List all worktrees
contxt list

# List worktrees for specific project
contxt list -p <project>
```

Output format:
```
myapp
  feature-auth
  bugfix-login
otherproject
  experiment-1
```

### Merge worktree branch

Merges the worktree's branch into the main branch of the original repository:

```bash
contxt merge <name>
contxt merge <name> -p <project>
```

### Delete a worktree

```bash
contxt delete <name>
contxt delete <name> -p <project>
```

This removes the worktree, deletes the branch, and cleans up metadata.

## How It Works

- **Metadata Storage**: Worktree information is stored in `~/worktrees/.contxt_metadata.json`
- **Original Repo Tracking**: Each worktree remembers its original repository location for merge operations
- **Branch Naming**: Branches are automatically named `worktree/<name>` to avoid conflicts
- **Project Detection**: Automatically detects the current git repository name unless `-p` is specified

## Example Workflow

```bash
# Start in your main repo
cd ~/projects/myapp

# Create a worktree for a new feature
contxt create ai-integration

# Open it in VS Code
contxt edit ai-integration

# Activate the venv in your terminal
source ~/worktrees/myapp/ai-integration/.venv/bin/activate

# Work on your feature...

# When done, merge back to main
contxt merge ai-integration

# Clean up the worktree
contxt delete ai-integration
```

## Requirements

- Python 3.6+
- Git
- VS Code with `code` CLI command (for `contxt edit`)

## Notes

- The `-p` flag allows you to work with worktrees from different projects without being in the original repository
- Metadata is persisted across sessions, so you can manage worktrees from anywhere
- Virtual environments are created using `python3 -m venv`
