# Underdog Auto-Trader

Automates the underdog strategy on **Polymarket US**: when a game is close
with little time left, buy $0.50 of the underdog side and rest a
take-profit sell. Runs as a Vercel cron every 2 minutes.

## Sports covered
NFL, NBA, MLB, NHL, and EPL soccer — each with its own close/late rules
(quarters/clock for NFL/NBA/NHL, innings for MLB, minutes for soccer).

## How it works
- Each cron run = one stateless pass: read live scores (ESPN) → find the
  Polymarket market → check the underdog's ask → buy + rest take-profit.
- Open positions are re-read every run: a resting take-profit is ensured,
  and a stop-loss closes anything that craters (via `close_position`).
- One position per game. Per-bet stake is fixed in `config.json`.
- **Paper mode (default)** is a dry run: it logs what it *would* do and
  places nothing. No keys needed.

## Deploy
1. Push this repo to GitHub, import into Vercel.
2. In Vercel → Project → Settings → Environment Variables, add:
   - `POLYMARKET_KEY_ID` — your key ID
   - `POLYMARKET_SECRET_KEY` — your secret (never commit this)
   - `CRON_SECRET` — any random string; Vercel sends it with cron calls
     and the endpoint rejects requests without it
3. Watch `/api/trade` output in the Vercel logs during a game night.
   It runs in paper mode until you're happy.
4. Go live: set env var `LIVE_MODE=true` (no redeploy of code needed…
   env change triggers one automatically).

## Safety notes
- Every live buy is previewed (`orders.preview`) before placement.
- If position/order reads fail, the run fails closed (no new entries).
- The endpoint requires the cron Bearer token when `CRON_SECRET` is set,
  so random visitors can't trigger trades.
- Nothing here guarantees profit — it automates entries/exits with
  discipline. The market still has to move your way.
- Don't manually trade the same games while the bot is live; it manages
  every open position it finds in games it's tracking.

## Strategy knobs (`config.json`)
`stake_usd` (0.50), `take_profit_mult` (2.5), `stop_loss_mult` (0.40),
`min_buy_price` (0.05) / `max_buy_price` (0.40), per-sport `max_diff`,
`late_period`, `late_clock_sec` / `late_clock_min`, `enabled`.
