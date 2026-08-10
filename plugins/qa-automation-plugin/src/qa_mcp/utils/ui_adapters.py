import asyncio
import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from playwright.async_api import FrameLocator, Locator, Page

from qa_mcp.config import (
    SELECT_WAIT_FIRST_MS,
    SELECT_WAIT_RETRY_MS,
    SELECT_RETRY_ATTEMPTS,
    SELECT_POLL_INTERVAL_MS,
)

UIContext = Page | FrameLocator


def _normalise_date_for_ant_calendar(date_str: str) -> str:
    for date_format in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(date_str.strip(), date_format).strftime("%Y/%m/%d")
        except ValueError:
            continue
    raise ValueError(f"无法识别日期格式: {date_str}")

logger = logging.getLogger("mcp_automation.ui_adapters")

class UIAdapter(ABC):
    @property
    @abstractmethod
    def framework_name(self) -> str:
        pass

    @abstractmethod
    async def select_option(self, page: UIContext, trigger_locator: Locator, option_text: str) -> None:
        pass

    @abstractmethod
    async def fill_date(self, page: UIContext, trigger_locator: Locator, date_str: str) -> None:
        pass


class StandardHTMLAdapter(UIAdapter):
    @property
    def framework_name(self) -> str:
        return "standard"

    async def select_option(self, page: UIContext, trigger_locator: Locator, option_text: str) -> None:
        await trigger_locator.select_option(label=option_text)

    async def fill_date(self, page: UIContext, trigger_locator: Locator, date_str: str) -> None:
        await trigger_locator.fill(date_str)


class ElementPlusAdapter(UIAdapter):
    @property
    def framework_name(self) -> str:
        return "element_plus"

    async def select_option(self, page: UIContext, trigger_locator: Locator, option_text: str) -> None:
        await trigger_locator.click()
        popover_selector = ".el-select-dropdown:visible, .el-popper:visible"
        await page.locator(popover_selector).wait_for(state="visible", timeout=4000)
        option = page.locator(".el-select-dropdown__item").filter(has_text=option_text).first
        await option.click()

    async def fill_date(self, page: UIContext, trigger_locator: Locator, date_str: str) -> None:
        await trigger_locator.evaluate("el => el.removeAttribute('readonly')")
        await trigger_locator.fill(date_str)
        await trigger_locator.press("Enter")


class AntDesignAdapter(UIAdapter):
    @property
    def framework_name(self) -> str:
        return "ant_design"

    async def select_option(self, page: UIContext, trigger_locator: Locator, option_text: str) -> None:
        await trigger_locator.click()

        # 连续下拉场景 (动作链里挨个选下拉) 存在动画竞态: 上一个下拉的收起动画
        # 未结束前 (元素仍 :visible), .last 可能命中残留下拉, 导致"选项不存在"。
        # 处理: 首次等 SELECT_WAIT_FIRST_MS; 找不到目标选项时, 每 SELECT_POLL_INTERVAL_MS
        # 重新定位最新可见下拉重试, 直至新下拉挂载, 最多 SELECT_RETRY_ATTEMPTS 次。
        for attempt in range(SELECT_RETRY_ATTEMPTS):
            dropdown = page.locator(
                ".ant-select-dropdown:visible, "
                ".ant-cascader-dropdown:visible, "
                ".ant-cascader-menus:visible"
            ).last
            try:
                await dropdown.wait_for(
                    state="visible",
                    timeout=SELECT_WAIT_FIRST_MS if attempt == 0 else SELECT_WAIT_RETRY_MS,
                )
            except Exception:
                if attempt == SELECT_RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(SELECT_POLL_INTERVAL_MS / 1000)
                continue

            if await self._click_option_in(dropdown, option_text):
                return
            await asyncio.sleep(SELECT_POLL_INTERVAL_MS / 1000)

        raise RuntimeError(f"Ant Design 下拉选项不存在: {option_text}")

    async def _click_option_in(self, dropdown: Locator, option_text: str) -> bool:
        """在下拉层内按文本定位选项并点击。

        匹配优先级: a11y role=option 精确 → 选项文本精确 → 唯一子串。
        歧义 (多个候选同时包含目标文本, 如 "含过敏原" vs "不含过敏原") 时
        抛出带完整候选列表的 RuntimeError, 让调用方 (Agent) 拿到可决策信息,
        而不是盲目点击第一个 (会误选) 或 strict violation 后无从下手。
        """
        role_opt = dropdown.get_by_role("option", name=option_text, exact=True).first
        if await role_opt.count() > 0:
            await role_opt.click()
            return True

        candidates = dropdown.locator(
            ".ant-select-item-option, "
            ".ant-select-dropdown-menu-item, "
            ".ant-select-tree-treenode, "
            ".ant-cascader-menu-item"
        )
        if await candidates.count() == 0:
            return False

        # 收集全部选项文本 (DOM 顺序, 与 nth() 索引一致), 做精确/子串决策
        texts = await candidates.evaluate_all(
            "els => els.map(e => (e.innerText || e.textContent || '').trim())"
        )
        exact_idx = [i for i, t in enumerate(texts) if t == option_text]
        if len(exact_idx) == 1:
            await candidates.nth(exact_idx[0]).click()
            return True
        if len(exact_idx) > 1:
            raise RuntimeError(
                f"Ant Design 下拉选项 '{option_text}' 存在多个精确匹配, "
                f"候选: {sorted(set(texts))[:20]}"
            )

        sub_idx = [i for i, t in enumerate(texts) if t and option_text in t]
        if len(sub_idx) == 1:
            await candidates.nth(sub_idx[0]).click()
            return True
        if len(sub_idx) > 1:
            raise RuntimeError(
                f"Ant Design 下拉选项 '{option_text}' 匹配到多个候选: "
                f"{sorted(set(texts))[:20]}, 请使用完整选项文本"
            )
        return False

    async def fill_date(self, page: UIContext, trigger_locator: Locator, date_str: str) -> None:
        date_input = trigger_locator.locator("input").first
        if await date_input.count() == 0:
            date_input = trigger_locator

        if await date_input.get_attribute("readonly") is not None:
            calendar = page.locator(".ant-calendar:visible").last
            if await calendar.count() == 0:
                await trigger_locator.click()
                calendar = page.locator(".ant-calendar:visible").last
            if await calendar.count() > 0:
                await self._fill_legacy_calendar(trigger_locator, page, date_str, calendar)
                return
            await date_input.evaluate("el => el.removeAttribute('readonly')")

        await date_input.fill(date_str)
        await date_input.press("Enter")

    async def _fill_legacy_calendar(
        self,
        trigger_locator: Locator,
        page: UIContext,
        date_str: str,
        calendar: Locator | None = None,
    ) -> None:
        target_title = _normalise_date_for_ant_calendar(date_str)

        if calendar is None:
            calendar = page.locator(".ant-calendar:visible").last
            if await calendar.count() == 0:
                await trigger_locator.click()
        await calendar.wait_for(state="visible", timeout=4000)
        target = calendar.locator(
            f'.ant-calendar-cell[title="{target_title}"] .ant-calendar-date'
        ).first

        for _ in range(120):
            if await target.count() > 0:
                await target.click()
                placeholder = await trigger_locator.get_attribute("placeholder") or ""
                class_name = await trigger_locator.get_attribute("class") or ""
                is_range_start = (
                    "ant-calendar-range-picker-input" in class_name
                    and placeholder in {"开始日期", "start"}
                )
                if not is_range_start:
                    await trigger_locator.page.keyboard.press("Escape")
                return

            visible_titles = await calendar.locator(".ant-calendar-cell[title]").evaluate_all(
                "els => els.map(el => el.getAttribute('title')).filter(Boolean)"
            )
            if not visible_titles:
                break

            target_month = datetime.strptime(target_title[:7], "%Y/%m")
            first_month = min(datetime.strptime(title[:7], "%Y/%m") for title in visible_titles)
            last_month = max(datetime.strptime(title[:7], "%Y/%m") for title in visible_titles)

            if target_month < first_month:
                await calendar.locator(".ant-calendar-prev-month-btn").click()
            elif target_month > last_month:
                await calendar.locator(".ant-calendar-next-month-btn").click()
            else:
                break

            await target.wait_for(state="attached", timeout=1000)

        raise RuntimeError(f"Ant Design 日历中不存在日期: {date_str}")


class SapFioriAdapter(UIAdapter):
    @property
    def framework_name(self) -> str:
        return "sap_fiori"

    async def select_option(self, page: UIContext, trigger_locator: Locator, option_text: str) -> None:
        await trigger_locator.click()
        option_selector = ".sapMComboBoxBoxItem, .sapMSelectListItem"
        await page.locator(option_selector).first.wait_for(state="visible", timeout=4000)
        option = page.locator(option_selector).filter(has_text=option_text).first
        await option.click()

    async def fill_date(self, page: UIContext, trigger_locator: Locator, date_str: str) -> None:
        await trigger_locator.focus()
        await trigger_locator.fill(date_str)
        await trigger_locator.press("Tab")


class UIAdapterRegistry:
    # 框架检测缓存: 同一页面 URL + frame 数未变时, 30s 内复用探测结果。
    # detect_framework 对每个 frame 跑一次 JS, 高频调用 (动作链每步一次) 时
    # 无谓重复探测; 页面内框架集合基本不变, 短 TTL 缓存零风险。
    FRAMEWORK_CACHE_TTL = 30.0

    def __init__(self):
        self._adapters: dict[str, UIAdapter] = {}
        self._framework_cache: dict[str, tuple[float, str]] = {}
        self.register(StandardHTMLAdapter())
        self.register(ElementPlusAdapter())
        self.register(AntDesignAdapter())
        self.register(SapFioriAdapter())

    def register(self, adapter: UIAdapter):
        self._adapters[adapter.framework_name] = adapter

    def get_adapter(self, name: str) -> UIAdapter:
        return self._adapters.get(name, self._adapters["standard"])

    async def detect_framework(self, page: Page) -> str:
        try:
            url = page.url
        except Exception:
            url = ""
        cache_key = f"{url}|{len(page.frames)}"
        now = time.monotonic()
        cached = self._framework_cache.get(cache_key)
        if cached and now - cached[0] < self.FRAMEWORK_CACHE_TTL:
            return cached[1]

        detection_script = """() => {
            if (window.sap || document.querySelector('[id^="sap-ui-bootstrap"]')) return 'sap_fiori';
            if (document.querySelector('[class^="ant-"], [class*=" ant-"]')) return 'ant_design';
            if (document.querySelector('[class^="el-"], [class*=" el-"]')) return 'element_plus';
            return 'standard';
        }"""

        try:
            detected = []
            for frame in page.frames:
                try:
                    framework = await frame.evaluate(detection_script)
                    if framework != "standard" and framework not in detected:
                        detected.append(framework)
                except Exception:
                    continue

            for framework in ("sap_fiori", "ant_design", "element_plus"):
                if framework in detected:
                    self._framework_cache[cache_key] = (now, framework)
                    return framework
            self._framework_cache[cache_key] = (now, "standard")
            return "standard"
        except Exception:
            return "standard"

adapter_registry = UIAdapterRegistry()
