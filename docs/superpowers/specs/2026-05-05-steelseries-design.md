# Claude SteelSeries — Design Spec

**Date:** 2026-05-05
**Status:** Approved

## Overview

Convert the Claude Lamp (Moonside BLE) project into a SteelSeries keyboard integration. Claude Code state is reflected via RGB keyboard backlighting (animated effects) and the OLED display (live token counts + current tool name) on Apex Pro / Apex 7 keyboards using the official GameSense SDK.

## Goals

- Replace BLE/Moonside transport with GameSense HTTP API
- Animated RGB working effect (white→navy breathing)
- OLED: two-line display — session token totals (top) + current tool/state (bottom)
- Full replacement of Moonside code (not coexistence)
- Never block Claude Code (hooks always exit 0)

## Non-Goals

- Direct USB HID (fragile, model-specific)
- Per-frame custom animation engine (GameSense built-ins are sufficient)
- Windows support (macOS first, same coreProps.json mechanism applies)
- Moonside backwards compatibility

## Requirements

- macOS
- Python 3.10+
- SteelSeries GG installed and running
- SteelSeries Apex Pro / Apex Pro TKL / Apex 7 (OLED + per-key RGB)

## Architecture

```
Claude Code hook event (stdin JSON)
  → steelseries_hook.sh
      Parses stdin JSON (python3 -c, no jq dependency)
      Extracts: tool_name, transcript_path
      Writes:
        /tmp/ss_state       → working | idle | input | off
        /tmp/ss_tool        → current tool name (e.g. "Edit", "Bash")
        /tmp/ss_transcript  → path to JSONL session transcript
      Launches daemon if not running. Always exits 0.

    → steelseries_daemon.py
        On startup:
          Read coreProps.json → discover GameSense HTTP port
          POST /game_metadata  → register "CLAUDE_CODE" game
          POST /bind_game_event → register SS_RGB handler (gradient/frequency)
          POST /bind_game_event → register SS_OLED handler (text frame)
        Loop every 200ms:
          Read /tmp/ss_state → compute SS_RGB event value
          Read /tmp/ss_tool  → OLED bottom line
          Read /tmp/ss_transcript → parse latest usage → OLED top line
          POST /game_event SS_RGB  (only on state change)
          POST /game_event SS_OLED (every tick, content may differ)
        On shutdown:
          POST /remove_game
          Exit
```

## Files

| File | Purpose |
|---|---|
| `claude_hooks/steelseries_hook.sh` | Shell hook. Parses stdin JSON, writes /tmp files, launches daemon. |
| `claude_hooks/steelseries_daemon.py` | GameSense daemon. Registers game, drives RGB + OLED. |
| `claude_hooks/settings.json` | Claude Code hooks config, paths updated to `steelseries_*`. |

`moonside_daemon.py` and `moonside_hook.sh` are deleted.

## GameSense Integration

### Port Discovery

```
/Library/Application Support/SteelSeries Engine 3/coreProps.json
→ { "address": "127.0.0.1:PORT" }
```

If file missing or unreadable, daemon logs and exits (GG not running). Hook still exits 0.

### Game Registration

```
POST http://<address>/game_metadata
{
  "game": "CLAUDE_CODE",
  "game_display_name": "Claude Code",
  "developer": "semvis123"
}
```

### RGB Handler (SS_RGB event)

Registered once on startup. Value range 0–100 mapped to state:

| Value | State | Effect |
|---|---|---|
| 0 | off | Solid black (all keys) |
| 25 | idle | Solid sunset mango — (255, 180, 50) |
| 50 | input | Solid purple (200, 0, 255), pulse 0.5 Hz |
| 100 | working | White → navy breathing gradient, 1 Hz, repeat_limit 0 |

Zone: `all` (full keyboard). GameSense handles animation — no per-frame pushing.

### OLED Handler (SS_OLED event)

Separate event, value always 0, frame content updated dynamically each tick. Two-line text:

```
▲ 12.4k  ▼ 3.2k
⚙ Edit
```

- Line 1: cumulative session input / output token counts
- Line 2: current tool name (truncated to ~16 chars), or state label when idle/input/off

Token counts: parsed by summing `usage.input_tokens` and `usage.output_tokens` from all assistant messages in the transcript JSONL. Cached between reads, re-parsed on file modification. Reset automatically when `ss_transcript` changes path (new session = new transcript file — no extra SessionStart signal needed).

## State Machine

| Claude Code event | State written |
|---|---|
| SessionStart | idle |
| UserPromptSubmit | working |
| PreToolUse (most tools) | working |
| PreToolUse: AskUserQuestion | input |
| PreToolUse: ExitPlanMode | input |
| PostToolUse: AskUserQuestion | input |
| PermissionRequest | input |
| Notification: permission_prompt / elicitation_dialog | input |
| Notification: idle_prompt | idle |
| Stop | idle |
| SessionEnd | off |

Debounce: working state transitions are subject to a 500ms debounce to ignore phantom PreToolUse events that fire immediately after Stop (same logic as Moonside daemon).

## Daemon Lifecycle

| File | Purpose |
|---|---|
| `/tmp/ss_daemon.lock` | fcntl exclusive lock — ensures single instance |
| `/tmp/ss_daemon.pid` | PID for `kill` in troubleshooting |
| `/tmp/ss_state` | Current desired state |
| `/tmp/ss_tool` | Most recent tool name |
| `/tmp/ss_transcript` | Path to current session transcript JSONL |
| `/tmp/ss_daemon.log` | Structured log output |

Idle timeout: 30 minutes in idle state → daemon sends off, unregisters game, exits.

## Error Handling

| Failure | Behavior |
|---|---|
| GG not running (coreProps.json missing) | Daemon logs error, exits cleanly. Hook exits 0. |
| GameSense HTTP error | Log + retry next tick. No crash. |
| Transcript unreadable / malformed | OLED shows `-- / --` for tokens. No crash. |
| Daemon crash | Next hook invocation restarts it. |
| Multiple hook invocations | Lock file prevents duplicate daemons. |

## RGB Color Reference

All colors configured in a single dict at top of daemon file for easy customization.

```python
COLORS = {
    "idle":    (255, 180,  50),   # sunset mango
    "input":   (200,   0, 255),   # purple
    "working_a": (255, 255, 255), # white  (gradient start)
    "working_b": (  0,   0, 140), # navy   (gradient end)
    "off":     (  0,   0,   0),
}
```

## Project Rename

- Repo name: `claude-steelseries` (already set)
- README rewritten for SteelSeries setup
- All `moonside_*` references removed
