import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp.tools.action_chain import (  # noqa: E402
    execute_action_chain_impl,
    build_action_fallbacks,
)


class FakeLocator:
    """locator 存根: 记录动作事件, 不做真实点击。

    fail_css 内选择器 = 定位失败 (wait_for 抛超时);
    hidden_css 内选择器 = 存在但不可见 (count=0 / is_visible=False, 预检跳过)。
    """

    def __init__(self, page, css):
        self.page = page
        self.css = css

    async def wait_for(self, state, timeout):
        if self.css in self.page.fail_css:
            raise TimeoutError(f"timeout: waiting for {self.css}")
        return None

    async def count(self):
        return 0 if self.css in self.page.hidden_css else 1

    async def is_visible(self):
        return self.css not in self.page.hidden_css

    async def scroll_into_view_if_needed(self):
        return None

    async def bounding_box(self):
        return None

    async def click(self, timeout=None, force=False):
        self.page.events.append(("click", self.css))

    async def dblclick(self, timeout=None, force=False):
        self.page.events.append(("dblclick", self.css))

    async def fill(self, value):
        self.page.events.append(("fill", self.css, value))

    async def press(self, key):
        self.page.events.append(("press", self.css, key))

    def locator(self, css):
        return FakeLocator(self.page, css)

class FakePage:
    """页面存根: locator/frame_locator 均返回 FakeLocator; 无 evaluate (视觉降级静默)。"""

    def __init__(self):
        self.events = []
        self.url = "https://x/"
        self.fail_css = set()
        self.hidden_css = set()
        self.keyboard = MagicMock()
        self.keyboard.type = AsyncMock()

    def locator(self, css):
        return FakeLocator(self, css)

    def get_by_role(self, role, name=None):
        suffix = f" name={name}" if name else ""
        return FakeLocator(self, f"[role={role}{suffix}]")

    def frame_locator(self, selector):
        return self

    async def wait_for_timeout(self, timeout):
        return None


def make_mocks(page):
    bm = MagicMock()
    bm.get_page = AsyncMock(return_value=page)
    snap = AsyncMock(return_value={"url": page.url, "frames": [], "popup_fingerprint": {}})
    observe = AsyncMock(return_value={"dynamic_layers": [], "new_layers": [], "summary": []})
    return bm, snap, observe


class ActionChainTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_actions_raises(self):
        with self.assertRaises(ValueError):
            await execute_action_chain_impl([])

    async def test_invalid_action_stops_and_raises(self):
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe):
            with self.assertRaises(RuntimeError) as ctx:
                await execute_action_chain_impl([{"action": "hover", "by": "css", "selector": "#x"}])
            self.assertIn("hover", str(ctx.exception))

    async def test_sequential_execution_with_single_observation(self):
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        actions = [
            {"action": "click", "by": "css", "selector": "#open", "description": "打开"},
            {"action": "fill", "by": "css", "selector": "#name", "value": "abc", "description": "输入"},
            {"action": "click", "by": "css", "selector": "#confirm", "description": "确定"},
        ]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe):
            result = await execute_action_chain_impl(actions)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["executed"], 3)
        self.assertEqual(result["failed"], [])
        # type 输入分支: 点击聚焦 → Ctrl+A 清空 → Backspace → 逐字键盘输入
        self.assertEqual(page.events, [
            ("click", "#open"),
            ("click", "#name"),
            ("press", "#name", "Control+A"),
            ("press", "#name", "Backspace"),
            ("click", "#confirm"),
        ])
        # 输入内容经键盘逐字输入 (非 locator.fill)
        page.keyboard.type.assert_awaited_once_with("abc", delay=100)
        # 链首 1 次快照 + 链尾仅 1 次观察
        snap.assert_awaited_once()
        observe.assert_awaited_once()

    async def test_confirm_approved_executes(self):
        """confirm=True + 用户批准 → 放行到执行阶段。"""
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        actions = [{"action": "click", "by": "css", "selector": "#ok", "description": "确定"}]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             patch("qa_mcp.tools.action_chain._do_click", new=AsyncMock()) as do_click, \
             patch("qa_mcp.tools.interact.elicit_with_timeout", new=AsyncMock(return_value="proceed")) as elicit:
            result = await execute_action_chain_impl(actions, confirm=True, ctx=object())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["executed"], 1)
        elicit.assert_awaited_once()
        do_click.assert_awaited_once()

    async def test_confirm_rejected_cancels(self):
        """confirm=True + 用户拒绝 → cancelled, 不执行任何动作。"""
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        actions = [{"action": "click", "by": "css", "selector": "#ok", "description": "确定"}]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             patch("qa_mcp.tools.action_chain._do_click", new=AsyncMock()) as do_click, \
             patch("qa_mcp.tools.interact.elicit_with_timeout", new=AsyncMock(return_value="abort")) as elicit:
            result = await execute_action_chain_impl(actions, confirm=True, ctx=object())
        self.assertEqual(result["status"], "cancelled")
        elicit.assert_awaited_once()
        do_click.assert_not_awaited()  # 拒绝后未执行任何动作

    async def test_confirm_timeout_defaults_proceed(self):
        """confirm=True + 超时未操作 → 默认继续执行 (proceed)。"""
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        actions = [{"action": "click", "by": "css", "selector": "#ok", "description": "确定"}]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             patch("qa_mcp.tools.action_chain._do_click", new=AsyncMock()) as do_click, \
             patch("qa_mcp.tools.interact.elicit_with_timeout", new=AsyncMock(return_value="proceed")):
            result = await execute_action_chain_impl(actions, confirm=True, ctx=object())
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["executed"], 1)
        do_click.assert_awaited_once()

    async def test_stop_on_error_false_collects_failures(self):
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        actions = [
            {"action": "click", "by": "css", "selector": "#ok1"},
            {"action": "select_option", "by": "css", "selector": "#sel"},  # 缺 value → 收集
            {"action": "click", "by": "css", "selector": "#ok2"},
        ]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe):
            result = await execute_action_chain_impl(actions, stop_on_error=False)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["executed"], 2)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["index"], 2)
        self.assertEqual(result["failed"][0]["action"], "select_option")
        self.assertEqual(page.events, [("click", "#ok1"), ("click", "#ok2")])

    async def test_select_option_routes_through_adapter(self):
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        adapter = MagicMock()
        adapter.select_option = AsyncMock()
        registry = MagicMock()
        registry.detect_framework = AsyncMock(return_value="antd")
        registry.get_adapter = MagicMock(return_value=adapter)
        actions = [
            {"action": "select_option", "by": "css", "selector": "#city", "value": "北京"},
        ]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             patch("qa_mcp.tools.action_chain.adapter_registry", registry):
            result = await execute_action_chain_impl(actions)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["executed"], 1)
        adapter.select_option.assert_awaited_once()
        # 首个参数为 UIContext (页面/locator 上下文), 第二个参数为触发框 locator, 第三个为选项文本
        self.assertEqual(adapter.select_option.await_args.args[2], "北京")

    async def test_role_locator_click(self):
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        actions = [
            {"action": "click", "by": "role", "role": "button", "name": "确定"},
        ]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe):
            result = await execute_action_chain_impl(actions)

        self.assertEqual(result["status"], "success")
        self.assertEqual(page.events, [("click", "[role=button name=确定]")])

    async def test_role_click_requires_role(self):
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe):
            with self.assertRaises(RuntimeError):
                await execute_action_chain_impl(
                    [{"action": "click", "by": "role", "selector": "#x"}]
                )

    async def test_rejects_invalid_detail(self):
        page = FakePage()
        bm, snap, observe = make_mocks(page)
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe):
            with self.assertRaises(ValueError):
                await execute_action_chain_impl([{"action": "click", "selector": "#x"}], detail="invalid")


async def _no_retry(label, fn, attempts=1, backoff_ms=0):
    """测试用: 关闭 retry_ui_action 的指数退避重试, 失败立即抛。"""
    return await fn()


def _patch_retries():
    return (
        patch("qa_mcp.tools.browser.retry_ui_action", _no_retry),
        patch("qa_mcp.tools.action_chain.retry_ui_action", _no_retry),
    )


class ActionChainFallbackTests(unittest.IsolatedAsyncioTestCase):
    """降级容错: 显式 fallbacks + 自动生成兜底变体, 主定位失败时链不中断。"""

    async def test_explicit_fallback_used_when_primary_fails(self):
        page = FakePage()
        page.fail_css.add('li[title="含附着物"]')
        bm, snap, observe = make_mocks(page)
        actions = [{
            "action": "click", "by": "css",
            "selector": 'li[title="含附着物"]',
            "fallbacks": [{
                "action": "click", "by": "css",
                "selector": 'li[title="含附着物"] >> nth=1',
            }],
        }]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             _patch_retries()[0], _patch_retries()[1]:
            result = await execute_action_chain_impl(actions)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["executed"], 1)
        self.assertEqual(page.events, [("click", 'li[title="含附着物"] >> nth=1')])

    async def test_auto_fallback_nth_variant(self):
        """li[title=...] 选项点击: 主定位失败时自动尝试 nth 变体, 命中最先可见的层。"""
        page = FakePage()
        page.fail_css.add('li[title="不含附着物"]')
        bm, snap, observe = make_mocks(page)
        actions = [{"action": "click", "by": "css", "selector": 'li[title="不含附着物"]'}]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             _patch_retries()[0], _patch_retries()[1]:
            result = await execute_action_chain_impl(actions)

        self.assertEqual(result["status"], "success")
        self.assertEqual(page.events, [("click", 'li[title="不含附着物"] >> nth=0')])

    async def test_auto_fallback_skips_hidden_nth_layers(self):
        """nth 变体预检: 隐藏层 (如已关闭的旧 dropdown) 直接跳过, 命中可见层。"""
        page = FakePage()
        # 主定位失败; nth=0 是隐藏旧层 (预检跳过); nth=1 是当前打开的可见层
        page.fail_css.add('li[title="X"]')
        page.hidden_css.add('li[title="X"] >> nth=0')
        bm, snap, observe = make_mocks(page)
        actions = [{"action": "click", "by": "css", "selector": 'li[title="X"]'}]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             _patch_retries()[0], _patch_retries()[1]:
            result = await execute_action_chain_impl(actions)

        self.assertEqual(result["status"], "success")
        self.assertEqual(page.events, [("click", 'li[title="X"] >> nth=1')])

    async def test_auto_fallback_removes_nth_when_primary_has_nth(self):
        """已带 nth 的定位指向隐藏元素时, 自动降级为去 nth 原 selector。"""
        page = FakePage()
        page.hidden_css.add('li[title="X"] >> nth=0')
        bm, snap, observe = make_mocks(page)
        actions = [{"action": "click", "by": "css", "selector": 'li[title="X"] >> nth=0'}]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             _patch_retries()[0], _patch_retries()[1]:
            result = await execute_action_chain_impl(actions)

        self.assertEqual(result["status"], "success")
        self.assertEqual(page.events, [("click", 'li[title="X"]')])

    async def test_all_attempts_fail_raises_with_attempt_count(self):
        page = FakePage()
        # 主 + nth=0/1/2 均失败; nth=3 隐藏被预检跳过 → 真实尝试 4 次
        page.fail_css.update(
            {'li[title="X"]', 'li[title="X"] >> nth=0',
             'li[title="X"] >> nth=1', 'li[title="X"] >> nth=2'}
        )
        page.hidden_css.add('li[title="X"] >> nth=3')
        bm, snap, observe = make_mocks(page)
        actions = [{"action": "click", "by": "css", "selector": 'li[title="X"]'}]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             _patch_retries()[0], _patch_retries()[1]:
            with self.assertRaises(RuntimeError) as ctx:
                await execute_action_chain_impl(actions)
        self.assertIn("已尝试 4 个定位方案", str(ctx.exception))

    async def test_dedup_same_selector_not_retried(self):
        """显式 fallback 与主定位相同时去重, 不重复尝试。"""
        page = FakePage()
        page.fail_css.add("#btn")
        bm, snap, observe = make_mocks(page)
        actions = [{
            "action": "click", "by": "css", "selector": "#btn",
            "fallbacks": [{"action": "click", "by": "css", "selector": "#btn"}],
        }]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             _patch_retries()[0], _patch_retries()[1]:
            with self.assertRaises(RuntimeError) as ctx:
                await execute_action_chain_impl(actions)
        self.assertIn("已尝试 1 个定位方案", str(ctx.exception))
        self.assertEqual(page.events, [])

    async def test_stop_on_error_false_records_attempts(self):
        page = FakePage()
        page.fail_css.add("#bad")
        bm, snap, observe = make_mocks(page)
        actions = [
            {"action": "click", "by": "css", "selector": "#bad"},
            {"action": "click", "by": "css", "selector": "#ok"},
        ]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             _patch_retries()[0], _patch_retries()[1]:
            result = await execute_action_chain_impl(actions, stop_on_error=False)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["executed"], 1)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["attempts"], 1)
        self.assertEqual(page.events, [("click", "#ok")])

    async def test_role_fallback_to_css(self):
        """by=role 且附带 selector: 语义定位失败自动退回 CSS。"""
        page = FakePage()
        page.fail_css.add("[role=button name=确定]")
        bm, snap, observe = make_mocks(page)
        actions = [{
            "action": "click", "by": "role", "role": "button", "name": "确定",
            "selector": "#confirm",
        }]
        with patch("qa_mcp.tools.action_chain.browser_mgr", bm), \
             patch("qa_mcp.tools.action_chain.snapshot_navigation", snap), \
             patch("qa_mcp.tools.action_chain.observe_after_click", observe), \
             _patch_retries()[0], _patch_retries()[1]:
            result = await execute_action_chain_impl(actions)

        self.assertEqual(result["status"], "success")
        self.assertEqual(page.events, [("click", "#confirm")])


class BuildActionFallbacksTests(unittest.TestCase):
    """build_action_fallbacks 纯函数规则。"""

    def test_li_title_adds_all_nth_variants(self):
        fbs = build_action_fallbacks(
            {"action": "click", "by": "css", "selector": 'li[title="含附着物"]'}
        )
        self.assertEqual(
            [f["selector"] for f in fbs],
            ['li[title="含附着物"] >> nth=0', 'li[title="含附着物"] >> nth=1',
             'li[title="含附着物"] >> nth=2', 'li[title="含附着物"] >> nth=3'],
        )

    def test_li_title_with_nth_removes_nth_and_other_positions(self):
        fbs = build_action_fallbacks(
            {"action": "click", "by": "css", "selector": 'li[title="X"] >> nth=0'}
        )
        self.assertEqual(
            [f["selector"] for f in fbs],
            ['li[title="X"]', 'li[title="X"] >> nth=1',
             'li[title="X"] >> nth=2', 'li[title="X"] >> nth=3'],
        )

    def test_role_with_selector_falls_back_to_css(self):
        fbs = build_action_fallbacks(
            {"action": "click", "by": "role", "role": "button", "name": "确定", "selector": "#ok"}
        )
        self.assertEqual(len(fbs), 1)
        self.assertEqual(fbs[0]["by"], "css")
        self.assertIsNone(fbs[0]["role"])
        self.assertIsNone(fbs[0]["name"])
        self.assertEqual(fbs[0]["selector"], "#ok")

    def test_css_with_role_name_falls_back_to_role(self):
        fbs = build_action_fallbacks(
            {"action": "click", "by": "css", "selector": "#ok", "role": "button", "name": "确定"}
        )
        self.assertEqual(
            fbs, [{"action": "click", "by": "role", "role": "button", "name": "确定", "selector": None}]
        )

    def test_fill_has_no_auto_fallback(self):
        self.assertEqual(
            build_action_fallbacks({"action": "fill", "by": "css", "selector": "#name"}), []
        )

    def test_press_li_title_gets_nth_variants(self):
        fbs = build_action_fallbacks(
            {"action": "press", "by": "css", "selector": 'li[title="X"]', "key": "Enter"}
        )
        self.assertEqual(len(fbs), 4)
        self.assertEqual(fbs[0]["key"], "Enter")
        self.assertEqual(fbs[0]["selector"], 'li[title="X"] >> nth=0')

    def test_fallback_preserves_action_fields(self):
        act = {"action": "click", "by": "css", "selector": 'li[title="X"]',
               "iframe_selector": "#f", "description": "选项"}
        fbs = build_action_fallbacks(act)
        self.assertEqual(fbs[0]["iframe_selector"], "#f")
        self.assertEqual(fbs[0]["description"], "选项")


if __name__ == "__main__":
    unittest.main()
