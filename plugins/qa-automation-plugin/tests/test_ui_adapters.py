import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp.utils.ui_adapters import (  # noqa: E402
    AntDesignAdapter,
    UIAdapterRegistry,
)


class FakeLocator:
    def __init__(self, count=1):
        self._count = count
        self.events = []

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def locator(self, selector):
        self.events.append(("locator", selector))
        return self

    def get_by_role(self, role, name, exact):
        self.events.append(("get_by_role", role, name, exact))
        return FakeLocator(count=0)

    def filter(self, has_text):
        self.events.append(("filter", has_text))
        return self

    async def count(self):
        return self._count

    async def wait_for(self, state, timeout):
        self.events.append(("wait_for", state, timeout))

    async def click(self):
        self.events.append(("click",))


class FakeSelectTrigger(FakeLocator):
    """带 class 属性的 select 触发框 (antd 3: selectUidXXX 与展开层 dropdownUidXXX 同源)。"""

    def __init__(self, class_name="selectUidabc123  ant-select"):
        super().__init__()
        self.class_name = class_name

    async def get_attribute(self, name):
        self.events.append(("get_attribute", name))
        return self.class_name


class FakeOption(FakeLocator):
    def __init__(self, texts=None):
        super().__init__(count=1)
        self.texts = texts or ["成品一库"]

    def nth(self, index):
        self.events.append(("nth", index))
        return self

    async def evaluate_all(self, script):
        self.events.append(("evaluate_all",))
        return self.texts


class FakeDropdown(FakeLocator):
    def __init__(self, texts=None):
        super().__init__()
        self.option = FakeOption(texts=texts)

    def locator(self, selector):
        self.events.append(("locator", selector))
        return self.option


class FakePage:
    def __init__(self, framework="standard", dropdown=None):
        self.frames = [FakeFrame(framework)]
        self.dropdown = dropdown or FakeDropdown()

    def locator(self, selector):
        self.locator_selector = selector
        return self.dropdown


class FakeFrame:
    def __init__(self, framework):
        self.framework = framework

    async def evaluate(self, script):
        self.script = script
        return self.framework


class FakeDateInput(FakeLocator):
    def __init__(self):
        super().__init__()
        self.value = None

    async def evaluate(self, script):
        self.events.append(("evaluate", script))

    async def get_attribute(self, name):
        return None

    async def fill(self, value):
        self.value = value
        self.events.append(("fill", value))

    async def press(self, key):
        self.events.append(("press", key))


class FakeDateTrigger(FakeLocator):
    def __init__(self, date_input):
        super().__init__()
        self.date_input = date_input

    def locator(self, selector):
        self.events.append(("locator", selector))
        return self.date_input


class UIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_ant_design_is_detected_inside_iframe(self):
        registry = UIAdapterRegistry()
        page = FakePage(framework="ant_design")

        framework = await registry.detect_framework(page)

        self.assertEqual(framework, "ant_design")
        self.assertIsInstance(registry.get_adapter(framework), AntDesignAdapter)

    async def test_ant_select_uses_visible_portal_and_text_fallback(self):
        adapter = AntDesignAdapter()
        dropdown = FakeDropdown()
        page = FakePage(dropdown=dropdown)
        trigger = FakeLocator()

        await adapter.select_option(page, trigger, "成品一库")

        self.assertIn(("click",), trigger.events)
        self.assertIn(".ant-select-dropdown:visible", page.locator_selector)
        self.assertIn(".ant-cascader-menus:visible", page.locator_selector)
        self.assertIn(("evaluate_all",), dropdown.option.events)
        self.assertIn(("nth", 0), dropdown.option.events)
        self.assertIn(("click",), dropdown.option.events)

    async def test_ant_select_substring_ambiguity_raises_with_candidates(self):
        """子串歧义 (如"含过敏原" vs "不含过敏原") 时必须抛带候选列表的错误,
        而不是盲目点击第一个 (会误选) 或 strict violation 后无从下手。"""
        adapter = AntDesignAdapter()
        dropdown = FakeDropdown(texts=["清洗方案A", "清洗方案B"])
        page = FakePage(dropdown=dropdown)
        trigger = FakeLocator()

        with self.assertRaises(RuntimeError) as ctx:
            await adapter.select_option(page, trigger, "清洗方案")
        message = str(ctx.exception)
        self.assertIn("清洗方案A", message)
        self.assertIn("清洗方案B", message)
        # 歧义时不得点击任何选项
        self.assertNotIn(("click",), dropdown.option.events)

    async def test_ant_select_expanded_dropdown_clicked_directly(self):
        """目标下拉已展开 (uid 关联命中) 时直接点选项, 不得点击触发框 (会关掉下拉)。"""
        adapter = AntDesignAdapter()
        dropdown = FakeDropdown(texts=["中班"])
        page = FakePage(dropdown=dropdown)
        trigger = FakeSelectTrigger(class_name="selectUidbdf6d3819d693682c06b4d2bedac532c  ant-select")

        await adapter.select_option(page, trigger, "中班")

        # 未点击触发框 (展开态直接点选项)
        self.assertNotIn(("click",), trigger.events)
        self.assertIn(".dropdownUidbdf6d3819d693682c06b4d2bedac532c:visible", page.locator_selector)
        self.assertIn(("click",), dropdown.option.events)

    async def test_ant_select_expanded_without_uid_falls_back(self):
        """触发框无 selectUid (antd 4/5 等) 时回退常规"点击触发框重开"流程。"""
        adapter = AntDesignAdapter()
        dropdown = FakeDropdown(texts=["白班"])
        page = FakePage(dropdown=dropdown)
        trigger = FakeSelectTrigger(class_name="legions-pro-select ant-select")

        await adapter.select_option(page, trigger, "白班")

        self.assertIn(("click",), trigger.events)
        self.assertIn(("click",), dropdown.option.events)

    async def test_ant_select_expanded_option_missing_falls_back(self):
        """展开层里没有目标选项 (异步加载/uid 误命中残留层) 时回退常规流程并报错。"""
        adapter = AntDesignAdapter()
        dropdown = FakeDropdown(texts=["白班"])
        page = FakePage(dropdown=dropdown)
        trigger = FakeSelectTrigger(class_name="selectUidabc123  ant-select")

        with self.assertRaises(RuntimeError) as ctx:
            await adapter.select_option(page, trigger, "中班")
        self.assertIn("中班", str(ctx.exception))
        # 回退后触发框被点击重开, 常规流程照常执行
        self.assertIn(("click",), trigger.events)

    async def test_ant_select_exact_match_wins_over_substring(self):
        """"含过敏原" 精确命中一个选项时, 不得因"不含过敏原"包含子串而误选/误报。"""
        adapter = AntDesignAdapter()
        dropdown = FakeDropdown(texts=["含过敏原", "不含过敏原"])
        page = FakePage(dropdown=dropdown)
        trigger = FakeLocator()

        await adapter.select_option(page, trigger, "含过敏原")

        self.assertIn(("nth", 0), dropdown.option.events)
        self.assertIn(("click",), dropdown.option.events)

    async def test_ant_date_picker_fills_nested_input_and_commits(self):
        adapter = AntDesignAdapter()
        date_input = FakeDateInput()
        trigger = FakeDateTrigger(date_input)

        await adapter.fill_date(None, trigger, "2026-08-02")

        self.assertEqual(date_input.value, "2026-08-02")
        self.assertIn(("press", "Enter"), date_input.events)


if __name__ == "__main__":
    unittest.main()
