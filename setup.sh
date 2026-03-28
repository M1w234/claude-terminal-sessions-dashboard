#!/bin/bash
#
# Claude Sessions Dashboard — Installer
#
# This script sets up the Claude Sessions Dashboard, a web-based UI for
# browsing, searching, and resuming your Claude Code sessions.
#
# What it does:
#   1. Copies dashboard files to ~/.claude/claude-sessions-manager/
#   2. Installs Python dependencies (fastapi, uvicorn, anthropic)
#   3. Creates LaunchAgents so both servers start on login
#   4. Starts the servers immediately
#
# Requirements:
#   - macOS
#   - Python 3.10+
#   - Claude Code installed
#
# After install, open http://localhost:3456 in your browser.
#

set -e

DASHBOARD_DIR="$HOME/.claude/claude-sessions-manager"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PYTHON=$(command -v python3)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Claude Sessions Dashboard Installer ==="
echo ""

# Check prerequisites
if [ ! -d "$HOME/.claude" ]; then
    echo "Error: ~/.claude directory not found. Install Claude Code first."
    exit 1
fi

if [ -z "$PYTHON" ]; then
    echo "Error: python3 not found. Install Python 3.10+ first."
    exit 1
fi

echo "Using Python: $PYTHON"
echo "Dashboard dir: $DASHBOARD_DIR"
echo ""

# Install Python dependencies
echo "Installing Python dependencies..."
$PYTHON -m pip install --quiet fastapi uvicorn anthropic 2>/dev/null || \
$PYTHON -m pip install --quiet --user fastapi uvicorn anthropic

# Create dashboard directory
mkdir -p "$DASHBOARD_DIR"

# Copy files
echo "Copying dashboard files..."
cp "$SCRIPT_DIR/server.py" "$DASHBOARD_DIR/"
cp "$SCRIPT_DIR/terminal_server.py" "$DASHBOARD_DIR/"
cp "$SCRIPT_DIR/index.html" "$DASHBOARD_DIR/"

# Create .gitignore
cat > "$DASHBOARD_DIR/.gitignore" << 'GITIGNORE'
__pycache__/
*.pyc
*.log
GITIGNORE

# Create LaunchAgents
echo "Setting up LaunchAgents..."
mkdir -p "$LAUNCH_AGENTS_DIR"

cat > "$LAUNCH_AGENTS_DIR/com.claude.dashboard.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>server:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>3456</string>
        <string>--log-level</string>
        <string>warning</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DASHBOARD_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>$DASHBOARD_DIR/error.log</string>
    <key>StandardOutPath</key>
    <string>$DASHBOARD_DIR/out.log</string>
</dict>
</plist>
PLIST

cat > "$LAUNCH_AGENTS_DIR/com.claude.terminal.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.terminal</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>terminal_server:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>3457</string>
        <string>--log-level</string>
        <string>warning</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DASHBOARD_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>$DASHBOARD_DIR/terminal-error.log</string>
    <key>StandardOutPath</key>
    <string>$DASHBOARD_DIR/terminal-out.log</string>
</dict>
</plist>
PLIST

# Stop existing services if running
launchctl unload "$LAUNCH_AGENTS_DIR/com.claude.dashboard.plist" 2>/dev/null || true
launchctl unload "$LAUNCH_AGENTS_DIR/com.claude.terminal.plist" 2>/dev/null || true

# Kill any existing processes on those ports
lsof -ti :3456 | xargs kill -9 2>/dev/null || true
lsof -ti :3457 | xargs kill -9 2>/dev/null || true
sleep 1

# Load and start services
launchctl load "$LAUNCH_AGENTS_DIR/com.claude.dashboard.plist"
launchctl load "$LAUNCH_AGENTS_DIR/com.claude.terminal.plist"

echo ""
echo "=== Installation complete! ==="
echo ""
echo "Dashboard: http://localhost:3456"
echo "Terminal:  ws://localhost:3457 (used by dashboard)"
echo ""
echo "The servers will start automatically on login."
echo "Open http://localhost:3456 in your browser to get started."
