#!/usr/bin/env python3
import asyncio
from playwright.async_api import async_playwright

async def inspect():
    targets = ['GEM/2026/B/8019919', 'GEM/2026/B/7996149', 'GEM/2026/B/7405559']
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto('https://bidplus.gem.gov.in/all-bids', wait_until='domcontentloaded')
        await page.wait_for_selector('#searchBid')

        for b in targets:
            print(f'=== BID {b} ===')
            inp = await page.query_selector('#searchBid')
            await inp.fill(b)
            btn = await page.query_selector('#searchBidRA')
            if btn:
                await btn.click()
            await asyncio.sleep(4)

            cards = await page.query_selector_all('#bidCard .card')
            for card in cards:
                t = await card.inner_text()
                if b in t:
                    # Click View Corrigendum/Representation
                    link1 = await card.query_selector('a:has-text("Corrigendum")')
                    if link1:
                        print('  Clicking first link: View Corrigendum/Representation...')
                        await link1.click()
                        await asyncio.sleep(3)

                        # Now check if 'View Corrigendum' link appeared inside card
                        link2 = await card.query_selector('a:has-text("View Corrigendum")')
                        if link2:
                            print('  Clicking second link: View Corrigendum...')
                            await link2.click()
                            await asyncio.sleep(4)
                            
                            # Check modal content
                            m5 = await page.query_selector('#myModal5')
                            if m5 and await m5.is_visible():
                                mtext = await m5.inner_text()
                                print(f'  MODAL TEXT FOR {b}:\n{mtext}')
                                
                                # Close modal
                                close_btn = await page.query_selector('#myModal5 .close, #myModal5 button[data-dismiss="modal"]')
                                if close_btn:
                                    await close_btn.click()
                                    await asyncio.sleep(2)
                        else:
                            print('  No second link appeared (corrigendum is False). Inner text:')
                            det = await card.query_selector('[id^="other-details-"]')
                            if det:
                                print('   ', (await det.inner_text()).strip())
            await asyncio.sleep(2)

        await browser.close()

if __name__ == '__main__':
    asyncio.run(inspect())
