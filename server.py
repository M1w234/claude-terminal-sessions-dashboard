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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import subprocess as sp
import httpx

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import anthropic

app = FastAPI(title="Claude Sessions Dashboard")

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
NAMES_FILE = CLAUDE_DIR / "session-names.json"
WORKSPACES_FILE = CLAUDE_DIR / "session-workspaces.json"
CMUX_MAP_FILE = CLAUDE_DIR / "cmux-session-map.json"
SUMMARIES_FILE = CLAUDE_DIR / "session-summaries.json"
SKILLS_DIR = CLAUDE_DIR / "skills"
COMMANDS_DIR = CLAUDE_DIR / "commands"
HOOKS_DIR = CLAUDE_DIR / "hooks"
PLUGINS_DIR = CLAUDE_DIR / "plugins" / "marketplaces"
TOOLS_FILE = Path(__file__).parent / "tools.json"
TOOLS_STATIC_FILE = Path(__file__).parent / "tools-static.json"
IDEAS_FILE = Path(__file__).parent / "ideas.json"

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")
APIFY_BASE = "https://api.apify.com/v2"

_ideas_lock = asyncio.Lock()

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


DASHBOARD_PATH = str(Path(__file__).parent)

# Patterns that indicate auto-generated sessions (e.g., from Skill Planner)
_AUTO_SESSION_PATTERNS = [
    "skill planning assistant",
    "tool recommendation engine",
    "catalog enrichment tool",
    "produce a complete brief",
    "conversation so far, produce",
]


def _detect_auto_tag(project_path: str, first_prompt: str) -> str:
    """Detect if a session should be auto-tagged."""
    # Only tag sessions whose first prompt matches Skill Planner patterns
    lower = (first_prompt or "").lower()
    for pattern in _AUTO_SESSION_PATTERNS:
        if pattern in lower:
            return "Skill Planner"
    return ""


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
        project_path = entry.get("projectPath", "")
        first_prompt = entry.get("firstPrompt", "")
        sessions.append({
            "id": sid,
            "name": clean_text(entry.get("summary") or auto_name or (first_prompt[:80])),
            "firstPrompt": clean_text(first_prompt),
            "messageCount": entry.get("messageCount", 0),
            "created": entry.get("created", ""),
            "modified": entry.get("modified", ""),
            "gitBranch": entry.get("gitBranch", ""),
            "projectPath": project_path,
            "workspace": ws_map.get(sid, ""),
            "autoTag": _detect_auto_tag(project_path, first_prompt),
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

        # Use file modification time as the authoritative "modified" timestamp
        # The JSONL scan only reads first 300 lines, so long sessions would have stale timestamps
        try:
            file_mtime = datetime.fromtimestamp(os.path.getmtime(jsonl_file), tz=timezone.utc).isoformat()
            modified = file_mtime
        except OSError:
            pass

        auto_name = auto_names.get(sid, "")

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
            "autoTag": _detect_auto_tag(cwd, first_prompt),
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

    # Strip ALL tags (including partial/broken tags from truncation)
    result = re.sub(r'<[^>]*>', '', text).strip()
    result = re.sub(r'<[^>]*$', '', result).strip()  # trailing partial tag
    result = re.sub(r'^[^<]*>', '', result).strip()   # leading partial close tag

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


# ── Tools Catalog ───────────────────────────────────────────────────────────


def parse_skill_frontmatter(filepath: Path):
    """Parse YAML-like frontmatter from a skill.md or command.md file."""
    try:
        with open(filepath) as f:
            content = f.read()
    except IOError:
        return None

    if not content.startswith("---"):
        return None

    end = content.find("---", 3)
    if end == -1:
        return None

    frontmatter = content[3:end].strip()
    result = {}
    for line in frontmatter.split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            val = val.strip().strip('"').strip("'")
            result[key.strip()] = val
    return result


def build_catalog():
    """Scan skills and commands, merge with tools.json enrichments."""
    # Load enrichments
    enrichments = {}
    try:
        with open(TOOLS_FILE) as f:
            enrichments = json.load(f)
    except (IOError, json.JSONDecodeError):
        pass

    catalog = []

    # Scan skills
    if SKILLS_DIR.exists():
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            skill_file = skill_dir / "skill.md"
            if not skill_file.exists():
                continue
            meta = parse_skill_frontmatter(skill_file)
            if not meta:
                continue

            skill_id = skill_dir.name
            enrich = enrichments.get(skill_id, {})

            catalog.append({
                "id": skill_id,
                "name": meta.get("name", skill_id),
                "command": f"/{skill_id}",
                "description": meta.get("description", ""),
                "type": "skill",
                "category": enrich.get("category", "uncategorized"),
                "subcategory": enrich.get("subcategory"),
                "useCases": enrich.get("useCases", []),
                "tags": enrich.get("tags", []),
                "related": enrich.get("related", []),
                "source": "apify" if "apify" in meta.get("description", "").lower() else "custom",
            })

    # Scan commands
    if COMMANDS_DIR.exists():
        for cmd_file in sorted(COMMANDS_DIR.glob("*.md")):
            meta = parse_skill_frontmatter(cmd_file)
            cmd_id = cmd_file.stem
            enrich = enrichments.get(cmd_id, {})

            catalog.append({
                "id": cmd_id,
                "name": meta.get("name", cmd_id) if meta else cmd_id,
                "command": f"/{cmd_id}",
                "description": (meta.get("description", "") if meta else ""),
                "type": "command",
                "category": enrich.get("category", "dev"),
                "subcategory": enrich.get("subcategory"),
                "useCases": enrich.get("useCases", []),
                "tags": enrich.get("tags", []),
                "related": enrich.get("related", []),
                "source": "custom",
            })

    # Scan hooks
    if HOOKS_DIR.exists():
        for hook_file in sorted(HOOKS_DIR.glob("*.sh")):
            hook_id = f"hook-{hook_file.stem}"
            enrich = enrichments.get(hook_id, {})
            # Read first comment line for description
            desc = ""
            try:
                with open(hook_file) as f:
                    for line in f:
                        if line.startswith("# ") and not line.startswith("#!"):
                            desc = line[2:].strip()
                            break
            except IOError:
                pass

            catalog.append({
                "id": hook_id,
                "name": hook_file.stem.replace("-", " ").replace("_", " ").title(),
                "command": "(automatic)",
                "description": desc or f"Hook script: {hook_file.name}",
                "type": "hook",
                "category": enrich.get("category", "hooks"),
                "subcategory": enrich.get("subcategory"),
                "useCases": enrich.get("useCases", []),
                "tags": enrich.get("tags", ["hook", "automation"]),
                "related": enrich.get("related", []),
                "source": "custom",
            })

    # Scan installed plugins
    if PLUGINS_DIR.exists():
        for marketplace_dir in PLUGINS_DIR.iterdir():
            plugins_root = marketplace_dir / "plugins"
            if not plugins_root.exists():
                continue
            for plugin_dir in sorted(plugins_root.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                plugin_id = f"plugin-{plugin_dir.name}"
                enrich = enrichments.get(plugin_id, {})
                # Try to read README or plugin.json for description
                desc = ""
                readme = plugin_dir / "README.md"
                if readme.exists():
                    try:
                        with open(readme) as f:
                            lines = f.readlines()
                        # Use first non-heading, non-empty line
                        for line in lines:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                desc = line[:200]
                                break
                    except IOError:
                        pass

                catalog.append({
                    "id": plugin_id,
                    "name": plugin_dir.name.replace("-", " ").title(),
                    "command": f"(plugin)",
                    "description": desc or f"Installed plugin: {plugin_dir.name}",
                    "type": "plugin",
                    "category": enrich.get("category", "plugins"),
                    "subcategory": enrich.get("subcategory"),
                    "useCases": enrich.get("useCases", []),
                    "tags": enrich.get("tags", ["plugin"]),
                    "related": enrich.get("related", []),
                    "source": "plugin",
                })

    # Scan external plugins (MCP servers from the plugin marketplace)
    if PLUGINS_DIR.exists():
        for marketplace_dir in PLUGINS_DIR.iterdir():
            ext_root = marketplace_dir / "external_plugins"
            if not ext_root.exists():
                continue
            for ext_dir in sorted(ext_root.iterdir()):
                if not ext_dir.is_dir():
                    continue
                mcp_file = ext_dir / ".mcp.json"
                if not mcp_file.exists():
                    continue

                ext_id = f"mcp-{ext_dir.name}"
                enrich = enrichments.get(ext_id, {})

                # Extract server name from .mcp.json (two formats)
                server_name = ext_dir.name
                try:
                    with open(mcp_file) as f:
                        mcp_data = json.load(f)
                    # Format 1: {"mcpServers": {"name": {...}}}
                    if "mcpServers" in mcp_data:
                        keys = list(mcp_data["mcpServers"].keys())
                        if keys:
                            server_name = keys[0]
                    # Format 2: {"name": {...}} — top-level keys are server names
                    else:
                        keys = list(mcp_data.keys())
                        if keys:
                            server_name = keys[0]
                except (IOError, json.JSONDecodeError):
                    pass

                # Read description from README.md
                desc = ""
                readme = ext_dir / "README.md"
                if readme.exists():
                    try:
                        with open(readme) as f:
                            lines = f.readlines()
                        for line in lines:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                desc = line[:200]
                                break
                    except IOError:
                        pass

                catalog.append({
                    "id": ext_id,
                    "name": server_name.replace("-", " ").replace("_", " ").title(),
                    "command": "(MCP server)",
                    "description": desc or f"MCP server: {server_name}",
                    "type": "mcp",
                    "category": enrich.get("category", "mcp"),
                    "subcategory": enrich.get("subcategory"),
                    "useCases": enrich.get("useCases", []),
                    "tags": enrich.get("tags", ["mcp", server_name]),
                    "related": enrich.get("related", []),
                    "source": "mcp",
                })

    # Load user-configured static entries (MCP integrations, etc.)
    if TOOLS_STATIC_FILE.exists():
        try:
            with open(TOOLS_STATIC_FILE) as f:
                static_entries = json.load(f)
            for entry in static_entries:
                entry_id = entry.get("id", "")
                enrich = enrichments.get(entry_id, {})
                # Merge enrichments into static entry
                for key in ("useCases", "tags", "related", "category", "subcategory"):
                    if key in enrich and key not in entry:
                        entry[key] = enrich[key]
                catalog.append(entry)
        except (IOError, json.JSONDecodeError):
            pass

    return catalog


@app.get("/api/catalog")
async def get_catalog():
    loop = asyncio.get_event_loop()
    catalog = await loop.run_in_executor(None, build_catalog)
    return {"items": catalog, "count": len(catalog)}


# ── Smart Search & Skill Workbench ───────────────────────────────────────────

CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
_smart_search_cache: dict = {}


def _run_claude(prompt: str, model: str = "haiku", timeout: int = 15) -> str:
    """Run claude -p and return the response text."""
    result = sp.run(
        [CLAUDE_BIN, "-p", prompt, "--model", model],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI error: {result.stderr[:200]}")
    return result.stdout.strip()


def _catalog_summary() -> str:
    """Build a compact text summary of the catalog for LLM context."""
    catalog = build_catalog()
    lines = []
    for item in catalog:
        use_cases = ", ".join(item.get("useCases", [])[:3])
        tags = ", ".join(item.get("tags", [])[:5])
        lines.append(
            f"- {item['id']} ({item['command']}): {item['description'][:150]}"
            + (f" | Use cases: {use_cases}" if use_cases else "")
            + (f" | Tags: {tags}" if tags else "")
        )
    return "\n".join(lines)


@app.post("/api/smart-search")
async def smart_search(request: Request):
    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        return {"error": "No query provided"}

    # Check cache
    if query.lower() in _smart_search_cache:
        return {"results": _smart_search_cache[query.lower()], "cached": True}

    catalog_text = await asyncio.get_event_loop().run_in_executor(None, _catalog_summary)

    prompt = f"""You are a tool recommendation engine. Given a user's query and a catalog of tools, return the most relevant tools ranked by relevance.

CATALOG:
{catalog_text}

USER QUERY: "{query}"

Return a JSON array of the top 5-10 most relevant tools. Each entry must have:
- "id": the tool's id (exact match from catalog)
- "relevance": a brief explanation of why this tool is relevant (1 sentence)
- "score": relevance score from 0.0 to 1.0

Return ONLY the JSON array, no other text. Example:
[{{"id": "competitor-intel", "relevance": "Directly analyzes competitors by location and industry", "score": 0.95}}]"""

    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _run_claude(prompt, model="haiku", timeout=10)
        )
        # Parse JSON from response (handle markdown code fences)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("```", 1)[0]
        results = json.loads(raw)
        # Cache it
        _smart_search_cache[query.lower()] = results
        return {"results": results, "cached": False}
    except sp.TimeoutExpired:
        return {"error": "timeout", "message": "Smart search timed out"}
    except (json.JSONDecodeError, RuntimeError) as e:
        return {"error": "parse_error", "message": str(e)}


@app.get("/api/skill/{skill_id}/content")
async def get_skill_content(skill_id: str):
    """Return the full raw content of a skill.md or command.md file."""
    # Check skills dir
    skill_file = SKILLS_DIR / skill_id / "skill.md"
    if skill_file.exists():
        content = await asyncio.get_event_loop().run_in_executor(
            None, lambda: skill_file.read_text()
        )
        return {"content": content, "found": True, "path": str(skill_file)}

    # Check commands dir
    cmd_file = COMMANDS_DIR / f"{skill_id}.md"
    if cmd_file.exists():
        content = await asyncio.get_event_loop().run_in_executor(
            None, lambda: cmd_file.read_text()
        )
        return {"content": content, "found": True, "path": str(cmd_file)}

    return {"content": "", "found": False}


_SKILL_KNOWLEDGE_BASE = """
## How Claude Code Skills Work

### Anatomy of a Skill
```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description — this is the primary trigger mechanism)
│   └── Markdown instructions (the skill's body — loaded when triggered)
└── Bundled Resources (optional)
    ├── scripts/    - Executable code for deterministic/repetitive tasks
    ├── references/ - Docs loaded into context as needed
    └── assets/     - Files used in output (templates, icons, fonts)
```

### Progressive Disclosure (3 levels)
1. **Metadata** (name + description) — ALWAYS in context (~100 words). This determines triggering.
2. **SKILL.md body** — loaded when skill triggers (<500 lines ideal, <5k words)
3. **Bundled resources** — loaded as needed (unlimited; scripts can execute without loading)

### SKILL.md Structure for Simple Skills (e.g., Apify scrapers)
```markdown
---
name: skill-name
description: "What it does + when to trigger. Be specific and slightly 'pushy' — Claude tends to undertrigger."
---
# Display Name
Brief intro of what this skill does.

## What You Get
- Bullet list of deliverables

## Workflow
### Step 1: Parse the User's Request
Extract from the user's message:
- **Parameter 1** (required) — description
- **Parameter 2** (optional) — defaults to X

### Step 2: Run the Tool
```bash
# Code blocks with API calls, curl commands, scripts
```

### Step 3: Summarize Results
Present the data in a clear format. Include key metrics.
```

### SKILL.md Structure for Complex Skills (multi-phase orchestration)
- Uses **Phases** instead of Steps (Phase 0-N)
- May delegate to sub-agents
- Has confidence gates / conditional logic between phases
- Explicit error handling section
- Inter-phase communication via JSON files

### Description Writing (Critical for Triggering)
- The description is THE primary mechanism that determines if Claude uses the skill
- Be specific: include exact phrases a user would say
- Be slightly "pushy" — Claude tends to undertrigger
- Include both what the skill does AND when to use it
- Example: "Scrape Instagram profile data (bio, followers, stats, recent posts) for one or more usernames via Apify. Use when the user wants profile intel on an Instagram account."

### Key Patterns in Existing Skills
- **Apify-based skills**: Parse user input → build Apify actor input JSON → run via curl to Apify API → download results → summarize
- **MCP-based skills**: Leverage existing MCP tool connections (Notion, Canva, etc.)
- **CLI-based skills**: Build and execute terminal commands with proper error handling
- **Orchestration skills**: Multi-phase with agent delegation, JSON handoffs, confidence gates

### The /skill-creator Workflow (what happens AFTER planning)
1. Capture intent and interview
2. Write draft SKILL.md
3. Create 2-3 test prompts
4. Run test cases (with-skill AND baseline comparison)
5. User reviews results in eval viewer
6. Iterate based on feedback
7. Optimize description for better triggering
8. Package and deliver

### What Makes a Skill Great
- Description triggers reliably on the right queries and NOT on wrong ones
- SKILL.md is lean (<2000 words) with detailed content in references/
- Workflow is clear: parse → execute → present
- Edge cases are handled gracefully (rate limits, missing data, auth)
- Explains WHY behind instructions, not just rigid MUSTs
- Includes bundled scripts for deterministic/repetitive operations
- Test cases cover happy path + edge cases
"""


def _build_system_prompt(mode: str, catalog_summary: str) -> str:
    """Build a rich system prompt for the skill planner."""
    prompts = WORKBENCH_SYSTEM_PROMPTS[mode]
    return prompts.replace("{SKILL_KNOWLEDGE_BASE}", _SKILL_KNOWLEDGE_BASE).replace("{CATALOG_SUMMARY}", catalog_summary)


WORKBENCH_SYSTEM_PROMPTS = {
    "create": """You are an expert Claude Code skill planning assistant, powered by Opus. Your job is to help the user think through what they want a new skill to do BEFORE they open a Claude Code session to build it with /skill-creator.

You are the PLANNING layer. /skill-creator in Claude Code is the EXECUTION layer — it writes the SKILL.md, runs test cases, benchmarks, and optimizes the description. You should NOT produce the final skill content. Instead, help the user develop a clear, thorough brief that gives /skill-creator a massive head start.

{SKILL_KNOWLEDGE_BASE}

## Your Existing Skill Catalog (for reference and pattern-matching)
{CATALOG_SUMMARY}

## How to Guide the User

Be conversational and thoughtful. Ask 2-3 questions at a time, not all at once. Build understanding incrementally. Tailor your questions based on what type of skill they're describing:

**For Apify/scraping skills**, ask about:
- Which platform/data source? Is there an existing Apify actor?
- What specific data fields do they need?
- What's the output format? (JSON files, summaries, both?)
- Rate limits or pagination concerns?
- Point to similar existing skills as templates (e.g., "ig-profile follows a pattern you could reuse")

**For integration/MCP skills**, ask about:
- Which service are they connecting to?
- What operations? (read, write, sync?)
- Authentication requirements?
- Error handling for API failures?

**For workflow/orchestration skills**, ask about:
- How many phases? What's the flow between them?
- Are there decision points or gates?
- What data passes between phases?
- Should it delegate to sub-agents?

**For content creation skills**, ask about:
- What's the input format?
- What quality/style standards?
- Any templates or assets to bundle?
- How should output be presented?

Always consider:
1. **Purpose**: What problem does this solve? What's the user's actual goal?
2. **Trigger**: What would someone say to invoke this? What SHOULDN'T trigger it?
3. **Data source**: What API/tool/service powers it?
4. **Input**: Required vs optional parameters, with sensible defaults
5. **Output**: What the user gets back — format, location, structure
6. **Workflow**: High-level steps with enough detail for implementation
7. **Edge cases**: What could go wrong? How to handle gracefully?
8. **Similar skills**: Which existing skills should /skill-creator use as templates?

If existing skills are referenced below, study their patterns deeply — they show what works.""",

    "modify": """You are an expert Claude Code skill modification planner, powered by Opus. The user wants to change an existing skill and needs help thinking through the changes BEFORE implementing with /skill-creator.

{SKILL_KNOWLEDGE_BASE}

## Your Existing Skill Catalog
{CATALOG_SUMMARY}

## How to Help

The referenced skill content is provided below. Work through these areas:

1. **Understand the current skill thoroughly**: Walk through what it does, how it's structured, what each workflow step accomplishes. Point out its strengths and any existing limitations.

2. **Clarify the desired changes**: What specifically should change? Probe for details:
   - New functionality being added?
   - Different output format or additional data fields?
   - New parameters or input types?
   - Better error handling or edge case coverage?
   - Performance or efficiency improvements?

3. **Assess impact**: How do the changes ripple through the skill?
   - Does the description/triggering need to change?
   - Which workflow steps are affected?
   - Are new API calls, scripts, or references needed?
   - Could this break existing behavior?

4. **Plan the approach**:
   - Modify the existing skill vs. create a new one?
   - What sections of SKILL.md change?
   - Should content move to references/ for better progressive disclosure?
   - Any new bundled scripts or assets needed?

5. **Test strategy**: What test cases would validate the changes work correctly?

Reference specific sections and line content of the existing skill when discussing changes. Be concrete, not abstract.""",

    "explain": """You are an expert Claude Code skill analyst, powered by Opus. The user wants to deeply understand how skills work.

{SKILL_KNOWLEDGE_BASE}

## Your Existing Skill Catalog
{CATALOG_SUMMARY}

The referenced skill content is provided below. Provide thorough analysis:

- **Architecture**: How is the skill structured? What design pattern does it follow?
- **Workflow walkthrough**: Step by step, what happens when this skill is invoked?
- **Data flow**: What input comes in, how is it transformed, what output comes out?
- **API/tool integration**: What external calls are made? What data comes back? What could fail?
- **Progressive disclosure**: What's in SKILL.md vs references/ vs scripts/? Is the structure optimal?
- **Trigger analysis**: How is the description written? Would it trigger reliably? Are there false-positive risks?
- **Patterns worth reusing**: What techniques or structures could be borrowed for other skills?

If multiple skills are referenced, compare them in depth: architecture differences, when to use each, patterns that could cross-pollinate.""",

    "enrich": """You are a Claude Code catalog enrichment tool. For each referenced skill, generate tools.json enrichment data.

{CATALOG_SUMMARY}

Output a JSON object where each key is the skill ID, and the value contains:
- "category": one of "social-media", "lead-gen", "local", "seo", "web", "content", "dev"
- "subcategory": platform name if applicable (e.g., "instagram", "facebook") or null
- "useCases": array of 5-8 natural language phrases a real person would say when they need this tool (conversational, specific, varied length)
- "tags": array of 8-12 searchable keywords covering the platform, action, data type, and use case
- "related": array of 3-5 IDs of related skills from the catalog (use exact IDs from the catalog above)

Return ONLY the JSON object, no other text.""",

    "generate-output": """Based on the conversation so far, produce a complete, detailed brief to paste into a fresh Claude Code session as the FIRST MESSAGE when using /skill-creator.

{SKILL_KNOWLEDGE_BASE}

Structure the brief as follows. Fill in ALL sections with specific, concrete details from the conversation. This brief should give /skill-creator everything it needs to start building immediately without asking many follow-up questions.

---
I want to [create / modify] a skill. Here's what I've planned out:

**Skill name**: [name]
**Purpose**: [1-2 sentences on what it does and the problem it solves]

**When it should trigger** (specific phrases users would say):
- "[phrase 1]"
- "[phrase 2]"
- "[phrase 3]"
- "[phrase 4]"

**When it should NOT trigger** (near-misses to avoid):
- "[phrase that's close but should use a different tool instead]"

**Data source / API**: [what powers this skill — be specific about the API, actor ID, endpoint, etc.]

**User input**:
- Required: [list each required parameter with description]
- Optional: [list each optional parameter with default value]

**Expected output**:
- Format: [JSON files, markdown summary, table, etc.]
- Location: [where files are saved, e.g., ~/skill-output/]
- Presentation: [how results should be summarized to the user]

**Workflow steps**:
1. Parse user input — extract [specific parameters] from their message
2. [Specific API call or tool invocation with details]
3. [Process/transform results — filtering, enrichment, formatting]
4. [Present to user — summary format, key metrics to highlight]

**Edge cases to handle**:
- [Specific edge case 1 and how to handle it]
- [Specific edge case 2 and how to handle it]

**Reference skills to use as templates**:
- [skill-id]: [what pattern to borrow from it — e.g., "use its Apify actor input structure"]
- [skill-id]: [what pattern to borrow]

**Suggested skill structure**:
```
skill-name/
├── SKILL.md
├── references/  (if needed — list what goes here)
└── scripts/     (if needed — list what goes here)
```

**Test cases to try**:
1. "[realistic user prompt]" → expected: [specific expected behavior]
2. "[different variation]" → expected: [specific expected behavior]
3. "[edge case prompt]" → expected: [graceful handling description]

Please use /skill-creator to build this out, run the test cases, and iterate until it's solid.
---

Be thorough and specific. The more detail you provide, the better the skill will be on the first iteration.""",
}


@app.post("/api/skill-builder")
async def skill_builder(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    referenced_skills = body.get("referencedSkills", [])
    mode = body.get("mode", "create")

    if not messages:
        return {"error": "No messages provided"}

    # Load referenced skill content
    skill_context = ""
    for skill_id in referenced_skills[:5]:
        skill_file = SKILLS_DIR / skill_id / "skill.md"
        if not skill_file.exists():
            skill_file = COMMANDS_DIR / f"{skill_id}.md"
        if skill_file.exists():
            try:
                content = skill_file.read_text()
                skill_context += f"\n\n--- SKILL: {skill_id} ---\n{content}\n--- END SKILL ---\n"
            except IOError:
                pass

    # Build system prompt with full context
    catalog_text = _catalog_summary()
    system = _build_system_prompt(
        mode if mode in WORKBENCH_SYSTEM_PROMPTS else "create",
        catalog_text,
    )
    if skill_context:
        system += f"\n\nREFERENCED SKILL CONTENT (full source):\n{skill_context}"

    # Load referenced idea content
    referenced_ideas = body.get("referencedIdeas", [])
    if referenced_ideas:
        async with _ideas_lock:
            ideas = load_ideas()
        idea_context = ""
        for idea_id in referenced_ideas[:3]:
            idea = next((i for i in ideas if i["id"] == idea_id), None)
            if idea and idea.get("enriched_content"):
                idea_context += f"\n\n--- IDEA: {idea.get('title', idea_id)} ---\n"
                idea_context += f"URL: {idea.get('url', 'N/A')}\nNotes: {idea.get('notes', '')}\n"
                idea_context += f"Content:\n{idea['enriched_content'][:10000]}\n--- END IDEA ---\n"
        if idea_context:
            system += f"\n\nREFERENCED IDEA CONTENT (user's saved research):\n{idea_context}"

    # Build conversation for claude -p
    conversation_parts = [f"<system>{system}</system>"]
    for msg in messages[-15:]:  # Last 15 messages for context management
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            conversation_parts.append(f"Human: {content}")
        else:
            conversation_parts.append(f"Assistant: {content}")

    # Add final Human turn if last message was assistant
    if messages and messages[-1].get("role") == "assistant":
        conversation_parts.append("Human: Please continue.")

    prompt = "\n\n".join(conversation_parts)

    try:
        raw = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _run_claude(prompt, model="opus", timeout=120)
        )
        return {"response": raw}
    except sp.TimeoutExpired:
        return {"error": "timeout", "message": "The builder is taking too long. Try again."}
    except RuntimeError as e:
        return {"error": "cli_error", "message": str(e)}


# ── Ideas Workshop ──────────────────────────────────────────────────────────


def load_ideas() -> list:
    try:
        with open(IDEAS_FILE) as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return []


def save_ideas(ideas: list):
    with open(IDEAS_FILE, "w") as f:
        json.dump(ideas, f, indent=2)


def generate_idea_id() -> str:
    return uuid.uuid4().hex[:12]


def detect_source_type(url: str) -> str:
    if not url:
        return "manual"
    lower = url.lower()
    if "instagram.com/reel" in lower or "instagram.com/p/" in lower:
        return "instagram"
    if "youtube.com/watch" in lower or "youtu.be/" in lower or "youtube.com/shorts" in lower:
        return "youtube"
    return "web"


def normalize_url(url: str) -> str:
    """Normalize URL for deduplication."""
    if not url:
        return ""
    parsed = urlparse(url)
    # Strip tracking params and trailing slash
    path = parsed.path.rstrip("/")
    # Rebuild without fragments and common tracking params
    clean_query = "&".join(
        p for p in (parsed.query or "").split("&")
        if p and not any(p.startswith(t) for t in ("utm_", "ref=", "fbclid=", "igshid="))
    )
    return f"{parsed.scheme}://{parsed.netloc}{path}" + (f"?{clean_query}" if clean_query else "")


def _auto_title(url: str, notes: str, source_type: str) -> str:
    """Generate a title from URL or notes."""
    if notes:
        return notes[:60].strip()
    if url:
        parsed = urlparse(url)
        host = parsed.netloc.replace("www.", "")
        path = parsed.path.strip("/")[:40]
        return f"{host}/{path}" if path else host
    return "Untitled idea"


# ── Enrichment Engine ───────────────────────────────────────────────────────

APIFY_ACTORS = {
    "youtube": "karamelo~youtube-transcripts",
    "instagram": "sian.agency~instagram-ai-transcript-extractor",
    "web": "apify~website-content-crawler",
}


def _build_apify_input(source_type: str, url: str) -> dict:
    if source_type == "youtube":
        return {"urls": [url]}
    elif source_type == "instagram":
        return {"instagramUrl": url, "fastProcessing": True}
    elif source_type == "web":
        return {"startUrls": [{"url": url}], "maxCrawlPages": 1, "crawlerType": "cheerio"}
    return {}


def _extract_enriched_content(source_type: str, items: list) -> str:
    """Extract the useful text from Apify dataset items."""
    if not items:
        return ""

    if source_type == "youtube":
        parts = []
        for item in items:
            title = item.get("title") or item.get("videoTitle", "")
            channel = item.get("channelName", "")
            if title:
                parts.append(f"Title: {title}")
            if channel:
                parts.append(f"Channel: {channel}")
            # Handle captions as list of strings or single transcript string
            captions = item.get("captions", [])
            if isinstance(captions, list):
                parts.append(" ".join(str(c) for c in captions))
            elif isinstance(captions, str):
                parts.append(captions)
            # Fallback to transcript/text fields
            transcript = item.get("transcript") or item.get("text", "")
            if transcript and not captions:
                parts.append(transcript)
        return "\n\n".join(parts)[:50000]

    elif source_type == "instagram":
        parts = []
        for item in items:
            transcript = item.get("transcript") or item.get("text", "")
            if transcript:
                parts.append(transcript)
        return "\n\n".join(parts)[:50000]

    elif source_type == "web":
        parts = []
        for item in items:
            title = item.get("metadata", {}).get("title") or item.get("title", "")
            content = item.get("markdown") or item.get("text", "")
            if title:
                parts.append(f"# {title}")
            if content:
                parts.append(content)
        return "\n\n".join(parts)[:50000]

    return ""


async def _run_apify_actor(actor_id: str, input_data: dict, timeout: int = 300) -> list:
    """Start an Apify actor run and poll until completion."""
    if not APIFY_API_TOKEN:
        raise RuntimeError("APIFY_API_TOKEN not set")

    async with httpx.AsyncClient(timeout=30) as client:
        # Start run
        resp = await client.post(
            f"{APIFY_BASE}/acts/{actor_id}/runs",
            params={"token": APIFY_API_TOKEN},
            json=input_data,
        )
        resp.raise_for_status()
        run_id = resp.json()["data"]["id"]

        # Poll for completion
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(5)
            elapsed += 5
            status_resp = await client.get(
                f"{APIFY_BASE}/actor-runs/{run_id}",
                params={"token": APIFY_API_TOKEN},
            )
            status_resp.raise_for_status()
            data = status_resp.json()["data"]
            status = data["status"]

            if status == "SUCCEEDED":
                dataset_id = data.get("defaultDatasetId")
                if not dataset_id:
                    return []
                items_resp = await client.get(
                    f"{APIFY_BASE}/datasets/{dataset_id}/items",
                    params={"token": APIFY_API_TOKEN, "format": "json"},
                )
                items_resp.raise_for_status()
                return items_resp.json()

            if status in ("FAILED", "ABORTED", "TIMED-OUT"):
                raise RuntimeError(f"Apify actor {actor_id} {status}")

        raise TimeoutError(f"Apify actor {actor_id} did not complete within {timeout}s")


async def _enrich_idea(idea_id: str):
    """Background task: enrich an idea with content from its URL."""
    try:
        async with _ideas_lock:
            ideas = load_ideas()
            idea = next((i for i in ideas if i["id"] == idea_id), None)
            if not idea or not idea.get("url"):
                return
            if idea.get("enrichment_status") == "running":
                return  # Already running
            idea["enrichment_status"] = "running"
            save_ideas(ideas)

        source_type = idea["source_type"]
        actor_id = APIFY_ACTORS.get(source_type)
        if not actor_id:
            raise RuntimeError(f"No actor for source type: {source_type}")

        input_data = _build_apify_input(source_type, idea["url"])
        items = await _run_apify_actor(actor_id, input_data)
        content = _extract_enriched_content(source_type, items)

        async with _ideas_lock:
            ideas = load_ideas()
            idea = next((i for i in ideas if i["id"] == idea_id), None)
            if idea:
                idea["enriched_content"] = content
                idea["enrichment_status"] = "done" if content else "failed"
                idea["updated"] = datetime.now(timezone.utc).isoformat()
                save_ideas(ideas)

    except Exception as e:
        import traceback
        traceback.print_exc()
        async with _ideas_lock:
            ideas = load_ideas()
            idea = next((i for i in ideas if i["id"] == idea_id), None)
            if idea:
                idea["enrichment_status"] = "failed"
                idea["enrichment_error"] = str(e)[:200]
                idea["updated"] = datetime.now(timezone.utc).isoformat()
                save_ideas(ideas)


# ── Ideas API Routes ────────────────────────────────────────────────────────


@app.get("/api/ideas")
async def list_ideas(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
):
    async with _ideas_lock:
        ideas = load_ideas()

    if status:
        ideas = [i for i in ideas if i.get("status") == status]
    if tag:
        ideas = [i for i in ideas if tag in i.get("tags", [])]
    if search:
        q = search.lower()
        ideas = [
            i for i in ideas
            if q in (i.get("title") or "").lower()
            or q in (i.get("notes") or "").lower()
            or q in (i.get("url") or "").lower()
            or any(q in t.lower() for t in i.get("tags", []))
        ]

    # Sort by updated desc
    ideas.sort(key=lambda i: i.get("updated", ""), reverse=True)
    return {"ideas": ideas, "total": len(ideas)}


@app.get("/api/ideas/{idea_id}")
async def get_idea(idea_id: str):
    async with _ideas_lock:
        ideas = load_ideas()
    idea = next((i for i in ideas if i["id"] == idea_id), None)
    if not idea:
        return JSONResponse({"error": "Idea not found"}, status_code=404)
    return idea


@app.post("/api/ideas")
async def create_idea(request: Request):
    body = await request.json()
    url = (body.get("url") or "").strip()
    notes = (body.get("notes") or "").strip()
    tags = body.get("tags") or []
    title = (body.get("title") or "").strip()

    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    if not url and not notes:
        return JSONResponse({"error": "Provide a URL or notes"}, status_code=400)

    source_type = detect_source_type(url)
    normalized = normalize_url(url)

    async with _ideas_lock:
        ideas = load_ideas()

        # Check for duplicate URL
        if normalized:
            existing = next(
                (i for i in ideas if normalize_url(i.get("url", "")) == normalized),
                None,
            )
            if existing:
                return JSONResponse(
                    {"error": "duplicate", "message": "URL already saved", "existing_id": existing["id"]},
                    status_code=409,
                )

        now = datetime.now(timezone.utc).isoformat()
        idea = {
            "id": generate_idea_id(),
            "title": title or _auto_title(url, notes, source_type),
            "url": url,
            "source_type": source_type,
            "notes": notes,
            "status": "inbox",
            "tags": tags,
            "related_skills": [],
            "enriched_content": "",
            "enrichment_status": "pending" if url else "none",
            "enrichment_error": "",
            "created": now,
            "updated": now,
        }
        ideas.append(idea)
        save_ideas(ideas)

    # Fire background enrichment
    if url and source_type != "manual":
        asyncio.create_task(_enrich_idea(idea["id"]))

    return idea


@app.put("/api/ideas/{idea_id}")
async def update_idea(idea_id: str, request: Request):
    body = await request.json()
    async with _ideas_lock:
        ideas = load_ideas()
        idea = next((i for i in ideas if i["id"] == idea_id), None)
        if not idea:
            return JSONResponse({"error": "Idea not found"}, status_code=404)

        for field in ("title", "notes", "status", "tags", "related_skills"):
            if field in body:
                idea[field] = body[field]
        idea["updated"] = datetime.now(timezone.utc).isoformat()
        save_ideas(ideas)

    return idea


@app.delete("/api/ideas/{idea_id}")
async def delete_idea(idea_id: str):
    async with _ideas_lock:
        ideas = load_ideas()
        ideas = [i for i in ideas if i["id"] != idea_id]
        save_ideas(ideas)
    return {"deleted": True}


@app.post("/api/ideas/{idea_id}/enrich")
async def enrich_idea(idea_id: str):
    async with _ideas_lock:
        ideas = load_ideas()
        idea = next((i for i in ideas if i["id"] == idea_id), None)
        if not idea:
            return JSONResponse({"error": "Idea not found"}, status_code=404)
        if not idea.get("url"):
            return JSONResponse({"error": "No URL to enrich"}, status_code=400)
        idea["enrichment_status"] = "pending"
        idea["enrichment_error"] = ""
        save_ideas(ideas)

    asyncio.create_task(_enrich_idea(idea_id))
    return {"status": "enrichment started"}


# ── Startup: reset stuck enrichments ────────────────────────────────────────


@app.on_event("startup")
async def startup_reset_enrichments():
    async with _ideas_lock:
        ideas = load_ideas()
        changed = False
        for idea in ideas:
            if idea.get("enrichment_status") == "running":
                idea["enrichment_status"] = "pending"
                changed = True
        if changed:
            save_ideas(ideas)


# ── Serve Frontend ───────────────────────────────────────────────────────────


DASHBOARD_DIR = Path(__file__).parent
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

@app.get("/")
def serve_frontend():
    return FileResponse(DASHBOARD_DIR / "index.html", headers=_NO_CACHE)


@app.get("/tools")
def serve_tools():
    return FileResponse(DASHBOARD_DIR / "tools.html", headers=_NO_CACHE)


@app.get("/registry")
def serve_registry_redirect():
    """Backwards-compatible alias."""
    return FileResponse(DASHBOARD_DIR / "tools.html", headers=_NO_CACHE)


@app.get("/workshop")
def serve_workshop():
    return FileResponse(DASHBOARD_DIR / "workshop.html", headers=_NO_CACHE)
