# Odyssey Seat Watcher

Polls Cinemark for **The Odyssey (IMAX 70mm)** at **Cinemark Dallas XD and IMAX**
and sends a **Telegram** message when a seat opens up that matches:

- **Dates:** today → open-ended. The start advances daily so it never searches
  past days (and once today's 3:15 pm show has started, today is skipped). There's
  no fixed end date: discovery walks forward until it passes Cinemark's booking
  horizon, so dates added later (e.g. beyond an original cutoff) are picked up
  automatically.
- **Times:** 11:30 am and 3:15 pm
- **Seats:** rows **E–J**, seat numbers **7–21**

Runs every 5 minutes on **GitHub Actions** — no server, no cost.

---

## How it works

1. `watch.py` reads the theater's showtimes page to find the `ShowtimeId` for each
   target date/time.
2. It fetches each seat map. Cinemark ships seat availability right in the page
   HTML (`available="True"` + `info="Row,Seat,..."`), so no browser is needed.
3. It keeps only available seats in the wanted rows / seat-number range.
4. If a seat is **new** since the last run, it sends you a Telegram message with a
   direct booking link. State is remembered between runs via the Actions cache, so
   you get pinged on new openings, not the same seat every 5 minutes.

### Staying under Cinemark's rate limit

Cinemark throttles at roughly **~30–35 requests per ~90s window** (per IP), and with
the open-ended date range there can be dozens of dates and showtimes. Two mechanisms
keep every run well under that cap (and under the Actions job timeout):

- **Incremental discovery** (`DISCO_BATCH_DATES`): instead of scanning the whole date
  horizon each run, a persistent cursor probes a handful of dates per run and advances
  across successive runs, looping when it passes the booking horizon. The full horizon
  refreshes every ~5 runs (~25 min), so newly-added dates are picked up automatically.
- **Adaptive sharding** (`MAX_SEATMAPS_PER_RUN`): seat maps are split into shards, one
  checked per 5-minute run (alternating). The number of shards scales with how many
  showtimes exist, so a run never fetches more than `MAX_SEATMAPS_PER_RUN` seat maps.
  More dates → more shards → each showtime is checked every *(shards × 5)* minutes.

Net effect: a run makes roughly 20–24 requests total and finishes in well under a
minute, regardless of how many dates get added.

---

## One-time setup (~10 minutes)

### 1. Create a Telegram bot and get your chat ID

1. In Telegram, message **@BotFather** → send `/newbot` → follow prompts.
   Copy the **bot token** it gives you (looks like `123456:ABC-DEF...`).
2. Send any message (e.g. "hi") to your new bot so it's allowed to message you.
3. Get your chat ID: open this URL in a browser (paste your token):
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   Look for `"chat":{"id":123456789,...}` — that number is your **chat ID**.

### 2. Put the code on GitHub

```bash
cd odyssey-seat-watcher
git init && git add . && git commit -m "Odyssey seat watcher"
# create a repo on github.com, then:
git remote add origin https://github.com/<you>/odyssey-seat-watcher.git
git push -u origin main
```

### 3. Add your secrets

In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add two:

| Name | Value |
|------|-------|
| `TELEGRAM_BOT_TOKEN` | the bot token from step 1 |
| `TELEGRAM_CHAT_ID`   | your chat ID from step 1 |

### 4. Turn it on

- Go to the **Actions** tab, enable workflows if prompted.
- Open **Odyssey Seat Watcher** → **Run workflow** to test it immediately.
  (The very first run alerts on anything already available.)
- After that it runs automatically every 5 minutes.

To stop it: disable the workflow in the Actions tab, or delete the repo.

---

## Run it locally (optional)

```bash
pip install -r requirements.txt

python watch.py --list      # show the showtimes it will watch
python watch.py --dry-run   # full check, prints the alert instead of texting
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python watch.py   # real run
```

---

## Changing what it watches

All settings are constants at the top of [`watch.py`](watch.py):
`TARGET_TIMES`, `SEASON_START`, `WANTED_ROWS`, `SEAT_MIN` / `SEAT_MAX`,
`WANTED_SEAT_TYPES` (add `"companion"` if you'd accept a companion seat),
`HEARTBEAT_EVERY_HOURS`, and the throttle knobs `DISCO_BATCH_DATES` /
`MAX_SEATMAPS_PER_RUN`.

### Heartbeat

Even when no seats are found, the watcher sends a periodic "still alive" message
so you know it's running — every `HEARTBEAT_EVERY_HOURS` hours (default **6**). The
first run sends one immediately as a delivery test, and any real seat alert resets
the timer. Set `HEARTBEAT_EVERY_HOURS = 0` to turn heartbeats off.

---

## Good to know

- **Timing:** GitHub's scheduled runs can be delayed a few minutes when their
  queue is busy — treat "every 5 minutes" as approximate.
- **Politeness / rate limits:** requests are paced ~1.5s apart with retry-and-
  backoff, plus incremental discovery and adaptive sharding (see above) so each run
  stays under Cinemark's ~30-request window. Raising `DISCO_BATCH_DATES` or
  `MAX_SEATMAPS_PER_RUN` too high brings back `429 Too Many Requests` and long runs.
- **Carrying seats over:** if a page fails to load, that showtime's previously
  known seats are kept so you don't get a false "gone then back" re-alert.
- **This is best-effort:** hot showtimes can sell out in the gap between checks.
  The alert links straight to the seat map so you can book fast.
