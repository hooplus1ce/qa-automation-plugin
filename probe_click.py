"""验证: 点击空下拉的 hidden selected-value → 深度上限内回退到 __rendered。"""
import asyncio
import json
import time

from qa_mcp.tools.browser import browser_mgr, click_interact_impl

IFRAME_SEL = "div[aria-hidden=false] iframe"


async def main() -> None:
    await browser_mgr.get_page()
    t0 = time.time()
    try:
        r = await click_interact_impl(by="css",
                                      selector="div.ant-select-selection-selected-value",
                                      iframe_selector=IFRAME_SEL, detail="brief")
        print(f"[result] {time.time()-t0:.1f}s status={r.get('status')}")
        print("[ancestor]", r.get("clicked_ancestor"))
        print("[obs]", json.dumps(r.get("observation", {}), ensure_ascii=False)[:200])
    except Exception as e:
        print(f"[EXC] {time.time()-t0:.1f}s {type(e).__name__}: {str(e)[:300]}")


asyncio.run(main())
