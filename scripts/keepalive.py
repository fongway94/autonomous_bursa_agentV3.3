#!/usr/bin/env python3
"""
Streamlit Community Cloud keepalive — real browser visit, not an HTTP ping.

Why this exists
---------------
UptimeRobot (and every other HTTP-only monitor) CANNOT keep a Streamlit app
awake. Streamlit Cloud serves a ~4 KB static HTML shell to a plain GET and
returns 200 without ever starting the Python process:

    HTTP GET -> static HTML shell (200 OK) -> Python never runs
                JS executes -> WebSocket /_stcore/stream -> app boots

So the monitor sees "up", reports green, and the container keeps sleeping.
Per Streamlit's docs, apps with no traffic for 12 hours hibernate:
https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app#app-hibernation

This script drives a headless Chromium instead. It executes the JS, opens the
real WebSocket, and — if the app is already asleep — clicks the
"Yes, get this app back up!" button to wake it.

Usage
-----
    pip install playwright && playwright install chromium --with-deps
    STREAMLIT_APP_URL="https://your-app.streamlit.app" python scripts/keepalive.py

Multiple apps: comma-separate the URLs.

Exit codes
----------
    0  every app is awake and rendered
    1  at least one app failed to wake (CI turns red)
"""

from __future__ import annotations

import os
import sys
import time

# The app is considered "really running" once Streamlit has mounted its root
# node. The shell page alone never produces this.
APP_ROOT_SELECTOR = 'div[data-testid="stAppViewContainer"], section.main, div.stApp'

# Text on the button Streamlit shows on a hibernating app.
WAKE_BUTTON_TEXT = "Yes, get this app back up!"

# Generous: a cold Streamlit container reinstalling deps can take a while.
PAGE_TIMEOUT_MS = 120_000
RENDER_SETTLE_MS = 12_000


def _urls() -> list[str]:
    raw = os.environ.get("STREAMLIT_APP_URL", "").strip()
    if not raw:
        print("ERROR: STREAMLIT_APP_URL is not set.", file=sys.stderr)
        print(
            "  Set it as a GitHub repo variable (Settings -> Secrets and "
            "variables -> Actions -> Variables) or export it locally.",
            file=sys.stderr,
        )
        sys.exit(1)
    return [u.strip() for u in raw.split(",") if u.strip()]


def wake(page, url: str) -> bool:
    """Visit one app and make sure it is actually running. True on success."""
    print(f"\n=== {url} ===", flush=True)

    try:
        page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    except Exception as e:
        print(f"  FAIL: could not load page: {type(e).__name__}: {e}", flush=True)
        return False

    # If the app is hibernating, Streamlit renders a wake button. Click it.
    try:
        button = page.get_by_text(WAKE_BUTTON_TEXT, exact=False)
        if button.count() > 0:
            print("  App was ASLEEP -> clicking wake button...", flush=True)
            button.first.click(timeout=30_000)
            # Cold start: container boots, deps load, script reruns.
            page.wait_for_timeout(45_000)
        else:
            print("  No wake button (app was already awake).", flush=True)
    except Exception as e:
        # Non-fatal: the button may have vanished because the app woke on its
        # own between the goto and the query.
        print(f"  note: wake-button step skipped ({type(e).__name__})", flush=True)

    # Confirm Streamlit actually mounted — this is the part an HTTP ping can
    # never assert.
    try:
        page.wait_for_selector(APP_ROOT_SELECTOR, timeout=PAGE_TIMEOUT_MS)
    except Exception:
        print("  FAIL: Streamlit root never mounted (still asleep or erroring).",
              flush=True)
        try:
            print(f"  page title: {page.title()!r}", flush=True)
        except Exception:
            pass
        return False

    # Hold the WebSocket open briefly so Streamlit records real session traffic
    # and the scheduler thread in app.py gets a chance to spin up.
    page.wait_for_timeout(RENDER_SETTLE_MS)

    try:
        title = page.title()
    except Exception:
        title = "<unknown>"

    print(f"  OK: app is awake and rendered (title={title!r})", flush=True)
    return True


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright is not installed.", file=sys.stderr)
        print("  pip install playwright && playwright install chromium --with-deps",
              file=sys.stderr)
        return 1

    urls = _urls()
    started = time.time()
    results: dict[str, bool] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 "
                    "bursa-agent-keepalive"
                ),
            )
            page = context.new_page()
            for url in urls:
                try:
                    results[url] = wake(page, url)
                except Exception as e:
                    print(f"  FAIL: unexpected error: {type(e).__name__}: {e}",
                          flush=True)
                    results[url] = False
        finally:
            browser.close()

    ok = sum(1 for v in results.values() if v)
    print(f"\n--- {ok}/{len(results)} app(s) awake in {time.time() - started:.0f}s ---",
          flush=True)
    for url, good in results.items():
        print(f"  {'OK  ' if good else 'FAIL'}  {url}", flush=True)

    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
