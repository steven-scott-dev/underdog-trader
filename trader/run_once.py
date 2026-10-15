"""CLI entry: one trading pass, then refresh the dashboard files."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trader.core import run_pass

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")


def build_status_md(res):
    daily = res.get("daily") or {}
    mode = str(res.get("mode", "?")).upper()
    L = []
    a = L.append
    a("# Underdog Trader — Status")
    a("")
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    a(f"Updated: {now} · Mode: **{mode}** · "
      f"Result: {'OK' if res.get('ok') else 'ERROR'} "
      f"({res.get('duration_s', '?')}s)")
    if res.get("error"):
        a(f"> {res['error']}")
    a("")
    a("## Today")
    a(f"- Bets placed: {daily.get('bets', 0)} / 5")
    a(f"- Realized P&L: {daily.get('realized', 0):+.2f}")
    a("")
    if mode == "PAPER":
        a("## Open paper positions")
        poss = res.get("paper_positions") or []
        if not poss:
            a("- _none_")
        for p in poss:
            bid = p.get("last_bid", p["entry"])
            unrl = (bid - p["entry"]) * p["qty"]
            a(f"- **{p['team']}** ({p['side']}) — {p['qty']} @ {p['entry']:.4f}, "
              f"now ~{bid:.4f}, unrealized {unrl:+.4f} "
              f"(TP {p['tp']:.4f} / SL {p['sl']:.4f})")
        a("")
        a("## Recent history")
        hist = res.get("paper_history") or []
        if not hist:
            a("- _none yet_")
        for h in reversed(hist[-8:]):
            a(f"- {h.get('closed', '')}: {h['team']} — {h['reason']} "
              f"@ {h['exit']:.4f} → {h['pnl']:+.4f}")
    else:
        a("## Live positions")
        lp = res.get("live_positions") or {}
        if not lp:
            a("- _none_")
        for slug, p in lp.items():
            a(f"- {p.get('title') or slug}: {p['qty']} @ avg {p['avg_px']:.4f}")
        a("")
        a("## Recent live history")
        lh = res.get("live_history") or []
        if not lh:
            a("- _none yet_")
        for h in reversed(lh[-8:]):
            a(f"- `{json.dumps(h)}`")
    a("")
    a("## Last run actions")
    acts = res.get("actions") or []
    if not acts:
        a("- _none_")
    for act in acts[-10:]:
        a(f"- `{json.dumps(act)}`")
    a("")
    return "\n".join(L)


def main():
    res = run_pass()
    md = build_status_md(res)
    (ROOT / "STATUS.md").write_text(md)
    (ROOT / "status.json").write_text(json.dumps({
        "updated": datetime.now(ET).isoformat(),
        "mode": res.get("mode"),
        "ok": res.get("ok"),
        "duration_s": res.get("duration_s"),
        "daily": res.get("daily"),
        "paper_positions": res.get("paper_positions"),
        "paper_history": res.get("paper_history"),
        "live_positions": res.get("live_positions"),
        "actions": res.get("actions"),
    }, indent=1, default=str))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
