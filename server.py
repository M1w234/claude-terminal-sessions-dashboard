#!/usr/bin/env python3
"""
Claude Sessions Dashboard — API Server

FastAPI backend that reads ~/.claude/ session data and serves a web dashboard.
Run with: uvicorn server:app --port 3456
"""

import json
import glob
import os
import re
import asyncio
import pty
import select
import signal
import struct
import fcntl
import termios
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import anthropic

app = FastAPI(title="Claude Sessions Dashboard")

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
NAMES_FILE = CLAUDE_DIR / "session-names.json"
WORKSPACES_FILE = CLAUDE_DIR / "session-workspaces.json"
CMUX_MAP_FILE = CLAUDE_DIR / "cmux-session-map.json"
SUMMARIES_FILE = CLAUDE_DIR / "session-summaries.json"

# ── Helpers ──────────────────────────────────────────────────────────────────


def load_auto_names():
    try:
        with open(NAMES_FILE) as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}


def load_workspace_map():
    """Load session -> workspace mappings from both sources."""
    ws_map = {}

    # From the workspace file (tab-title matching + screen scraping)
    try:
        with open(WORKSPACES_FILE) as f:
            ws_map.update(json.load(f))
    except (IOError, json.JSONDecodeError):
        pass

    # From the cmux panel map (SessionStart hook)
    try:
        with open(CMUX_MAP_FILE) as f:
            panel_map = json.load(f)
        for panel_id, info in panel_map.items():
            sid = info.get("session_id", "")
            ws = info.get("workspace_name", "").strip()
            if sid and ws:
                ws_map[sid] = ws
    except (IOError, json.JSONDecodeError):
        pass

    return ws_map


def load_index_entries():
    """Load all sessions from sessions-index.json files."""
    entries = {}
    for idx_file in glob.glob(str(PROJECTS_DIR / "*/sessions-index.json")):
        try:
            with open(idx_file) as f:
                data = json.load(f)
            for e in data.get("entries", []):
                entries[e["sessionId"]] = e
        except (IOError, json.JSONDecodeError):
            pass
    return entries


def find_all_sessions():
    """Find all sessions across all projects."""
    index_entries = load_index_entries()
    auto_names = load_auto_names()
    ws_map = load_workspace_map()
    sessions = []
    seen_ids = set()

    # Indexed sessions first (they have summaries)
    for sid, entry in index_entries.items():
        seen_ids.add(sid)
        auto_name = auto_names.get(sid, "")
        if auto_name:
            auto_name = auto_name.replace("-", " ").title()
        sessions.append({
            "id": sid,
            "name": clean_text(entry.get("summary") or auto_name or (entry.get("firstPrompt", "")[:80])),
            "firstPrompt": clean_text(entry.get("firstPrompt", "")),
            "messageCount": entry.get("messageCount", 0),
            "created": entry.get("created", ""),
            "modified": entry.get("modified", ""),
            "gitBranch": entry.get("gitBranch", ""),
            "projectPath": entry.get("projectPath", ""),
            "workspace": ws_map.get(sid, ""),
            "source": "indexed",
        })

    # Unindexed JSONL files
    for jsonl_file in glob.glob(str(PROJECTS_DIR / "*/*.jsonl")):
        sid = Path(jsonl_file).stem
        if sid in seen_ids:
            continue
        seen_ids.add(sid)

        first_prompt = ""
        created = ""
        modified = ""
        cwd = ""
        git_branch = ""
        msg_count = 0

        try:
            with open(jsonl_file) as f:
                for i, line in enumerate(f):
                    if i > 300:
                        break
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if entry.get("type") == "user" and not first_prompt:
                        msg = entry.get("message", {})
                        if isinstance(msg, dict):
                            c = msg.get("content", "")
                            if isinstance(c, str) and c:
                                first_prompt = c[:200]
                            elif isinstance(c, list):
                                for item in c:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        first_prompt = item.get("text", "")[:200]
                                        break

                    if not created and entry.get("timestamp"):
                        created = entry["timestamp"]
                    if entry.get("timestamp"):
                        modified = entry["timestamp"]
                    if entry.get("cwd"):
                        cwd = entry["cwd"]
                    if entry.get("gitBranch"):
                        git_branch = entry["gitBranch"]
                    if entry.get("type") in ("user", "assistant"):
                        msg_count += 1
        except IOError:
            continue

        if not first_prompt:
            continue

        auto_name = auto_names.get(sid, "")
        if auto_name:
            auto_name = auto_name.replace("-", " ").title()

        sessions.append({
            "id": sid,
            "name": clean_text(auto_name or first_prompt[:80]),
            "firstPrompt": clean_text(first_prompt),
            "messageCount": msg_count,
            "created": created,
            "modified": modified,
            "gitBranch": git_branch,
            "projectPath": cwd,
            "workspace": ws_map.get(sid, ""),
            "source": "jsonl",
        })

    # Sort by modified (most recent first)
    def sort_key(s):
        mod = s.get("modified") or s.get("created") or ""
        try:
            return datetime.fromisoformat(mod.replace("Z", "+00:00"))
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    sessions.sort(key=sort_key, reverse=True)
    return sessions


def clean_text(text):
    """Extract meaningful human content from a message.

    Handles slash commands (/plan, /skill, etc.) where real content is
    inside <command-args>, and strips system boilerplate tags.
    """
    if not text:
        return ""

    # If message contains <command-args> with actual content, use that
    args_match = re.search(r'<command-args>(.*?)(?:</command-args>|$)', text, re.DOTALL)
    if args_match:
        extracted = args_match.group(1).strip()
        extracted = re.sub(r'<[^>]+>', '', extracted).strip()
        if len(extracted) > 10:
            return extracted
        # Empty command-args = pure command invocation, skip
        if '<command-name>' in text:
            return ""

    # Strip ALL tags
    result = re.sub(r'<[^>]+>', '', text).strip()

    # Remove leading slash commands
    result = re.sub(r'^/\w+\s*', '', result).strip()

    # Skip boilerplate
    if len(result) < 10:
        return ""
    lower = result.lower()
    if lower.startswith('caveat:') or lower == 'enabled plan mode':
        return ""

    return result


def extract_text_content(msg):
    """Extract text from a message content field."""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif item.get("type") == "tool_use":
                        tool_name = item.get("name", "unknown")
                        tool_input = item.get("input", {})
                        # Summarize tool use
                        if tool_name == "Bash":
                            cmd = tool_input.get("command", "")[:120]
                            parts.append(f"[Tool: Bash] `{cmd}`")
                        elif tool_name == "Read":
                            parts.append(f"[Tool: Read] {tool_input.get('file_path', '')}")
                        elif tool_name == "Edit":
                            parts.append(f"[Tool: Edit] {tool_input.get('file_path', '')}")
                        elif tool_name == "Write":
                            parts.append(f"[Tool: Write] {tool_input.get('file_path', '')}")
                        elif tool_name == "Grep":
                            parts.append(f"[Tool: Grep] pattern=`{tool_input.get('pattern', '')}`")
                        elif tool_name == "Glob":
                            parts.append(f"[Tool: Glob] {tool_input.get('pattern', '')}")
                        elif tool_name == "Agent":
                            parts.append(f"[Tool: Agent] {tool_input.get('description', '')}")
                        elif tool_name == "WebSearch":
                            parts.append(f"[Tool: WebSearch] {tool_input.get('query', '')}")
                        else:
                            parts.append(f"[Tool: {tool_name}]")
                    elif item.get("type") == "tool_result":
                        # Skip tool results in the display (they're verbose)
                        pass
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
    return ""


def read_session_conversation(session_id: str, limit: int = 200):
    """Read full conversation from a session JSONL file."""
    messages = []

    for jsonl_file in glob.glob(str(PROJECTS_DIR / f"*/{session_id}.jsonl")):
        try:
            with open(jsonl_file) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    entry_type = entry.get("type")
                    if entry_type == "user":
                        text = extract_text_content(entry.get("message", {}))
                        text = clean_text(text) if text else ""
                        if text:
                            messages.append({
                                "role": "user",
                                "content": text,
                                "timestamp": entry.get("timestamp", ""),
                            })
                    elif entry_type == "assistant":
                        msg = entry.get("message", {})
                        text = extract_text_content(msg)
                        if text:
                            # Deduplicate: assistant messages can appear multiple times
                            # (streaming updates). Keep only the last one with same requestId.
                            req_id = entry.get("requestId", "")
                            if req_id and messages and messages[-1].get("_reqId") == req_id:
                                messages[-1]["content"] = text
                                messages[-1]["timestamp"] = entry.get("timestamp", "")
                            else:
                                messages.append({
                                    "role": "assistant",
                                    "content": text,
                                    "timestamp": entry.get("timestamp", ""),
                                    "_reqId": req_id,
                                })

                    if len(messages) >= limit:
                        break
        except IOError:
            pass
        break

    # Clean up internal fields
    for m in messages:
        m.pop("_reqId", None)

    return messages


# ── API Routes ───────────────────────────────────────────────────────────────


@app.get("/api/sessions")
async def list_sessions(
    search: Optional[str] = Query(None),
    project: Optional[str] = Query(None),
    workspace: Optional[str] = Query(None),
    limit: int = Query(100),
    offset: int = Query(0),
):
    loop = asyncio.get_event_loop()
    sessions = await loop.run_in_executor(None, find_all_sessions)

    if search:
        q = search.lower()
        sessions = [
            s for s in sessions
            if q in s["name"].lower()
            or q in s["firstPrompt"].lower()
            or q in s.get("projectPath", "").lower()
            or q in s.get("workspace", "").lower()
        ]

    if project:
        sessions = [s for s in sessions if project in s.get("projectPath", "")]

    if workspace:
        sessions = [s for s in sessions if s.get("workspace", "") == workspace]

    total = len(sessions)
    sessions = sessions[offset: offset + limit]

    return {"sessions": sessions, "total": total}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    loop = asyncio.get_event_loop()
    sessions = await loop.run_in_executor(None, find_all_sessions)
    session = next((s for s in sessions if s["id"] == session_id), None)
    if not session:
        return {"error": "Session not found"}
    return session


@app.get("/api/workspaces")
async def list_workspaces():
    """List all known workspaces with session counts."""
    loop = asyncio.get_event_loop()
    sessions = await loop.run_in_executor(None, find_all_sessions)
    workspaces = {}
    for s in sessions:
        ws = s.get("workspace", "")
        if ws:
            if ws not in workspaces:
                workspaces[ws] = {"name": ws, "sessionCount": 0}
            workspaces[ws]["sessionCount"] += 1
    return {"workspaces": sorted(workspaces.values(), key=lambda w: -w["sessionCount"])}


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, limit: int = Query(200)):
    loop = asyncio.get_event_loop()
    messages = await loop.run_in_executor(None, read_session_conversation, session_id, limit)
    return {"messages": messages, "count": len(messages)}


@app.get("/api/stats")
async def get_stats():
    loop = asyncio.get_event_loop()
    sessions = await loop.run_in_executor(None, find_all_sessions)

    # Basic stats
    total_sessions = len(sessions)
    total_messages = sum(s.get("messageCount", 0) for s in sessions)

    # Sessions by project
    projects = {}
    for s in sessions:
        p = s.get("projectPath", "unknown")
        home = str(Path.home())
        if p.startswith(home):
            p = "~" + p[len(home):]
        projects[p] = projects.get(p, 0) + 1

    # Sessions by day (last 30 days)
    daily = {}
    for s in sessions:
        created = s.get("created") or s.get("modified", "")
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                day = dt.strftime("%Y-%m-%d")
                daily[day] = daily.get(day, 0) + 1
            except Exception:
                pass

    # Sort daily by date
    daily_sorted = sorted(daily.items())[-30:]

    # Sessions by branch
    branches = {}
    for s in sessions:
        b = s.get("gitBranch", "") or "none"
        branches[b] = branches.get(b, 0) + 1

    return {
        "totalSessions": total_sessions,
        "totalMessages": total_messages,
        "sessionsByProject": dict(sorted(projects.items(), key=lambda x: -x[1])),
        "sessionsByDay": daily_sorted,
        "sessionsByBranch": dict(sorted(branches.items(), key=lambda x: -x[1])[:10]),
    }


@app.get("/api/timeline")
async def get_timeline():
    """Return sessions grouped by day for timeline view."""
    loop = asyncio.get_event_loop()
    sessions = await loop.run_in_executor(None, find_all_sessions)
    days = {}

    for s in sessions:
        mod = s.get("modified") or s.get("created", "")
        if mod:
            try:
                dt = datetime.fromisoformat(mod.replace("Z", "+00:00"))
                day = dt.strftime("%Y-%m-%d")
                if day not in days:
                    days[day] = []
                days[day].append({
                    "id": s["id"],
                    "name": s["name"],
                    "messageCount": s.get("messageCount", 0),
                    "projectPath": s.get("projectPath", ""),
                    "created": s.get("created", ""),
                    "modified": mod,
                    "gitBranch": s.get("gitBranch", ""),
                })
            except Exception:
                pass

    # Sort days descending, sessions within each day by time
    timeline = []
    for day in sorted(days.keys(), reverse=True):
        timeline.append({
            "date": day,
            "sessions": days[day],
        })

    return {"timeline": timeline}


@app.get("/api/sessions/{session_id}/launch-dir")
def get_launch_dir(session_id: str):
    """Get the original launch directory for a session (derived from its storage path)."""
    for jsonl_file in glob.glob(str(PROJECTS_DIR / f"*/{session_id}.jsonl")):
        # The parent dir name is the encoded project path
        project_dir_name = Path(jsonl_file).parent.name  # e.g., "-Users-michaelwong"
        # Convert back to real path: replace leading dash, then remaining dashes with /
        launch_dir = "/" + project_dir_name[1:].replace("-", "/")
        return {"launchDir": launch_dir}
    return {"error": "Session not found"}


# ── Session Summary ──────────────────────────────────────────────────────────


def load_summaries():
    try:
        with open(SUMMARIES_FILE) as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}


def save_summary(session_id: str, summary: str):
    summaries = load_summaries()
    summaries[session_id] = {
        "summary": summary,
        "generated": datetime.now(timezone.utc).isoformat(),
    }
    with open(SUMMARIES_FILE, "w") as f:
        json.dump(summaries, f, indent=2)


@app.get("/api/sessions/{session_id}/summary")
def get_session_summary(session_id: str, refresh: bool = Query(False)):
    """Get or generate an LLM summary of a session."""
    # Check cache first
    if not refresh:
        summaries = load_summaries()
        cached = summaries.get(session_id)
        if cached:
            return {"summary": cached["summary"], "cached": True, "generated": cached["generated"]}

    # Build a condensed transcript for the LLM
    messages = read_session_conversation(session_id, limit=300)
    if not messages:
        return {"error": "No messages found for this session"}

    # Condense: keep user messages in full, truncate claude responses
    condensed = []
    for m in messages:
        role = m["role"]
        content = m["content"]
        if role == "user":
            condensed.append(f"User: {content[:500]}")
        else:
            # Truncate long assistant messages but keep enough for context
            if len(content) > 300:
                content = content[:300] + "..."
            condensed.append(f"Claude: {content}")

    transcript = "\n\n".join(condensed)
    # Limit total size to ~12k chars to stay well within context
    if len(transcript) > 12000:
        transcript = transcript[:12000] + "\n\n[...truncated]"

    # Use claude CLI to generate summary (uses OAuth auth, no API key needed)
    try:
        prompt = f"""Summarize this Claude Code session transcript. Write a concise summary with these sections:

**Started with:** What the user initially asked for (1 sentence)
**What happened:** Key actions, decisions, and progress (2-4 bullet points)
**Current state:** Where things stand now (1 sentence)

Be specific about what was built, changed, or decided. Keep the total summary under 150 words.

<transcript>
{transcript}
</transcript>"""

        import subprocess as sp
        claude_bin = os.path.expanduser("~/.local/bin/claude")
        result = sp.run(
            [claude_bin, "-p", prompt, "--model", "haiku"],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0:
            return {"error": f"Claude CLI failed: {result.stderr[:200]}"}

        summary = result.stdout.strip()
        if not summary:
            return {"error": "Empty response from Claude"}

        # Cache it
        save_summary(session_id, summary)

        return {"summary": summary, "cached": False, "generated": datetime.now(timezone.utc).isoformat()}

    except sp.TimeoutExpired:
        return {"error": "Summary generation timed out (30s)"}
    except Exception as e:
        return {"error": f"Failed to generate summary: {str(e)}"}


# ── Terminal WebSocket ────────────────────────────────────────────────────────


# Track active terminal processes for cleanup
_terminal_processes: dict[int, int] = {}  # fd -> pid


def _set_pty_size(fd: int, rows: int, cols: int):
    """Set the terminal size on the pty."""
    size = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


@app.websocket("/ws/terminal")
async def terminal_websocket(websocket: WebSocket):
    await websocket.accept()

    # Spawn a shell in a pty
    pid, fd = pty.openpty() if False else (0, 0)  # placeholder
    child_pid, fd = pty.fork()

    if child_pid == 0:
        # Child process — exec a shell
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        os.execvp("/bin/zsh", ["/bin/zsh", "-l"])

    # Parent process — relay between WebSocket and pty
    _terminal_processes[fd] = child_pid
    _set_pty_size(fd, 24, 80)

    loop = asyncio.get_event_loop()

    # Make the pty fd non-blocking so we can use asyncio reader
    import os as _os
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | _os.O_NONBLOCK)

    async def read_pty():
        """Read from pty and send to WebSocket using asyncio fd reader."""
        try:
            read_event = asyncio.Event()

            def on_readable():
                read_event.set()

            loop.add_reader(fd, on_readable)

            while True:
                await read_event.wait()
                read_event.clear()
                try:
                    while True:
                        data = os.read(fd, 4096)
                        if data:
                            await websocket.send_bytes(data)
                        else:
                            return  # EOF
                except BlockingIOError:
                    pass  # No more data right now, wait for next event
                except OSError:
                    return
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass

    read_task = asyncio.create_task(read_pty())

    try:
        while True:
            msg = await websocket.receive()

            if msg.get("type") == "websocket.disconnect":
                break

            if "text" in msg:
                # JSON control messages (resize, etc.)
                try:
                    ctrl = json.loads(msg["text"])
                    if ctrl.get("type") == "resize":
                        _set_pty_size(fd, ctrl.get("rows", 24), ctrl.get("cols", 80))
                        continue
                except (json.JSONDecodeError, KeyError):
                    # Plain text input
                    os.write(fd, msg["text"].encode())
                    continue

            if "bytes" in msg:
                os.write(fd, msg["bytes"])

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        read_task.cancel()
        # Clean up the child process
        try:
            os.kill(child_pid, signal.SIGTERM)
            os.waitpid(child_pid, 0)
        except (OSError, ChildProcessError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        _terminal_processes.pop(fd, None)


# ── Serve Frontend ───────────────────────────────────────────────────────────


DASHBOARD_DIR = Path(__file__).parent

@app.get("/")
def serve_frontend():
    return FileResponse(DASHBOARD_DIR / "index.html")
