# Odyssey Seat Watcher

Polls Cinemark for **The Odyssey (IMAX 70mm)** at **Cinemark Dallas XD and IMAX**
and sends a **Telegram** message when a seat opens up that matches:

- **Dates:** 2026-07-21 → 2026-08-13
- **Times:** 11:30 am and 3:15 pm
- **Seats:** rows **E–J**, seat numbers **7–21**

Runs every 5 minutes on **GitHub Actions** — no server, no cost.

---

## How it works

1. `watch.py` reads the theater's showtimes page to find the `ShowtimeId` for each
   target date/time (cached for 90 min, so most runs skip this).
2. It fetches each seat map. Cinemark ships seat availability right in the page
   HTML (`available="True"` + `info="Row,Seat,..."`), so no browser is needed.
3. It keeps only available seats in rows E–J / seats 7–21.
4. If a seat is **new** since the last run, it sends you a Telegram message with a
   direct booking link. State is remembered between runs via the Actions cache, so
   you get pinged on new openings, not the same seat every 5 minutes.

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
`TARGET_TIMES`, `DATE_START` / `DATE_END`, `WANTED_ROWS`, `SEAT_MIN` / `SEAT_MAX`,
and `WANTED_SEAT_TYPES` (add `"companion"` if you'd accept a companion seat).

---

## Good to know

- **Timing:** GitHub's scheduled runs can be delayed a few minutes when their
  queue is busy — treat "every 5 minutes" as approximate.
- **Politeness / rate limits:** requests are paced ~1.5s apart with retry-and-
  backoff; Cinemark returned `429 Too Many Requests` when hit faster than that.
  Don't lower the pause or shorten the cron interval much.
- **Carrying seats over:** if a page fails to load, that showtime's previously
  known seats are kept so you don't get a false "gone then back" re-alert.
- **This is best-effort:** hot showtimes can sell out in the gap between checks.
  The alert links straight to the seat map so you can book fast.
