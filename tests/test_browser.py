"""Open the demo reviews in a real browser and check the layout at phone, laptop and desktop widths.

Skipped when Playwright's Chromium is not installed (`uv run playwright install chromium`),
except in CI, where a missing browser fails.
"""

import dataclasses
import importlib.util
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

if os.environ.get("CI"):
    from playwright import sync_api
else:
    sync_api = pytest.importorskip("playwright.sync_api")

DEMO = Path(__file__).resolve().parents[1] / "scripts" / "demo.py"
WIDTHS = (390, 1024, 1280)
LIST_PAGES = (
    "/",
    "/changes",
    "/changes?tab=changes_requested",
    "/changes?tab=archived",
    "/changes?scope=reviewer",
    "/changes?scope=author",
)
REPORT_PAGES = (
    "/r/demo-prorate-mid-cycle",
    "/r/demo-currency-rounding",
    "/r/demo-export-ledger?tab=plan",
    "/r/demo-price-cache",
    "/r/demo-price-cache?tab=execution",
    "/r/demo-webhook-retries",
)

# Returns what a reader would see as broken: sideways scrolling, list cells running into
# each other, text cut off without an ellipsis, and a status tab strip hiding the current tab
LAYOUT_PROBLEMS = """() => {
  const problems = [];
  const page = document.documentElement;
  if (page.scrollWidth > innerWidth + 1) {
    problems.push(`page scrolls sideways: ${page.scrollWidth}px wide in ${innerWidth}px`);
  }
  const name = (el) => el.className || el.tagName.toLowerCase();
  const text = (el) => el.innerText.replace(/\\s+/g, " ");
  for (const row of document.querySelectorAll(".chg")) {
    const cells = [...row.children]
      .map((cell) => [cell, cell.getBoundingClientRect()])
      .filter(([cell, box]) => box.width > 1 && getComputedStyle(cell).position !== "absolute");
    for (const [a, boxA] of cells) {
      for (const [b, boxB] of cells) {
        const sameLine = boxA.top < boxB.bottom && boxB.top < boxA.bottom;
        if (a !== b && sameLine && boxA.left <= boxB.left && boxA.right > boxB.left + 1) {
          problems.push(`"${name(a)}" runs into "${name(b)}" in "${text(row).slice(0, 60)}"`);
        }
      }
    }
  }
  for (const el of document.querySelectorAll(".chg *")) {
    const style = getComputedStyle(el);
    if (style.overflow === "hidden" && style.textOverflow !== "ellipsis"
        && el.clientWidth > 1 && el.scrollWidth > el.clientWidth + 1) {
      problems.push(`text cut off in "${name(el)}": "${text(el)}"`);
    }
  }
  const nav = document.querySelector("[data-status-nav]");
  const current = nav?.querySelector('[aria-current="page"]');
  if (current) {
    const box = current.getBoundingClientRect();
    const strip = nav.getBoundingClientRect();
    if (box.left < strip.left - 1 || box.right > strip.right + 1) {
      problems.push(`current status tab "${text(current)}" is scrolled out of view`);
    }
    if (nav.scrollLeft > 0 && box.right + nav.scrollLeft <= strip.right) {
      problems.push("status tabs are scrolled although the current tab fits");
    }
  }
  return problems;
}"""


@pytest.fixture
def live_url(settings, make_app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    app = make_app(dataclasses.replace(settings, base_url=url, db_pool_size=4))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the app did not start"
        time.sleep(0.05)
    yield url
    server.should_exit = True
    thread.join(10)


@pytest.fixture
def demo_url(live_url, monkeypatch):
    spec = importlib.util.spec_from_file_location("demo", DEMO)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    monkeypatch.setattr(demo, "URL", live_url)
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    demo.main()
    return live_url


@pytest.fixture
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except sync_api.Error as error:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"Chromium for Playwright does not start: {error}")
        yield browser
        browser.close()


@pytest.mark.parametrize("width", WIDTHS)
def test_demo_pages_lay_out_cleanly(browser, demo_url, width):
    context = browser.new_context(viewport={"width": width, "height": 900})
    context.add_cookies(
        [
            {"name": "vf_id", "value": "alice-demo", "url": demo_url},
            {"name": "vf_name", "value": "Alice", "url": demo_url},
        ]
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: message.type == "error" and errors.append(message.text))
    problems = []
    for path in LIST_PAGES + REPORT_PAGES:
        response = page.goto(demo_url + path)
        page.evaluate("document.fonts.ready")
        if response.status != 200:
            problems.append(f"{path}: HTTP {response.status}")
        else:
            problems += [f"{path}: {problem}" for problem in page.evaluate(LAYOUT_PROBLEMS)]
        problems += [f"{path}: browser error: {error}" for error in errors]
        errors.clear()
    page.goto(demo_url + "/")
    viewer = page.locator(".me-name").first.inner_text()
    context.close()

    assert viewer == "Alice", f"the pages were shown as {viewer!r}, not the demo user Alice"

    assert not problems, f"at {width}px:\n" + "\n".join(problems)
