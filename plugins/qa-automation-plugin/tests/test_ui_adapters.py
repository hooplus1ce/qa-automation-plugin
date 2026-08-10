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
