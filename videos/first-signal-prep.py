"""Pre-compute a week of real ccvitals outputs (replay clock) for the video."""
import json, os, shutil, subprocess, sys
from datetime import timedelta
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from test_replay import stable_week, T0

FS = Path(sys.argv[1])
home = FS / "home"
shutil.rmtree(home, ignore_errors=True); home.mkdir(parents=True)
readings = list(stable_week(weekly_shift=18.0, shift_day=5))
shift_idx = next(i for i, r in enumerate(readings) if r[0] >= T0 + timedelta(days=5))

scenes = {
    0:  "DAY 1, 9:00 AM — first reading. ccvitals starts learning your baseline.",
    5:  "DAY 1, evening — still collecting…",
    20: "DAY 3 — baseline established. The bar stays clean while everything is normal.",
    40: "DAY 5 — still quiet. The 5h window sawtoothed all week: zero false alarms.",
    shift_idx:     "DAY 6, 9:00 AM — your ceiling just shifted. Odd readings arrive: ccvitals stays quiet (debounce)…",
    shift_idx + 1: "DAY 6, 9:30 AM — three consecutive confirmations later:",
}
# scenes needing a hidden warm-up reading 4min prior (kills replay-artifact
# idle warnings; uses the same weekly value so debounce counting stays honest)
warm = {20, 40, shift_idx, shift_idx + 1}

def send(ts, mid, name, s5, w7):
    payload = {"model": {"id": mid, "display_name": name}, "session_id": "wk",
        "rate_limits": {
            "five_hour": {"used_percentage": round(s5,1), "resets_at": int((ts+timedelta(hours=2)).timestamp())},
            "seven_day": {"used_percentage": round(w7,1), "resets_at": int((ts+timedelta(days=2)).timestamp())}},
        "context_window": {"used_percentage": 30, "context_window_size": 200000},
        "cost": {"total_cost_usd": 1.0}}
    env = dict(os.environ); env["HOME"] = str(home)
    env["CCVITALS_FAKE_NOW"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    p = subprocess.run([sys.executable, "-m", "claude_code_vitals", "run"],
        input=json.dumps(payload), capture_output=True, text=True, cwd=REPO, env=env)
    return p.stdout.rstrip("\n")

outs = {}
for i, (ts, mid, name, s5, w7) in enumerate(readings):
    if i in warm:
        send(ts - timedelta(minutes=4), mid, name, max(s5 - 1, 0), w7)
    out = send(ts, mid, name, s5, w7)
    if i in scenes:
        outs[i] = out

play = ["#!/bin/sh"]
for i in sorted(scenes):
    play.append(f'printf "\\033[2m%s\\033[0m\\n" "{scenes[i]}"')
    body = outs[i].replace("\\", "\\\\").replace("'", "'\\''")
    play.append(f"printf '%s\\n' '{body}'")
    play.append("echo ''")
    play.append("sleep 4")
play.append('printf "\\033[1mccvitals — know your limits before they know you\\033[0m\\n"')
play.append('printf "\\033[2msimulated readings, real pipeline  |  pipx install claude-code-vitals\\033[0m\\n"')
play.append("sleep 5")
(FS / "play.sh").write_text("\n".join(play) + "\n")
print("prep done")
