# CLAUDE.md — claude-code-vitals

## What This Project Is

**claude-code-vitals** -- passive rate-limit drift detector for Claude Code.
Tagline: "Know your limits before they know you."

Reads rate limit utilization % from Claude Code's statusLine stdin JSON, logs it to `~/.claude-code-vitals/history.jsonl`, detects drift vs. rolling median, and surfaces one of three signals in the status bar: **DOWN / NORMAL / UP**.

**Zero external dependencies. Zero token cost. Purely passive.**

---

## Module Map

| Module | Purpose |
|--------|---------|
| `claude_code_vitals/config.py` | Custom TOML parser, dataclass configs, no deps |
| `claude_code_vitals/logger.py` | Stdin JSON parsing, JSONL append, debounced writes |
| `claude_code_vitals/detector.py` | Rolling median baseline, 3-signal drift, debounce, time-of-day patterns, burn rate, prompt delta, peak detection, cache health, hourly comparison, anomaly detection |
| `claude_code_vitals/renderer.py` | Compact + expanded ANSI status bar views, color-coded %, prompt delta, peak indicator, cache display, compact warning, idle warning, hourly multiplier |
| `claude_code_vitals/explain.py` | Explain subtopics: cache, compact, peak, models |
| `claude_code_vitals/oauth.py` | OAuth `/usage` client, caching, rate limiting (built, not yet wired) |
| `claude_code_vitals/init_cmd.py` | Auto-configures Claude Code settings.json, wrapper for existing statuslines |
| `claude_code_vitals/__main__.py` | CLI entry point: all subcommands |
| `tests/test_core.py` | Config, logger, detector, renderer tests |
| `install.sh` | One-liner curl installer |
| `pyproject.toml` | PyPI packaging with entry points |

---

## Architecture

```
Claude Code -> stdin JSON -> python3 -m claude_code_vitals run
                              |-> logger.py      -> ~/.claude-code-vitals/history.jsonl
                              |-> detector.py    -> rolling median + drift signal
                              |-> renderer.py    -> ANSI status bar output (stdout)
                              \-> oauth.py       -> /api/oauth/usage (NOT YET WIRED)
```

**Key algorithm files:**
- `detector.py:detect_drift()` -- loads history, computes rolling median, applies threshold, debounces, emits signal
- `logger.py:should_log()` -- debounce writes, max 1x/5min if values unchanged
- `init_cmd.py:_configure_statusline()` -- handles users with existing custom statuslines via wrapper script

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `ccvitals run` | Main statusline pipeline (stdin JSON -> log -> detect -> render) |
| `ccvitals init` | Auto-configure Claude Code settings.json |
| `ccvitals status [--session]` | Show current drift state and recent readings |
| `ccvitals report` | Generate HTML usage report |
| `ccvitals compare [--global]` | Usage trending (default: --session) |
| `ccvitals budget [--global]` | Show remaining time per model (default: --session) |
| `ccvitals suggest [--session]` | Ranked model availability with burn rates (default: --global) |
| `ccvitals baseline [--session]` | Show rolling median baseline (default: --global) |
| `ccvitals explain cache\|compact\|peak\|models` | Subtopic guides |
| `ccvitals privacy` | Show what data is stored and where |
| `ccvitals uninstall` | Remove all claude-code-vitals data and config |
| `ccvitals config set\|list` | View/update configuration |

---

## Design Decisions -- DO NOT CHANGE

1. **Zero external dependencies** -- No `requests`, no `toml`, no `click`, no `rich`. Pure stdlib.
2. **JSONL over SQLite** -- Human-readable, grep-able, `tail -f` friendly. SQLite adds a C dependency.
3. **Rolling MEDIAN over mean** -- Resistant to outliers. One spike doesn't shift the baseline.
4. **Utilization % as signal, not raw TPM/RPM** -- We track %, not ceilings. Utilization changes reflect either consumption changes or external ceiling shifts; the tool surfaces the delta without attributing cause.
5. **Debounce via `~/.claude-code-vitals/state.json`** -- Persistent across invocations. Default: 3 consecutive readings past threshold to trigger state change.
6. **No canary prompts** -- Zero synthetic requests. Purely passive observation of already-flowing data.
7. **Wrapper script pattern** -- `ccvitals init` wraps existing statuslines so both run.

---

## Testing

```bash
# Run test suite
python3 tests/test_core.py

# E2E pipeline test
echo '{"model":{"id":"claude-opus-4-6","display_name":"Opus 4.6"},"rate_limits":{"session":{"used_percentage":42.0,"resets_at":"2026-03-30T19:00:00Z"},"weekly":{"used_percentage":67.0,"resets_at":"2026-04-05T08:00:00Z"}},"context_window":{"used_percentage":12,"context_window_size":200000},"cost":{"total_cost_usd":0.80}}' | python3 -m claude_code_vitals run
```

Rules:
- Run tests before AND after every change.
- `tempfile.TemporaryDirectory()` for test isolation -- never touch real `~/.claude-code-vitals/`.
- Add a test for every bug fix and new feature.

---

## Code Style

- Type hints on all function signatures.
- Google-style docstrings on all public functions.
- No classes where a function will do; dataclasses for structured data.
- Catch specific exceptions, never bare `except:`.
- All file I/O uses `pathlib.Path`.

---

## Git Conventions

- Prefix: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- One logical change per commit.
- Never commit `history.jsonl`, `state.json`, or any user data.

---

## Known Issues

- `rate_limits` field in statusline JSON was recently added to Claude Code -- absent in older versions. Code handles this gracefully (shows "waiting for data").
- OAuth endpoint is not yet wired into the run pipeline. The tool works fully without it.
- Windows: untested. Bash scripts only.
