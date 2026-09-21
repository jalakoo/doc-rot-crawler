"""Render images/architecture-flowchart.html to a PNG.

    .venv/bin/python images/render.py

Uses the Chromium that pytest-playwright already installs for the browser tests,
at 2x so the pixel type stays sharp on a retina screen.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "architecture-flowchart.html"
OUT = HERE / "architecture-flowchart.png"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1700, "height": 600}, device_scale_factor=2)
        page.goto(SOURCE.as_uri())
        # The two pixel typefaces come from Google Fonts; screenshotting before they
        # arrive renders the whole diagram in a fallback monospace.
        page.wait_for_function("document.fonts.ready.then(() => true)")
        # The body is the drawing; screenshotting the page would pad it out
        # to the viewport height.
        page.locator("body").screenshot(path=OUT)
        browser.close()
    print(f"wrote {OUT.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
