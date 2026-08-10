import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional
from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from qa_mcp.config import (
    CDP_URL,
    EVIDENCE_DIR,
    DOWNLOAD_DIR,
    PROJECT_DIR,
    ELEMENT_WAIT_TIMEOUT_MS,
    OBSERVE_WAIT_MS,
    ACTION_STEP_TIMEOUT_MS,
    ACTION_RETRY_ATTEMPTS,
    ACTION_RETRY_BACKOFF_MS,
    CONNECT_RETRY_ATTEMPTS,
    CONNECT_RETRY_BACKOFF_MS,
)
from qa_mcp.utils.dynamic_layers import scan_dynamic_layers
from qa_mcp.utils.ui_adapters import adapter_registry

logger = logging.getLogger("mcp_automation.browser")

class BrowserManager:
    def __init__(self, cdp_url: str = CDP_URL):
        self.cdp_url = cdp_url
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        # 目标标签页锁定: 首次选择后固定作用于同一标签页, 防止用户在浏览器中
        # 新开/切换标签页导致 MCP 误操作其他页面 (丢失测试上下文);
        # 锁定页被关闭后自动重新选择, 或通过 switch_target_page 显式重绑。
        self._target_page: Optional[Page] = None
        # 连接重建互斥: 并发工具调用时防止多个协程同时重连 (asyncio.Lock 不可重入,
        # 锁内不得再调 close; 内部统一走 _close_unlocked)
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        """建立 CDP 连接。

        首次连接失败按指数退避重试 (常见场景: MCP 服务先于 Chrome 启动,
        或 Chrome 正重启中)。重试耗尽后抛最后一次异常。
        """
        last_exc: Optional[Exception] = None
        for attempt in range(CONNECT_RETRY_ATTEMPTS):
            try:
                # no_defaults (Playwright v1.60+): 接管用户日常浏览器时禁用 Playwright
                # 对默认上下文的默认覆盖 (下载行为/焦点模拟/媒体模拟), 不干扰用户浏览器状态。
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self.cdp_url, no_defaults=True
                )
                self._context = self._browser.contexts[0]
                return
            except Exception as e:
                last_exc = e
                logger.warning(
                    f"Failed to connect to Chrome at {self.cdp_url} "
                    f"(第 {attempt + 1}/{CONNECT_RETRY_ATTEMPTS} 次): {e}"
                )
                if attempt < CONNECT_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(
                        CONNECT_RETRY_BACKOFF_MS / 1000 * (2**attempt)
                    )
        assert last_exc is not None
        raise last_exc

    async def _select_page(self) -> Page:
        """从当前 context 中智能匹配目标标签页。

        选择规则 (按优先级):
          1. 已锁定的目标页仍存活 → 直接复用 (用户新开/切换标签页不影响测试上下文);
          2. 可见 (document.visibilityState === 'visible') 的 hoolinks.com 标签页;
          3. 任意 hoolinks.com 标签页;
          4. 非系统内建页 (chrome:// edge:// about:);
          5. 兜底: 第一个标签页。
        选定后锁定为 _target_page, 直到其被关闭或显式 switch_target_page。
        """
        if not self._context or not self._context.pages:
            raise RuntimeError("No active tab found in the Chrome browser session.")

        # 1) 锁定页复用
        if (
            self._target_page is not None
            and not self._target_page.is_closed()
            and self._target_page in self._context.pages
        ):
            return self._target_page

        # 2-5) 重新选择 (首次连接或锁定页被关闭); 先剔除已关闭页 (防列表竞态)
        pages = [p for p in list(self._context.pages) if not p.is_closed()]
        try:
            visible_hoolinks = [
                p for p in pages
                if "hoolinks.com" in p.url and await self._is_visible(p)
            ]
        except Exception:
            visible_hoolinks = []
        hoolinks = [p for p in pages if "hoolinks.com" in p.url]
        non_system_pages = [
            p for p in pages if not p.url.startswith(("chrome://", "edge://", "about:"))
        ]
        target_page = (visible_hoolinks or hoolinks or non_system_pages or [pages[0]])[0]
        logger.info(f"锁定目标标签页: {target_page.url}")
        self._target_page = target_page
        return target_page

    async def _is_visible(self, page: Page) -> bool:
        """探测标签页是否为浏览器当前激活页 (窗口前台时可见)。探测失败按不可见处理。"""
        try:
            return bool(await asyncio.wait_for(
                page.evaluate("() => document.visibilityState === 'visible'"), timeout=3
            ))
        except (asyncio.TimeoutError, Exception):
            return False

    def reset_target(self) -> None:
        """清除目标页锁定 (下次 get_page 重新选择)。"""
        self._target_page = None

    async def switch_target(self, url_pattern: str) -> Page:
        """按 URL 子串匹配切换目标页并锁定; 无匹配抛 RuntimeError。"""
        pages = list(self._context.pages) if self._context else []
        matches = [p for p in pages if url_pattern in p.url]
        if not matches:
            raise RuntimeError(
                f"未找到 URL 包含 [{url_pattern}] 的标签页; 当前标签页: {[p.url for p in pages]}"
            )
        self._target_page = matches[0]
        logger.info(f"切换目标标签页: {self._target_page.url}")
        return self._target_page

    async def get_page(self) -> Page:
        async with self._lock:
            if not self._playwright:
                self._playwright = await async_playwright().start()
            if not self._browser:
                await self._connect()

            # 连接可能陈旧 (Chrome 重启 / 标签页全部关闭), 此时 _select_page 会抛错;
            # 自动重建一次连接后重试, 避免 MCP 服务长驻期间浏览器重启后永久失效。
            try:
                return await self._select_page()
            except Exception:
                logger.warning(
                    "浏览器连接状态陈旧 (无可用标签页), 正在重建 CDP 连接..."
                )
                await self._close_unlocked()
                self._playwright = await async_playwright().start()
                await self._connect()
                return await self._select_page()

    async def _close_unlocked(self) -> None:
        """释放连接与全部字段 (调用方必须持有 _lock)。

        先复位字段、再停连接: 半开连接上 stop() 可能无限等待, 外部 wait_for
        强杀后字段已复位, 下次 get_page 走全新连接而非残留半死对象。
        """
        pw = self._playwright
        self._playwright = None
        self._browser = None
        self._context = None
        self._target_page = None
        if pw:
            try:
                await asyncio.wait_for(pw.stop(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass

    async def recover(self, preferred_url: Optional[str] = None) -> Page:
        """重建 CDP 连接 (动作被 wait_for 强杀后的自愈入口)。

        触发场景: asyncio.wait_for 强杀一个 CDP 请求半开的协程后, Playwright 底层
        可能残留 pending 协议请求, 导致后续所有工具调用全部排队挂死 (现象: 同一
        定位单独调用成功、紧随失败动作后调用却超时)。重建连接是最干净的恢复方式,
        等价于 MCP 服务重启但保留浏览器目标页锁定语义。

        preferred_url: 重建后优先按 URL 子串恢复目标页锁定; 无匹配则走默认选择规则。
        """
        async with self._lock:
            # 先复位字段再停连接 (同 _close_unlocked 语义): 旧连接可能半开,
            # stop() 无限等待被外部强杀后字段已复位, 下次 get_page 走全新连接。
            pw = self._playwright
            self._playwright = None
            self._browser = None
            self._context = None
            self._target_page = None
            if pw:
                try:
                    await asyncio.wait_for(pw.stop(), timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass

            self._playwright = await async_playwright().start()
            await self._connect()
            if preferred_url:
                try:
                    self._target_page = await self.switch_target(preferred_url)
                    return self._target_page
                except RuntimeError:
                    pass
            return await self._select_page()

    async def close(self):
        """关闭 CDP 连接 (幂等, 协程安全)。"""
        async with self._lock:
            await self._close_unlocked()

browser_mgr = BrowserManager()

async def get_frame_path(frame) -> List[str]:
    path = []
    current = frame
    while current and current.parent_frame:
        element = await current.frame_element()
        if element:
            selector = await element.evaluate("""el => {
                if (el.id) return `#${el.id}`;
                if (el.name) return `iframe[name="${el.name}"]`;
                if (el.getAttribute('data-testid')) return `iframe[data-testid="${el.getAttribute('data-testid')}"]`;
                const parent = el.parentNode;
                if (parent) {
                    const siblings = Array.from(parent.querySelectorAll('iframe'));
                    const index = siblings.indexOf(el);
                    if (index !== -1) return `iframe:nth-of-type(${index + 1})`;
                }
                return 'iframe';
            }""")
            path.insert(0, selector)
        current = current.parent_frame
    return path

async def analyze_elements_impl() -> dict:
    page = await browser_mgr.get_page()
    url = page.url
    title = await page.title()
    framework = await adapter_registry.detect_framework(page)
    
    all_elements = []
    for frame in page.frames:
        frame_path = await get_frame_path(frame)
        try:
            elements = await frame.evaluate("""() => {
                const results = [];
                const interactables = document.querySelectorAll(
                    'button, input, select, textarea, [role="button"], [role="combobox"], a, ' +
                    '.ant-select, .ant-picker, .ant-cascader, .ant-tree-select, ' +
                    '.ant-checkbox-wrapper, .ant-radio-wrapper, .ant-switch'
                );

                const antComponentSelector = [
                    '.ant-select',
                    '.ant-picker',
                    '.ant-cascader',
                    '.ant-tree-select',
                    '.ant-checkbox-wrapper',
                    '.ant-radio-wrapper',
                    '.ant-switch'
                ].join(', ');

                function isVisibleElement(el) {
                    let current = el;
                    while (current && current.nodeType === Node.ELEMENT_NODE) {
                        const className = String(current.className || '');
                        const style = getComputedStyle(current);
                        if (
                            current.hidden ||
                            current.getAttribute('aria-hidden') === 'true' ||
                            /hidden/i.test(className) ||
                            style.display === 'none' ||
                            style.visibility === 'hidden' ||
                            style.visibility === 'collapse' ||
                            Number.parseFloat(style.opacity || '1') === 0
                        ) return false;
                        current = current.parentElement;
                    }
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }

                function makeUniqueSelector(selector, el) {
                    try {
                        const matches = Array.from(document.querySelectorAll(selector));
                        const visibleMatches = matches.filter(isVisibleElement);
                        const candidates = visibleMatches.includes(el) ? visibleMatches : matches;
                        if (candidates.length <= 1) return selector;
                        const index = candidates.indexOf(el);
                        return index >= 0 ? `${selector} >> nth=${index}` : selector;
                    } catch (error) {
                        return selector;
                    }
                }

                function getAccessibleName(el) {
                    const ariaLabelledBy = el.getAttribute('aria-labelledby');
                    if (ariaLabelledBy) {
                        const labelledText = ariaLabelledBy
                            .split(/\\s+/)
                            .map(id => document.getElementById(id)?.innerText || '')
                            .join(' ')
                            .trim();
                        if (labelledText) return labelledText;
                    }

                    const ariaLabel = el.getAttribute('aria-label');
                    if (ariaLabel) return ariaLabel.trim();

                    const label = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
                    if (label?.innerText) return label.innerText.trim();

                    const parentLabel = el.closest('label');
                    if (parentLabel?.innerText) return parentLabel.innerText.trim();

                    const antComponent = el.closest(antComponentSelector);
                    const antInput = antComponent?.querySelector('input[aria-label], input[placeholder], input');
                    const componentText = (antComponent?.innerText || '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    return (
                        el.getAttribute('placeholder') ||
                        el.title ||
                        antInput?.getAttribute('aria-label') ||
                        antInput?.getAttribute('placeholder') ||
                        componentText.slice(0, 80)
                    ).trim();
                }
                
                function getCleanSelector(el) {
                    if (el.id) return makeUniqueSelector(`#${el.id}`, el);
                    if (el.name) return makeUniqueSelector(`[name="${el.name}"]`, el);
                    if (el.getAttribute('data-testid')) {
                        return makeUniqueSelector(`[data-testid="${el.getAttribute('data-testid')}"]`, el);
                    }

                    const antRoot = el.matches(antComponentSelector) ? el : null;
                    if (antRoot) {
                        const stableClass = Array.from(antRoot.classList).find(c =>
                            /^ant-(select|picker|cascader|tree-select|checkbox-wrapper|radio-wrapper|switch)$/.test(c)
                        );
                        if (stableClass) return makeUniqueSelector(`.${stableClass}`, el);
                    }
                    
                    let path = [];
                    let temp = el;
                    while (temp && temp.nodeType === Node.ELEMENT_NODE) {
                        let selector = temp.nodeName.toLowerCase();
                        if (temp.id) {
                            selector += `#${temp.id}`;
                            path.unshift(selector);
                            break;
                        }
                        if (temp.className) {
                            const classes = Array.from(temp.classList)
                                .filter(c => !c.includes('active') && !c.includes('focus') && !c.includes('hover'));
                            if (classes.length) selector += '.' + classes.slice(0, 2).join('.');
                        }
                        path.unshift(selector);
                        temp = temp.parentNode;
                    }
                    return makeUniqueSelector(path.slice(-3).join(' > '), el);
                }

                interactables.forEach(el => {
                    if (isVisibleElement(el)) {
                        const role = el.getAttribute('role') || '';
                        const antComponent = el.closest(antComponentSelector);
                        results.push({
                            tagName: el.tagName.toLowerCase(),
                            text: el.innerText ? el.innerText.trim().substring(0, 30) : '',
                            placeholder: el.placeholder || '',
                            role,
                            accessible_name: getAccessibleName(el),
                            component: antComponent ? antComponent.className : '',
                            selector: getCleanSelector(el)
                        });
                    }
                });
                return results;
            }""")
            
            for el in elements:
                el["frame_path"] = frame_path
                all_elements.append(el)
        except Exception:
            continue

    # 扁平化 ref 编号 (全局连续; 在截断前编号, ref 与返回顺序一致)
    for i, el in enumerate(all_elements):
        el["ref"] = f"e{i + 1}"

    return {
        "url": url,
        "title": title,
        "detected_framework": framework,
        "elements": all_elements[:100]
    }


async def probe_dynamic_layers_impl(
    iframe_selector: Optional[str] = None,
    wait_ms: int = 1200,
    poll_interval_ms: int = 100,
    detail: str = "brief",
) -> dict:
    """探查目标 iframe 中刚出现的弹窗、消息和悬浮层。
    detail: brief(默认, 剪枝输出) | full(完整输出)。"""
    page = await browser_mgr.get_page()
    return await scan_dynamic_layers(
        page,
        get_frame_path,
        iframe_selector=iframe_selector,
        wait_ms=wait_ms,
        poll_interval_ms=poll_interval_ms,
        detail=detail,
    )


# ==================== 点击交互统一观察机制 (公共) ====================
# 所有点击类工具 (click_interact / vtable_click_at / vtable_select_rows /
# vtable_drag_column / execute_and_record) 共用同一套点击后观察:
#   1. 浮窗/弹窗/下拉浮层: scan_dynamic_layers 扫描 (dynamic_layers / new_layers)
#   2. 消息提示弹窗: kind=message 的层 (随 new_layers 一并上报)
#   3. tab 页跳转: 主页面 URL 前后对比 (navigation.url_changed)
#   4. iframe 跳转: iframe 清单 (id/src/可见性) 前后对比 (navigation.frames_changed)

async def scan_frames(page) -> List[Dict[str, Any]]:
    """扫描顶层文档中所有 iframe 的 id/src/可见性, 用于点击前后对比是否发生 iframe 跳转。

    主线程假死/渲染繁忙时 evaluate 可能无限等待协议响应, 3s 超时后按空清单
    尽力而为 (前后对比降级为全量上报, 不阻塞点击流程)。
    """
    try:
        return await asyncio.wait_for(page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('iframe').forEach(f => {
                const r = f.getBoundingClientRect();
                out.push({
                    id: f.id || '',
                    src: f.src || '',
                    visible: !!(f.offsetWidth || f.offsetHeight || r.width)
                });
            });
            return out;
        }"""), timeout=3)
    except (asyncio.TimeoutError, Exception):
        return []


async def popup_fingerprint(page) -> Dict[str, Dict[str, Any]]:
    """扫描顶层文档与所有 iframe 中当前可见的弹层(下拉/筛选面板/消息等),
    返回 key -> layer 指纹, 用于点击前后对比判断是否弹出了新层。

    注意: 这是【点击前】快照 —— 页面处于静止态, 单次扫描即可,
    不设轮询等待 (wait_ms=0), 否则每次点击前都会固定空等 ~800ms
    (表现为: 光标动画播完 → 快照空转 → 才真正执行点击)。
    轮询捕捉短暂弹层的任务交给点击后的 observe_after_click (wait_ms=1500)。
    """
    probe = await scan_dynamic_layers(
        page, get_frame_path, iframe_selector=None, wait_ms=0
    )
    fingerprint = {}
    for layer in probe.get("layers", []):
        key = "|".join([
            "->".join(layer.get("frame_path", [])),
            str(layer.get("kind", "")),
            str(layer.get("selector", "")),
            str(layer.get("text", "")),
        ])
        fingerprint[key] = layer
    return fingerprint


async def snapshot_navigation(page) -> Dict[str, Any]:
    """点击前导航快照: 主页面 URL + iframe 清单 + 可见弹层指纹。"""
    frames = await scan_frames(page)
    fp = await popup_fingerprint(page)
    return {"url": page.url, "frames": frames, "popup_fingerprint": fp}


# ==================== Portal 域隔离 (Modal Focus 摘要) ====================
# 页面存在 modal 弹层时, 背景处于遮罩且不可交互; 全量上报背景层是 Token 浪费。
# 本脚本提取每个 frame 中顶层可见 modal 的标题与内部可交互元素, 供观察结果
# focus 字段使用 (背景层 text 相应折叠)。

FOCUS_MODAL_SCAN_SCRIPT = r"""() => {
    const visible = (el) => {
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const roots = Array.from(document.querySelectorAll(
        '.ant-modal-wrap, .ant-modal-root, .ant-drawer, [role="dialog"]'
    )).filter(visible);
    // 只保留顶层 modal (不被其他 modal 包含)
    const top = roots.filter(el => !roots.some(other => other !== el && other.contains(el)));
    function selFor(el) {
        if (el.id) return `#${CSS.escape(el.id)}`;
        const path = [];
        let cur = el;
        while (cur && cur.nodeType === Node.ELEMENT_NODE && path.length < 3) {
            let part = cur.tagName.toLowerCase();
            const cls = (typeof cur.className === 'string' ? cur.className : cur.getAttribute('class') || '')
                .split(/\s+/).filter(Boolean).slice(0, 2);
            if (cls.length) part += '.' + cls.map(CSS.escape).join('.');
            path.unshift(part);
            cur = cur.parentElement;
        }
        return path.join(' > ');
    }
    return top.slice(0, 2).map(root => {
        const els = Array.from(root.querySelectorAll(
            'button, input, select, textarea, a, [role="button"], [role="combobox"]'
        )).filter(visible);
        return {
            selector: root.id ? `#${CSS.escape(root.id)}` :
                (typeof root.className === 'string' && root.className.trim()
                    ? '.' + root.className.split(/\s+/).filter(Boolean).map(CSS.escape).join('.')
                    : root.tagName.toLowerCase()),
            title: (root.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100),
            interactive: els.slice(0, 30).map(el => ({
                name: (el.getAttribute('aria-label') || el.title || el.placeholder || el.innerText || '')
                    .replace(/\s+/g, ' ').trim().slice(0, 80),
                role: el.getAttribute('role') || el.tagName.toLowerCase(),
                selector: selFor(el)
            }))
        };
    });
}"""


async def _scan_focus_modals(page) -> List[Dict[str, Any]]:
    """扫描所有 frame 中顶层可见 modal, 返回摘要列表 (每项含 frame_path)。
    任一 frame 执行失败仅跳过该 frame, 不影响其余。
    """
    modals = []
    for frame in page.frames:
        try:
            frame_path = await asyncio.wait_for(get_frame_path(frame), timeout=3)
            found = await asyncio.wait_for(
                frame.evaluate(FOCUS_MODAL_SCAN_SCRIPT), timeout=3
            )
        except (asyncio.TimeoutError, Exception):
            continue
        for m in found:
            m["frame_path"] = frame_path
            modals.append(m)
    return modals


async def observe_after_click(
    page,
    before: Dict[str, Any],
    wait_ms: int = OBSERVE_WAIT_MS,
    detail: str = "brief",
) -> Dict[str, Any]:
    """
    点击后统一观察 (与 execute_and_record 对齐):
      1. dynamic_layers: 扫描当前可见的浮窗/弹窗/下拉浮层/消息提示
         (brief 模式剪枝: html≤600/文本≤300/交互元素≤20/attributes 精简);
      2. new_layers: 对比点击前指纹, 只返回【本次点击新出现】的层
         (kind=message 的消息提示弹窗同样上报, 便于断言操作反馈);
      3. summary: 每层一行摘要 [kind] frame::selector: text[:80], 供快速浏览;
      4. focus: Portal 域隔离摘要 —— 存在 modal 时只详报 modal 内部交互元素,
         背景层 text 折叠为 40 字符 (background_collapsed=true);
      5. navigation.url_changed: 主页面 URL 是否变化 (tab 页跳转/路由跳转);
      6. navigation.frames_changed: iframe 清单 (id/src/可见性) 是否变化 (iframe 跳转)。
    detail: brief(默认) | full 透传 scan_dynamic_layers 控制输出体积。
    """
    probe = await scan_dynamic_layers(
        page, get_frame_path, iframe_selector=None, wait_ms=wait_ms, detail=detail
    )
    after_url = page.url
    after_frames = await scan_frames(page)
    layers = probe.get("layers", [])

    # 新增层 = 点击后出现且点击前不存在的层
    new_layers = []
    for layer in layers:
        key = "|".join([
            "->".join(layer.get("frame_path", [])),
            str(layer.get("kind", "")),
            str(layer.get("selector", "")),
            str(layer.get("text", "")),
        ])
        if key not in before.get("popup_fingerprint", {}):
            new_layers.append(layer)

    # Portal 域隔离: 存在 modal 时, 非 modal 背景层 text 折叠 (遮罩下不可交互,
    # 保留 modal 内下拉等浮层: 判定层是否位于某个 modal 内部)
    focus_modals = await _scan_focus_modals(page)
    has_modal = bool(focus_modals)
    if has_modal:
        frame_selectors: Dict[str, List[str]] = {}
        for m in focus_modals:
            frame_selectors.setdefault("->".join(m.get("frame_path", [])), []).append(m.get("selector", ""))
        for layer in layers:
            if layer.get("kind") == "modal":
                continue
            fkey = "->".join(layer.get("frame_path", []))
            sels = frame_selectors.get(fkey, [])
            inside = False
            if sels:
                # 注意: 不能用 next((f for f in page.frames if await ...), None) ——
                # async 函数内含 await 的生成器表达式会编译为 async generator,
                # next() 对其调用抛 TypeError: 'async_generator' object is not an iterator。
                target_frame_path = layer.get("frame_path", [])
                frame = None
                for f in page.frames:
                    if await get_frame_path(f) == target_frame_path:
                        frame = f
                        break
                if frame is not None:
                    try:
                        inside = await asyncio.wait_for(
                            frame.evaluate(
                                """(sels) => {
                                    const el = sels.map(s => { try { return document.querySelector(s); } catch (e) { return null; } })
                                        .find(e => e);
                                    if (!el) return null;
                                    return Boolean(el.closest('.ant-modal-wrap, .ant-modal, .ant-drawer'));
                                }""",
                                sels,
                            ),
                            timeout=3,
                        )
                    except (asyncio.TimeoutError, Exception):
                        inside = False
            if not inside:
                layer["text"] = (layer.get("text") or "")[:40]

    result: Dict[str, Any] = {
        "dynamic_layers": layers,
        "dynamic_layer_count": probe.get("layer_count", 0),
        "new_layer_count": len(new_layers),
        "summary": [
            f"[{l.get('kind', '?')}] {'->'.join(l.get('frame_path', []))}::{l.get('selector', '')}: {(l.get('text') or '')[:80]}"
            for l in layers
        ],
        "focus": {
            "scope": "modal" if has_modal else "page",
            "modal_count": len(focus_modals),
            "modals": focus_modals,
            "background_collapsed": has_modal,
        },
    }
    # Token 精简: 无新层时省略 new_layers 数组 (它等于 dynamic_layers 全量,
    # 纯重复; new_layer_count=0 已表达语义), 有新层时才输出增量部分。
    if new_layers:
        result["new_layers"] = new_layers
    # 导航: 未变化时省略 before 侧完整清单 (占位无信息量)。
    url_changed = after_url != before.get("url")
    frames_changed = after_frames != before.get("frames")
    nav: Dict[str, Any] = {
        "url_changed": url_changed,
        "url_after": after_url,
        "frames_changed": frames_changed,
        "frames_after": after_frames,
    }
    if url_changed:
        nav["url_before"] = before.get("url")
    if frames_changed:
        nav["frames_before"] = before.get("frames")
    result["navigation"] = nav
    return result


# ==================== 通用点击交互工具 (统一入口) ====================

# 鼠标光标可视化 + 目标待交互元素高亮 (迁移自 drissionpage-mcp, 见 visuals_pw.py)。
# 渲染能力在冻结的 visuals.js / visuals_js.INSTALL_SCRIPT, 本模块只负责接线:
#   - 动作前 visuals.show()  → 光标平滑移动到目标 + 高亮框 + 动作标签 (+ 点击波纹)
#   - 动作后 visuals.finish() → 高亮变绿(成功)/变红(失败), 随后自动淡出
# 失败隔离: 视觉任何异常只进结果 data["visual_effects"], 绝不影响动作本身。
from qa_mcp.config import VISUAL_EFFECTS
from qa_mcp.utils.visual_effects.visuals_pw import AsyncPlaywrightVisualEffects

visuals = AsyncPlaywrightVisualEffects()


def _visualize_enabled(visualize: Optional[bool]) -> bool:
    """三态开关: None=跟随服务默认, True/False=强制覆盖。"""
    return VISUAL_EFFECTS if visualize is None else bool(visualize)


async def _visual_show(
    page,
    rect: tuple,
    point: tuple,
    label: str,
    action: str,
    enabled: bool,
) -> Optional[dict]:
    """动作前视觉: 光标移动 + 高亮 + 标签。异常只记录, 不影响动作。"""
    if not enabled:
        try:
            await visuals.disable(page)
        except Exception as e:
            return {"enabled": False, "error": f"visuals.disable: {e}"}
        return {"enabled": False}
    try:
        return await visuals.show(
            page, rect=rect, point=point, label=label[:80], action=action
        )
    except Exception as e:
        return {"enabled": False, "error": f"visuals.show: {e}"}


async def _visual_finish(page, success: bool, enabled: bool, result: Optional[dict]) -> Optional[dict]:
    """动作后视觉: 高亮染色 (绿=成功/红=失败)。异常只记录, 不影响动作。"""
    if not enabled:
        return result
    try:
        await visuals.finish(page, success)
    except Exception as e:
        if result is None:
            result = {"enabled": True, "error": f"visuals.finish: {e}"}
        else:
            result["finish_error"] = f"visuals.finish: {e}"
    return result


async def _frame_locator_selector(target, sub_frame: str) -> str:
    """把子 frame 选择器解析为可用的 frame_locator 参数。

    兼容动态 uid iframe: 业务页 iframe id 带运行时数字后缀
    (#react_iframe_66250001), 标签页刷新/版本部署后后缀变化, 硬编码精确 id
    会全链失效。规则: 精确 #id 匹配不到时, 自动回退前缀匹配 —
    优先 [id^="前缀"]:visible (唯一可见目标), 再退全量前缀 (唯一时);
    仍无唯一匹配则原样返回, 由 frame_locator 抛错报出。
    """
    if sub_frame.startswith("#") and len(sub_frame) > 1:
        try:
            if await target.locator(sub_frame).count() == 0:
                prefix = sub_frame[1:]
                visible_sel = f'[id^="{prefix}"]:visible'
                if await target.locator(visible_sel).count() == 1:
                    return visible_sel
                plain_sel = f'[id^="{prefix}"]'
                if await target.locator(plain_sel).count() == 1:
                    return plain_sel
        except Exception:
            return sub_frame
    return sub_frame


async def _locator_content_frame(target) -> Optional[Any]:
    """从 FrameLocator 解析出实际 Frame。

    该 Playwright 版本 FrameLocator 无 content_frame 属性,
    需经 owner(iframe 元素 Locator) -> element_handle -> content_frame 链路。
    解析失败返回 None (由调用方降级处理)。
    """
    try:
        handle = await target.owner.element_handle()
        return await handle.content_frame()
    except Exception:
        return None


async def _resolve_frame_target(page, iframe_selector: Optional[str]):
    """iframe 链式穿透解析: 返回 (目标上下文, frame_path_list)。

    供 click_interact / fill_input / execute_and_record 共用,
    保证所有工具的 iframe 穿透定位行为一致。支持动态 uid 前缀回退
    (见 _frame_locator_selector)。
    """
    target = page
    frame_path_list = []
    if iframe_selector:
        frame_path_list = [f.strip() for f in iframe_selector.split("->") if f.strip()]
        for sub_frame in frame_path_list:
            sub_frame = await _frame_locator_selector(target, sub_frame)
            target = target.frame_locator(sub_frame)
    return target, frame_path_list


# ==================== 统一"定位-执行"重试 ====================

RETRYABLE_ERROR_MARKERS = (
    "timeout",
    "detached",
    "strict mode",
    "is not attached",
    "has been closed",
    "frame was detached",
    "element is not attached",
    "context was destroyed",
)


async def retry_ui_action(
    description: str,
    action_fn: Callable[[], Awaitable[Any]],
    attempts: int = ACTION_RETRY_ATTEMPTS,
    backoff_ms: int = ACTION_RETRY_BACKOFF_MS,
) -> Any:
    """统一"定位-执行"重试: 覆盖 SPA 重渲染/元素 detach/短暂遮挡等可恢复失败。

    每次重试由 action_fn 重新执行完整链路 (重新解析 iframe 目标 + 重新创建
    locator), 因此元素被 React 重挂载或 iframe 重新导航后仍能恢复。
    仅对可恢复类错误 (超时/元素悬空/严格模式多匹配) 重试; 参数错误等直接抛出。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return await action_fn()
        except Exception as e:
            last_exc = e
            message = str(e).lower()
            if not any(marker in message for marker in RETRYABLE_ERROR_MARKERS):
                raise
            if attempt < attempts - 1:
                logger.warning(
                    f"[{description}] 第 {attempt + 1}/{attempts} 次执行失败 "
                    f"({type(e).__name__}: {e}), {backoff_ms / 1000}s 后重试..."
                )
                await asyncio.sleep(backoff_ms / 1000)
    assert last_exc is not None
    raise last_exc


async def _recover_after_hang(context: str) -> None:
    """动作被看门狗强杀后的 CDP 连接重建 (对齐 action_chain 的自愈语义)。

    强杀一个 CDP 请求半开的协程后, Playwright 底层可能残留 pending 协议请求,
    后续所有工具调用会排队挂死 (典型症状: 同一定位单独调用成功、紧随失败动作
    后调用却超时)。重建连接是最干净的恢复方式, 等价于 MCP 服务重启。
    """
    try:
        await asyncio.wait_for(browser_mgr.recover(), timeout=10)
        logger.warning(f"{context} 已重建 CDP 连接")
    except Exception as exc:
        logger.warning(f"{context} 连接重建失败: {exc}")


async def _enhance_locator_timeout(e: Exception, locator, label: str) -> Exception:
    """定位超时错误附加诊断: selector 当前在页面匹配的元素数。

    让 Agent 区分两类失败: 选择器失效 (匹配 0 个) vs 页面未就绪 (匹配 N 个但不可见)。
    """
    try:
        count = await locator.count()
    except Exception:
        return e
    error = RuntimeError(f"{label} 定位超时: {e} [诊断: 选择器当前匹配 {count} 个元素]")
    error.__cause__ = e
    return error


async def _wait_visible_or_first(
    lc, action_label: str, timeout_ms: int
):
    """可见性等待 + strict violation 消歧。

    多元素匹配 (典型: 日历双面板同名 td[title=...]、antd 常驻 dropdown 残余层)
    时 wait_for 抛 strict mode violation, 直接失败挂死重试。此时自动取 .first
    (DOM 顺序靠前的匹配, 如日历左面板优先于右面板预览) 消歧重试。
    非 strict 错误原样抛出 (由调用方增强诊断/重试)。
    """
    try:
        await lc.wait_for(state="visible", timeout=timeout_ms)
    except Exception as e:
        if "strict mode violation" in str(e).lower():
            logger.warning(
                f"[{action_label}] 多元素匹配 (strict violation), 取 .first 消歧: {e}"
            )
            lc = lc.first
            await lc.wait_for(state="visible", timeout=timeout_ms)
            return lc
        raise
    return lc


def _is_actionability_failure(e: Exception) -> bool:
    """判断点击/悬停/聚焦失败是否为 actionability 检查类 (可 force 兜底)。

    Playwright actionability 失败类型: 持续动画 (not stable)、元素被遮挡
    (intercepts pointer events)、不在视口 (outside of the viewport)、不可见等;
    click/hover 的 timeout 只发生在 actionability 阶段, 超时异常一律视为
    actionability 失败。force=True 跳过全部检查直接派发事件, 是这类失败的
    标准兜底 (先正常尝试短超时, 失败后 force, 再失败交重试/抛错)。

    非 actionability 错误 (元素不存在/不可编辑等) 返回 False, 原样抛出。
    """
    if isinstance(e, TimeoutError):
        return True
    message = str(e).lower()
    return any(
        marker in message
        for marker in (
            "stable",
            "actionability",
            "not stable",
            "receives events",
            "intercepts pointer events",
            "outside of the viewport",
            "element is not visible",
            "element is hidden",
        )
    )


async def _do_click(
    page,
    by: str,
    selector: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    coordinate_space: str = "top",
    click_type: str = "single",
    visualize: Optional[bool] = None,
    description: Optional[str] = None,
    role: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """单次点击执行体 (不含导航快照/统一观察): 定位 + 视觉 + 点击。

    供 click_interact_impl / execute_action_chain 共用。
    by=coordinate 时内部走 vtable_mgr.click_at (自带点击后观察, 返回结果含 observation);
    by=role 时用 get_by_role 语义定位 (role + name, 支持 iframe 穿透);
    by=css/xpath 时返回不含 observation, 由调用方统一 observe_after_click。
    """
    by_lower = (by or "").lower()
    click_kind = (click_type or "single").lower()
    viz = _visualize_enabled(visualize)

    if by_lower == "coordinate":
        from qa_mcp.tools.vtable import vtable_mgr

        # 视觉: 仅 top 空间坐标可直接使用 (已是顶层视口坐标), 以点击点为中心合成
        # 40x40 目标框供高亮展示 (canvas 内部元素无 DOM 矩形可取)
        viz_result = None
        if not viz:
            viz_result = await _visual_show(page, (), (), "", "", enabled=False)
        elif coordinate_space == "top":
            viz_result = await _visual_show(
                page,
                rect=(x - 20, y - 20, 40, 40),
                point=(x, y),
                label=description or f"坐标点击({x:.0f},{y:.0f})",
                action="click",
                enabled=True,
            )
        try:
            # 单步硬上限: CDP 挂死时 Playwright 动作级 timeout 不生效,
            # 用外层 wait_for 兜底, 防止一个死动作把整个工具调用堵死
            # (对齐 action_chain 的 ACTION_STEP_TIMEOUT_MS 语义)。
            result = await asyncio.wait_for(
                vtable_mgr.click_at(
                    x=x, y=y,
                    iframe_selector=iframe_selector if iframe_selector is not None else "div[aria-hidden=false] iframe",
                    coordinate_space=coordinate_space,
                    click_type=click_kind,
                ),
                timeout=ACTION_STEP_TIMEOUT_MS / 1000,
            )
        except asyncio.TimeoutError:
            await _recover_after_hang(f"坐标点击 ({x:.0f},{y:.0f})")
            raise RuntimeError(
                f"坐标点击 ({x:.0f},{y:.0f}) 执行超过 {ACTION_STEP_TIMEOUT_MS}ms 上限, "
                "已强制中断并重建 CDP 连接。请检查浏览器/页面状态后重试。"
            ) from None
        except Exception:
            if viz:
                viz_result = await _visual_finish(page, False, True, viz_result)
            raise
        result["visual_effects"] = await _visual_finish(
            page, result.get("status") == "success", True, viz_result
        )
        result["by"] = "coordinate"
        return result

    # ---- CSS / XPath / Role: 定位 DOM 元素 (iframe 链式穿透) ----
    if by_lower == "role":
        # 语义定位 (Playwright get_by_role): 无视 Portal/DOM 层级, 按角色+可访问名称匹配
        if not role:
            raise RuntimeError("by=role 时必须提供 role")
        full_selector = f"role={role}" + (f" name={name}" if name else "")
        locator_kind = "role"
    else:
        full_selector = f"xpath={selector}" if by_lower == "xpath" else selector
        locator_kind = "css"

    action_label = description or full_selector or "点击"
    viz_result = None
    frame_path_list: List[str] = []

    async def _dom_click_once():
        # 每次执行/重试重新解析目标与 locator: 覆盖 SPA 重渲染 detach、
        # iframe 重新导航等导致旧 locator 失效的场景。
        nonlocal frame_path_list, viz_result
        target, frame_path_list = await _resolve_frame_target(page, iframe_selector)
        if locator_kind == "role":
            lc = target.get_by_role(role, name=name or None)
        else:
            lc = target.locator(full_selector)
        try:
            lc = await _wait_visible_or_first(lc, action_label, ELEMENT_WAIT_TIMEOUT_MS)
        except Exception as e:
            err = await _enhance_locator_timeout(e, lc, action_label)
            # 诊断增强: antd hover 态元素 (clear 图标等) 默认 display:none,
            # 直接定位必然失败 — 提示先 hover_interact 悬停父级再取坐标。
            if "匹配 0 个元素" in str(err) and ("__clear" in str(full_selector) or "clear" in str(full_selector).lower()):
                raise RuntimeError(
                    f"{err} [提示: 该元素为 hover 态派生元素 (如 antd clear 图标), "
                    f"默认 display:none 不可见; 请先 hover_interact 悬停其父级元素, "
                    f"从返回的 revealed_elements 中取 topX/topY 坐标点击]"
                ) from e
            raise err from e
        # 滚动降级: 持续动画页面 (VTable 重绘/antd 动效) 会使稳定等待超时,
        # 此时元素往往已在视口内, click 内部自带滚动, 无需硬等稳定。
        try:
            await asyncio.wait_for(lc.scroll_into_view_if_needed(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

        # 动作前视觉: 光标平滑移动到目标中心 + 高亮框 + 动作标签
        box = await lc.bounding_box()
        if viz and box:
            viz_result = await _visual_show(
                page,
                rect=(box["x"], box["y"], box["width"], box["height"]),
                point=(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2),
                label=action_label,
                action="click",
                enabled=True,
            )
        # 点击执行: 常规 actionability (含 stable) 等待; 持续动画页面 stable 检查
        # 会挂到默认 30s, 故先给短超时, 超时后 force 兜底 (跳过 actionability 直接派发事件)。
        click_timeout = max(ELEMENT_WAIT_TIMEOUT_MS, 5000)
        try:
            if click_kind == "double":
                await lc.dblclick(timeout=click_timeout)
            else:
                await lc.click(timeout=click_timeout)
        except Exception as e:
            if _is_actionability_failure(e):
                logger.warning(
                    f"[{action_label}] actionability 检查未通过 (持续动画/遮挡/不在视口), "
                    f"force 点击兜底: {e}"
                )
                if click_kind == "double":
                    await lc.dblclick(force=True)
                else:
                    await lc.click(force=True)
            else:
                raise
        return box

    async def _exec_click() -> tuple:
        # 单次完整点击动作 (视觉 + 定位重试 + force 兜底), 供外层看门狗限时
        nonlocal viz_result
        if not viz:
            viz_result = await _visual_show(page, (), (), "", "", enabled=False)
        try:
            box = await retry_ui_action(action_label, _dom_click_once)
        except Exception:
            if viz:
                viz_result = await _visual_finish(page, False, True, viz_result)
            raise
        return box, await _visual_finish(page, True, viz, viz_result)

    try:
        # 单步硬上限: CDP 挂死时 Playwright 动作级 timeout 不生效, 用外层
        # wait_for 兜底 (对齐 action_chain 的 ACTION_STEP_TIMEOUT_MS 语义)。
        box, viz_result = await asyncio.wait_for(
            _exec_click(), timeout=ACTION_STEP_TIMEOUT_MS / 1000
        )
    except asyncio.TimeoutError:
        await _recover_after_hang(f"点击 [{action_label}]")
        raise RuntimeError(
            f"点击动作 [{action_label}] 执行超过 {ACTION_STEP_TIMEOUT_MS}ms 上限, "
            "已强制中断并重建 CDP 连接。请检查浏览器/页面状态后重试。"
        ) from None

    return {
        "status": "success",
        "by": by_lower,
        "click_type": click_kind,
        "selector": full_selector,
        "frame_path": frame_path_list,
        "element_box": {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]} if box else None,
        "element_center": {"x": round(box["x"] + box["width"] / 2, 2), "y": round(box["y"] + box["height"] / 2, 2)} if box else None,
        "description": description,
        "visual_effects": viz_result,
    }


async def _do_fill(
    page,
    by: str,
    selector: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    value: str = "",
    input_method: str = "type",
    clear_first: bool = True,
    press_enter: bool = False,
    visualize: Optional[bool] = None,
    description: Optional[str] = None,
) -> dict:
    """单次输入执行体 (不含导航快照/统一观察): 定位 + 视觉 + 输入。

    供 fill_input_impl / execute_action_chain 共用。
    """
    by_lower = (by or "").lower()
    method = (input_method or "type").lower()
    viz = _visualize_enabled(visualize)

    full_selector = f"xpath={selector}" if by_lower == "xpath" else selector
    action_label = description or (f"输入: {value}" if value else "清空输入框") or selector or "input"
    viz_result = None
    frame_path_list: List[str] = []

    async def _dom_fill_once():
        # 每次执行/重试重新解析目标与 locator (同 _do_click 的重试语义)
        nonlocal frame_path_list, viz_result
        target, frame_path_list = await _resolve_frame_target(page, iframe_selector)
        lc = target.locator(full_selector)
        try:
            lc = await _wait_visible_or_first(lc, action_label, ELEMENT_WAIT_TIMEOUT_MS)
        except Exception as e:
            raise await _enhance_locator_timeout(e, lc, action_label) from e
        # 滚动降级: 持续动画页面 (VTable 重绘/antd 动效) 会使稳定等待超时,
        # 此时元素往往已在视口内, click/fill 内部自带滚动, 无需硬等稳定。
        try:
            await asyncio.wait_for(lc.scroll_into_view_if_needed(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

        # 动作前视觉: 光标移动至输入框 + 高亮框 + 动作标签
        box = await lc.bounding_box()
        if viz and box:
            viz_result = await _visual_show(
                page,
                rect=(box["x"], box["y"], box["width"], box["height"]),
                point=(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2),
                label=action_label,
                action="input",
                enabled=True,
            )
        # 先点击聚焦 (对齐人工操作: 触发组件的 focus/激活逻辑);
        # actionability 检查失败 (持续动画/遮挡等) → force 聚焦兜底。
        try:
            await lc.click(timeout=max(ELEMENT_WAIT_TIMEOUT_MS, 5000))
        except Exception as e:
            if _is_actionability_failure(e):
                logger.warning(
                    f"[{action_label}] actionability 检查未通过, force 点击聚焦兜底: {e}"
                )
                await lc.click(force=True)
            else:
                raise
        if method == "fill":
            await lc.fill(value or "")
        else:
            if clear_first:
                await lc.press("Control+A")
                await lc.press("Backspace")
            if value:
                # 逐字输入, 每字间隔 0.1s (模拟人工打字节奏);
                # v1.50+: press_sequentially 元素级逐键输入 (替代全局 keyboard.type),
                # 输入目标更稳 (元素失焦也能继续输入); 旧版本回退 keyboard.type。
                try:
                    await lc.press_sequentially(value, delay=100)
                except AttributeError:
                    await page.keyboard.type(value, delay=100)
        if press_enter:
            await lc.press("Enter")
        return box

    async def _exec_fill() -> tuple:
        # 单次完整输入动作 (视觉 + 定位重试 + 聚焦兜底), 供外层看门狗限时
        nonlocal viz_result
        if not viz:
            viz_result = await _visual_show(page, (), (), "", "", enabled=False)
        try:
            box = await retry_ui_action(action_label, _dom_fill_once)
        except Exception:
            if viz:
                viz_result = await _visual_finish(page, False, True, viz_result)
            raise
        return box, await _visual_finish(page, True, viz, viz_result)

    try:
        # 单步硬上限: CDP 挂死时 Playwright 动作级 timeout 不生效, 用外层
        # wait_for 兜底 (对齐 action_chain 的 ACTION_STEP_TIMEOUT_MS 语义)。
        box, viz_result = await asyncio.wait_for(
            _exec_fill(), timeout=ACTION_STEP_TIMEOUT_MS / 1000
        )
    except asyncio.TimeoutError:
        await _recover_after_hang(f"输入 [{action_label}]")
        raise RuntimeError(
            f"输入动作 [{action_label}] 执行超过 {ACTION_STEP_TIMEOUT_MS}ms 上限, "
            "已强制中断并重建 CDP 连接。请检查浏览器/页面状态后重试。"
        ) from None

    return {
        "status": "success",
        "by": by_lower,
        "selector": full_selector,
        "frame_path": frame_path_list,
        "element_box": {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]} if box else None,
        "element_center": {"x": round(box["x"] + box["width"] / 2, 2), "y": round(box["y"] + box["height"] / 2, 2)} if box else None,
        "description": description,
        "visual_effects": viz_result,
    }


HOVER_REVEAL_SCAN_JS = r"""function(el) {
    // 悬停态新出现元素扫描: 目标元素(如 antd select)内由 display:none 转为可见的
    // 子元素 (典型: clear 清空图标、hover 提示), 返回顶层视口坐标 + 相对CSS路径。
    // 注意: 必须用普通函数 (Playwright evaluate 以首个参数注入元素, 箭头函数
    // 无 arguments 且不接收注入参数, 会静默返回空)。
    function topOffset() {
        let ox = 0, oy = 0, cur = window;
        try {
            while (cur !== window.top) {
                const fe = cur.frameElement;
                if (fe) { const r = fe.getBoundingClientRect(); ox += r.x; oy += r.y; }
                cur = cur.parent;
            }
        } catch (e) {}
        return [ox, oy];
    }
    function relPath(node, rootEl) {
        const parts = [];
        let cur = node;
        while (cur && cur !== rootEl && cur.nodeType === 1) {
            const tag = cur.tagName.toLowerCase();
            const parent = cur.parentElement;
            const idx = parent ? Array.from(parent.children).indexOf(cur) + 1 : 1;
            parts.unshift(tag + ':nth-child(' + idx + ')');
            cur = parent;
        }
        return parts.join(' > ');
    }
    const revealed = [];
    if (!el) return revealed;
    const [ox, oy] = topOffset();
    const all = el.querySelectorAll('*');
    for (const node of all) {
        const style = getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') continue;
        const rect = node.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) continue;
        // 排除大块容器与纯文本节点容器 (rendered/search 区域)
        const cls = String(node.className || '');
        if (/rendered|search|selected-value|selection-selected/i.test(cls)) continue;
        revealed.push({
            tag: node.tagName.toLowerCase(),
            cls: cls,
            text: (node.textContent || '').trim().slice(0, 20),
            topX: Math.round(rect.x + rect.width / 2 + ox),
            topY: Math.round(rect.y + rect.height / 2 + oy),
            relPath: relPath(node, el),
        });
    }
    return revealed.slice(0, 20);
}"""


async def _scan_hover_revealed(page, lc, iframe_selector: Optional[str]) -> List[dict]:
    """悬停后扫描目标元素内 hover 态新出现的可见子元素 (如 antd clear 图标)。

    返回 [{tag, cls, text, topX, topY, relPath}]: topX/topY 为浏览器顶层视口坐标,
    可直接 click_interact(by=coordinate) 点击; relPath 为相对目标元素的选择器路径,
    可与目标 selector 拼接为完整 CSS 定位。
    """
    try:
        target, _ = await _resolve_frame_target(page, iframe_selector)
        # evaluate 无动作级超时, 渲染繁忙时可能无限等待, 3s 上限兜底
        return await asyncio.wait_for(lc.evaluate(HOVER_REVEAL_SCAN_JS), timeout=3)
    except Exception as e:
        logger.warning(f"悬停态元素扫描失败: {e}")
        return []


async def _do_hover(
    page,
    by: str,
    selector: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    hold_ms: int = 500,
    visualize: Optional[bool] = None,
    description: Optional[str] = None,
    role: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """单次悬停执行体 (不含导航快照/统一观察): 定位 + 光标移动悬停。

    供 hover_interact_impl / execute_action_chain 共用。
    hold_ms: 悬停停留时长 (ms), 默认 500ms — 让 CSS :hover 触发的子元素
    (如 antd select 的 clear 图标、tooltip) 渲染完成后再返回, 便于链式下一步
    直接点击该 hover 态元素。
    悬停后自动扫描目标元素内 hover 态新出现的可见子元素 (clear 图标等),
    返回其顶层坐标与相对路径 (revealed_elements), 无需截图推断坐标。
    """
    by_lower = (by or "").lower()
    viz = _visualize_enabled(visualize)

    if by_lower == "role":
        if not role:
            raise RuntimeError("by=role 时必须提供 role")
        full_selector = f"role={role}" + (f" name={name}" if name else "")
        locator_kind = "role"
    else:
        full_selector = f"xpath={selector}" if by_lower == "xpath" else selector
        locator_kind = "css"

    action_label = description or full_selector or "悬停"
    viz_result = None
    frame_path_list: List[str] = []

    async def _dom_hover_once():
        # 每次执行/重试重新解析目标与 locator (同 _do_click 的重试语义)
        nonlocal frame_path_list, viz_result
        target, frame_path_list = await _resolve_frame_target(page, iframe_selector)
        if locator_kind == "role":
            lc = target.get_by_role(role, name=name or None)
        else:
            lc = target.locator(full_selector)
        try:
            lc = await _wait_visible_or_first(lc, action_label, ELEMENT_WAIT_TIMEOUT_MS)
        except Exception as e:
            raise await _enhance_locator_timeout(e, lc, action_label) from e
        # 滚动降级: 持续动画页面 (VTable 重绘/antd 动效) 会使稳定等待超时,
        # 此时元素往往已在视口内, hover 内部自带滚动, 无需硬等稳定。
        try:
            await asyncio.wait_for(lc.scroll_into_view_if_needed(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass

        # 动作前视觉: 光标平滑移动到目标中心 + 高亮框 + 动作标签
        box = await lc.bounding_box()
        if viz and box:
            viz_result = await _visual_show(
                page,
                rect=(box["x"], box["y"], box["width"], box["height"]),
                point=(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2),
                label=action_label,
                action="hover",
                enabled=True,
            )
        # actionability 检查失败 (持续动画/遮挡等) → force 悬停兜底 (直接移动鼠标, 跳过检查)。
        try:
            await lc.hover(timeout=max(ELEMENT_WAIT_TIMEOUT_MS, 5000))
        except Exception as e:
            if _is_actionability_failure(e):
                logger.warning(
                    f"[{action_label}] actionability 检查未通过, force 悬停兜底: {e}"
                )
                await lc.hover(force=True)
            else:
                raise
        # 停留让 CSS :hover 派生元素 (clear 图标/tooltip) 渲染完成
        if hold_ms and hold_ms > 0:
            await asyncio.sleep(hold_ms / 1000)
        return box

    async def _exec_hover() -> tuple:
        # 单次完整悬停动作 (视觉 + 定位重试 + force 兜底), 供外层看门狗限时
        nonlocal viz_result
        if not viz:
            viz_result = await _visual_show(page, (), (), "", "", enabled=False)
        try:
            box = await retry_ui_action(action_label, _dom_hover_once)
        except Exception:
            if viz:
                viz_result = await _visual_finish(page, False, True, viz_result)
            raise
        return box, await _visual_finish(page, True, viz, viz_result)

    try:
        # 单步硬上限: CDP 挂死时 Playwright 动作级 timeout 不生效, 用外层
        # wait_for 兜底 (对齐 action_chain 的 ACTION_STEP_TIMEOUT_MS 语义)。
        box, viz_result = await asyncio.wait_for(
            _exec_hover(), timeout=ACTION_STEP_TIMEOUT_MS / 1000
        )
    except asyncio.TimeoutError:
        await _recover_after_hang(f"悬停 [{action_label}]")
        raise RuntimeError(
            f"悬停动作 [{action_label}] 执行超过 {ACTION_STEP_TIMEOUT_MS}ms 上限, "
            "已强制中断并重建 CDP 连接。请检查浏览器/页面状态后重试。"
        ) from None

    # 悬停态元素扫描: 目标元素内由隐藏转可见的子元素 (clear 图标/tooltip),
    # 返回顶层视口坐标与相对路径, 供下一步直接点击。
    revealed = []
    try:
        target, _ = await _resolve_frame_target(page, iframe_selector)
        if locator_kind == "role":
            revealed = await _scan_hover_revealed(page, target.get_by_role(role, name=name or None), iframe_selector)
        else:
            revealed = await _scan_hover_revealed(page, target.locator(full_selector), iframe_selector)
    except Exception as e:
        logger.warning(f"悬停态元素扫描失败: {e}")

    return {
        "status": "success",
        "by": by_lower,
        "selector": full_selector,
        "frame_path": frame_path_list,
        "element_box": {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]} if box else None,
        "element_center": {"x": round(box["x"] + box["width"] / 2, 2), "y": round(box["y"] + box["height"] / 2, 2)} if box else None,
        "revealed_elements": revealed,
        "description": description,
        "visual_effects": viz_result,
    }


async def hover_interact_impl(
    by: str = "css",
    selector: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    hold_ms: int = 500,
    description: Optional[str] = None,
    expected_result: Optional[str] = None,
    visualize: Optional[bool] = None,
    detail: str = "brief",
    role: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """
    通用悬停工具: 将鼠标移动到目标元素中心并停留, 用于触发 CSS :hover 效果
    (如 antd Select 的 clear 清空图标、tooltip、下拉箭头翻转等), 随后统一观察
    (浮窗/弹窗/消息提示 + tab 页跳转 + iframe 跳转)。

    by=css/xpath: selector 为 CSS 选择器/XPath 表达式 (支持 iframe_selector 链式穿透);
    by=role:      role/name 语义定位 (get_by_role), role 必填, name 可选;
    hold_ms:      悬停停留时长 (默认 500ms), hover 态派生元素 (如 clear 图标) 渲染
                  完成后返回, 链式下一步可直接点击该元素;
    detail:       brief(默认) | full — 悬停后观察输出体积。

    悬停态元素扫描: 悬停完成后自动扫描目标元素内由隐藏转可见的子元素
    (antd clear 清空图标、hover 提示等), 返回 revealed_elements 数组, 每项含
    topX/topY (浏览器顶层视口坐标, 可直接 click_interact by=coordinate 点击)
    与 relPath (相对目标元素的 CSS 路径, 可与目标 selector 拼接定位) —
    无需截图推断坐标。

    典型用法 (清空 antd 单选值, 不依赖猜坐标):
      1) hover_interact 悬停到 select 本体 → 返回 revealed_elements 中
         clear 图标的 topX/topY
      2) click_interact by=coordinate 用该坐标点击 → 值清空

    返回: status/by/定位信息 + revealed_elements + visual_effects + observation
          (dynamic_layers/new_layers/summary/focus 浮层弹窗消息 + navigation 跳转对比)。
    """
    by_lower = (by or "").lower()
    if by_lower not in ("css", "xpath", "role"):
        raise RuntimeError(f"by 仅支持 css / xpath / role, 收到: {by}")
    if by_lower in ("css", "xpath") and not selector:
        raise RuntimeError(f"by={by_lower} 时必须提供 selector")
    if by_lower == "role" and not role:
        raise RuntimeError("by=role 时必须提供 role")

    page = await browser_mgr.get_page()
    before = await snapshot_navigation(page)
    result = await _do_hover(
        page, by_lower, selector, iframe_selector,
        hold_ms=hold_ms, visualize=visualize, description=description,
        role=role, name=name,
    )
    result["expected_result"] = expected_result
    result["observation"] = await observe_after_click(page, before, detail=detail)
    return result


async def click_interact_impl(
    by: str = "css",
    selector: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    coordinate_space: str = "top",
    click_type: str = "single",
    description: Optional[str] = None,
    expected_result: Optional[str] = None,
    visualize: Optional[bool] = None,
    detail: str = "brief",
    role: Optional[str] = None,
    name: Optional[str] = None,
) -> dict:
    """
    通用点击交互工具: 通过 by 参数选择定位方式, 点击后立即统一观察
    (浮窗/弹窗/消息提示弹窗/下拉框浮层 + tab 页跳转 + iframe 跳转)。

    by=css:        selector 为 CSS 选择器, 点击 DOM 元素
                  (支持 iframe_selector 链式穿透, 如 "#f1->#f2");
    by=xpath:      selector 为 XPath 表达式, 点击 DOM 元素
                  (支持 iframe_selector 链式穿透);
    by=role:       role/name 语义定位 (Playwright get_by_role, v1.60+ 支持
                  accessible description 匹配), 无视 Portal/DOM 层级,
                  role 必填, name 可选; 适合 Portal 弹层内的按钮/选项;
    by=coordinate: x/y 为点击坐标。coordinate_space:
                  - top(默认): 坐标即浏览器顶层视口坐标, 页面任意元素/区域
                    均可直接点击, 不要求页面存在 VTable 实例 (点击普通 DOM
                    浮层选项、按钮等场景无需挂载); 配合 vtable_scan_columns /
                    vtable_get_cell_center 返回的 viewportX/viewportY 点击
                    VTable 内部图标/单元格时同样直接可用;
                  - viewport | content: 坐标相对 VTable canvas (可视区/内容区),
                    仅当页面存在 VTable 实例时可用。

    visualize: True=启用鼠标光标可视化+目标高亮 (光标平滑移动到目标中心、
               高亮框+动作标签、点击波纹、成功绿/失败红后淡出); False=立即清理
               视觉层且不做任何展示; None=跟随服务配置 VISUAL_EFFECTS (默认 false)。
               视觉任何异常只记录到结果 visual_effects 字段, 不影响点击本身。

    detail: brief(默认, 观察输出剪枝) | full(完整 html/文本)。

    返回: status/by/click_type/定位信息 + visual_effects + observation
          (dynamic_layers/new_layers/summary/focus 浮窗弹窗消息 + navigation 跳转对比)。
    """
    by_lower = (by or "").lower()
    if by_lower not in ("css", "xpath", "coordinate", "role"):
        raise RuntimeError(f"by 仅支持 css / xpath / role / coordinate, 收到: {by}")
    if by_lower == "coordinate" and (x is None or y is None):
        raise RuntimeError(f"by=coordinate 时必须提供 x 与 y")
    if by_lower in ("css", "xpath") and not selector:
        raise RuntimeError(f"by={by_lower} 时必须提供 selector")
    if by_lower == "role" and not role:
        raise RuntimeError("by=role 时必须提供 role")
    click_kind = (click_type or "single").lower()
    if click_kind not in ("single", "double"):
        raise RuntimeError(f"click_type 仅支持 single / double, 收到: {click_type}")

    page = await browser_mgr.get_page()

    # ---- 坐标点击: vtable_mgr.click_at 内部自带快照+观察, 直接返回 ----
    if by_lower == "coordinate":
        result = await _do_click(
            page, by_lower, selector, iframe_selector,
            x=x, y=y, coordinate_space=coordinate_space,
            click_type=click_kind, visualize=visualize, description=description,
        )
        result["description"] = description
        result["expected_result"] = expected_result
        return result

    # ---- CSS / XPath / Role: 点击前导航快照, 点击后统一观察 ----
    before = await snapshot_navigation(page)
    result = await _do_click(
        page, by_lower, selector, iframe_selector,
        click_type=click_kind, visualize=visualize, description=description,
        role=role, name=name,
    )
    result["expected_result"] = expected_result
    result["observation"] = await observe_after_click(page, before, detail=detail)
    return result


# ==================== 目标标签页切换工具 ====================

async def switch_target_page_impl(url_pattern: str) -> dict:
    """显式切换/重绑 MCP 操作目标标签页 (按 URL 子串匹配) 并锁定。

    默认目标页锁定机制: 首次工具调用自动选择并锁定一个标签页, 后续操作固定
    作用于该页, 不受用户新开/切换标签页影响。当需要把自动化切到另一个页面
    (如同时维护多系统测试) 时, 用本工具重绑。
    """
    await browser_mgr.get_page()  # 确保 CDP 连接就绪
    try:
        await browser_mgr.switch_target(url_pattern)
    except RuntimeError as e:
        return {"status": "error", "message": str(e)}
    page = await browser_mgr.get_page()
    return {
        "status": "success",
        "url": page.url,
        "title": await page.title(),
        "note": "目标标签页已锁定, 后续所有工具操作固定作用于该页 (除非被关闭或再次切换)",
    }


# ==================== 通用文本框输入工具 (独立于录制会话) ====================

async def fill_input_impl(
    by: str = "css",
    selector: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    value: str = "",
    input_method: str = "type",
    clear_first: bool = True,
    press_enter: bool = False,
    description: Optional[str] = None,
    expected_result: Optional[str] = None,
    visualize: Optional[bool] = None,
    detail: str = "brief",
) -> dict:
    """
    通用文本框输入工具: 向输入框/文本域/可编辑框输入数据。
    与 execute_and_record 解耦 —— 不依赖录制会话, 无需 start_recording 即可独立使用;
    录制场景请继续走 execute_and_record (会自动沉淀语义定位步骤到用例)。

    by=css/xpath: 定位方式, selector 支持 iframe_selector 链式穿透;
    value: 要输入的内容 (空字符串=清空输入框);
    input_method: type(默认, 真实键盘逐字输入, 触发 keydown/keyup, 逐字模拟人工
                  打字节奏, 适用于监听键盘事件的组件)
                  | fill(Playwright 原生填充, 快且稳, 自动清空旧值并触发 input 事件);
    clear_first: True(默认) 输入前清空已有内容 (type 通过 Ctrl+A + Backspace 清空;
                  fill 天然清空);
    press_enter: True 则输入完成后按回车 (常见于搜索框/确认输入场景);
    visualize: True=光标移动至输入框+高亮+动作标签; False=不展示; None=跟随服务配置
               VISUAL_EFFECTS(默认 false); 视觉任何异常只记录到 visual_effects 字段,
               不影响输入本身。
    detail: brief(默认, 观察输出剪枝) | full(完整 html/文本)。

    返回: status/定位信息/输入详情 + visual_effects + observation
          (dynamic_layers/new_layers/summary/focus 浮层弹窗消息 + navigation 跳转对比)。
    """
    by_lower = (by or "").lower()
    if by_lower not in ("css", "xpath"):
        raise RuntimeError(f"by 仅支持 css / xpath, 收到: {by}")
    if not selector:
        raise RuntimeError("必须提供 selector")
    method = (input_method or "type").lower()
    if method not in ("fill", "type"):
        raise RuntimeError(f"input_method 仅支持 fill / type, 收到: {input_method}")

    page = await browser_mgr.get_page()

    # 输入前导航快照 (URL + iframe 清单 + 弹层指纹), 输入后统一观察
    before = await snapshot_navigation(page)
    result = await _do_fill(
        page, by_lower, selector, iframe_selector,
        value=value, input_method=method, clear_first=clear_first,
        press_enter=press_enter, visualize=visualize, description=description,
    )
    result["value"] = value
    result["input_method"] = method
    result["clear_first"] = bool(clear_first)
    result["press_enter"] = bool(press_enter)
    result["expected_result"] = expected_result
    result["observation"] = await observe_after_click(page, before, detail=detail)
    return result


# ==================== 页面截图工具 ====================

async def capture_screenshot_impl(
    filename: Optional[str] = None,
    full_page: bool = False,
):
    """截取当前页面截图并返回 (文件落盘 evidence_assets/ + 内联 PNG)。

    用 CDP Page.captureScreenshot 而非 page.screenshot: 后者会等待页面字体加载,
    在本项目页面上会因永不加载完的 webfont 超时挂起。

    filename: 输出文件名, 默认 screenshot_YYYYmmdd_HHMMSS.png (自动补 .png);
    full_page: False(默认)=当前视口; True=整页 (含滚动区外的全部内容)。

    返回: [文本摘要, Image] —— 摘要含文件路径/尺寸/字节数; Image 为内联 PNG,
    支持图片的 MCP 客户端可直接查看截图。
    """
    from fastmcp.utilities.types import Image

    page = await browser_mgr.get_page()
    session = await page.context.new_cdp_session(page)

    if full_page:
        size = await page.evaluate("""() => {
            const de = document.documentElement;
            const body = document.body;
            return {
                width: Math.max(de.scrollWidth, body ? body.scrollWidth : 0, de.clientWidth),
                height: Math.max(de.scrollHeight, body ? body.scrollHeight : 0, de.clientHeight)
            };
        }""")
        width = int(size.get("width") or 0)
        height = int(size.get("height") or 0)
        params = {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1},
        }
    else:
        viewport = await page.evaluate(
            "() => ({ width: window.innerWidth, height: window.innerHeight })"
        )
        width = int(viewport.get("width") or 0)
        height = int(viewport.get("height") or 0)
        params = {"format": "png"}

    data = await session.send("Page.captureScreenshot", params)
    raw = base64.b64decode(data["data"])

    if not filename:
        filename = datetime.now().strftime("screenshot_%Y%m%d_%H%M%S.png")
    if not filename.lower().endswith(".png"):
        filename += ".png"
    # filename 支持子目录 (如 "数据字典/用例001_新增.png"), 按模块组织证据资产
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    path = os.path.join(EVIDENCE_DIR, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)

    summary = {
        "status": "success",
        "file": path,
        "filename": filename,
        "size_bytes": len(raw),
        "dimensions": {"width": width, "height": height},
        "full_page": full_page,
        "note": "截图已保存到 evidence_assets/, 支持图片的客户端可直接查看内联 PNG",
    }
    return [json.dumps(summary, ensure_ascii=False), Image(path=path)]


# ==================== 等待条件工具 (增强成功率的核心缺口) ====================
# 操作后消息/状态往往延迟出现 (慢接口 2~3s 后才有成功提示), 固定观察窗口
# (OBSERVE_WAIT_MS) 会漏报 → Agent 误判"无反馈"。本工具轮询直到条件成立
# 或超时, 超时不抛错, 返回最后一次状态快照供 Agent 决策。

WAIT_CONDITIONS = ("element_visible", "element_hidden", "element_has_text", "text_present", "url_contains")


async def wait_for_condition_impl(
    condition: str = "element_visible",
    selector: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    expected_text: Optional[str] = None,
    exact: bool = False,
    timeout_ms: int = 15000,
    poll_interval_ms: int = 300,
) -> dict:
    """等待页面条件成立 (轮询), 超时返回最后一次状态快照, 不抛错。

    condition:
      element_visible:  selector 可见 (默认);
      element_hidden:   selector 不存在或不可见;
      element_has_text: selector 的可见文本包含 expected_text (exact=True 时精确相等);
      text_present:     目标 iframe (或全部 frame) 页面文本出现 expected_text;
      url_contains:     页面 URL 包含 expected_text。

    selector/expected_text 必填要求随 condition 而异; timeout_ms 上限 60s。
    典型用法: 提交表单后 wait_for_condition(condition="text_present",
    expected_text="新增成功", timeout_ms=10000) 等成功消息, 再断言收尾。
    """
    if condition not in WAIT_CONDITIONS:
        raise RuntimeError(f"condition 仅支持: {sorted(WAIT_CONDITIONS)}")
    if condition in ("element_visible", "element_hidden", "element_has_text") and not selector:
        raise RuntimeError(f"condition={condition} 时必须提供 selector")
    if condition in ("element_has_text", "text_present", "url_contains") and not expected_text:
        raise RuntimeError(f"condition={condition} 时必须提供 expected_text")
    timeout_ms = max(100, min(int(timeout_ms), 60000))
    poll_interval_ms = max(50, min(int(poll_interval_ms), 2000))

    page = await browser_mgr.get_page()
    started = time.monotonic()
    last_state: Dict[str, Any] = {}

    while True:
        try:
            last_state = await _check_wait_condition(
                page, condition, selector, iframe_selector, expected_text, exact
            )
        except Exception as e:
            last_state = {"met": False, "error": str(e)}
        if last_state.get("met"):
            return {
                "status": "success",
                "condition": condition,
                "met": True,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "state": last_state.get("detail"),
            }
        elapsed = time.monotonic() - started
        if elapsed >= timeout_ms / 1000:
            return {
                "status": "timeout",
                "condition": condition,
                "met": False,
                "elapsed_ms": int(elapsed * 1000),
                "state": last_state.get("detail"),
                "note": "超时未满足, 返回最后一次检查状态; 可用 probe_dynamic_layers / analyze_current_page 复核",
            }
        await asyncio.sleep(poll_interval_ms / 1000)


async def _check_wait_condition(
    page: Page,
    condition: str,
    selector: Optional[str],
    iframe_selector: Optional[str],
    expected_text: Optional[str],
    exact: bool,
) -> Dict[str, Any]:
    """单次条件检查: 返回 {"met": bool, "detail": ...}。"""
    if condition == "url_contains":
        url = page.url
        return {"met": expected_text in url, "detail": {"url": url}}

    target, frame_path_list = await _resolve_frame_target(page, iframe_selector)
    if condition in ("element_visible", "element_hidden", "element_has_text"):
        lc = target.locator(selector)
        count = await lc.count()
        if count == 0:
            return {"met": condition == "element_hidden", "detail": {"count": 0}}
        if condition == "element_hidden":
            return {"met": False, "detail": {"count": count, "visible": True}}
        first = lc.first
        visible = await first.is_visible()
        if condition == "element_visible":
            return {"met": visible, "detail": {"count": count, "visible": visible}}
        # element_has_text
        if not visible:
            return {"met": False, "detail": {"count": count, "visible": False}}
        texts = await first.all_inner_texts() if count == 1 else await lc.locator("visible=true").all_inner_texts()
        joined = " ".join(t.strip() for t in texts if t.strip())
        matched = joined == expected_text if exact else expected_text in joined
        return {"met": matched, "detail": {"count": count, "text": joined[:200]}}

    # text_present: 目标 frame (若指定 iframe) 或全部 frame 的 body 文本
    frames: List[Any] = []
    if iframe_selector:
        frame = await _locator_content_frame(target)
        if frame is not None:
            frames = [frame]
    else:
        frames = page.frames
    for f in frames:
        try:
            body_text = await f.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            continue
        if body_text and expected_text in body_text:
            return {"met": True, "detail": {"frame": f.url, "match": True}}
    return {"met": False, "detail": {"frames_checked": len(frames)}}


# ==================== 文件下载 / 上传工具 ====================
# 背景: 项目以 no_defaults=True 接管用户日常浏览器, Playwright 不下发
# Browser.setDownloadBehavior (acceptDownloads='internal-browser-default'),
# 因此 page.on("download") 事件流不开启、Playwright 下载 API 不可用。
# 下载工具在动作窗口内自行通过浏览器级 CDP 会话开启事件流并把下载定向到
# 指定目录, 完成后恢复浏览器默认下载行为 (不干扰用户日常下载)。
# 上传工具直接走 Playwright 原生 API (set_input_files / filechooser 拦截)。

DEFAULT_DOWNLOAD_WAIT_MS = 30000


async def _resolve_download_dir(download_dir: Optional[str]) -> str:
    """解析下载保存目录: 相对路径基于用户项目根目录 (PROJECT_DIR) 而非进程 cwd。"""
    base = download_dir or DOWNLOAD_DIR
    path = os.path.abspath(base) if os.path.isabs(base) else os.path.abspath(
        os.path.join(PROJECT_DIR, base)
    )
    os.makedirs(path, exist_ok=True)
    return path


async def download_file_impl(
    by: str = "css",
    selector: Optional[str] = None,
    role: Optional[str] = None,
    name: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    download_dir: Optional[str] = None,
    filename: Optional[str] = None,
    wait_timeout_ms: int = DEFAULT_DOWNLOAD_WAIT_MS,
    description: Optional[str] = None,
) -> dict:
    """点击触发下载的按钮/链接, 将下载文件保存到指定目录并验证落盘。

    定位参数与 click_interact 一致: by=css/xpath 传 selector (支持
    iframe_selector 链式穿透), by=role 传 role+name。
    download_dir 默认 ./downloads (相对用户项目根目录, 可用环境变量
    DOWNLOAD_DIR 覆盖); filename 可指定保存名 (默认浏览器提供的文件名,
    已存在同名文件时覆盖); wait_timeout_ms 为下载完成等待上限 (默认 30s)。

    返回 status: success (文件已落盘并验证) / timeout (超时未完成) /
    no_download (点击未触发下载) / canceled (下载被取消), 附落盘文件
    路径/大小与下载来源信息, 便于后续读取分析 (如 xlsx 用 pandas 编辑保存)。
    """
    by_lower = (by or "").lower()
    if by_lower not in ("css", "xpath", "role"):
        raise RuntimeError(f"by 仅支持 css / xpath / role (下载按钮为 DOM 元素), 收到: {by}")
    if by_lower in ("css", "xpath") and not selector:
        raise RuntimeError(f"by={by_lower} 时必须提供 selector")
    if by_lower == "role" and not role:
        raise RuntimeError("by=role 时必须提供 role")
    timeout = max(1000, min(int(wait_timeout_ms), 120000))

    page = await browser_mgr.get_page()
    save_dir = await _resolve_download_dir(download_dir)
    started = time.monotonic()

    cdp = await page.context.browser.new_browser_cdp_session()
    begin_info: Dict[str, Dict[str, Any]] = {}
    state_map: Dict[str, str] = {}

    def _on_will_begin(payload: Dict[str, Any]) -> None:
        guid = str(payload.get("guid", ""))
        begin_info[guid] = {
            "guid": guid,
            "suggested_filename": payload.get("suggestedFilename"),
            "url": payload.get("url"),
        }

    def _on_progress(payload: Dict[str, Any]) -> None:
        guid = str(payload.get("guid", ""))
        state = payload.get("state")
        if guid:
            state_map[guid] = state

    cdp.on("Browser.downloadWillBegin", _on_will_begin)
    cdp.on("Browser.downloadProgress", _on_progress)
    try:
        await cdp.send("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": save_dir,
            "eventsEnabled": True,
        })
        pre_files = set(os.listdir(save_dir))
        click_result = await _do_click(
            page, by_lower, selector, iframe_selector,
            role=role, name=name,
            description=description or "触发下载",
        )
        while True:
            finished = begin_info and all(
                state_map.get(g) in ("completed", "canceled") for g in begin_info
            )
            if finished or time.monotonic() - started >= timeout / 1000:
                break
            await asyncio.sleep(0.1)
    finally:
        # 恢复浏览器默认下载行为 (不干扰用户日常下载); 失败仅告警。
        try:
            await cdp.send("Browser.setDownloadBehavior", {
                "behavior": "default",
                "eventsEnabled": False,
            })
        except Exception as e:
            logger.warning(f"恢复浏览器默认下载行为失败: {e}")
        try:
            await cdp.detach()
        except Exception:
            pass

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if not begin_info:
        return {
            "status": "no_download",
            "note": "点击未触发下载 (页面无 download 事件); 请核对按钮定位或确认下载由其他交互触发",
            "elapsed_ms": elapsed_ms,
            "download_dir": save_dir,
            "files": [],
            "click": {k: click_result.get(k) for k in ("status", "by", "selector", "element_center")},
        }

    new_files = sorted(set(os.listdir(save_dir)) - pre_files)
    if filename and new_files:
        # 指定保存名: Chrome 对重名会自动加 "(1)" 后缀, 统一改回用户指定名 (覆盖旧文件)
        src = os.path.join(save_dir, new_files[0])
        dst = os.path.join(save_dir, filename)
        if src != dst:
            try:
                os.replace(src, dst)
                new_files[0] = filename
            except OSError as e:
                logger.warning(f"重命名下载文件失败 ({src} -> {dst}): {e}")

    files_info = []
    for f in new_files:
        fp = os.path.join(save_dir, f)
        try:
            size = os.path.getsize(fp)
        except OSError:
            size = None
        files_info.append({"filename": f, "path": fp, "size_bytes": size})

    pending = [g for g in begin_info if state_map.get(g) not in ("completed", "canceled")]
    canceled = [g for g in begin_info if state_map.get(g) == "canceled"]
    if pending:
        status = "timeout"
        note = f"下载未在 {timeout}ms 内完成 (pending: {len(pending)} 个)"
    elif canceled and not any(state_map.get(g) == "completed" for g in begin_info):
        status = "canceled"
        note = "下载被浏览器取消"
    else:
        status = "success"
        note = "下载完成; 文件已落盘, 可进一步读取分析 (如 xlsx 用 pandas 编辑)"

    return {
        "status": status,
        "download_dir": save_dir,
        "files": files_info,
        "downloads": [
            {**begin_info[g], "state": state_map.get(g)}
            for g in begin_info
        ],
        "click": {k: click_result.get(k) for k in ("status", "by", "selector", "element_center")},
        "elapsed_ms": elapsed_ms,
        "note": note,
    }


def _resolve_upload_paths(file_paths: List[str]) -> List[str]:
    """上传文件路径解析: 相对路径优先基于用户项目根 (PROJECT_DIR), 其次进程 cwd。"""
    if not file_paths:
        raise RuntimeError("file_paths 不能为空")
    resolved: List[str] = []
    missing: List[str] = []
    for p in file_paths:
        if os.path.isabs(p):
            candidates = [os.path.abspath(p)]
        else:
            candidates = [
                os.path.abspath(os.path.join(PROJECT_DIR, p)),
                os.path.abspath(p),
            ]
        found = next((c for c in candidates if os.path.isfile(c)), None)
        if found is None:
            missing.append(p)
        else:
            resolved.append(found)
    if missing:
        raise RuntimeError(f"待上传文件不存在: {missing}")
    return resolved


async def upload_file_impl(
    file_paths: List[str],
    by: str = "css",
    selector: Optional[str] = None,
    role: Optional[str] = None,
    name: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    success_text: Optional[str] = None,
    wait_timeout_ms: int = 15000,
    description: Optional[str] = None,
) -> dict:
    """点击上传按钮/输入框并注入要上传的文件, 可选等待上传成功的页面反馈。

    两条路径:
      1. 定位到 <input type=file> → set_input_files 直接设置 (含隐藏/antd 包装);
      2. 定位到普通按钮 → 点击后拦截系统文件选择框 (filechooser), 不弹原生
         对话框, 直接注入文件路径 (页面逻辑照常触发上传)。

    file_paths: 一个或多个文件 (相对路径基于用户项目根目录, 必须存在)。
    success_text: 可选, 上传成功后页面出现的文本 (如"上传成功"); 指定后工具
      轮询等待其出现, 返回 success_text_found 供判断上传是否成功。
    wait_timeout_ms: success_text 等待上限 (默认 15s)。

    返回: status / mode (set_input_files | filechooser) / file_paths /
    success_text_found / 轮询详情, 便于 Agent 断言上传结果。
    """
    by_lower = (by or "").lower()
    if by_lower not in ("css", "xpath", "role"):
        raise RuntimeError(f"by 仅支持 css / xpath / role, 收到: {by}")
    if by_lower in ("css", "xpath") and not selector:
        raise RuntimeError(f"by={by_lower} 时必须提供 selector")
    if by_lower == "role" and not role:
        raise RuntimeError("by=role 时必须提供 role")
    paths = _resolve_upload_paths(file_paths)

    page = await browser_mgr.get_page()
    started = time.monotonic()
    frame_path_list: List[str] = []
    action_label = description or f"上传: {os.path.basename(paths[0])}"

    async def _set_files_once():
        nonlocal frame_path_list
        target, frame_path_list = await _resolve_frame_target(page, iframe_selector)
        if by_lower == "role":
            lc = target.get_by_role(role, name=name or None)
        else:
            full_selector = f"xpath={selector}" if by_lower == "xpath" else selector
            lc = target.locator(full_selector)
        await lc.wait_for(state="visible", timeout=ELEMENT_WAIT_TIMEOUT_MS)
        await lc.scroll_into_view_if_needed()
        is_file_input = await lc.evaluate(
            "el => el.tagName === 'INPUT' && el.type === 'file'"
        )
        if is_file_input:
            await lc.set_input_files(paths)
            return {"mode": "set_input_files", "is_multiple": None}
        async with page.expect_file_chooser(timeout=ELEMENT_WAIT_TIMEOUT_MS) as fc_info:
            await lc.click()
        chooser = await fc_info.value
        multiple = chooser.is_multiple()
        await chooser.set_files(paths)
        return {"mode": "filechooser", "is_multiple": multiple}

    try:
        set_result = await retry_ui_action(action_label, _set_files_once)
    except Exception as e:
        raise RuntimeError(f"上传文件失败: {e}") from e

    result: Dict[str, Any] = {
        "status": "success",
        **set_result,
        "file_paths": paths,
        "frame_path": frame_path_list,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if success_text:
        check = await wait_for_condition_impl(
            condition="text_present",
            expected_text=success_text,
            timeout_ms=wait_timeout_ms,
        )
        result["success_text"] = success_text
        result["success_text_found"] = check.get("status") == "success"
        result["success_check"] = {
            "status": check.get("status"),
            "state": check.get("state"),
            "elapsed_ms": check.get("elapsed_ms"),
        }
    return result
