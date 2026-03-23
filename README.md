# Claude Terminal Sessions Dashboard

A web dashboard for browsing, searching, and resuming your Claude Code sessions — with an embedded terminal.

![Dashboard](https://img.shields.io/badge/localhost-3456-C45A36) ![Terminal](https://img.shields.io/badge/terminal-3457-1a1a1a)

## Quick Start

Paste this into Claude Code:

```
Clone https://github.com/M1w234/claude-terminal-sessions-dashboard and run the setup.sh script to install it. It's a web dashboard for browsing my Claude Code sessions. If I'm missing any dependencies like python3, fastapi, or uvicorn, help me install them first. After setup, open http://localhost:3456 in my browser.
```

That's it. Claude Code handles the rest.

## What You Get

- **Session Browser** — Search and browse all your Claude Code sessions in one place
- **Conversation Viewer** — Read through past sessions with formatted messages and tool calls
- **Embedded Terminal** — Open a terminal right in the dashboard, resume sessions with one click
- **Timeline View** — See your sessions organized by day
- **Analytics** — Session counts, activity charts, projects and branches breakdown
- **Workspace Filters** — Filter sessions by workspace
- **AI Summaries** — Generate quick summaries of any session (requires Anthropic API key)

## Requirements

- macOS
- Python 3.10+
- Claude Code installed (`~/.claude/` directory must exist)

## Manual Install

If you'd rather do it yourself:

```bash
git clone https://github.com/M1w234/claude-terminal-sessions-dashboard.git
cd claude-terminal-sessions-dashboard
pip install fastapi uvicorn anthropic
./setup.sh
```

Then open **http://localhost:3456**.

## Architecture

Two lightweight servers run on localhost:

| Service | Port | Purpose |
|---------|------|---------|
| Dashboard | 3456 | Web UI + REST API for session data |
| Terminal | 3457 | WebSocket server for the embedded terminal |

Both are managed by macOS LaunchAgents and start automatically on login.

## Keyboard Shortcuts

- **Cmd+K** — Focus search
- **Cmd+`** — Toggle terminal
- **Esc** — Clear search
