"""Temporary diagnostic for ACP SLI public search UI.

Runs a small number of normal, public searches and records page state.
No authentication, bypass, or hidden API calls.
"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

HOME = "https://apps.pancanal.com/sli/LicitacionesBusqueda/Welcome"
DEST = Path("data/downloads")
DEST.mkdir(parents=True, exist_ok=True)

async def main():
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True, locale="es-PA", timezone_id="America/Panama")
        for status in ("ABIERTAS", "ADJUDICACIÓN", "EVALUACIÓN"):
            page = await context.new_page()
            network = []
            page.on("response", lambda r: network.append({"url": r.url.split("?")[0], "status": r.status, "method": r.request.method}) if "pancanal.com/sli/" in r.url else None)
            report = {"query": status, "network": network}
            try:
                response = await page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                report["home_status"] = response.status if response else None
                await page.wait_for_timeout(1200)
                sel = page.locator("select").first
                report["select_before"] = await sel.evaluate("(e) => ({id:e.id,name:e.name,value:e.value,html:e.outerHTML.slice(0,1000)})")
                await sel.select_option(label=status)
                report["select_after"] = await sel.evaluate("(e) => ({id:e.id,name:e.name,value:e.value})")
                await page.locator('input[value="Desplegar resultado"]').first.click(timeout=15000)
                await page.wait_for_timeout(4000)
                report["url"] = page.url
                report["page"] = await page.evaluate("""() => ({
                    title: document.title,
                    text: document.body.innerText.slice(0,6000),
                    controls: [...document.querySelectorAll("button,a,input[type=button],input[type=submit]")].map(x=>({
                        tag:x.tagName,id:x.id,text:(x.textContent||x.value||"").trim().slice(0,90),
                        href:x.getAttribute("href"),title:x.title,onclick:x.getAttribute("onclick")
                    })).filter(x=>x.text || x.title || x.onclick).slice(0,150),
                    tables: [...document.querySelectorAll("table")].map(x=>({rows:x.rows.length,html:x.outerHTML.slice(0,1500)})).slice(0,5)
                })""")
                await page.screenshot(path=str(DEST / ("diag-"+str(len(out)+1)+".png")),full_page=True)
                (DEST / ("diag-"+str(len(out)+1)+".html")).write_text(await page.content(),encoding="utf-8")
            except Exception as e:
                report["error"] = type(e).__name__+": "+str(e)
                try:
                    await page.screenshot(path=str(DEST / ("diag-"+str(len(out)+1)+"-error.png")),full_page=True)
                except Exception:
                    pass
            out.append(report)
            print(json.dumps({"query":status,"url":report.get("url"),"error":report.get("error"),"page_excerpt":report.get("page",{}).get("text","")[:550]},ensure_ascii=False))
            await page.close()
            await asyncio.sleep(2)
        await browser.close()
    (DEST / "search-diagnostics.json").write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")

if __name__=="__main__":
    asyncio.run(main())
