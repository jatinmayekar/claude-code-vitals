import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
sys.path.insert(0, ".")
from claude_code_vitals.config import Config
from claude_code_vitals.logger import RateLimitSnapshot, append_snapshot

home = Path(sys.argv[1])
c = Config(); c.data_dir = home / ".claude-code-vitals"
c.data_dir.mkdir(parents=True, exist_ok=True)
now = datetime.now(timezone.utc)

def iso(m):
    return (now + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")

def snap(mid, name, m, s5, w7):
    return RateLimitSnapshot(
        ts=iso(m), provider="anthropic", model_id=mid, model_name=name,
        session_5h_pct=s5, session_5h_reset=iso(150), weekly_7d_pct=w7, weekly_7d_reset=iso(6000),
        context_used_pct=48, context_window_size=200000, session_cost_usd=3.5, session_id="demo",
    )

rows = []
opus = [(-230,20,40),(-210,25,42),(-190,31,45),(-170,37,48),(-150,43,51),(-130,49,54),
        (-110,54,57),(-90,58,60),(-65,62,63),(-45,66,65)]
son = [(-35,67,65),(-25,67.6,66),(-8,68,66)]
for m,s5,w7 in opus: rows.append(("claude-opus-4-6","Opus 4.6",m,s5,w7))
for m,s5,w7 in son: rows.append(("claude-sonnet-4-6","Sonnet 4.6",m,s5,w7))
rows.sort(key=lambda r: r[2])
for mid,name,m,s5,w7 in rows:
    append_snapshot(snap(mid,name,m,s5,w7), c)
print("reseeded", len(rows))
