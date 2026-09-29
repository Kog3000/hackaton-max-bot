"""Собирает docs/Aperio-presentation.pdf из docs/presentation.html.

Нужен Playwright с Chromium:  pip install playwright && playwright install chromium
Схемы и скриншоты берутся из docs/schemes и docs/screens2 по относительным путям.
"""

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

DOCS = Path(__file__).parent
SOURCE = DOCS / "presentation.html"
RESULT = DOCS / "Aperio-presentation.pdf"
# если в системе уже есть Chromium, путь к нему можно передать переменной CHROMIUM_PATH
CHROMIUM = os.getenv("CHROMIUM_PATH") or None


async def build() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROMIUM)
        page = await browser.new_page(viewport={"width": 1600, "height": 900})
        await page.goto(SOURCE.as_uri(), wait_until="networkidle")
        await page.wait_for_timeout(600)
        await page.pdf(path=str(RESULT), prefer_css_page_size=True, print_background=True)
        await browser.close()
    print(f"готово: {RESULT} ({RESULT.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    asyncio.run(build())
