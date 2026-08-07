# Odyssey Seat Watcher

Polls Cinemark for **The Odyssey (IMAX 70mm)** at **Cinemark Dallas XD and IMAX**
and sends a **Telegram** message when a seat opens up that matches:

- **Dates:** today → open-ended. The start advances daily so it never searches
  past days (individual showtimes drop off once they've started). There's
  no fixed end date: discovery walks forward until it passes Cinemark's booking
  horizon, so dates added later (e.g. beyond an original cutoff) are picked up
  automatically.
- **Times:** every IMAX 70mm showtime (`TARGET_TIMES = None`). Cinemark shifted the
  schedule mid-run (11:30/3:15 became 11:45/3:30 from 8/21), and exact-time matching
  silently missed those, so all times are watched. Set `TARGET_TIMES` to a set of
  `"HH:MM:SS"` strings to narrow it again.
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

- **Near-window seat watching** (`WATCH_AHEAD_DAYS`, default **14**): only showtimes
  within the next two weeks get a seat-map request each run. This is what keeps the
  shard count — and therefore the re-check interval — low.

Net effect: a run makes roughly 20–24 requests total and finishes in well under a
minute, regardless of how many dates get added.

### Watching seats vs. watching the schedule

These are deliberately separate, because they cost very different amounts:

| | Scope | Cost | You get |
|---|---|---|---|
| **Seat watching** | next `WATCH_AHEAD_DAYS` days | 1 request per showtime, every run | alert when a *seat* opens |
| **Schedule watching** | the entire booking horizon | free — discovery already sweeps it | alert when a *showtime* is added |

So limiting the seat window keeps checks fast **without** going blind to the future:
if Cinemark puts a brand-new date on sale three weeks out, you still get a
"🆕 New showtime(s) on sale" message, and that date starts getting seat-watched
automatically once it comes within the window. Set `NOTIFY_NEW_SHOWTIMES = False`
to turn those off, or `WATCH_AHEAD_DAYS = None` to seat-watch every date.

New-showtime alerts stay silent until the sweep has covered the whole horizon twice,
so the initial fill-in never reports the existing schedule as "new".

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
`HEARTBEAT_EVERY_HOURS`, `WATCH_AHEAD_DAYS` / `NOTIFY_NEW_SHOWTIMES`, and the
throttle knobs `DISCO_BATCH_DATES` / `MAX_SEATMAPS_PER_RUN`.

### Heartbeat

Even when no seats are found, the watcher sends a periodic "still alive" message
so you know it's running — every `HEARTBEAT_EVERY_HOURS` hours (default **6**). The
first run sends one immediately as a delivery test, and any real seat alert resets
the timer. Set `HEARTBEAT_EVERY_HOURS = 0` to turn heartbeats off.

The heartbeat lists any still-open seats broken down by showtime date/time, and
only ever counts showtimes that are **still upcoming** — seats belonging to a
showtime that has already started are purged from state and never reported.

---

## How often it actually runs

The workflow asks for every 5 minutes (`cron: "2-59/5 * * * *"`), but **GitHub drops
most of those ticks**. Measured over 54 hours on this repo: a median gap of **131
minutes**, effectively **one run every ~2 hours**, with a worst case of 6 hours.
Scheduled workflows are explicitly best-effort and get deprioritized under load.

The script adapts to this on its own (see below). If you want true 5-minute checks,
add an external trigger.

### Adaptive run size

Each run checks how long it's been since the last one and sizes itself:

| Gap since last run | Pass | Dates probed | Seat maps | Pacing |
|---|---|---|---|---|
| < `FULL_PASS_AFTER_MIN` (30 min) | light | 8 | 16 | ~1.5s |
| ≥ 30 min, or first run | **full** | 35 | 45 | ~3.2s |

So if GitHub only fires once every 2 hours, that run covers the whole horizon and
every showtime in the window. If an external trigger is firing every 5 minutes, runs
stay small and fast. Cinemark's limit is ~30–35 requests per ~90s window and that
window fully resets between sparse runs, so a full pass just paces slower (~14
req/min) rather than doing less.

### Optional: real 5-minute checks via an external trigger

The workflow already accepts `workflow_dispatch`, so any external scheduler can fire
it on time. Using the free [cron-job.org](https://cron-job.org):

1. Create a **fine-grained personal access token** on GitHub (Settings → Developer
   settings → Fine-grained tokens): repository access = this repo only, permission
   **Actions: Read and write**.
2. On cron-job.org, create a job that runs every 5 minutes:
   - URL: `https://api.github.com/repos/<you>/odyssey-seat-watcher/actions/workflows/watch.yml/dispatches`
   - Method: `POST`
   - Body: `{"ref":"main"}`
   - Headers:
     ```
     Authorization: Bearer <YOUR_TOKEN>
     Accept: application/vnd.github+json
     Content-Type: application/json
     X-GitHub-Api-Version: 2022-11-28
     ```

`Content-Type` and `X-GitHub-Api-Version` aren't strictly required — GitHub parses
the JSON body either way — but setting them explicitly avoids the scheduler sending
the body with an unexpected encoding, and pins the API version.

A successful dispatch returns **HTTP 204** with an empty body. Common failures:
`401` = bad/expired token, `403` = token lacks **Actions: Read and write** on this
repo, `404` = wrong owner/repo/workflow filename (or the token can't see the repo),
`422` = the `ref` doesn't exist (must be `main`).

Keep the GitHub `schedule` block as a fallback — the adaptive run sizing means the
two firing together coexist fine.

## Good to know

- **Timing:** see "How often it actually runs" above — GitHub's own scheduler is
  unreliable for frequent crons.
- **Politeness / rate limits:** requests are paced ~1.5s apart with retry-and-
  backoff, plus incremental discovery and adaptive sharding (see above) so each run
  stays under Cinemark's ~30-request window. Raising `DISCO_BATCH_DATES` or
  `MAX_SEATMAPS_PER_RUN` too high brings back `429 Too Many Requests` and long runs.
- **Carrying seats over:** if a page fails to load, that showtime's previously
  known seats are kept so you don't get a false "gone then back" re-alert.
- **This is best-effort:** hot showtimes can sell out in the gap between checks.
  The alert links straight to the seat map so you can book fast.
