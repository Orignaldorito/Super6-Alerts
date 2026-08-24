"""
Super6 deadline checker + ntfy.sh push notifier.

Why it's built this way
------------------------
super6.skysports.com has no public API and is a JS single-page app (a plain
HTTP GET returns an empty shell — "You need to enable JavaScript to run this
app."). So this uses a real headless browser (Playwright) to load the page
the way your phone/laptop would, then reads the kickoff times straight out
of the rendered DOM.

The deadline for a Super6 round is defined (per Sky's own rules) as the
kick-off time of the earliest of the six fixtures in that round. So:

1. Load https://super6.skysports.com/
2. Collect every <time datetime="..."> element on the page (fixture lists
   in SPAs are almost always rendered with HTML5 <time> tags carrying an
   ISO datetime attribute).
3. The earliest *future* one of those is the deadline.

This is a best-effort scrape of a site I don't control, so it's written to
fail loudly and leave evidence rather than guess silently:
- Every run writes deadline_debug.txt with the raw times it found and the
  full visible page text, so if it ever picks the wrong thing (or Sky
  changes their markup) you can see exactly what the scraper saw.
- If it finds nothing, it raises an error instead of pretending.

If Sky changes the site and this stops finding times, open
deadline_debug.txt, find where the kickoff times actually appear in the
text dump, and adjust the selector in find_deadline() below.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

SUPER6_URL = "https://super6.skysports.com/"
STATE_FILE = "state.json"
DEBUG_FILE = "deadline_debug.txt"
UK_TZ = ZoneInfo("Europe/London")  # Sky shows times in UK local time (GMT/BST)
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

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

    # Attempt 2: when the deadline is more than roughly a day or two away,
    # Sky shows an absolute date/time instead of a countdown, e.g.
    # "Round 2 - Deadline 29th Aug @ 2:00pm" (UK local time).
    abs_match = re.search(
        r"Deadline\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s*@\s*"
        r"(\d{1,2}):(\d{2})\s*(am|pm)",
        body_text,
        re.IGNORECASE,
    )
    if abs_match:
        day = int(abs_match.group(1))
        month = MONTHS.get(abs_match.group(2)[:3].lower())
        hour = int(abs_match.group(3))
        minute = int(abs_match.group(4))
        ampm = abs_match.group(5).lower()
        if month:
            if ampm == "pm" and hour != 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            now_uk = scrape_time.astimezone(UK_TZ)
            candidate = datetime(now_uk.year, month, day, hour, minute, tzinfo=UK_TZ)
            # if that date is far in the past, it must mean the year rolled over
            if candidate < now_uk - timedelta(days=300):
                candidate = candidate.replace(year=now_uk.year + 1)
            return candidate.astimezone(timezone.utc)

    # Attempt 3: closer to the deadline, Sky switches to a live countdown like
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
        print("NTFY_TOPIC not set, skipping ntfy push. Message was:", message)
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
    now = datetime.now(timezone.utc)
    hours_left = (deadline - now).total_seconds() / 3600

    # Since the deadline is recomputed from a live countdown each run, it'll
    # drift by a few seconds run to run even for the same round. Only treat it
    # as a genuinely new round (and reset reminders) if it moved by more than
    # 15 minutes.
    is_new_round = old_deadline is None or abs((deadline - old_deadline).total_seconds()) > 900
    if is_new_round:
        state = {"deadline": deadline_iso, "notified": []}
        print(f"New deadline detected: {deadline_iso}")
        # Only push a "new round" confirmation if we actually had a previous
        # deadline on record — skip it on the very first run ever, since
        # that's just the script starting up, not a new round appearing.
        if old_deadline is not None:
            send_push(
                "New Super 6 deadline",
                f"New round detected. Deadline is "
                f"{deadline.strftime('%a %d %b, %H:%M UTC')} "
                f"(about {hours_left:.1f}h from now).",
            )
    else:
        state["deadline"] = deadline_iso

    for threshold in REMINDER_HOURS_BEFORE:
        key = str(threshold)
        if hours_left <= threshold and key not in state["notified"]:
            reminder_text = (
                f"Deadline is in about {threshold}h "
                f"({deadline.strftime('%a %d %b, %H:%M UTC')}). "
                "Get your predictions in!"
            )
            send_push("Super 6 reminder", reminder_text)
            state["notified"].append(key)
            print(f"Sent {threshold}h reminder")

    save_state(state)
    print(f"Current deadline: {deadline_iso} ({hours_left:.1f}h from now)")


if __name__ == "__main__":
    asyncio.run(main())
