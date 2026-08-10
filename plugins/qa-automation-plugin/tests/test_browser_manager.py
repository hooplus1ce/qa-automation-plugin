import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp.tools.browser import BrowserManager, _fill_watchdog_timeout  # noqa: E402


class FakeTab:
    def __init__(self, url, visible=True, closed=False):
        self.url = url
        self._visible = visible
        self._closed = closed

    def is_closed(self):
        return self._closed

    async def evaluate(self, expression):
        return self._visible  # 真实浏览器返回布尔 (visibilityState === 'visible')

    def close(self):
        self._closed = True

    def __repr__(self):
        return f"<FakeTab {self.url}>"


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


def make_manager(pages):
    mgr = BrowserManager("http://fake-cdp:9222")
    mgr._context = FakeContext(pages)
    return mgr


class PageSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_picks_visible_hoolinks_first(self):
        hidden = FakeTab("https://demo18-scm.hoolinks.com/old", visible=False)
        visible = FakeTab("https://demo18-scm.hoolinks.com/static/admin")
        mgr = make_manager([hidden, visible])
        self.assertIs(await mgr._select_page(), visible)

    async def test_falls_back_to_any_hoolinks_when_none_visible(self):
        hidden1 = FakeTab("https://demo18-scm.hoolinks.com/a", visible=False)
        hidden2 = FakeTab("https://demo18-scm.hoolinks.com/b", visible=False)
        mgr = make_manager([hidden1, hidden2])
        self.assertIs(await mgr._select_page(), hidden1)

    async def test_non_hoolinks_falls_back_to_non_system_page(self):
        user_page = FakeTab("https://www.google.com/")
        chrome_page = FakeTab("chrome://newtab/")
        mgr = make_manager([chrome_page, user_page])
        self.assertIs(await mgr._select_page(), user_page)

    async def test_all_system_pages_falls_back_to_first(self):
        a = FakeTab("chrome://newtab/")
        b = FakeTab("about:blank")
        mgr = make_manager([a, b])
        self.assertIs(await mgr._select_page(), a)

    async def test_locked_page_is_reused_after_new_tab_opens(self):
        """锁定页保持: 用户新开 hoolinks 标签页后, 操作仍作用于原测试页。"""
        test_page = FakeTab("https://demo18-scm.hoolinks.com/static/admin")
        mgr = make_manager([test_page])
        first = await mgr._select_page()
        self.assertIs(first, test_page)

        # 用户新开一个 hoolinks 标签页 (模拟日常活动)
        user_page = FakeTab("https://www.hoolinks.com/somewhere", visible=True)
        mgr._context.pages.append(user_page)
        self.assertIs(await mgr._select_page(), test_page)

    async def test_re_selects_when_locked_page_closed(self):
        """锁定页被关闭 → 重新选择, 不误选用户活动页 (优先可见 hoolinks/非系统页)。"""
        test_page = FakeTab("https://demo18-scm.hoolinks.com/static/admin")
        mgr = make_manager([test_page])
        self.assertIs(await mgr._select_page(), test_page)

        test_page.close()
        user_page = FakeTab("https://www.google.com/", visible=True)
        mgr._context.pages.append(user_page)
        self.assertIs(await mgr._select_page(), user_page)

    async def test_switch_target_binds_by_url_substring(self):
        test_page = FakeTab("https://demo18-scm.hoolinks.com/static/admin")
        other_page = FakeTab("https://docs.qq.com/doc/xyz")
        mgr = make_manager([test_page, other_page])
        self.assertIs(await mgr._select_page(), test_page)

        bound = await mgr.switch_target("docs.qq.com")
        self.assertIs(bound, other_page)
        self.assertIs(await mgr._select_page(), other_page)

    async def test_switch_target_without_match_raises(self):
        mgr = make_manager([FakeTab("https://demo18-scm.hoolinks.com/static/admin")])
        with self.assertRaises(RuntimeError):
            await mgr.switch_target("does-not-exist.example")


class TestFillWatchdogTimeout(unittest.TestCase):
    def test_short_input_uses_base(self):
        """短输入: 看门狗超时 = 基础单步上限 (15s)。"""
        self.assertEqual(_fill_watchdog_timeout("abc"), 15.0)

    def test_long_input_scales_with_length(self):
        """长文本逐字输入: 超时按字符数放宽, 不被看门狗误杀。"""
        # 200 字符 @100ms/字 ≈ 20s 输入 + 6s 余量 → >15s 基础值
        t = _fill_watchdog_timeout("x" * 200)
        self.assertGreater(t, 15.0)
        self.assertAlmostEqual(t, 200 * 0.12 + 6.0, places=6)

    def test_empty_value_uses_base(self):
        self.assertEqual(_fill_watchdog_timeout(""), 15.0)

    def test_custom_base(self):
        """基础值可配 (ACTION_STEP_TIMEOUT_MS 环境变量覆盖时)。"""
        self.assertEqual(_fill_watchdog_timeout("x" * 10, base_ms=30000), 30.0)


class TestLayerAwareDiagnosis(unittest.IsolatedAsyncioTestCase):
    """弹层感知定位诊断: 区分选择器失效 / 隐藏弹层需激活 / 消息容器未挂载。"""

    async def test_visible_wait_success_returns_locator(self):
        from unittest.mock import AsyncMock

        from qa_mcp.tools import browser

        lc = AsyncMock()
        lc.wait_for = AsyncMock(return_value=None)
        out = await browser._wait_visible_or_first(lc, "点击", 3000)
        self.assertIs(out, lc)
        lc.wait_for.assert_awaited_once_with(state="visible", timeout=3000)

    async def test_not_attached_falls_back_to_count_diagnosis(self):
        from unittest.mock import AsyncMock

        from qa_mcp.tools import browser

        lc = AsyncMock()
        lc.wait_for = AsyncMock(side_effect=TimeoutError("timeout: waiting for #btn"))
        lc.first = AsyncMock()
        lc.first.wait_for = AsyncMock(side_effect=TimeoutError("not attached"))
        lc.count = AsyncMock(return_value=0)
        with self.assertRaisesRegex(RuntimeError, "匹配 0 个元素"):
            await browser._wait_visible_or_first(lc, "点击", 3000)

    async def test_hidden_layer_reports_container(self):
        """元素已挂载但不可见: 报告最近弹层容器 (hidden/display:none), 提示先激活。"""
        from unittest.mock import AsyncMock

        from qa_mcp.tools import browser

        lc = AsyncMock()
        lc.wait_for = AsyncMock(side_effect=TimeoutError("timeout: waiting for #sel"))
        lc.first = AsyncMock()
        lc.first.wait_for = AsyncMock(return_value=None)
        lc.first.evaluate = AsyncMock(
            return_value={
                "containerClass": "ant-select-dropdown",
                "role": "listbox",
                "hidden": True,
                "display": "none",
                "visibility": "hidden",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "ant-select-dropdown") as ctx:
            await browser._wait_visible_or_first(lc, "选择", 3000)
        self.assertIn("先点击触发器激活", str(ctx.exception))

    async def test_message_container_not_mounted(self):
        """消息容器 (message-content): 提示内容可能未挂载完成或已消失。"""
        from unittest.mock import AsyncMock

        from qa_mcp.tools import browser

        lc = AsyncMock()
        lc.wait_for = AsyncMock(side_effect=TimeoutError("timeout"))
        lc.first = AsyncMock()
        lc.first.wait_for = AsyncMock(return_value=None)
        lc.first.evaluate = AsyncMock(
            return_value={
                "containerClass": "message-content",
                "role": "",
                "hidden": False,
                "display": "block",
                "visibility": "visible",
            }
        )
        with self.assertRaisesRegex(RuntimeError, "message-content") as ctx:
            await browser._wait_visible_or_first(lc, "读取消息", 3000)
        self.assertIn("未挂载完成或已消失", str(ctx.exception))

    async def test_evaluate_failure_falls_back_to_count(self):
        """探针 evaluate 失败 (strict 等) → 回退 count 诊断。"""
        from unittest.mock import AsyncMock

        from qa_mcp.tools import browser

        lc = AsyncMock()
        lc.wait_for = AsyncMock(side_effect=TimeoutError("timeout"))
        lc.first = AsyncMock()
        lc.first.wait_for = AsyncMock(return_value=None)
        lc.first.evaluate = AsyncMock(side_effect=RuntimeError("strict mode violation"))
        lc.count = AsyncMock(return_value=2)
        with self.assertRaisesRegex(RuntimeError, "匹配 2 个元素"):
            await browser._wait_visible_or_first(lc, "点击", 3000)


if __name__ == "__main__":
    unittest.main()
