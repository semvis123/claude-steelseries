#!/usr/bin/env bash
set -euo pipefail

# SteelSeries hook for Claude Code
# Parses stdin JSON, writes /tmp/ss_state, /tmp/ss_tool, /tmp/ss_transcript
# Ensures the daemon is running. Always exits 0.

STATE_ARG="${1:-}"

# Read stdin (may be empty)
JSON_RAW=$(cat || true)

# Extract tool_name, transcript_path, and a tool-specific short label
# (heredoc + pipe on the same `python3 -` would conflict — pass JSON via env var)
PARSED=$(JSON_RAW="$JSON_RAW" python3 -c '
import os, json
s = os.environ.get("JSON_RAW", "")
try:
    d = json.loads(s) if s.strip() else {}
except Exception:
    d = {}
tool = d.get("tool_name", "") or ""
ti = d.get("tool_input", {}) if isinstance(d.get("tool_input"), dict) else {}
label = ""
fp = ti.get("file_path") or ti.get("notebook_path") or ""
if fp:
    label = os.path.basename(fp)
elif tool == "Bash":
    cmd = (ti.get("command") or "").strip().split("\n", 1)[0]
    label = cmd.split()[0] if cmd else ""
elif tool in ("Grep", "Glob"):
    label = (ti.get("pattern") or "")
elif tool in ("WebFetch", "WebSearch"):
    label = (ti.get("url") or ti.get("query") or "")
elif tool == "Task":
    label = (ti.get("subagent_type") or ti.get("description") or "")
elif tool == "Skill":
    label = (ti.get("skill") or "")
# Strip whitespace and clamp length
label = " ".join(label.split())[:24]
print(tool)
print(d.get("transcript_path", ""))
print(label)
')
TOOL=$(printf '%s\n' "$PARSED" | sed -n '1p')
TRANSCRIPT=$(printf '%s\n' "$PARSED" | sed -n '2p')
TOOL_LABEL=$(printf '%s\n' "$PARSED" | sed -n '3p')

# Write transient files
printf '%s' "$STATE_ARG" > /tmp/ss_state || true
printf '%s' "$TOOL" > /tmp/ss_tool || true
printf '%s' "$TOOL_LABEL" > /tmp/ss_tool_label || true
printf '%s' "$TRANSCRIPT" > /tmp/ss_transcript || true

# Determine project dir if not provided
: "${CLAUDE_PROJECT_DIR:=}"
if [ -z "$CLAUDE_PROJECT_DIR" ]; then
  # Use BASH_SOURCE to correctly locate the script even when invoked via 'bash script'
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  CLAUDE_PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

# Start daemon if not running
LOCK_FILE=/tmp/ss_daemon.lock
PID_FILE=/tmp/ss_daemon.pid

start_daemon() {
  # Launch daemon in background, detach from terminal
  # Use BASH_SOURCE for reliable script location
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  DAEMON_PATH="$SCRIPT_DIR/steelseries_daemon.py"
  # Fallback to project-style path for repo installs
  if [ ! -f "$DAEMON_PATH" ]; then
    DAEMON_PATH="$CLAUDE_PROJECT_DIR/claude_hooks/steelseries_daemon.py"
  fi
  nohup python3 "$DAEMON_PATH" >> /tmp/ss_daemon.log 2>&1 &
  echo $! > $PID_FILE || true
}

# If pid file exists and process alive, do nothing
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    exit 0
  fi
fi

# If lock file exists, assume running
if [ -e "$LOCK_FILE" ]; then
  # best-effort check: if lock file exists but process dead, start
  start_daemon || true
  exit 0
fi

# Start the daemon (best-effort). Do not wait.
start_daemon || true

# Always exit 0 to avoid blocking Claude Code
exit 0
