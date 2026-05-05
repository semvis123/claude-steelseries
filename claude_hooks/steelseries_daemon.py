#!/usr/bin/env python3
"""SteelSeries GameSense daemon for Claude Code
Registers CLAUDE_CODE game and posts SS_RGB and SS_OLED events.
"""
import fcntl
import json
import math
import os
import sys
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path

CORE_PROPS = Path("/Library/Application Support/SteelSeries Engine 3/coreProps.json")
LOCK_PATH = Path("/tmp/ss_daemon.lock")
PID_PATH = Path("/tmp/ss_daemon.pid")
LOG_PATH = Path("/tmp/ss_daemon.log")
STATE_PATH = Path("/tmp/ss_state")
TOOL_PATH = Path("/tmp/ss_tool")
TOOL_LABEL_PATH = Path("/tmp/ss_tool_label")
TRANSCRIPT_PATH = Path("/tmp/ss_transcript")

TICK = 0.05  # 50ms
IDLE_TIMEOUT_SECONDS = 30 * 60  # 30 minutes
DEBOUNCE_SECONDS = 0.5
TOOL_STICKY_SECONDS = 1.5  # how long to keep a tool name visible after it clears
NUM_LEDS = 132  # Apex Pro full keyboard (22 cols x 6 rows, row-major: i = row*22 + col)
GRID_COLS = 22
GRID_ROWS = 6
WAVE_SPEED_HZ = 0.4   # how fast the wave travels left→right
WAVE_SPREAD = 0.35    # phase delta per column (controls wavelength)

# Top-row context indicator: 16 keys (Esc, F1-F12, PrtSc, ScrLk, Pause)
# light up green proportionally to context_size / CONTEXT_FULL.
CONTEXT_INDICATOR_KEYS = 16
CONTEXT_FULL = 500_000  # 500k tokens = full row
CONTEXT_GREEN = (0, 220, 80)

COLORS = {
    "idle":    (255, 180,  50),   # sunset mango
    "input":   (200,   0, 255),   # purple
    "working_a": (255, 255, 255), # white
    "working_b": (  0,   0, 140), # navy
    "compacting": (  0, 200, 220),# cyan
    "off":     (  0,   0,   0),
}

GAME_NAME = "CLAUDE_CODE"
DEVELOPER = "semvis123"


def log(*args, **kwargs):
    try:
        ts = time.strftime('%Y-%m-%dT%H:%M:%S')
        with LOG_PATH.open('a') as f:
            print(ts, *args, **kwargs, file=f)
    except Exception:
        pass


class SingleInstance:
    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, 'w')
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self.fd

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.fd:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                self.fd.close()
                # keep the lock file present as a marker; do not remove
        except Exception:
            pass


class GameSenseClient:
    def __init__(self, address):
        self.address = address.rstrip('/')

    def post(self, path, payload):
        url = f"http://{self.address}{path}"
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={
            'Content-Type': 'application/json'
        })
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                body = resp.read().decode('utf-8')
                try:
                    status = resp.getcode()
                except Exception:
                    status = None
                log('POST', path, 'payload', payload, 'status', status, 'response', body)
                return body
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode('utf-8')
            except Exception:
                body = str(e)
            log('HTTP error', path, e, 'body', body)
            raise
        except Exception as e:
            log('HTTP error', path, e)
            raise

    def register_game(self):
        payload = {
            'game': GAME_NAME,
            'game_display_name': 'Claude Code',
            'developer': DEVELOPER
        }
        try:
            self.post('/game_metadata', payload)
            log('Registered game')
        except Exception:
            log('Failed registering game')

    def bind_rgb_event(self):
        # Per-key bitmap. The daemon computes a wave each tick and sends it
        # via SS_RGB events as {bitmap: [[r,g,b], ...]}.
        payload = {
            'game': GAME_NAME,
            'event': 'SS_RGB',
            'value_optional': True,
            'handlers': [
                {
                    'device-type': 'rgb-per-key-zones',
                    'zone': 'all',
                    'mode': 'bitmap'
                }
            ]
        }
        try:
            self.post('/bind_game_event', payload)
            log('Bound SS_RGB (bitmap)')
        except Exception:
            log('Failed binding SS_RGB')

    def bind_oled_event(self):
        # Single bind call with data_fields declared so SteelSeries GG
        # categorizes the event as a screen event in the OLED tab.
        payload = {
            'game': GAME_NAME,
            'event': 'SS_OLED',
            'min_value': 0,
            'max_value': 100,
            'value_optional': True,
            'data_fields': [
                {'context-frame-key': 'top', 'localized-label': 'Top'},
                {'context-frame-key': 'bottom', 'localized-label': 'Bottom'}
            ],
            'handlers': [
                {
                    'device-type': 'screened-2-lines',
                    'mode': 'screen',
                    'zone': 'one',
                    'datas': [
                        {
                            'lines': [
                                {'has-text': True, 'context-frame-key': 'top'},
                                {'has-text': True, 'context-frame-key': 'bottom'}
                            ]
                        }
                    ]
                }
            ]
        }
        try:
            self.post('/bind_game_event', payload)
            log('Bound SS_OLED')
        except Exception:
            log('Failed binding SS_OLED')

    def post_event(self, event, value):
        payload = {'game': GAME_NAME, 'event': event}
        # Accept either scalar values (rgb) or dict payloads (oled frames/data)
        if isinstance(value, dict):
            payload['data'] = value
        else:
            payload['data'] = {'value': value}
        try:
            self.post('/game_event', payload)
        except Exception as e:
            log('Failed posting event', event, e)

    def remove_game(self):
        try:
            self.post('/remove_game', {'game': GAME_NAME})
            log('Removed game')
        except Exception:
            pass


class TokenParser:
    def __init__(self):
        self.cached_path = None
        self.cached_mtime = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_context_size = 0  # input + cache of the latest assistant msg

    def parse(self, path):
        if not path:
            return (None, None)
        try:
            p = Path(path)
            mtime = p.stat().st_mtime
            if self.cached_path == path and mtime == self.cached_mtime:
                return (self.input_tokens, self.output_tokens)
            # re-parse
            input_sum = 0
            output_sum = 0
            last_ctx = 0
            with p.open('r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    # look for assistant messages with usage
                    # Claude Code transcript: usage is nested at obj.message.usage;
                    # also count cache_read + cache_creation as input tokens.
                    try:
                        if obj.get('type') == 'assistant' or obj.get('role') == 'assistant':
                            msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
                            usage = msg.get('usage', {}) if isinstance(msg, dict) else {}
                            if not usage:
                                usage = obj.get('usage', {}) or {}
                            inp = int(usage.get('input_tokens', 0) or 0)
                            cache_r = int(usage.get('cache_read_input_tokens', 0) or 0)
                            cache_c = int(usage.get('cache_creation_input_tokens', 0) or 0)
                            input_sum += inp + cache_r + cache_c
                            output_sum += int(usage.get('output_tokens', 0) or 0)
                            last_ctx = inp + cache_r + cache_c
                    except Exception:
                        continue
            self.cached_path = path
            self.cached_mtime = mtime
            self.input_tokens = input_sum
            self.output_tokens = output_sum
            self.last_context_size = last_ctx
            return (input_sum, output_sum)
        except Exception:
            return (None, None)


def discover_address():
    try:
        with CORE_PROPS.open('r', encoding='utf-8') as f:
            d = json.load(f)
            addr = d.get('address')
            if addr:
                # address may be "127.0.0.1:PORT"
                return addr
    except Exception as e:
        log('coreProps not readable', e)
    return None


def gen_wave_bitmap(state, t, context_size=0):
    """Return [[r,g,b], ...] of length NUM_LEDS for the given state at time t.
    Bitmap is column-major (i = col*GRID_ROWS + row) so phase keyed off col
    produces a horizontal wave traveling across the keyboard.
    context_size drives the green top-row indicator overlay."""
    if state == 'off':
        return [[0, 0, 0]] * NUM_LEDS
    # Per-state base color and wave parameters; brightness modulation only.
    if state == 'idle':
        base = COLORS['idle']
        speed, spread, b_min, b_max = 0.2, 0.20, 0.85, 1.0
    elif state == 'input':
        base = COLORS['input']
        speed, spread, b_min, b_max = 0.5, 0.30, 0.55, 1.0
    elif state == 'compacting':
        base = COLORS['compacting']
        speed, spread, b_min, b_max = 1.0, 0.45, 0.45, 1.0
    else:  # working
        base = COLORS['working_a']
        speed, spread, b_min, b_max = WAVE_SPEED_HZ, WAVE_SPREAD, 0.65, 1.0
    bitmap = []
    span = b_max - b_min
    for i in range(NUM_LEDS):
        col = i % GRID_COLS  # row-major: column = i mod 22
        phase = (t * speed * 2 * math.pi) - (col * spread)
        b = b_min + span * (0.5 + 0.5 * math.sin(phase))
        bitmap.append([int(base[0] * b), int(base[1] * b), int(base[2] * b)])
    apply_context_indicator(bitmap, context_size)
    return bitmap


def apply_context_indicator(bitmap, context_size):
    """Overlay the top-row context indicator. First N keys of row 0 (indices
    0..15) turn solid green based on context_size / CONTEXT_FULL; remaining
    keys keep the wave animation."""
    if not context_size or context_size <= 0:
        return
    ratio = min(context_size / CONTEXT_FULL, 1.0)
    filled = int(round(ratio * CONTEXT_INDICATOR_KEYS))
    for col in range(filled):
        bitmap[col] = list(CONTEXT_GREEN)  # row-major: row 0, col c → index c


def fmt_tokens(n):
    if n is None:
        return '--'
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def read_file_text(path):
    try:
        return Path(path).read_text(encoding='utf-8').strip()
    except Exception:
        return ''


def main_loop():
    address = discover_address()
    if not address:
        log('GameSense address not found; exiting')
        return
    client = GameSenseClient(address)
    client.register_game()
    client.bind_rgb_event()
    client.bind_oled_event()

    token_parser = TokenParser()

    last_state = None
    candidate_state = None
    candidate_since = 0
    last_activity_time = time.time()
    sticky_tool = ''
    sticky_tool_label = ''
    sticky_tool_seen = 0

    try:
        while True:
            state = read_file_text(STATE_PATH) or 'idle'
            tool = read_file_text(TOOL_PATH) or ''
            tool_label = read_file_text(TOOL_LABEL_PATH) or ''
            transcript = read_file_text(TRANSCRIPT_PATH) or ''

            now = time.time()

            # Debounce working transitions for state tracking only — RGB now
            # uses a per-tick bitmap so we don't need to post on transitions.
            if state == 'working' and last_state != 'working':
                if candidate_state != 'working':
                    candidate_state = 'working'
                    candidate_since = now
                elif now - candidate_since >= DEBOUNCE_SECONDS:
                    last_state = 'working'
                    last_activity_time = now
            elif state != 'working' and last_state == 'working' and state != last_state:
                last_state = state
                last_activity_time = now
            elif last_state is None:
                last_state = state

            # Parse tokens first so the RGB indicator can use the latest context size.
            input_tokens, output_tokens = token_parser.parse(transcript)
            if input_tokens is None or output_tokens is None:
                top_line = ' -- / -- '
            else:
                top_line = f"▲{fmt_tokens(input_tokens)} ▼{fmt_tokens(output_tokens)}"

            effective_state = last_state or state
            try:
                bitmap = gen_wave_bitmap(effective_state, now, token_parser.last_context_size)
                client.post_event('SS_RGB', {'value': 0, 'frame': {'bitmap': bitmap}})
            except Exception:
                pass

            # Sticky tool: hold the most recent tool name for TOOL_STICKY_SECONDS
            # so fast tools (Edit/Read) remain visible even after PostToolUse clears.
            if tool:
                sticky_tool = tool
                sticky_tool_label = tool_label
                sticky_tool_seen = now
            display_tool = sticky_tool if (now - sticky_tool_seen) < TOOL_STICKY_SECONDS else ''
            display_label = sticky_tool_label if display_tool else ''
            # Drop sticky tool when state implies no tool activity (idle/input/off/compacting)
            if state in ('idle', 'input', 'off', 'compacting'):
                display_tool = ''
                display_label = ''
                sticky_tool = ''
                sticky_tool_label = ''
            if state == 'compacting':
                bottom_line = 'compacting...'
            elif display_tool:
                bottom_line = f"{display_tool} {display_label}".rstrip()
            else:
                bottom_line = state
            oled_data = {'value': 0, 'frame': {'top': top_line, 'bottom': bottom_line}}
            try:
                client.post_event('SS_OLED', oled_data)
            except Exception:
                pass

            # Idle timeout
            if last_state == 'idle' and (now - last_activity_time) > IDLE_TIMEOUT_SECONDS:
                log('Idle timeout; removing game and exiting')
                client.remove_game()
                return

            time.sleep(TICK)
    except KeyboardInterrupt:
        client.remove_game()
    except Exception as e:
        log('Daemon error', e)


if __name__ == '__main__':
    # write pid
    try:
        PID_PATH.write_text(str(os.getpid()))
    except Exception:
        pass
    # acquire exclusive lock
    try:
        with SingleInstance(str(LOCK_PATH)):
            main_loop()
    except Exception as e:
        log('Could not acquire lock or run main loop', e)

