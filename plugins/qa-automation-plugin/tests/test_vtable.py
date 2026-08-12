import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp.tools.browser import capture_screenshot_impl, click_interact_impl  # noqa: E402
from qa_mcp.tools.vtable import VTableManager  # noqa: E402


class FakeFrame:
    """返回预设 evaluate 结果的 frame mock; 结果耗尽后重复最后一个"""

    def __init__(self, results):
        self._results = list(results)
        self._last = None

    async def evaluate(self, script, arg=None):
        if not self._results:
            return self._last
        self._last = self._results.pop(0)
        return self._last


class FakePage:
    def __init__(self, iframe_rect, url="https://example.com/page"):
        self._iframe_rect = iframe_rect
        self.url = url
        self.mouse = MagicMock()
        self.mouse.click = AsyncMock()
        self.mouse.dblclick = AsyncMock()
        self.mouse.move = AsyncMock()
        self.mouse.down = AsyncMock()
        self.mouse.up = AsyncMock()
        # vtable 坐标合成改用 Playwright 原生 locator.bounding_box() 读 iframe 偏移
        _loc = MagicMock()
        _loc.bounding_box = AsyncMock(
            return_value={
                "x": iframe_rect.get("left", 0.0),
                "y": iframe_rect.get("top", 0.0),
                "width": 800,
                "height": 600,
            }
        )
        self.locator = MagicMock(return_value=_loc)

    async def evaluate(self, script, arg=None):
        return self._iframe_rect


LOCATED = {
    "col_index": 0,
    "canvas_rect": {"left": 303.98, "top": 130.59},
    "targets": [
        {
            "record_index": 16,
            "body_row": 17,
            "rect": {"x1": 0.0, "y1": 28.0, "x2": 40.0, "y2": 52.0},
            "visible": True,
        }
    ],
}

IFRAME_RECT = {"left": 169.99, "top": 79.99}


def _observe_result(page):
    """构造固定的点击后观察结果 (单测打桩用)。"""
    return {
        "dynamic_layers": [], "dynamic_layer_count": 0, "new_layers": [], "new_layer_count": 0,
        "navigation": {"url_changed": False, "url_before": page.url, "url_after": page.url,
                       "frames_changed": False, "frames_before": [], "frames_after": []},
    }


def _patch_observe(page, mgr):
    """打桩公共观察函数 (qa_mcp.tools.browser.snapshot_navigation / observe_after_click),
    返回 (snap_mock, obs_mock, patchers); 调用方须对 patchers 逐个 addCleanup(stop)。"""
    snap_mock = AsyncMock(return_value={"url": page.url, "frames": [], "popup_fingerprint": {}})
    obs_mock = AsyncMock(return_value=_observe_result(page))
    p1 = patch("qa_mcp.tools.vtable.snapshot_navigation", snap_mock)
    p2 = patch("qa_mcp.tools.vtable.observe_after_click", obs_mock)
    p1.start()
    p2.start()
    return snap_mock, obs_mock, [p1, p2]


class VTableSelectRowsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mgr = VTableManager()
        self.mgr.refresh_instance = AsyncMock()
        self.frame = FakeFrame([LOCATED])
        self.mgr._get_target_frame = AsyncMock(return_value=self.frame)
        self.page = FakePage(IFRAME_RECT)
        self._patcher = patch("qa_mcp.tools.vtable.browser_mgr.get_page", return_value=self.page)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        # 点击后观察机制 (公共函数) 在单测中打桩, 避免触发真实浏览器探查
        self.snap_mock, self.obs_mock, self._obs_patchers = _patch_observe(self.page, self.mgr)
        for p in self._obs_patchers:
            self.addCleanup(p.stop)

    async def test_coordinate_math_combines_iframe_and_canvas_offsets_in_one_call(self):
        """两次偏移 (iframe 相对顶层 + canvas 相对 iframe) 应合成出页面级坐标"""
        self.mgr._get_checked_keys = AsyncMock(side_effect=[[], ["16"]])
        self.mgr._is_checked = AsyncMock(return_value=True)

        await self.mgr.select_rows([16], action="check")

        # 断言仅一次真实点击, 且坐标 = iframe 偏移 + canvas 偏移 + 单元格中心
        self.page.mouse.click.assert_awaited_once()
        cx, cy = self.page.mouse.click.await_args.args
        self.assertAlmostEqual(cx, IFRAME_RECT["left"] + LOCATED["canvas_rect"]["left"] + 20.0, places=2)
        self.assertAlmostEqual(cy, IFRAME_RECT["top"] + LOCATED["canvas_rect"]["top"] + 40.0, places=2)

    async def test_check_is_idempotent_when_row_already_checked(self):
        """check 已勾选的行不应产生任何点击"""
        self.mgr._get_checked_keys = AsyncMock(side_effect=[["16"], ["16"]])

        await self.mgr.select_rows([16], action="check")

        self.page.mouse.click.assert_not_awaited()

    async def test_uncheck_clicked_rows_only(self):
        """uncheck 只点击当前已勾选的行"""
        self.mgr._get_checked_keys = AsyncMock(side_effect=[["16"], []])
        self.mgr._is_checked = AsyncMock(return_value=False)

        result = await self.mgr.select_rows([16], action="uncheck")

        self.page.mouse.click.assert_awaited_once()
        self.assertEqual(result["removed"], ["16"])
        self.assertEqual(result["checked_after"], [])

    async def test_get_all_records_falls_back_to_vtable_records(self):
        """列配置无 vtable_aggregator.records 时应回退读取 vtable.records"""
        mgr = VTableManager()
        mgr.refresh_instance = AsyncMock()
        frame = FakeFrame([{"status": "success", "records": [{"id": 1}, {"id": 2}]}])
        mgr._get_target_frame = AsyncMock(return_value=frame)

        records = await mgr.get_all_records()

        self.assertEqual(len(records), 2)


CANVAS_LOCATED = {
    "canvas_rect": {"left": 303.98, "top": 130.59},
    "scroll_left": 30.0,
    "scroll_top": 10.0,
}


def _make_click_at_mgr(located=CANVAS_LOCATED):
    mgr = VTableManager()
    mgr.refresh_instance = AsyncMock()
    frame = FakeFrame([located])
    mgr._get_target_frame = AsyncMock(return_value=frame)
    page = FakePage(IFRAME_RECT)
    patcher = patch("qa_mcp.tools.vtable.browser_mgr.get_page", return_value=page)
    patcher.start()
    # 点击后观察机制 (公共函数) 在单测中打桩, 避免触发真实浏览器探查
    snap_mock, obs_mock, obs_patchers = _patch_observe(page, mgr)
    return mgr, page, [patcher] + obs_patchers, snap_mock, obs_mock


class VTableClickAtTests(unittest.IsolatedAsyncioTestCase):
    async def test_viewport_space_composes_iframe_and_canvas_offsets(self):
        """viewport 坐标 = iframe 偏移 + canvas 偏移 + 输入坐标"""
        mgr, page, patchers, _, _ = _make_click_at_mgr()
        for p in patchers:
            self.addCleanup(p.stop)

        result = await mgr.click_at(10, 20, coordinate_space="viewport")

        page.mouse.click.assert_awaited_once()
        cx, cy = page.mouse.click.await_args.args
        self.assertAlmostEqual(cx, IFRAME_RECT["left"] + CANVAS_LOCATED["canvas_rect"]["left"] + 10, places=2)
        self.assertAlmostEqual(cy, IFRAME_RECT["top"] + CANVAS_LOCATED["canvas_rect"]["top"] + 20, places=2)
        self.assertEqual(result["page_coords"], {"x": round(cx, 2), "y": round(cy, 2)})

    async def test_content_space_subtracts_scroll_offsets(self):
        """content 坐标应自动扣除 scrollLeft/scrollTop"""
        mgr, page, patchers, _, _ = _make_click_at_mgr()
        for p in patchers:
            self.addCleanup(p.stop)

        await mgr.click_at(100, 50, coordinate_space="content")

        cx, cy = page.mouse.click.await_args.args
        self.assertAlmostEqual(cx, IFRAME_RECT["left"] + CANVAS_LOCATED["canvas_rect"]["left"] + (100 - 30), places=2)
        self.assertAlmostEqual(cy, IFRAME_RECT["top"] + CANVAS_LOCATED["canvas_rect"]["top"] + (50 - 10), places=2)

    async def test_double_click_uses_dblclick(self):
        mgr, page, patchers, _, _ = _make_click_at_mgr()
        for p in patchers:
            self.addCleanup(p.stop)

        await mgr.click_at(5, 5, click_type="double")

        page.mouse.dblclick.assert_awaited_once()
        page.mouse.click.assert_not_awaited()

    async def test_invalid_coordinate_space_raises_without_clicking(self):
        mgr, page, patchers, _, _ = _make_click_at_mgr()
        for p in patchers:
            self.addCleanup(p.stop)

        with self.assertRaises(Exception):
            await mgr.click_at(5, 5, coordinate_space="nope")

        page.mouse.click.assert_not_awaited()
        page.mouse.dblclick.assert_not_awaited()

    async def test_top_space_click_skips_vtable_mount(self):
        """coordinate_space=top: 坐标即顶层视口坐标, 直接点击, 不挂载 VTable 实例
        (点击普通 DOM 浮层选项/按钮等非 VTable 区域时不应强制要求页面存在 VTable)"""
        mgr, page, patchers, _, _ = _make_click_at_mgr()
        for p in patchers:
            self.addCleanup(p.stop)

        result = await mgr.click_at(877, 386, coordinate_space="top")

        mgr.refresh_instance.assert_not_awaited()
        mgr._get_target_frame.assert_not_awaited()
        page.mouse.click.assert_awaited_once()
        cx, cy = page.mouse.click.await_args.args
        self.assertEqual((cx, cy), (877.0, 386.0))
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["vtable_mounted"])
        self.assertIn("observation", result)

    async def test_viewport_space_click_mounts_vtable(self):
        """viewport/content 空间仍须挂载 VTable 实例 (坐标相对 canvas, 语义需要)"""
        mgr, page, patchers, _, _ = _make_click_at_mgr()
        for p in patchers:
            self.addCleanup(p.stop)

        result = await mgr.click_at(10, 20, coordinate_space="viewport")

        mgr.refresh_instance.assert_awaited_once()
        self.assertTrue(result["vtable_mounted"])


SCENEGRAPH_COLUMNS = {
    "ok": True,
    "columns": [
        {
            "col": 0, "field": "goodsName", "title": "商品名称", "isFrozen": True,
            "headerIcons": [
                {"name": "sort", "func": "排序", "viewportX": 500.0, "viewportY": 150.0},
            ],
            "cellIcons": [
                {"name": "checkbox", "func": "复选框", "viewportX": 510.0, "viewportY": 180.0,
                 "rowIndex": 0, "bodyRow": 1},
            ],
        },
        {
            "col": 1, "field": "status", "title": "状态", "isFrozen": False,
            "headerIcons": [],
            "cellIcons": [],
        },
    ],
}


class VTableAnalyzeHeadersTests(unittest.IsolatedAsyncioTestCase):
    """analyze_headers 应为场景图驱动: 返回表头图标 + 单元格内交互图标组件"""

    async def test_scenegraph_driven_analysis_returns_header_and_cell_icons(self):
        mgr = VTableManager()
        mgr.refresh_instance = AsyncMock()
        frame = FakeFrame([SCENEGRAPH_COLUMNS])
        mgr._get_target_frame = AsyncMock(return_value=frame)

        headers = await mgr.analyze_headers()

        self.assertEqual(len(headers), 2)
        col0 = headers[0]
        self.assertEqual(col0["col"], 0)
        self.assertEqual(col0["field"], "goodsName")
        self.assertEqual(col0["title"], "商品名称")
        self.assertTrue(col0["isFrozen"])
        # 表头图标 (场景图渲染层)
        self.assertEqual(col0["header_icons"][0]["func"], "排序")
        self.assertEqual(col0["header_icons"][0]["viewportX"], 500.0)
        # 单元格内交互图标 (旧实现仅读 columns 配置永远拿不到)
        self.assertEqual(col0["cell_icons"][0]["func"], "复选框")
        self.assertEqual(col0["cell_icons"][0]["bodyRow"], 1)
        # capabilities 从场景图图标推导
        self.assertTrue(col0["capabilities"]["sortable"])
        self.assertFalse(col0["capabilities"]["filterable"])
        self.assertTrue(col0["capabilities"]["interactiveCell"])
        self.assertEqual(col0["capabilities"]["cellIconCount"], 1)
        # 无图标列: 全部能力为 False
        col1 = headers[1]
        self.assertFalse(col1["capabilities"]["sortable"])
        self.assertFalse(col1["capabilities"]["filterable"])
        self.assertFalse(col1["capabilities"]["interactiveCell"])
        self.assertEqual(col1["capabilities"]["cellIconCount"], 0)


class VTableClickInteractTests(unittest.IsolatedAsyncioTestCase):
    """通用点击工具 click_interact: by=css / by=xpath / by=coordinate + 点击后观察"""

    def _make_css_env(self):
        """构造 css/xpath 分支环境: mock page + locator 链 + 公共观察函数 + visuals"""
        locator = AsyncMock()
        locator.bounding_box = AsyncMock(
            return_value={"x": 100.0, "y": 200.0, "width": 50.0, "height": 30.0}
        )
        page = MagicMock()
        page.url = "https://example.com/page"
        page.locator.return_value = locator
        page.frame_locator.return_value = MagicMock()

        visuals = MagicMock()
        visuals.show = AsyncMock(return_value={"enabled": True, "mode": "cursor_highlight"})
        visuals.finish = AsyncMock()
        visuals.disable = AsyncMock()

        snap_mock = AsyncMock(return_value={"url": page.url, "frames": [], "popup_fingerprint": {}})
        obs_mock = AsyncMock(return_value=_observe_result(page))
        patchers = [
            patch("qa_mcp.tools.browser.browser_mgr.get_page", return_value=page),
            patch("qa_mcp.tools.browser.snapshot_navigation", snap_mock),
            patch("qa_mcp.tools.browser.observe_after_click", obs_mock),
            patch("qa_mcp.tools.browser.visuals", visuals),
        ]
        for p in patchers:
            p.start()
        return page, locator, snap_mock, obs_mock, patchers, visuals

    async def test_css_by_clicks_dom_element_and_observes(self):
        page, locator, snap_mock, obs_mock, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)

        result = await click_interact_impl(by="css", selector="#goodsName", description="点击商品名称")

        page.locator.assert_called_once_with("#goodsName")
        locator.wait_for.assert_awaited()
        locator.click.assert_awaited_once()
        self.assertEqual(result["by"], "css")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["selector"], "#goodsName")
        self.assertIn("observation", result)
        snap_mock.assert_awaited_once()
        obs_mock.assert_awaited_once()

    async def test_xpath_by_prefixes_selector(self):
        page, locator, _, _, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)

        await click_interact_impl(by="xpath", selector="//button[@id='add']")

        page.locator.assert_called_once_with("xpath=//button[@id='add']")
        locator.click.assert_awaited_once()

    async def test_css_double_click_uses_dblclick(self):
        page, locator, _, _, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)

        await click_interact_impl(by="css", selector="#goodsName", click_type="double")

        locator.dblclick.assert_awaited_once()
        locator.click.assert_not_awaited()

    async def test_visualize_true_shows_highlight_before_click_and_finishes_after(self):
        """visualize=True: 动作前 show(光标+高亮+标签), 点击成功后 finish(True)"""
        page, locator, _, _, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)

        result = await click_interact_impl(
            by="css", selector="#goodsName", description="点击商品名称", visualize=True
        )

        visuals.show.assert_awaited_once()
        kwargs = visuals.show.await_args.kwargs
        self.assertEqual(kwargs["action"], "click")
        self.assertEqual(kwargs["label"], "点击商品名称")
        self.assertEqual(kwargs["rect"], (100.0, 200.0, 50.0, 30.0))  # bounding_box 矩形
        self.assertEqual(kwargs["point"], (125.0, 215.0))            # 元素中心
        visuals.finish.assert_awaited_once()
        self.assertEqual(visuals.finish.await_args.args[1], True)     # 成功 → 绿色
        visuals.disable.assert_not_awaited()
        self.assertEqual(result["visual_effects"]["enabled"], True)
        self.assertEqual(result["status"], "success")

    async def test_visualize_false_disables_and_does_not_show(self):
        """visualize=False: 只调 disable() 清理, 不做 show, 结果无视觉层"""
        page, locator, _, _, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)

        result = await click_interact_impl(by="css", selector="#goodsName", visualize=False)

        visuals.disable.assert_awaited_once()
        visuals.show.assert_not_awaited()
        visuals.finish.assert_not_awaited()
        self.assertEqual(result["visual_effects"], {"enabled": False})
        self.assertEqual(result["status"], "success")

    async def test_visualize_none_follows_service_default_config(self):
        """visualize=None 跟随服务配置 VISUAL_EFFECTS"""
        page, locator, _, _, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)

        # 默认配置 false → disable
        with patch("qa_mcp.tools.browser.VISUAL_EFFECTS", False):
            result = await click_interact_impl(by="css", selector="#goodsName")
        visuals.disable.assert_awaited_once()
        visuals.show.assert_not_awaited()
        self.assertEqual(result["visual_effects"], {"enabled": False})

        # 服务配置 true → show
        with patch("qa_mcp.tools.browser.VISUAL_EFFECTS", True):
            result = await click_interact_impl(by="css", selector="#goodsName")
        visuals.show.assert_awaited_once()
        self.assertEqual(result["visual_effects"]["enabled"], True)

    async def test_visual_error_never_affects_click_result(self):
        """视觉注入/渲染异常: 只记录到 visual_effects, 点击照常成功"""
        page, locator, _, _, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)
        visuals.show = AsyncMock(side_effect=RuntimeError("inject failed"))

        result = await click_interact_impl(by="css", selector="#goodsName", visualize=True)

        locator.click.assert_awaited_once()
        self.assertEqual(result["status"], "success")
        self.assertIn("error", result["visual_effects"])
        self.assertIn("visuals.show", result["visual_effects"]["error"])

    async def test_visual_finish_error_keeps_success(self):
        """finish 染色异常: 点击结果不受影响, 错误记录到 visual_effects"""
        page, locator, _, _, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)
        visuals.finish = AsyncMock(side_effect=RuntimeError("finish failed"))

        result = await click_interact_impl(by="css", selector="#goodsName", visualize=True)

        self.assertEqual(result["status"], "success")
        self.assertIn("finish_error", result["visual_effects"])

    async def test_visualize_skipped_when_bounding_box_none(self):
        """bounding_box=None (元素不可定位): 跳过视觉, 动作照常"""
        page, locator, _, _, patchers, visuals = self._make_css_env()
        for p in patchers:
            self.addCleanup(p.stop)
        locator.bounding_box = AsyncMock(return_value=None)

        result = await click_interact_impl(by="css", selector="#goodsName", visualize=True)

        visuals.show.assert_not_awaited()
        locator.click.assert_awaited_once()
        self.assertEqual(result["status"], "success")

    async def test_coordinate_by_delegates_to_vtable_click_at(self):
        # 坐标分支先取页面 (browser_mgr.get_page), 再委托 vtable_mgr.click_at
        page = MagicMock()
        page.url = "https://example.com/page"
        visuals = MagicMock()
        visuals.disable = AsyncMock()
        visuals.show = AsyncMock(return_value={"enabled": True, "mode": "cursor_highlight"})
        visuals.finish = AsyncMock()
        with patch("qa_mcp.tools.browser.browser_mgr.get_page", return_value=page), \
             patch("qa_mcp.tools.browser.visuals", visuals), \
             patch("qa_mcp.tools.vtable.vtable_mgr.click_at", AsyncMock(return_value={
                "status": "success", "coordinate_space": "top", "click_type": "single",
                "input_coord": {"x": 500.0, "y": 150.0}, "page_coords": {"x": 500.0, "y": 150.0},
             })) as m:
            result = await click_interact_impl(
                by="coordinate", x=500, y=150, description="点击列头筛选图标"
            )

        m.assert_awaited_once()
        self.assertEqual(result["by"], "coordinate")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["description"], "点击列头筛选图标")

    async def test_coordinate_visualize_true_uses_point_centered_rect(self):
        """坐标分支 visualize=True: 以点击点为中心合成 40x40 目标框, 成功后 finish(True)"""
        page = MagicMock()
        page.url = "https://example.com/page"
        visuals = MagicMock()
        visuals.disable = AsyncMock()
        visuals.show = AsyncMock(return_value={"enabled": True, "mode": "cursor_highlight"})
        visuals.finish = AsyncMock()
        with patch("qa_mcp.tools.browser.browser_mgr.get_page", return_value=page), \
             patch("qa_mcp.tools.browser.visuals", visuals), \
             patch("qa_mcp.tools.vtable.vtable_mgr.click_at", AsyncMock(return_value={
                "status": "success", "coordinate_space": "top", "click_type": "single",
                "input_coord": {"x": 500.0, "y": 150.0}, "page_coords": {"x": 500.0, "y": 150.0},
             })):
            result = await click_interact_impl(
                by="coordinate", x=500, y=150, description="点击列头筛选图标", visualize=True
            )

        visuals.show.assert_awaited_once()
        kwargs = visuals.show.await_args.kwargs
        self.assertEqual(kwargs["action"], "click")
        self.assertEqual(kwargs["rect"], (480.0, 130.0, 40.0, 40.0))
        self.assertEqual(kwargs["point"], (500.0, 150.0))
        visuals.finish.assert_awaited_once()
        self.assertEqual(visuals.finish.await_args.args[1], True)
        self.assertEqual(result["visual_effects"]["enabled"], True)

    async def test_coordinate_top_click_works_without_vtable(self):
        """by=coordinate + coordinate_space=top: 页面无 VTable 时直接坐标点击成功
        (真实场景: 点击浮层选项等非 VTable 区域, 不应报"挂载 VTable 实例失败")"""
        page = FakePage(IFRAME_RECT)
        visuals = MagicMock()
        visuals.disable = AsyncMock()
        visuals.show = AsyncMock(return_value={"enabled": True, "mode": "cursor_highlight"})
        visuals.finish = AsyncMock()
        snap_mock = AsyncMock(return_value={"url": page.url, "frames": [], "popup_fingerprint": {}})
        obs_mock = AsyncMock(return_value=_observe_result(page))
        with patch("qa_mcp.tools.browser.browser_mgr.get_page", return_value=page), \
             patch("qa_mcp.tools.browser.visuals", visuals), \
             patch("qa_mcp.tools.browser.snapshot_navigation", snap_mock), \
             patch("qa_mcp.tools.browser.observe_after_click", obs_mock), \
             patch("qa_mcp.tools.vtable.snapshot_navigation", snap_mock), \
             patch("qa_mcp.tools.vtable.observe_after_click", obs_mock):
            result = await click_interact_impl(
                by="coordinate", x=877, y=386, coordinate_space="top",
                description="点击中度选项", expected_result="改机等级回填",
            )

        page.mouse.click.assert_awaited_once()
        cx, cy = page.mouse.click.await_args.args
        self.assertEqual((cx, cy), (877.0, 386.0))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["by"], "coordinate")
        self.assertFalse(result["vtable_mounted"])
        self.assertEqual(result["description"], "点击中度选项")
        self.assertEqual(result["expected_result"], "改机等级回填")
        self.assertIn("observation", result)

    async def test_invalid_by_raises(self):
        with self.assertRaises(RuntimeError):
            await click_interact_impl(by="id")

    async def test_coordinate_without_xy_raises(self):
        with self.assertRaises(RuntimeError):
            await click_interact_impl(by="coordinate")

    async def test_css_without_selector_raises(self):
        with self.assertRaises(RuntimeError):
            await click_interact_impl(by="css")


class VTableClickObservationTests(unittest.IsolatedAsyncioTestCase):
    """点击后观察: 浮层/消息弹窗 + tab 页跳转 (URL) + iframe 跳转"""

    async def test_click_at_returns_observation_with_navigation(self):
        mgr, page, patchers, snap_mock, obs_mock = _make_click_at_mgr()
        for p in patchers:
            self.addCleanup(p.stop)

        result = await mgr.click_at(10, 20, coordinate_space="viewport")

        self.assertIn("observation", result)
        obs = result["observation"]
        self.assertIn("dynamic_layers", obs)
        self.assertIn("new_layers", obs)
        self.assertIn("navigation", obs)
        self.assertIn("url_changed", obs["navigation"])
        self.assertIn("frames_changed", obs["navigation"])
        snap_mock.assert_awaited_once()
        obs_mock.assert_awaited_once()

    async def test_select_rows_returns_observation_after_clicks(self):
        mgr = VTableManager()
        mgr.refresh_instance = AsyncMock()
        frame = FakeFrame([LOCATED])
        mgr._get_target_frame = AsyncMock(return_value=frame)
        page = FakePage(IFRAME_RECT)
        patcher = patch("qa_mcp.tools.vtable.browser_mgr.get_page", return_value=page)
        patcher.start()
        self.addCleanup(patcher.stop)
        snap_mock, obs_mock, obs_patchers = _patch_observe(page, mgr)
        for p in obs_patchers:
            self.addCleanup(p.stop)
        mgr._get_checked_keys = AsyncMock(side_effect=[[], ["16"]])
        mgr._is_checked = AsyncMock(return_value=True)

        result = await mgr.select_rows([16], action="check")

        self.assertIn("observation", result)
        self.assertIn("navigation", result["observation"])
        obs_mock.assert_awaited_once()


DRAG_GEOM = {
    "ok": True,
    "headerRow": 0,
    "headerLevel": 1,
    "colCount": 6,
    "rowCount": 10,
    "canvasViewportWidth": 800,
    "scrollLeft": 0,
    "dragHeaderMode": "all",
    "frozenColDragHeaderMode": None,
    "headerSelectMode": "single",
    "sourceHeader": {"x1": 400.0, "x2": 500.0, "y1": 100.0, "y2": 140.0, "visible": True, "source": "scenegraph"},
    "dropHeader": {"x1": 500.0, "x2": 600.0, "y1": 100.0, "y2": 140.0, "visible": True, "source": "scenegraph"},
    "fields": ["A", "B", "C", "D", "E", "F"],
    "titles": ["甲", "乙", "丙", "丁", "戊", "己"],
    "sourceIsFrozen": False,
    "dropIsFrozen": False,
    "sourceCanDragByDefine": False,
    # 源列 body 最后一行几何 (框选兜底使用; rowCount=10 → 全局最后一行 9)
    "lastBodyRowGlobal": 9,
    "sourceLastBodyCenter": {"x": 450.0, "y": 1000.0},
    "sourceLastBodyVisible": True,
}

AFTER_GEOM_AFTER = {
    "fields": ["A", "C", "D", "E", "B", "F"],
    "titles": ["甲", "丙", "丁", "戊", "乙", "己"],
}

AFTER_GEOM_BEFORE = {
    "fields": ["A", "E", "B", "C", "D", "F"],
    "titles": ["甲", "戊", "乙", "丙", "丁", "己"],
}

RESOLVE_AFTER = {
    "ok": True,
    "sourceCol": 1,
    "targetCol": 4,
    "fieldOf": "B",
    "titleOf": "乙",
    "targetField": "E",
    "targetTitle": "戊",
}

RESOLVE_BEFORE = {
    "ok": True,
    "sourceCol": 4,
    "targetCol": 1,
    "fieldOf": "E",
    "titleOf": "戊",
    "targetField": "B",
    "targetTitle": "乙",
}

RESOLVE_NOOP_AFTER = {
    "ok": True,
    "sourceCol": 4,
    "targetCol": 3,
    "fieldOf": "E",
    "titleOf": "戊",
    "targetField": "D",
    "targetTitle": "丁",
}

DRAG_GEOM_BEFORE = dict(
    DRAG_GEOM,
    dropHeader={"x1": 100.0, "x2": 200.0, "y1": 100.0, "y2": 140.0, "visible": True, "source": "scenegraph"},
)


NO_ICONS = {"ok": True, "count": 0, "icons": []}


RESIZE_GEOM = {
    "ok": True,
    "col": 1,
    "field": "B",
    "title": "乙",
    "headerRow": 0,
    "colCount": 6,
    "canvasViewportWidth": 800,
    "header": {"x1": 400.0, "x2": 500.0, "y1": 100.0, "y2": 140.0, "width": 100.0, "visible": True, "source": "scenegraph"},
    "resize": {
        "columnResize": {"resizable": True},
        "resize": {},
        "resizeEnabled": True,
        "columnResizeMode": None,
        "canResize": None,
        "minColumnWidth": None,
        "maxColumnWidth": None,
    },
}

AFTER_WIDTH_OK = {"ok": True, "col": 1, "field": "B", "title": "乙", "width": 160.0, "header": RESIZE_GEOM["header"]}
AFTER_WIDTH_OFF = {"ok": True, "col": 1, "field": "B", "title": "乙", "width": 165.0, "header": RESIZE_GEOM["header"]}


class VTableDragColumnTests(unittest.IsolatedAsyncioTestCase):
    """vtable_drag_column: 真实鼠标拖拽列头换位 (先点击整列选中 → 按下 → 分步拖拽 → 松开)"""

    def _make_mgr(self, frame_results):
        mgr = VTableManager()
        mgr.refresh_instance = AsyncMock()
        frame = FakeFrame(frame_results)
        mgr._get_target_frame = AsyncMock(return_value=frame)
        page = FakePage(IFRAME_RECT)
        patcher = patch("qa_mcp.tools.vtable.browser_mgr.get_page", return_value=page)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mgr, page

    async def test_drag_after_drops_on_target_right_of_source(self):
        """position=after 且目标列在源列右侧: 落点列=目标列, 完整复刻 点击选中→按下→移动→松开"""
        mgr, page = self._make_mgr([
            RESOLVE_AFTER,       # 1. 解析源/目标列
            DRAG_GEOM,           # 2. 几何信息
            True,                # 3. 列级 dragHeader 校验
            NO_ICONS,            # 4. 源列表头图标 (无交互图标 → 点击点=列头中心)
            {"selected": True},  # 5. 点击列头后的整列选中检查
            True,                # 6. 拖拽启动条件
            AFTER_GEOM_AFTER,    # 7. 拖拽后列顺序
        ])

        result = await mgr.drag_column("B", "E", position="after")

        # 先真实点击源列头中部 (选中整列)
        page.mouse.click.assert_awaited_once()
        cx, cy = page.mouse.click.await_args.args
        self.assertAlmostEqual(cx, 450.0, places=2)  # 源列中心 (400..500)
        self.assertAlmostEqual(cy, 120.0, places=2)  # 表头中心 (100..140)
        # 按下 + 松开
        page.mouse.down.assert_awaited_once()
        page.mouse.up.assert_awaited_once()
        # 1 次初始定位 + 1 次带 steps 的平滑移动 (Playwright 内部派发 14 个 mousemove)
        self.assertEqual(page.mouse.move.await_count, 2)
        last_call = page.mouse.move.await_args
        self.assertEqual(last_call.kwargs.get("steps"), 14)
        last_x, last_y = last_call.args
        self.assertAlmostEqual(last_x, 550.0, places=2)
        self.assertAlmostEqual(last_y, 120.0, places=2)
        # 验证结果: 源列 B 新位置在 E 的后一位
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["verification"]["source_index_after"], 4)
        self.assertEqual(result["verification"]["target_index_after"], 3)
        self.assertTrue(result["verification"]["ok"])

    async def test_drag_before_drops_on_target_left_of_source(self):
        """position=before 且目标列在源列左侧: 落点列=目标列, 源列插到目标列前方"""
        mgr, page = self._make_mgr([
            RESOLVE_BEFORE,          # source=E(4), target=B(1)
            DRAG_GEOM_BEFORE,        # 落点列=col1 (100..200)
            True,
            NO_ICONS,
            {"selected": True},
            True,
            AFTER_GEOM_BEFORE,       # E 移到 B 前方
        ])

        result = await mgr.drag_column("E", "B", position="before")

        page.mouse.click.assert_awaited_once()
        page.mouse.down.assert_awaited_once()
        page.mouse.up.assert_awaited_once()
        last_x, last_y = page.mouse.move.await_args.args
        # 落点列 = 目标列 (col 1, 中心 150, 表头 y 中心 120)
        self.assertAlmostEqual(last_x, 150.0, places=2)
        self.assertAlmostEqual(last_y, 120.0, places=2)
        self.assertEqual(result["verification"]["source_index_after"], 1)
        self.assertEqual(result["verification"]["target_index_after"], 2)
        self.assertTrue(result["verification"]["ok"])

    async def test_noop_when_source_already_at_position(self):
        """源列已在目标位置 (E 紧邻 D 后方): 不产生任何鼠标事件, 返回 noop"""
        mgr, page = self._make_mgr([RESOLVE_NOOP_AFTER])  # 只需解析即返回

        result = await mgr.drag_column("E", "D", position="after")

        # source=4, target=3, after -> drop_col = target+1 = 4 == source -> noop
        self.assertEqual(result["status"], "noop")
        page.mouse.click.assert_not_awaited()
        page.mouse.down.assert_not_awaited()
        page.mouse.up.assert_not_awaited()

    async def test_drag_raises_when_dragHeaderMode_disabled(self):
        """VTable 未开启 dragHeaderMode 时给出明确报错"""
        geom = dict(DRAG_GEOM, dragHeaderMode="none")
        mgr, page = self._make_mgr([RESOLVE_AFTER, geom])

        with self.assertRaisesRegex(Exception, "dragHeaderMode"):
            await mgr.drag_column("B", "E", position="after")

        page.mouse.down.assert_not_awaited()

    async def test_click_point_avoids_header_icons(self):
        """源列表头中心压着交互图标 (冻结/排序等) 时, 点击点应避开图标区域"""
        icons = {
            "ok": True,
            "count": 1,
            "icons": [{"name": "freeze", "viewportX": 450, "viewportY": 120, "width": 22, "height": 22}],
        }
        mgr, page = self._make_mgr([
            RESOLVE_AFTER,
            DRAG_GEOM,
            True,
            icons,
            {"selected": True},
            True,
            AFTER_GEOM_AFTER,
        ])

        result = await mgr.drag_column("B", "E", position="after")

        # 中心 (450,120) 被图标挡住 → 取 25% 分位 (400 + 100*0.25 = 425)
        cx, cy = page.mouse.click.await_args.args
        self.assertAlmostEqual(cx, 425.0, places=2)
        self.assertAlmostEqual(cy, 120.0, places=2)
        # 按下点与点击点一致 (同样避开图标)
        first_x, first_y = page.mouse.move.await_args_list[0].args
        self.assertAlmostEqual(first_x, 425.0, places=2)
        self.assertAlmostEqual(first_y, 120.0, places=2)
        self.assertEqual(result["verification"]["ok"], True)

    async def test_drag_falls_back_to_box_select_when_header_select_disabled(self):
        """表头未启用整列选中 (headerSelectMode='cell'): 点击两轮失败 → 真实鼠标纵向框选整列兜底 → 拖拽成功"""
        geom = dict(DRAG_GEOM, headerSelectMode="cell")
        mgr, page = self._make_mgr([
            RESOLVE_AFTER,       # 1. 解析源/目标列
            geom,                # 2. 几何信息 (headerSelectMode='cell')
            True,                # 3. 列级 dragHeader 校验
            NO_ICONS,            # 4. 源列表头图标 (无图标 → 点击点=列头中心)
            # 5-52. 点击列头选中已改为轮询: 两次点击各 24 轮轮询均未选中 (cell 模式只选表头单元格)
            *([{"selected": False}] * 48),
            {"selected": True},   # 53. 框选(按下→拖到最后一行→松开)后整列选中
            True,                # 54. 拖拽启动条件
            AFTER_GEOM_AFTER,    # 55. 拖拽后列顺序
        ])

        result = await mgr.drag_column("B", "E", position="after")

        # 两轮点击 (各 1 次, 无图标 → 单点击点)
        self.assertEqual(page.mouse.click.await_count, 2)
        # 框选 down/up 1 次 + 拖拽 down/up 1 次
        self.assertEqual(page.mouse.down.await_count, 2)
        self.assertEqual(page.mouse.up.await_count, 2)
        # 框选: 初始定位 1 + 1 次带 steps 的平滑移动 (Playwright 内部派发 18 个 mousemove), 终点 = (450,1000)
        # 拖拽: 初始定位 1 + 1 次带 steps 的平滑移动
        self.assertEqual(page.mouse.move.await_count, 4)
        box_last_call = page.mouse.move.await_args_list[1]
        self.assertEqual(box_last_call.kwargs.get("steps"), 18)
        box_last_x, box_last_y = box_last_call.args
        self.assertAlmostEqual(box_last_x, 450.0, places=2)
        self.assertAlmostEqual(box_last_y, 1000.0, places=2)
        # 拖拽落点不变 (最后一次调用为带 steps 的平滑移动)
        self.assertEqual(page.mouse.move.await_args.kwargs.get("steps"), 14)
        last_x, last_y = page.mouse.move.await_args.args
        self.assertAlmostEqual(last_x, 550.0, places=2)
        self.assertAlmostEqual(last_y, 120.0, places=2)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["verification"]["source_index_after"], 4)
        self.assertTrue(result["verification"]["ok"])

    async def test_box_select_scrolls_to_last_row_when_off_viewport(self):
        """源列 body 最后一行不在视口 (虚拟滚动未渲染): 框选前先 scrollToRow 滚动再重采几何"""
        geom = dict(
            DRAG_GEOM,
            headerSelectMode="cell",
            sourceLastBodyCenter=None,
            sourceLastBodyVisible=False,
        )
        geom2 = dict(
            DRAG_GEOM,
            headerSelectMode="cell",
            sourceLastBodyCenter={"x": 450.0, "y": 2000.0},
            sourceLastBodyVisible=True,
        )
        mgr, page = self._make_mgr([
            RESOLVE_AFTER,
            geom,
            True,
            NO_ICONS,
            *([{"selected": False}] * 48),  # 两次点击各 24 轮轮询未选中
            True,                # scrollToRow (滚动到最后一行, 仅视图滚动)
            geom2,               # 滚动后重采几何 (最后一行已渲染)
            {"selected": True},
            True,
            AFTER_GEOM_AFTER,
        ])

        result = await mgr.drag_column("B", "E", position="after")

        self.assertEqual(page.mouse.down.await_count, 2)
        # 框选: 初始定位 1 + 1 次带 steps 的平滑移动 (Playwright 内部派发 32 个 mousemove), 终点 = 重采后的 (450,2000)
        # 拖拽: 初始定位 1 + 1 次带 steps 的平滑移动
        self.assertEqual(page.mouse.move.await_count, 4)
        box_last_call = page.mouse.move.await_args_list[1]
        self.assertEqual(box_last_call.kwargs.get("steps"), 32)
        box_last_x, box_last_y = box_last_call.args
        self.assertAlmostEqual(box_last_x, 450.0, places=2)
        self.assertAlmostEqual(box_last_y, 2000.0, places=2)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["verification"]["ok"])

    async def test_drag_with_nan_last_center_does_not_crash_and_falls_back_to_scroll_box_select(self):
        """回归: 源列 body 最后一行几何为 NaN (虚拟滚动哨兵值) 时, 不再抛 int(NaN) 崩溃,
        而是净化后走 scrollToRow + 重采几何 + 框选整列 → 拖拽成功"""
        geom = dict(
            DRAG_GEOM,
            headerSelectMode="cell",
            sourceLastBodyCenter={"x": float("nan"), "y": float("nan")},
            sourceLastBodyVisible=False,
        )
        geom2 = dict(
            DRAG_GEOM,
            headerSelectMode="cell",
            sourceLastBodyCenter={"x": 450.0, "y": 1000.0},
            sourceLastBodyVisible=True,
        )
        mgr, page = self._make_mgr([
            RESOLVE_AFTER,
            geom,                # NaN 最后一行几何 (未渲染哨兵值)
            True,
            NO_ICONS,
            *([{"selected": False}] * 48),  # 两次点击各 24 轮轮询未选中
            True,                # scrollToRow (滚动到源列最后一行)
            geom2,               # 滚动后重采几何 (坐标有效)
            {"selected": True},
            True,
            AFTER_GEOM_AFTER,
        ])

        result = await mgr.drag_column("B", "E", position="after")

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["verification"]["ok"])
        # 框选 down/up 1 次 + 拖拽 down/up 1 次 (证明确实走了框选兜底)
        self.assertEqual(page.mouse.down.await_count, 2)
        self.assertEqual(page.mouse.up.await_count, 2)

    async def test_drag_with_nan_last_center_after_scroll_returns_not_effective(self):
        """回归: 滚动重采后几何仍为 NaN → 不再抛错, 编程式兜底也失败时继续动作链, 返回明确诊断结果"""
        geom = dict(
            DRAG_GEOM,
            headerSelectMode="cell",
            sourceLastBodyCenter={"x": float("nan"), "y": float("nan")},
            sourceLastBodyVisible=False,
        )
        after_unchanged = dict(DRAG_GEOM)  # 拖拽后列顺序未变
        mgr, page = self._make_mgr([
            RESOLVE_AFTER,
            geom,
            True,
            NO_ICONS,
            {"selected": False},
            {"selected": False},
            True,                # scrollToRow
            geom,                # 重采后仍为 NaN → 框选放弃
            {"ok": False, "reason": "无 selectCells"},  # 编程式选中兜底 (实例无选中 API)
            {"selected": False}, # 编程式后仍未选中
            True,                # _canDragHeaderPosition 拖拽启动条件
            after_unchanged,     # 拖拽后列顺序未变 → 返回 not_effective
        ])

        result = await mgr.drag_column("B", "E", position="after")

        # 不再抛异常: 返回明确诊断结果, 而非中断/崩溃
        self.assertEqual(result["status"], "not_effective")
        self.assertFalse(result["verification"]["ok"])
        self.assertIn("headerSelectMode", result["reason"])

    async def test_drag_programmatic_select_when_header_select_none(self):
        """headerSelectMode=None 且框选禁用: 点击/框选均失败 → 编程式整列选中兜底 → 拖拽成功"""
        geom = dict(DRAG_GEOM, headerSelectMode=None)
        mgr, page = self._make_mgr([
            RESOLVE_AFTER,
            geom,
            True,
            NO_ICONS,
            {"selected": False},  # 第一次点击后未选中
            {"selected": False},  # 第二次点击后未选中
            {"selected": False},  # 框选后仍未选中 (框选被禁用)
            {"ok": True, "method": "selectCells"},  # 编程式选中兜底 JS
            {"selected": True},   # 编程式选中后整列选中
            True,                 # _canDragHeaderPosition 拖拽启动条件
            AFTER_GEOM_AFTER,     # 拖拽后列顺序
        ])

        result = await mgr.drag_column("B", "E", position="after")

        self.assertEqual(result["status"], "success")
        self.assertTrue(result["verification"]["ok"])
        self.assertEqual(result["selection"]["header_select_mode"], None)
        # 无真实框选 (编程式选中), 按压点回退到源列头中心 (450,120)
        first_x, first_y = page.mouse.move.await_args_list[0].args
        self.assertAlmostEqual(first_x, 450.0, places=2)
        self.assertAlmostEqual(first_y, 120.0, places=2)

    async def test_drag_with_nan_source_header_geometry_raises_clear_error(self):
        """回归: 源列表头矩形坐标为 NaN (未渲染) → 明确报错, 而不是 int(NaN) 崩溃"""
        geom = dict(
            DRAG_GEOM,
            sourceHeader={"x1": float("nan"), "x2": 500.0, "y1": 100.0, "y2": 140.0, "visible": True, "source": "scenegraph"},
        )
        mgr, page = self._make_mgr([RESOLVE_AFTER, geom, True])

        with self.assertRaisesRegex(Exception, "无法获取源列表头矩形"):
            await mgr.drag_column("B", "E", position="after")

        page.mouse.down.assert_not_awaited()

    async def test_drag_with_nan_drop_header_geometry_raises_clear_error(self):
        """回归: 落点列矩形坐标为 NaN → 明确报错, 而不是 int(NaN) 崩溃"""
        geom = dict(
            DRAG_GEOM,
            dropHeader={"x1": 500.0, "x2": float("nan"), "y1": 100.0, "y2": 140.0, "visible": True, "source": "scenegraph"},
        )
        mgr, page = self._make_mgr([RESOLVE_AFTER, geom, True])

        with self.assertRaisesRegex(Exception, "无法获取落点列"):
            await mgr.drag_column("B", "E", position="after")

        page.mouse.down.assert_not_awaited()


class VTableResizeColumnTests(unittest.IsolatedAsyncioTestCase):
    """vtable_resize_column: 真实鼠标拖拽列头分隔线调整列宽 (悬停 → 按下 → 分步缓动 → 松开 → 验证)"""

    def _make_mgr(self, frame_results):
        mgr = VTableManager()
        mgr.refresh_instance = AsyncMock()
        frame = FakeFrame(frame_results)
        mgr._get_target_frame = AsyncMock(return_value=frame)
        page = FakePage(IFRAME_RECT)
        patcher = patch("qa_mcp.tools.vtable.browser_mgr.get_page", return_value=page)
        patcher.start()
        self.addCleanup(patcher.stop)
        return mgr, page

    async def test_drag_divider_to_target_width_drives_mouse_and_verifies(self):
        """完整流程: 采集几何 → 悬停分隔线按下 → 18 步缓动拖到目标宽度 → 松开 → 重读列宽验证"""
        mgr, page = self._make_mgr([RESIZE_GEOM, AFTER_WIDTH_OK])

        result = await mgr.resize_column("乙", 160)

        # 1 次初始悬停定位 + 1 次带 steps 的平滑移动 (Playwright 内部派发 18 个 mousemove)
        self.assertEqual(page.mouse.move.await_count, 2)
        self.assertEqual(page.mouse.move.await_args.kwargs.get("steps"), 18)
        first_x, first_y = page.mouse.move.await_args_list[0].args
        self.assertAlmostEqual(first_x, 500.0, places=2)  # 分隔线 = 列头右边界 x2
        self.assertAlmostEqual(first_y, 120.0, places=2)  # 表头行中线
        last_x, last_y = page.mouse.move.await_args.args
        self.assertAlmostEqual(last_x, 560.0, places=2)
        self.assertAlmostEqual(last_y, 120.0, places=2)
        page.mouse.down.assert_awaited_once()
        page.mouse.up.assert_awaited_once()
        # 结果: 列宽 100 -> 160, 误差 0 ≤ 2px → success
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["width_before"], 100.0)
        self.assertEqual(result["width_after"], 160.0)
        self.assertEqual(result["delta"], 60.0)
        self.assertTrue(result["verified"])
        self.assertEqual(result["drag_points"]["start"], {"x": 500.0, "y": 120.0})
        self.assertEqual(result["drag_points"]["end"], {"x": 560.0, "y": 120.0})

    async def test_partial_when_after_width_outside_2px_tolerance(self):
        """拖拽后重读列宽与目标偏差 > 2px 时返回 partial 而非 success"""
        mgr, page = self._make_mgr([RESIZE_GEOM, AFTER_WIDTH_OFF])

        result = await mgr.resize_column("乙", 160)

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["verified"])
        self.assertEqual(result["width_after"], 165.0)

    async def test_raises_when_column_resize_disabled(self):
        """VTable 未开启列宽调整 (columnResize.resizable=false) 时明确报错且不产生鼠标事件"""
        geom = dict(RESIZE_GEOM)
        geom["resize"] = dict(RESIZE_GEOM["resize"], resizeEnabled=False)
        mgr, page = self._make_mgr([geom])

        with self.assertRaisesRegex(Exception, "columnResize.resizable=false"):
            await mgr.resize_column("乙", 160)

        page.mouse.down.assert_not_awaited()

    async def test_raises_clear_error_when_header_geometry_nan(self):
        """回归: 列头矩形坐标为 NaN (虚拟滚动哨兵值) → 明确报错, 而不是 int(NaN)/坐标 NaN 崩溃"""
        geom = dict(RESIZE_GEOM)
        geom["header"] = dict(RESIZE_GEOM["header"], x1=float("nan"), width=float("nan"))
        mgr, page = self._make_mgr([geom])

        with self.assertRaisesRegex(Exception, "无法获取列头矩形"):
            await mgr.resize_column("乙", 160)

        page.mouse.down.assert_not_awaited()
        page.mouse.move.assert_not_awaited()

    async def test_raises_when_width_below_min_column_width(self):
        """目标宽度小于配置的 minColumnWidth 时拒绝拖拽"""
        geom = dict(RESIZE_GEOM)
        geom["resize"] = dict(RESIZE_GEOM["resize"], minColumnWidth=120)
        mgr, page = self._make_mgr([geom])

        with self.assertRaisesRegex(Exception, "最小列宽"):
            await mgr.resize_column("乙", 100)

        page.mouse.down.assert_not_awaited()

    async def test_raises_when_width_above_max_column_width(self):
        """目标宽度大于配置的 maxColumnWidth 时拒绝拖拽"""
        geom = dict(RESIZE_GEOM)
        geom["resize"] = dict(RESIZE_GEOM["resize"], maxColumnWidth=150)
        mgr, page = self._make_mgr([geom])

        with self.assertRaisesRegex(Exception, "最大列宽"):
            await mgr.resize_column("乙", 200)

        page.mouse.down.assert_not_awaited()

    async def test_raises_when_header_not_visible(self):
        """列头在横向视口外时给出滚动提示, 不拖拽"""
        geom = dict(RESIZE_GEOM)
        geom["header"] = dict(RESIZE_GEOM["header"], visible=False)
        mgr, page = self._make_mgr([geom])

        with self.assertRaisesRegex(Exception, "横向视口外"):
            await mgr.resize_column("乙", 160)

        page.mouse.down.assert_not_awaited()

    async def test_raises_on_non_positive_width_without_browser_access(self):
        """width 非法 (0/负数) 时在最前置校验拒绝, 不触碰浏览器"""
        mgr = VTableManager()
        mgr.refresh_instance = AsyncMock()

        with self.assertRaisesRegex(Exception, "width 必须为正数"):
            await mgr.resize_column("乙", 0)

        mgr.refresh_instance.assert_not_awaited()

    async def test_raises_when_column_not_resolved(self):
        """JS 侧找不到列时透传明确报错"""
        mgr, page = self._make_mgr([{"error": "未找到列: X (colCount=6, 可尝试列索引或先 vtable_scan_columns 查看列标题)"}])

        with self.assertRaisesRegex(Exception, "未找到列"):
            await mgr.resize_column("不存在列", 160)

        page.mouse.down.assert_not_awaited()


class CaptureScreenshotTests(unittest.IsolatedAsyncioTestCase):
    """capture_screenshot: CDP 采集 + 落盘 evidence_assets + 内联 Image 返回"""

    def _make_env(self, tmp_dir):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-png-bytes").decode()
        session = MagicMock()
        session.send = AsyncMock(return_value={"data": png})
        context = MagicMock()
        context.new_cdp_session = AsyncMock(return_value=session)
        page = MagicMock()
        page.context = context
        page.evaluate = AsyncMock(return_value={"width": 800, "height": 600})
        patchers = [
            patch("qa_mcp.tools.browser.browser_mgr.get_page", return_value=page),
            patch("qa_mcp.tools.browser.EVIDENCE_DIR", str(tmp_dir)),
        ]
        for p in patchers:
            p.start()
        return page, session, patchers

    async def test_viewport_screenshot_saves_file_and_returns_image(self):
        """默认视口截图: CDP captureScreenshot + 落盘 + [文本摘要, Image]"""
        with tempfile.TemporaryDirectory() as tmp:
            page, session, patchers = self._make_env(tmp)
            for p in patchers:
                self.addCleanup(p.stop)

            result = await capture_screenshot_impl()

            # CDP 采集 (不卡字体), 默认视口不带 clip
            session.send.assert_awaited_once()
            args = session.send.await_args.args
            self.assertEqual(args[0], "Page.captureScreenshot")
            self.assertEqual(args[1]["format"], "png")
            self.assertNotIn("clip", args[1])
            # 落盘
            summary = json.loads(result[0])
            self.assertEqual(summary["status"], "success")
            self.assertTrue(Path(summary["file"]).exists())
            self.assertEqual(summary["dimensions"], {"width": 800, "height": 600})
            self.assertEqual(summary["full_page"], False)
            # 内联 Image
            from fastmcp.utilities.types import Image
            self.assertIsInstance(result[1], Image)
            content = result[1].to_image_content()
            self.assertEqual(content.type, "image")
            self.assertEqual(content.mimeType, "image/png")

    async def test_full_page_uses_capture_beyond_viewport_clip(self):
        """full_page=True: 计算整页尺寸并传 captureBeyondViewport + clip"""
        with tempfile.TemporaryDirectory() as tmp:
            page, session, patchers = self._make_env(tmp)
            for p in patchers:
                self.addCleanup(p.stop)

            await capture_screenshot_impl(full_page=True)

            params = session.send.await_args.args[1]
            self.assertEqual(params["captureBeyondViewport"], True)
            self.assertEqual(params["clip"], {"x": 0, "y": 0, "width": 800, "height": 600, "scale": 1})

    async def test_explicit_filename_gets_png_extension(self):
        """显式文件名自动补 .png 后缀"""
        with tempfile.TemporaryDirectory() as tmp:
            page, session, patchers = self._make_env(tmp)
            for p in patchers:
                self.addCleanup(p.stop)

            result = await capture_screenshot_impl(filename="snap1")

            summary = json.loads(result[0])
            self.assertTrue(summary["filename"].endswith(".png"))
            self.assertTrue(Path(summary["file"]).exists())


if __name__ == "__main__":
    unittest.main()
