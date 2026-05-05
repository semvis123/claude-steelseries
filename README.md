# Claude SteelSeries

SteelSeries GameSense integration for Claude Code — displays Claude's state on Apex keyboards using RGB backlighting and the OLED display.

WARNING: Run these scripts at your own risk. They interact with SteelSeries GameSense over HTTP and write transient files in /tmp.

## Features

- Per-key RGB bitmap with a subtle horizontal brightness wave; one base color per state (idle/input/working/compacting)
- Top-row context indicator: first 16 keys (Esc, F1–F12, PrtSc, ScrLk, Pause) light up solid green proportional to the latest assistant message's context size, scaled against 500k tokens
- OLED two-line display:
  - top: latest session input / output token totals (▲input ▼output, with k/M suffixes)
  - bottom: current tool + file/label (e.g. `Edit steelseries_daemon.py`, `Bash grep`, `Read README.md`) or the current state
- Sticky tool dwell (1.5s) so fast tools like Read/Edit remain visible on the OLED after PostToolUse clears
- `PreCompact` hook switches to a `compacting` state with a cyan wave and `compacting...` on the OLED
- Non-blocking Claude Code hook (always exits 0)
- macOS-first (uses SteelSeries GG `coreProps.json` to find the GameSense port)

## Requirements

- macOS
- Python 3.10+
- SteelSeries GG (SteelSeries Engine) installed and running
- SteelSeries keyboard with per-key RGB and OLED (developed against Apex Pro: 132 LEDs, 22×6 row-major bitmap). Other models with `rgb-per-key-zones` and `screened-2-lines` should work but have not been tested.

## Installation

1. Copy hooks to a global location (recommended):

```sh
mkdir -p ~/.claude/hooks
cp claude_hooks/steelseries_hook.sh claude_hooks/steelseries_daemon.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/steelseries_hook.sh
```

2. Merge hooks config into your Claude settings (update paths):

```sh
# Preview updated config (replace $CLAUDE_PROJECT_DIR paths)
sed 's|\$CLAUDE_PROJECT_DIR/claude_hooks|~/.claude/hooks|g' claude_hooks/settings.json > ~/.claude/settings.json
```

3. Restart Claude Code (or open a new session). Hooks will invoke the shell script which starts the daemon as-needed.

## Testing and verification

Prerequisite: SteelSeries GG must be running. Confirm coreProps.json exists:

```sh
cat "/Library/Application Support/SteelSeries Engine 3/coreProps.json"
```

Run the daemon manually to watch logs:

```sh
python3 ~/.claude/hooks/steelseries_daemon.py &
tail -f /tmp/ss_daemon.log
```

Simulate a hook invocation (pipe JSON to the hook):

```sh
echo '{"tool_name": "Edit", "tool_input": {"file_path": "/tmp/foo.py"}, "transcript_path": "/tmp/test_transcript.jsonl"}' \
  | bash ~/.claude/hooks/steelseries_hook.sh working
# Verify transient files
cat /tmp/ss_state        # → working
cat /tmp/ss_tool         # → Edit
cat /tmp/ss_tool_label   # → foo.py
cat /tmp/ss_transcript   # → /tmp/test_transcript.jsonl
```

Check daemon log for GameSense registration and posted events. You can also verify GameSense HTTP is reachable by reading coreProps.json (address like 127.0.0.1:PORT) and curling the metadata endpoint:

```sh
addr=$(jq -r '.address' "/Library/Application Support/SteelSeries Engine 3/coreProps.json")
curl -s "http://$addr/game_metadata"
```

Validate OLED content: daemon posts SS_OLED events each tick. GameSense handles rendering; if keyboard OLED doesn't update, ensure SteelSeries GG is running and the keyboard model supports GameSense.

## Uninstall / cleanup

To stop and remove the integration:

```sh
# kill daemon if running
kill "$(cat /tmp/ss_daemon.pid 2>/dev/null)" 2>/dev/null || true
rm -f /tmp/ss_daemon.pid /tmp/ss_daemon.lock /tmp/ss_state /tmp/ss_tool /tmp/ss_tool_label /tmp/ss_transcript /tmp/ss_daemon.log
# remove hooks from ~/.claude/settings.json or restore previous config
```

## Troubleshooting

- coreProps.json missing → SteelSeries GG not running. Start SteelSeries GG and try again.
- Daemon log: /tmp/ss_daemon.log
- PID file: /tmp/ss_daemon.pid
- If GameSense HTTP calls fail, daemon will retry on next tick. Check that the address in coreProps.json is reachable.
- Transcript parsing errors: OLED will show `-- / --`. Ensure transcript JSONL path is correct and readable.

## Development notes

- Per-state base color and wave parameters live in `claude_hooks/steelseries_daemon.py` (`COLORS` dict and `gen_wave_bitmap`).
- Bitmap layout for the Apex Pro is 22 columns × 6 rows, row-major (`i = row*22 + col`). The horizontal wave keys phase off the column index.
- Top-row context indicator: `CONTEXT_INDICATOR_KEYS = 16`, `CONTEXT_FULL = 500_000`. Tweak `CONTEXT_FULL` to change the scale or `CONTEXT_GREEN` to change the color.
- Tick rate: 50ms. Sticky tool dwell: 1.5s. Daemon debounce on working transitions: 500ms. Idle timeout: 30 minutes.
- The hook script writes state into `/tmp/ss_state`, the tool name into `/tmp/ss_tool`, a tool-specific label (file basename, command head, pattern, …) into `/tmp/ss_tool_label`, and the transcript path into `/tmp/ss_transcript`. The daemon polls these on every tick.

### Hook events used

| Hook | Argument written to `/tmp/ss_state` |
|---|---|
| `SessionStart`, `Stop`, `Notification:idle_prompt` | `idle` |
| `UserPromptSubmit`, `PreToolUse` (most tools) | `working` |
| `PreToolUse:AskUserQuestion`, `PreToolUse:ExitPlanMode`, `PermissionRequest`, `Notification:permission_prompt` | `input` |
| `PreCompact` | `compacting` |
| `SessionEnd` | `off` |

There is no `PostCompact` event — the `compacting` state clears naturally on the next `UserPromptSubmit` / `Stop` / `SessionStart` after compaction completes.

## License

MIT
