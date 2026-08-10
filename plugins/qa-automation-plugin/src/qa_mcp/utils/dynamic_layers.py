import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import Frame, Page


FramePathResolver = Callable[[Frame], Awaitable[list[str]]]


DYNAMIC_LAYER_SCAN_SCRIPT = r"""(detail) => {
    const verbose = detail === 'full';
    const layerSelectors = [
        '.message-content',
        '[class*="message-content"]',
        '.ant-message',
        '.ant-message-notice',
        '.ant-notification',
        '.ant-notification-notice',
        '.ant-modal-root',
        '.ant-modal-wrap',
        '.ant-modal',
        '.ant-drawer',
        '.ant-popover',
        '.ant-tooltip',
        '.ant-dropdown',
        '.ant-dropdown-menu',
        '.ant-select-dropdown',
        '.ant-cascader-dropdown',
        '.ant-cascader-menus',
        '.ant-calendar',
        '.ant-picker-dropdown',
        '.ant-tree-select-dropdown',
        '.el-select-dropdown',
        '.el-popper',
        '.el-message',
        '.el-message-box',
        '.el-dialog',
        '[role="dialog"]',
        '[role="alert"]',
        '[role="status"]',
        '[role="listbox"]',
        '[role="menu"]',
        '[aria-live="assertive"]',
        '[aria-live="polite"]',
        // VTable canvas 同级弹层 (iframe 内 .vtable 容器下的 DOM 弹层):
        // 右键菜单 / 气泡提示, 隐藏态带 --hidden 类会被 hasHiddenMarker 跳过
        '.vtable__menu-element',
        '.vtable__bubble-tooltip-element'
    ];
    const dynamicClassPattern = /(message|toast|modal|dialog|drawer|popup|popper|dropdown|select|cascader|calendar|picker|tree|menu|tooltip|popover|notice|notification|alert|overlay|filter)/i;
    const interactiveSelector = [
        'button', 'input', 'select', 'textarea', 'a',
        '[role]', '[tabindex]:not([tabindex="-1"])'
    ].join(', ');
    const seen = new Map();

    function cleanText(value) {
        return String(value || '').replace(/\s+/g, ' ').trim();
    }

    function classText(element) {
        return typeof element.className === 'string'
            ? element.className
            : element.getAttribute('class') || '';
    }

    function hasHiddenMarker(element) {
        const cls = classText(element);
        // antd/el 收起动画残影 (slide-up-leave / slide-up-leave-active / leave-done):
        // 动画结束前元素仍 :visible, 会被误判为"还开着的下拉/弹层",
        // 造成观察结果噪音 (Agent 误以为浮层未关闭)。
        return element.hidden ||
            element.getAttribute('aria-hidden') === 'true' ||
            /hidden/i.test(cls) ||
            /-leave\b|leave-active\b|leave-done\b/.test(cls);
    }

    function isVisible(element) {
        let current = element;
        while (current && current.nodeType === Node.ELEMENT_NODE) {
            if (hasHiddenMarker(current)) return false;
            const style = getComputedStyle(current);
            if (
                style.display === 'none' ||
                style.visibility === 'hidden' ||
                style.visibility === 'collapse' ||
                Number.parseFloat(style.opacity || '1') === 0
            ) return false;
            current = current.parentElement;
        }
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    function selectorFor(element) {
        if (element.id) return `#${CSS.escape(element.id)}`;
        const testId = element.getAttribute('data-testid') ||
            element.getAttribute('data-test') ||
            element.getAttribute('data-qa');
        if (testId) return `[data-testid="${CSS.escape(testId)}"]`;

        const path = [];
        let current = element;
        while (current && current.nodeType === Node.ELEMENT_NODE && path.length < 4) {
            let part = current.tagName.toLowerCase();
            const classes = classText(current).split(/\s+/).filter(Boolean).slice(0, 2);
            if (classes.length) part += '.' + classes.map(CSS.escape).join('.');
            path.unshift(part);
            current = current.parentElement;
        }
        return path.join(' > ');
    }

    function attributesFor(element) {
        // 精简名单: 只保留对 Agent 定位/判读有用的属性; style/aria-hidden 等噪音剔除。
        // class 单独保留(截断 120 字符), 用于组件类型识别(ant-select-sm 等)。
        const names = [
            'id', 'role', 'aria-label', 'aria-live', 'title', 'placeholder',
            'name', 'data-testid', 'data-test', 'data-qa', 'hidden'
        ];
        const attributes = {};
        for (const name of names) {
            if (element.hasAttribute(name)) attributes[name] = element.getAttribute(name) || '';
        }
        const cls = classText(element).replace(/\s+/g, ' ').trim();
        if (cls) attributes['class'] = cls.slice(0, 120);
        return attributes;
    }

    function accessibleName(element) {
        const labelledBy = element.getAttribute('aria-labelledby');
        if (labelledBy) {
            const text = labelledBy.split(/\s+/)
                .map(id => document.getElementById(id)?.innerText || '')
                .join(' ');
            if (cleanText(text)) return cleanText(text);
        }
        return cleanText(
            element.getAttribute('aria-label') ||
            element.getAttribute('title') ||
            element.getAttribute('placeholder') ||
            element.innerText
        ).slice(0, 200);
    }

    function elementInfo(element) {
        // Token 精简: 空值字段 (空 role/placeholder/disabled=false/空 attributes) 一律
        // 不出 key, 避免每元素都带一串 ""/false 占位; 判读语义不变。
        const info = {
            tag: element.tagName.toLowerCase(),
            selector: selectorFor(element),
            text: cleanText(element.innerText || element.value || '').slice(0, 500),
            accessible_name: accessibleName(element)
        };
        const role = element.getAttribute('role');
        if (role) info.role = role;
        const placeholder = element.getAttribute('placeholder');
        if (placeholder) info.placeholder = placeholder;
        if ('value' in element) info.value = String(element.value || '');
        if (Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true')) {
            info.disabled = true;
        }
        const attrs = attributesFor(element);
        if (Object.keys(attrs).length) info.attributes = attrs;
        return info;
    }

    function kindFor(element) {
        const value = (classText(element) + ' ' + (element.getAttribute('role') || '')).toLowerCase();
        if (value.includes('message') || value.includes('toast') || value.includes('alert') || value.includes('status')) return 'message';
        if (value.includes('modal') || value.includes('dialog') || value.includes('drawer')) return 'modal';
        if (value.includes('notification') || value.includes('notice')) return 'notification';
        if (value.includes('tooltip') || value.includes('popover')) return 'tooltip';
        if (value.includes('dropdown') || value.includes('select') || value.includes('cascader') || value.includes('calendar') || value.includes('picker') || value.includes('menu') || value.includes('listbox') || value.includes('filter')) return 'dropdown';
        return 'popup';
    }

    function addCandidate(element, source) {
        if (!element || !isVisible(element)) return;
        // 静态常驻菜单过滤: 页面侧边导航等 ant-menu/el-menu 不是动态层(每轮全量上报是 Token 噪音);
        // 位于 modal/drawer/dropdown 等浮层容器内的菜单(筛选面板/右键菜单等)是动态层, 保留。
        const isStaticMenu = /\bant-menu\b|\bel-menu\b/.test(classText(element)) ||
            (element.matches && element.matches('.ant-menu, .el-menu')) ||
            Boolean(element.closest && element.closest('.ant-menu, .el-menu'));
        if (isStaticMenu && !element.closest(
            '.ant-modal, .ant-drawer, .ant-dropdown, .ant-popover, .ant-select-dropdown, ' +
            '.ant-picker-dropdown, .ant-cascader-dropdown, .vtable__menu-element'
        )) return;
        const existing = seen.get(element);
        if (existing) {
            if (!existing.matched_by.includes(source)) existing.matched_by.push(source);
            return;
        }
        seen.set(element, {
            element,
            matched_by: [source]
        });
    }

    for (const selector of layerSelectors) {
        document.querySelectorAll(selector).forEach(element => addCandidate(element, selector));
    }

    const bodyChildren = Array.from(document.body?.children || []);
    const scripts = Array.from(document.scripts);
    const lastScript = scripts.at(-1);
    const tailNodes = [];
    if (lastScript?.parentElement === document.body) {
        let node = lastScript.nextElementSibling;
        while (node) {
            tailNodes.push(node);
            node = node.nextElementSibling;
        }
    } else {
        tailNodes.push(...bodyChildren);
    }
    for (const element of tailNodes) {
        if (dynamicClassPattern.test(classText(element))) {
            addCandidate(element, 'body-tail');
        }
    }

    const entries = Array.from(seen.values());
    const topLevelEntries = entries.filter(entry =>
        !entries.some(parent => parent !== entry && parent.element.contains(entry.element))
    );

    const allLayers = topLevelEntries.map(entry => {
        const element = entry.element;
        const kind = kindFor(element);
        const interactive = Array.from(element.querySelectorAll(interactiveSelector))
            .filter(isVisible)
            // Token 精简: role=document/dialog 的是"层容器本身" (其文本与层 text
            // 完全重复), 内部控件会被单独列出, 剔除纯冗余项
            .filter(el => {
                const r = el.getAttribute('role');
                return r !== 'document' && r !== 'dialog';
            })
            .slice(0, verbose ? 50 : 20)
            .map(elementInfo);
        const text = cleanText(element.innerText || element.textContent).slice(0, verbose ? 2000 : 300);
        // 空骨架弹层过滤: 消息容器无任何内容 (如 <div class="ant-message"><span></span></div>)
        // 视为"无消息"不报出; 出现 .ant-message-notice 等实际内容后才会被观察到
        if (kind === 'message' && !text && interactive.length === 0) return null;
        return {
            kind: kind,
            tag: element.tagName.toLowerCase(),
            selector: selectorFor(element),
            matched_by: entry.matched_by,
            text: text,
            html: element.outerHTML.slice(0, verbose ? 4000 : 600),
            attributes: attributesFor(element),
            interactive_elements: interactive,
            tail_node: tailNodes.some(node => node === element || node.contains(element))
        };
    }).filter(Boolean);

    // 层数量上限: 异常页面防爆炸 (每 frame 最多 15 层, 超出截断并标记)
    return {
        last_script: lastScript ? {
            src: lastScript.src || '',
            id: lastScript.id || '',
            parent: lastScript.parentElement?.tagName || ''
        } : null,
        truncated: allLayers.length > 15,
        layers: allLayers.slice(0, 15)
    };
}"""


async def scan_dynamic_layers(
    page: Page,
    frame_path_resolver: FramePathResolver,
    iframe_selector: Optional[str] = None,
    wait_ms: int = 1200,
    poll_interval_ms: int = 100,
    detail: str = "brief",
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """轮询目标 frame, 捕获短暂出现的 APS 消息/弹层。

    detail: brief(默认, 剪枝输出: html≤600/text≤300/交互元素≤20/attributes 精简)
            | full(完整输出: html≤4000/text≤2000/交互元素≤50)。
    clock: 时间源, 默认 time.monotonic; 测试可注入假时钟。
    提前返回: 首轮后出现新层且稳定 250ms 即返回, 不等满 wait_ms。
    """
    if detail not in ("brief", "full"):
        raise ValueError(f"detail 仅支持 brief / full, 收到: {detail}")
    selectors = [part.strip() for part in (iframe_selector or '').split('->') if part.strip()]
    wait_ms = max(0, min(wait_ms, 5000))
    poll_interval_ms = max(50, min(poll_interval_ms, 500))
    started = clock()
    observed: dict[str, dict[str, Any]] = {}
    scanned_frames: set[str] = set()
    first_keys: Optional[set[str]] = None
    last_new_keys: Optional[set[str]] = None
    new_seen_at: Optional[float] = None
    # 单帧 evaluate 挂死 (3s 超时) 的 frame: 后续轮询跳过, 不让一个坏 frame
    # 在每轮轮询各拖 3s, 把整个观察窗口无限拉长
    dead_frames: set[int] = set()

    while True:
        for frame in page.frames:
            if id(frame) in dead_frames:
                continue
            try:
                # frame_path_resolver / evaluate 均为无动作级超时的 CDP 调用,
                # 渲染繁忙/主线程假死时可能无限等待, 逐帧加 3s 上限兜底。
                frame_path = await asyncio.wait_for(
                    frame_path_resolver(frame), timeout=3
                )
                if selectors and frame_path != selectors:
                    continue
                scanned_frames.add("->".join(frame_path))
                snapshot = await asyncio.wait_for(
                    frame.evaluate(DYNAMIC_LAYER_SCAN_SCRIPT, detail), timeout=3
                )
            except asyncio.TimeoutError:
                dead_frames.add(id(frame))
                continue
            except Exception:
                continue

            for layer in snapshot.get("layers", []):
                key = "|".join([
                    "->".join(frame_path),
                    layer.get("kind", ""),
                    layer.get("selector", ""),
                    layer.get("text", "")
                ])
                layer["frame_path"] = frame_path
                layer["frame_url"] = frame.url
                observed[key] = layer

        # 提前返回: 首轮后的新层已稳定 250ms 即停 (典型弹层 300-600ms 返回,
        # 不再固定等满 wait_ms); 无新层则等满 wait_ms 兜底。
        # 注意: 新层集合不变时不得刷新稳定计时 (否则永不稳定)。
        if first_keys is None:
            first_keys = set(observed)
        else:
            current_new = set(observed) - first_keys
            if current_new and current_new != last_new_keys:
                last_new_keys = current_new
                new_seen_at = clock()
            if new_seen_at is not None and clock() - new_seen_at >= 0.25:
                break

        elapsed_ms = int((clock() - started) * 1000)
        if elapsed_ms >= wait_ms:
            break
        await page.wait_for_timeout(min(poll_interval_ms, wait_ms - elapsed_ms))

    return {
        "layers": list(observed.values()),
        "layer_count": len(observed),
        "frames_scanned": len(scanned_frames),
        "observation_ms": int((clock() - started) * 1000),
        "iframe_selector": iframe_selector,
        "detail": detail,
    }
