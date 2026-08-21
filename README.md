# Super 6 deadline reminders

Checks super6.skysports.com every 30 minutes and pushes a notification to
your phone 24 hours and 2 hours before the round's deadline (the kick-off
time of the earliest of the six fixtures — that's how Sky defines it).

No app store, no server to maintain — it runs on GitHub's free scheduler
and delivers the push via [ntfy.sh](https://ntfy.sh), a free, no-signup
push notification service with iOS/Android apps.

## Setup (10 minutes, one time)

1. **Install the ntfy app** on your phone (search "ntfy" on the App
   Store / Play Store).
2. **Pick a private topic name** — this is like a password for your
   notification channel, so make it random, e.g. `super6-yourname-8f3k`.
   In the ntfy app, tap "+" and subscribe to that topic name.
3. **Create a GitHub repo** (github.com → New repository, can be private)
   and upload all the files in this folder, keeping the folder structure
   (the `.github/workflows/check-deadline.yml` path matters — GitHub only
   picks up workflows from exactly that location).
4. **Add your topic as a secret**: in the repo, go to
   Settings → Secrets and variables → Actions → New repository secret.
   Name it `NTFY_TOPIC`, value = the topic name you picked in step 2.
5. **Enable Actions**: go to the Actions tab of your repo, click
   "I understand my workflows, go ahead and enable them".
6. **Test it**: Actions tab → "Super 6 deadline check" → "Run workflow".
   After ~1-2 minutes check the run log — it should print the deadline it
   found. If it's your first run and you're within 24h of a deadline,
   you'll get a push immediately.

After that it just runs itself every 30 minutes.

## If it stops finding a deadline

Sky can change their site's markup at any time. Each run writes a
`deadline_debug.txt` to the repo with everything the scraper saw on the
page — open that file, search for the actual kick-off time text, and
adjust the CSS selector in `find_deadline()` in `scraper.py` to match.

## Adjusting reminder timing

Edit `REMINDER_HOURS_BEFORE = [24, 2]` in `scraper.py` — e.g. change to
`[48, 24, 3]` for three reminders instead of two.
