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
from datetime import datetime, timezone
from playwright.async_api import async_playwright

SUPER6_URL = "https://super6.skysports.com/"
STATE_FILE = "state.json"
DEBUG_FILE = "deadline_debug.txt"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # set this as a GitHub secret
REMINDER_HOURS_BEFORE = [24, 2]  # send a push this many hours before deadline


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
        await browser.close()

    with open(DEBUG_FILE, "w") as f:
        f.write("Raw <time datetime> values found:\n")
        f.write("\n".join(times) if times else "(none)")
        f.write("\n\nFull visible page text (for manual inspection):\n")
        f.write(body_text)

    now = datetime.now(timezone.utc)
    future = []
    for t in times:
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if dt > now:
                future.append(dt)
        except ValueError:
            continue

    if not future:
        raise RuntimeError(
            "Couldn't find any future kickoff times on the page. "
            f"Check {DEBUG_FILE} to see what the scraper actually saw — "
            "the site's markup may have changed, or fixtures may need a "
            "logged-in session to view."
        )

    return min(future)


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

    if state["deadline"] != deadline_iso:
        # new round detected — reset which reminders have fired
        state = {"deadline": deadline_iso, "notified": []}
        print(f"New deadline detected: {deadline_iso}")

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
