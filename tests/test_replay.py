"""Sawtooth replay harness — end-to-end false-positive/true-positive audit.

Drives the REAL CLI pipeline (``python -m claude_code_vitals run``) through a
realistic multi-day trace using two test hooks:
  - HOME is pointed at a throwaway directory (isolated data dir)
  - CCVITALS_FAKE_NOW pins the clock for each replayed reading

Scenarios:
  A. Stable week      — 5h window sawtooths hard, weekly drifts gently.
                        Invariant: ZERO SPIKE/DROP signals ever fire.
  B. Ceiling shift up — from day 6, weekly utilization steps +18 and stays.
                        Invariant: SPIKE fires within `debounce` readings of
                        the shift and persists to the end of the trace.
  C. Ceiling drop     — from day 6, weekly steps -18 and stays.
                        Invariant: DROP fires within `debounce` readings.
  D. Day-1 experience — during the first 10 readings the bar must show
                        COLLECTING and never a SPIKE/DROP (folded into A).
  E. Multi-model week — Opus/Sonnet alternate blocks (B4): no false signals,
                        and `suggest` afterwards ranks Sonnet's burn below
                        Opus's.

Run standalone:  python3 tests/test_replay.py
(Not part of test_core.py's default run — takes ~15-30s of subprocess calls.)
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).parent.parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Trace starts 8 days before the real now so history ages are realistic.
T0 = (datetime.now(timezone.utc) - timedelta(days=8)).replace(
    minute=0, second=0, microsecond=0
)


def run_reading(home: Path, ts: datetime, model_id: str, model_name: str,
                s5: float, w7: float, ctx: int = 30, session: str = "replay") -> str:
    """Pipe one statusline JSON through the real pipeline at a pinned clock."""
    payload = {
        "model": {"id": model_id, "display_name": model_name},
        "session_id": session,
        "rate_limits": {
            "five_hour": {
                "used_percentage": round(s5, 1),
                "resets_at": int((ts + timedelta(hours=2)).timestamp()),
            },
            "seven_day": {
                "used_percentage": round(w7, 1),
                "resets_at": int((ts + timedelta(days=2)).timestamp()),
            },
        },
        "context_window": {"used_percentage": ctx, "context_window_size": 200000},
        "cost": {"total_cost_usd": 1.0},
    }
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CCVITALS_FAKE_NOW"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    proc = subprocess.run(
        [sys.executable, "-m", "claude_code_vitals", "run"],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=REPO, env=env, timeout=30,
    )
    assert proc.returncode == 0, f"run crashed at {ts}: {proc.stderr[-400:]}"
    return ANSI.sub("", proc.stdout)


def stable_week(weekly_shift: float = 0.0, shift_day: int = 5,
                multi_model: bool = False):
    """Yield (ts, model_id, model_name, s5, w7) for a realistic 7-day trace.

    Two 2.5h work blocks/day (morning + evening), readings every 30min.
    The 5h % climbs steeply within each block and RESETS between blocks
    (the sawtooth). Weekly climbs slowly through the week, dips after the
    weekly reset, and oscillates in a ~6-point band — below the 10-point
    signal threshold unless `weekly_shift` is applied from `shift_day` on.
    """
    for day in range(7):
        for block, start_h in enumerate((9, 19)):  # 9am / 7pm blocks
            for i in range(5):  # 5 readings per block, 30min apart
                ts = T0 + timedelta(days=day, hours=start_h, minutes=30 * i)
                # sawtooth: fresh window each block, steep climb
                s5 = 5 + i * 12                        # 5 -> 53 within block
                # weekly: slow drift up through the week + small wiggle
                w7 = 38 + day * 0.8 + (i % 3)          # ~38..46 band
                if day >= shift_day:
                    w7 += weekly_shift
                if multi_model and block == 1:
                    # evening block on Sonnet: shallower 5h climb
                    yield ts, "claude-sonnet-4-6", "Sonnet 4.6", 5 + i * 3, w7
                else:
                    yield ts, "claude-opus-4-6", "Opus 4.6", s5, w7


def replay(readings, home: Path):
    """Run every reading; return list of (ts, cleaned_output)."""
    out = []
    for ts, mid, name, s5, w7 in readings:
        out.append((ts, run_reading(home, ts, mid, name, s5, w7)))
    return out


def signals_in(outputs):
    spikes = [(ts, o) for ts, o in outputs if "USAGE SPIKE" in o]
    drops = [(ts, o) for ts, o in outputs if "USAGE DROP" in o]
    return spikes, drops


def scenario_a_stable():
    print("Scenario A+D: stable sawtooth week — expecting ZERO signals")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        outputs = replay(stable_week(), home)
        spikes, drops = signals_in(outputs)
        assert not spikes, f"FALSE POSITIVE spike at {spikes[0][0]}:\n{spikes[0][1]}"
        assert not drops, f"FALSE POSITIVE drop at {drops[0][0]}:\n{drops[0][1]}"
        # D: readings 1-9 must be COLLECTING (each run logs BEFORE detecting,
        # so reading #10 already sees 10 points and exits the collecting phase)
        first9 = outputs[:9]
        assert all("COLLECTING" in o for _, o in first9), "day-1 must show COLLECTING"
        assert all("COLLECTING" not in o for _, o in outputs[10:]), \
            "COLLECTING must clear once the baseline exists"
    print(f"  ✓ {len(outputs)} readings, 0 false signals; day-1 COLLECTING correct")


def scenario_b_spike():
    print("Scenario B: +18 weekly step from day 6 — expecting SPIKE within debounce")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        readings = list(stable_week(weekly_shift=18.0, shift_day=5))
        outputs = replay(readings, home)
        shift_idx = next(i for i, r in enumerate(readings)
                         if r[0] >= T0 + timedelta(days=5))
        pre, post = outputs[:shift_idx], outputs[shift_idx:]
        assert not any("USAGE SPIKE" in o for _, o in pre), "spike before the shift"
        fired_at = next((i for i, (_, o) in enumerate(post) if "USAGE SPIKE" in o), None)
        assert fired_at is not None, "spike never fired after a +18 sustained shift"
        assert fired_at <= 3, f"spike too slow: fired {fired_at+1} readings after shift"
        tail = post[fired_at:]
        assert all("USAGE SPIKE" in o for _, o in tail), "spike did not persist"
    print(f"  ✓ spike fired {fired_at+1} readings after the shift and persisted")


def scenario_c_drop():
    print("Scenario C: -18 weekly step from day 6 — expecting DROP within debounce")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        readings = list(stable_week(weekly_shift=-18.0, shift_day=5))
        outputs = replay(readings, home)
        shift_idx = next(i for i, r in enumerate(readings)
                         if r[0] >= T0 + timedelta(days=5))
        post = outputs[shift_idx:]
        fired_at = next((i for i, (_, o) in enumerate(post) if "USAGE DROP" in o), None)
        assert fired_at is not None, "drop never fired after a -18 sustained shift"
        assert fired_at <= 3, f"drop too slow: fired {fired_at+1} readings after shift"
    print(f"  ✓ drop fired {fired_at+1} readings after the shift")


def scenario_e_multimodel():
    print("Scenario E: Opus/Sonnet alternating blocks — no false signals; suggest sane")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        outputs = replay(stable_week(multi_model=True), home)
        spikes, drops = signals_in(outputs)
        assert not spikes and not drops, "model switching produced a false signal"
        # suggest must rank Sonnet (shallow burn) above Opus (steep burn)
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["CCVITALS_FAKE_NOW"] = (T0 + timedelta(days=6, hours=21, minutes=30)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        proc = subprocess.run([sys.executable, "-m", "claude_code_vitals", "suggest"],
                              capture_output=True, text=True, cwd=REPO, env=env)
        out = ANSI.sub("", proc.stdout)
        assert "Shared 5h window" in out, out
        s_pos, o_pos = out.find("Sonnet"), out.find("Opus")
        assert 0 <= s_pos < o_pos, f"Sonnet must rank above Opus:\n{out}"
    print("  ✓ no false signals across model switches; suggest ranks Sonnet first")


def main():
    print("\n⚡ ccvitals sawtooth replay harness\n")
    scenario_a_stable()
    scenario_b_spike()
    scenario_c_drop()
    scenario_e_multimodel()
    print("\n🎉 Replay harness: all scenarios passed\n")


if __name__ == "__main__":
    main()
