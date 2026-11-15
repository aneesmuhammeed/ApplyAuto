import asyncio
from playwright.async_api import async_playwright

async def inspect():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto('https://job-boards.greenhouse.io/netlify/jobs/4224129002?gh_src=9f1ba7492', wait_until='networkidle')
        
        cb = await page.query_selector('#question_8159407002')
        parent_container = await cb.query_selector("xpath=ancestor::div[contains(@class, 'select')]")
        control = await parent_container.query_selector(".select__control") if parent_container else None
        
        print("Before click, aria-controls:", await cb.get_attribute("aria-controls"))
        await control.click()
        await asyncio.sleep(0.4)
        aria_controls = await cb.get_attribute("aria-controls")
        print("After click, aria-controls:", aria_controls)
        
        if aria_controls:
            listbox = await page.query_selector(f'[id="{aria_controls}"]')
            print("listbox element found?", bool(listbox))
            if listbox:
                opt_els = await listbox.query_selector_all('[role="option"], .select__option')
                print(f"opt_els count: {len(opt_els)}")
                for o in opt_els:
                    print("  Text:", await o.inner_text())

        await browser.close()

if __name__ == '__main__':
    asyncio.run(inspect())
