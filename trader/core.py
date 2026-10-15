"""Underdog Auto-Trader core: multi-sport, stateless, serverless-friendly.

Strategy: watch live games (NFL/NBA/MLB/NHL/soccer) via ESPN's free
scoreboard API. When a game is CLOSE and LATE, buy STAKE_USD of the
underdog side on Polymarket US (limit order at the ask), then rest a
take-profit limit sell. Per-run stop-loss closes positions that crater.

Stateless design: each run reads open positions / open orders from
Polymarket instead of a local state file, so it works as a Vercel cron
(one run = one pass). Paper mode = dry run: evaluates triggers and
reports what it WOULD do, placing nothing.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

# --- network bootstrap (sandbox egress; no-op on normal machines) ---
for _var in ("NO_PROXY", "no_proxy"):
    _v = os.environ.get(_var)
    if _v:
        os.environ[_var] = ",".join(
            p for p in _v.split(",") if not (p.startswith("[") and p.endswith("]")))
if os.path.exists("/etc/ssl/certs/ca-certificates.crt"):
    os.environ.setdefault("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")

import httpx  # noqa: E402

HERE = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = {
    "live_mode": False,          # env LIVE_MODE=true overrides
    "stake_usd": 0.50,
    "take_profit_mult": 2.5,
    "stop_loss_mult": 0.40,
    "min_buy_price": 0.05,
    "max_buy_price": 0.40,
    "poll_seconds": 20,          # local runs only; Vercel uses cron
    "sports": {
        "nfl": {"enabled": True, "espn": "football/nfl",
                "max_diff": 8, "late_period": 4, "late_clock_sec": 300,
                "clock": "down"},
        "nba": {"enabled": True, "espn": "basketball/nba",
                "max_diff": 8, "late_period": 4, "late_clock_sec": 300,
                "clock": "down"},
        "mlb": {"enabled": True, "espn": "baseball/mlb",
                "max_diff": 2, "late_period": 8,
                "clock": "none"},
        "nhl": {"enabled": True, "espn": "hockey/nhl",
                "max_diff": 1, "late_period": 3, "late_clock_sec": 300,
                "clock": "down"},
        "epl": {"enabled": True, "espn": "soccer/eng.1",
                "max_diff": 1, "late_period": 2, "late_clock_min": 80,
                "clock": "up"},
    },
}

# ---------------------------------------------------------------- config

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    p = HERE / "config.json"
    if p.exists():
        user = json.loads(p.read_text())
        cfg.update({k: v for k, v in user.items() if k != "sports"})
        for sport, rules in user.get("sports", {}).items():
            if sport in cfg["sports"]:
                cfg["sports"][sport].update(rules)
            else:
                cfg["sports"][sport] = rules
    if os.environ.get("LIVE_MODE", "").lower() == "true":
        cfg["live_mode"] = True
    return cfg


# ---------------------------------------------------------------- logging

_log_lines = []

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _log_lines.append(line)


# ---------------------------------------------------------------- client

def make_client(cfg):
    from polymarket_us import PolymarketUS
    key_id = os.environ.get("POLYMARKET_KEY_ID")
    secret = os.environ.get("POLYMARKET_SECRET_KEY")
    if cfg["live_mode"] and not (key_id and secret):
        raise RuntimeError("live_mode needs POLYMARKET_KEY_ID + POLYMARKET_SECRET_KEY env vars")
    return PolymarketUS(key_id=key_id, secret_key=secret)


# ---------------------------------------------------------------- ESPN

def _parse_clock(display_clock, clock_kind):
    """Return (sec_remaining | min_elapsed | None) depending on clock kind."""
    dc = (display_clock or "").strip()
    if clock_kind == "down" and ":" in dc:
        try:
            m, s = dc.split(":")
            return ("sec", int(m) * 60 + int(s))
        except (ValueError, TypeError):
            return (None, None)
    if clock_kind == "up":
        # soccer: "78'" or "45'+2'"
        num = "".join(ch for ch in dc.split("+")[0] if ch.isdigit())
        if num:
            return ("min", int(num))
    return (None, None)


def espn_games(sport_key, rules):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{rules['espn']}/scoreboard"
    try:
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"ESPN {sport_key} fetch failed: {e}")
        return []
    games = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        st = comp["status"]["type"]
        teams = {}
        for t in comp["competitors"]:
            teams[t["homeAway"]] = {
                "abbr": t["team"].get("abbreviation", ""),
                "name": t["team"].get("displayName", ""),
                "score": int(t.get("score") or 0),
            }
        kind, clock = _parse_clock(comp["status"].get("displayClock", ""),
                                  rules.get("clock"))
        games.append({
            "sport": sport_key,
            "away": teams.get("away", {}), "home": teams.get("home", {}),
            "live": st.get("name") == "STATUS_IN_PROGRESS",
            "over": bool(st.get("completed")),
            "period": comp["status"].get("period", 0),
            "clock_kind": kind, "clock": clock,
            "diff": abs(teams.get("away", {}).get("score", 0)
                        - teams.get("home", {}).get("score", 0)),
        })
    return games


def is_trigger(game, rules):
    """Close + late?"""
    if not game["live"] or game["over"]:
        return False
    if game["diff"] > rules["max_diff"]:
        return False
    if game["period"] < rules["late_period"]:
        return False
    ck = rules.get("clock")
    if ck == "down":
        return game["clock"] is not None and game["clock"] <= rules["late_clock_sec"]
    if ck == "up":
        return game["clock"] is not None and game["clock"] >= rules["late_clock_min"]
    return True  # "none" (MLB): inning is enough


# ---------------------------------------------------------------- Polymarket: public

_market_cache = {}

def find_market(client, game):
    key = f"{game['away'].get('name')}|{game['home'].get('name')}"
    if key in _market_cache:
        return _market_cache[key]
    a, h = game["away"], game["home"]
    query = f"{a.get('abbr', '')} {h.get('abbr', '')}".strip()
    try:
        res = client.search.query({"query": query})
    except Exception as e:
        log(f"  market search failed: {e}")
        return None
    needles = {a.get("name", "").lower(), a.get("abbr", "").lower(),
               h.get("name", "").lower(), h.get("abbr", "").lower()} - {""}
    for ev in res.get("events", []):
        title = ev.get("title", "").lower()
        if sum(1 for n in needles if n in title) >= 2:
            for m in ev.get("markets", []):
                if len(m.get("marketSides", [])) >= 2:
                    out = {"event_slug": ev["slug"], "market_slug": m["slug"]}
                    _market_cache[key] = out
                    return out
    return None


def side_info(client, market_slug):
    m = client.markets.retrieve_by_slug(market_slug)["market"]
    out = {"tick": m.get("orderPriceMinTickSize", 0.0025),
           "min_qty": m.get("minimumTradeQty", 0.01)}
    for s in m.get("marketSides", []):
        out["long" if s.get("long") else "short"] = {
            "desc": s.get("description"), "price": float(s.get("price") or 0),
            "team_id": s.get("teamId"),
        }
    return out


def quotes(client, market_slug):
    bbo = client.markets.bbo(market_slug)["marketData"]
    def px(d, k):
        try:
            return float((d.get(k) or {}).get("value") or 0)
        except (TypeError, ValueError):
            return 0.0
    return {
        "long_ask": px(bbo, "longQuote") or px(bbo, "bestAsk"),
        "short_ask": px(bbo, "shortQuote"),
        "long_bid": px(bbo, "bestBid"),
    }


# ---------------------------------------------------------------- Polymarket: authenticated

def get_positions(client):
    """slug -> {qty, avg_px, cash_value, team_id, outcome}"""
    out = {}
    try:
        resp = client.portfolio.positions()
    except Exception as e:
        log(f"  positions read failed: {e}")
        return None
    for key, p in (resp.get("positions") or {}).items():
        meta = p.get("marketMetadata") or {}
        slug = meta.get("slug") or key
        try:
            qty = float(p.get("netPositionDecimal") or p.get("netPosition") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        avg = p.get("avgPx") or {}
        cash = p.get("cashValue") or {}
        out[slug] = {
            "qty": qty,
            "avg_px": float(avg.get("value") or 0),
            "cash_value": float(cash.get("value") or 0),
            "team_id": (meta.get("team") or {}).get("id") or meta.get("teamId"),
            "outcome": meta.get("outcome"),
            "title": meta.get("title"),
        }
    return out


def open_sell_exists(client, market_slug):
    try:
        try:
            orders = client.orders.list({"marketSlug": market_slug})
        except TypeError:
            orders = client.orders.list()
    except Exception as e:
        log(f"  open-orders read failed: {e}")
        return None
    items = orders.get("orders", orders) if isinstance(orders, dict) else orders
    for o in items or []:
        if not isinstance(o, dict):
            continue
        if o.get("marketSlug", market_slug) != market_slug:
            continue
        if o.get("side") == "ORDER_SIDE_SELL" and o.get("state") in (
                "ORDER_STATE_NEW", "ORDER_STATE_PENDING_NEW",
                "ORDER_STATE_PARTIALLY_FILLED"):
            return True
    return False


def resolve_side(client, market_slug, pos):
    """Which side ('long'/'short') a position is on, via teamId/outcome."""
    info = side_info(client, market_slug)
    if pos.get("team_id"):
        for side in ("long", "short"):
            if info.get(side, {}).get("team_id") == pos["team_id"]:
                return side
    oc = (pos.get("outcome") or "").lower()
    if oc:
        for side in ("long", "short"):
            if (info.get(side, {}).get("desc") or "").lower() == oc:
                return side
    return None


def place_buy(client, cfg, market_slug, side, ask):
    qty = round(cfg["stake_usd"] / ask, 4)
    intent = "ORDER_INTENT_BUY_LONG" if side == "long" else "ORDER_INTENT_BUY_SHORT"
    params = {"marketSlug": market_slug, "intent": intent,
              "type": "ORDER_TYPE_LIMIT",
              "price": {"value": f"{ask:.4f}", "currency": "USD"},
              "quantity": qty, "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL"}
    try:
        prev = client.orders.preview(params)
        log(f"  preview ok: {str(prev)[:160]}")
    except Exception as e:
        log(f"  preview failed, skipping buy: {e}")
        return None
    order = client.orders.create(params)
    log(f"  LIVE BUY {qty} @ {ask:.4f} ({side}) id={order.get('id')}")
    return {"id": order.get("id"), "qty": qty, "price": ask}


def ensure_take_profit(client, cfg, market_slug, side, entry_px, qty):
    target = round(entry_px * cfg["take_profit_mult"], 4)
    if target >= 1.0:
        target = 0.99
    exists = open_sell_exists(client, market_slug)
    if exists is None:
        log("  cannot verify open orders; skipping TP placement this run")
        return
    if exists:
        return
    intent = "ORDER_INTENT_SELL_LONG" if side == "long" else "ORDER_INTENT_SELL_SHORT"
    order = client.orders.create({
        "marketSlug": market_slug, "intent": intent,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": f"{target:.4f}", "currency": "USD"},
        "quantity": qty, "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL"})
    log(f"  take-profit resting: sell {qty} @ {target:.4f} id={order.get('id')}")


# ---------------------------------------------------------------- one pass

def run_pass():
    global _log_lines
    _log_lines = []
    t0 = time.time()
    cfg = load_config()
    paper = not cfg["live_mode"]
    actions = []
    log(f"pass starting in {'PAPER (dry run)' if paper else 'LIVE'} mode")

    client = make_client(cfg)

    # public reachability check (works without keys)
    try:
        n = len(client.sports.list().get("sports", []))
        log(f"Polymarket US reachable ({n} sports)")
    except Exception as e:
        return {"ok": False, "error": f"Polymarket unreachable: {e}",
                "log": _log_lines}

    positions = {} if paper else get_positions(client)
    if positions is None:
        return {"ok": False, "error": "could not read positions; fail-closed",
                "log": _log_lines}

    # 1) manage existing positions (live only)
    for slug, pos in positions.items():
        if pos["qty"] <= 0 or pos["avg_px"] <= 0:
            continue
        side = resolve_side(client, slug, pos)
        entry = pos["avg_px"]
        # stop-loss on market value vs entry cost
        if pos["cash_value"] and pos["cash_value"] < pos["qty"] * entry * cfg["stop_loss_mult"]:
            actions.append({"type": "stop_loss", "market": slug})
            if paper:
                log(f"  PAPER stop-loss would close {slug}")
            else:
                log(f"  STOP-LOSS closing {slug}")
                try:
                    client.orders.close_position({"marketSlug": slug})
                except Exception as e:
                    log(f"  close failed ({e}); cancelling open orders")
                    try:
                        client.orders.cancel_all({"marketSlug": slug})
                    except Exception as e2:
                        log(f"  cancel failed: {e2}")
            continue
        # make sure a take-profit is resting
        if side and not paper:
            try:
                ensure_take_profit(client, cfg, slug, side, entry, pos["qty"])
            except Exception as e:
                log(f"  TP ensure failed for {slug}: {e}")

    # 2) scan games for new entries
    held_slugs = set(positions)
    for sport_key, rules in cfg["sports"].items():
        if not rules.get("enabled"):
            continue
        for g in espn_games(sport_key, rules):
            if not g["live"] or g["over"]:
                continue
            gid = f"{sport_key}:{g['away'].get('abbr')}@{g['home'].get('abbr')}"
            trig = is_trigger(g, rules)
            log(f"{gid} Q{g['period']} diff={g['diff']} clock={g['clock']} trigger={trig}")
            if not trig:
                continue
            try:
                meta = find_market(client, g)
            except Exception as e:
                log(f"  market lookup error: {e}")
                continue
            if not meta:
                log(f"  no Polymarket market for {gid}")
                continue
            if meta["market_slug"] in held_slugs:
                log("  already holding this game, skipping")
                continue
            try:
                info = side_info(client, meta["market_slug"])
                q = quotes(client, meta["market_slug"])
            except Exception as e:
                log(f"  market data error: {e}")
                continue
            dog = "long" if info["long"]["price"] < info["short"]["price"] else "short"
            ask = q["long_ask"] if dog == "long" else q["short_ask"]
            desc = info[dog]["desc"]
            log(f"  underdog: {desc} ask {ask:.4f}")
            if not (cfg["min_buy_price"] <= ask <= cfg["max_buy_price"]) or ask <= 0:
                log("  ask outside buy window, skipping")
                continue
            qty = round(cfg["stake_usd"] / ask, 4)
            if qty < info.get("min_qty", 0):
                log(f"  qty {qty} below market minimum, skipping")
                continue
            action = {"type": "buy", "game": gid, "market": meta["market_slug"],
                      "side": dog, "team": desc, "ask": round(ask, 4),
                      "qty": qty, "stake": cfg["stake_usd"]}
            actions.append(action)
            if paper:
                tp = min(round(ask * cfg["take_profit_mult"], 4), 0.99)
                log(f"  PAPER: would BUY {qty} {desc} @ {ask:.4f} "
                    f"(${(qty*ask):.2f}), TP @ {tp:.4f}")
            else:
                buy = place_buy(client, cfg, meta["market_slug"], dog, ask)
                if buy:
                    held_slugs.add(meta["market_slug"])
                    try:
                        ensure_take_profit(client, cfg, meta["market_slug"],
                                           dog, buy["price"], buy["qty"])
                    except Exception as e:
                        log(f"  TP placement failed: {e} (will retry next run)")

    dt = round(time.time() - t0, 1)
    log(f"pass done in {dt}s, {len(actions)} action(s)")
    return {"ok": True, "mode": "paper" if paper else "live",
            "duration_s": dt, "actions": actions, "log": _log_lines}
