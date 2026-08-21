"""
Super6 deadline checker + ntfy.sh push notifier.

Why it's built this way
------------------------
super6.skysports.com has no public API and is a JS single-page app (a plain
HTTP GET returns an empty shell — "You need to enable JavaScript to run this
app."). So this uses a real headless browser (Playwright) to load the page
the way your phone/laptop would, then reads the deadline straight out of
the rendered page.

Sky's homepage doesn't show an absolute kickoff time — it shows a live
countdown like "Round 1 - Deadline In 18H 54M 15S". So this reads that
countdown and adds it to the current time to get an absolute deadline.

This is a best-effort scrape of a site I don't control, so it's written to
fail loudly and leave evidence rather than guess silently:
- Every run writes deadline_debug.txt with the raw times it found and the
  full visible page text, so if it ever picks the wrong thing (or Sky
  changes their wording) you can see exactly what the scraper saw.
- If it finds nothing, it raises an error instead of pretending.

If Sky changes the site and this stops finding a deadline, open
deadline_debug.txt (uploaded as a workflow artifact even on failure) and
adjust find_deadline() below to match the new wording/layout.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright

SUPER6_URL = "https://super6.skysports.com/"
STATE_FILE = "state.json"
DEBUG_FILE = "deadline_debug.txt"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # set this as a GitHub secret
REMINDER_HOURS_BEFORE = [24, 12, 6, 3, 1]  # send a push at each of these checkpoints


async def find_deadline():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(SUPER6_URL, wait_until="networkidle", timeout=30000)
        # give the SPA a moment to finish client-side rendering
        await page.wait_for_timeout(3000)

        times = await page.eval_on_selector_all(
            "time[datetime]", "els => els.map(e => e.getAttribute('datetime'))"
        )
        body_text = await page.inner_text("body")
        # Grab "now" right after reading the page, since the countdown fallback
        # below is relative to this exact moment.
        scrape_time = datetime.now(timezone.utc)
        await browser.close()

    with open(DEBUG_FILE, "w") as f:
        f.write("Raw <time datetime> values found:\n")
        f.write("\n".join(times) if times else "(none)")
        f.write("\n\nFull visible page text (for manual inspection):\n")
        f.write(body_text)

    # Attempt 1: <time datetime="..."> elements (not used by super6.skysports.com
    # as of writing, but kept in case Sky changes the page later).
    future = []
    for t in times:
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if dt > scrape_time:
                future.append(dt)
        except ValueError:
            continue
    if future:
        return min(future)

    # Attempt 2: super6.skysports.com actually shows a live countdown like
    # "Round 1 - Deadline In 18H 54M 15S", with each digit/letter rendered on
    # its own line. Find the text between "Deadline In" and "Play For Free"
    # and pull the H/M/S numbers out of it.
    match = re.search(r"Deadline In(.*?)Play For Free", body_text, re.DOTALL)
    if match:
        chunk = "".join(match.group(1).split())  # collapse the one-char-per-line text
        hms = re.search(r"(\d+)H(\d+)M(\d+)S", chunk)
        if hms:
            hours, minutes, seconds = (int(x) for x in hms.groups())
            return scrape_time + timedelta(hours=hours, minutes=minutes, seconds=seconds)

    raise RuntimeError(
        "Couldn't find a deadline on the page using either method. "
        f"Check {DEBUG_FILE} to see what the scraper actually saw — "
        "Sky may have changed the page's wording or layout."
    )


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"deadline": None, "notified": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_push(title, message):
    import urllib.request

    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set, skipping push. Message was:", message)
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": "soccer"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


async def main():
    deadline = await find_deadline()
    deadline_iso = deadline.isoformat()
    state = load_state()

    old_deadline = (
        datetime.fromisoformat(state["deadline"]) if state["deadline"] else None
    )
    # Since the deadline is recomputed from a live countdown each run, it'll
    # drift by a few seconds run to run even for the same round. Only treat it
    # as a genuinely new round (and reset reminders) if it moved by more than
    # 15 minutes.
    if old_deadline is None or abs((deadline - old_deadline).total_seconds()) > 900:
        state = {"deadline": deadline_iso, "notified": []}
        print(f"New deadline detected: {deadline_iso}")
    else:
        state["deadline"] = deadline_iso

    now = datetime.now(timezone.utc)
    hours_left = (deadline - now).total_seconds() / 3600

    for threshold in REMINDER_HOURS_BEFORE:
        key = str(threshold)
        if hours_left <= threshold and key not in state["notified"]:
            send_push(
                "Super 6 reminder",
                f"Deadline is in about {threshold}h "
                f"({deadline.strftime('%a %d %b, %H:%M UTC')}). "
                "Get your predictions in!",
            )
            state["notified"].append(key)
            print(f"Sent {threshold}h reminder")

    save_state(state)
    print(f"Current deadline: {deadline_iso} ({hours_left:.1f}h from now)")


if __name__ == "__main__":
    asyncio.run(main())
