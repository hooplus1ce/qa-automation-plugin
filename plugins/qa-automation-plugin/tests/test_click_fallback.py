import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp.tools.browser import _visible_ancestor_path, _wait_visible_or_first  # noqa: E402


class FakeHandle:
    def __init__(self, evaluate_result=None, evaluate_exc=None):
        self._result = evaluate_result
        self._exc = evaluate_exc

    async def evaluate(self, expression):
        if self._exc:
            raise self._exc
        return self._result


class FakeLocator:
    """最小 locator 替身: wait_for 行为可编排 (None=成功, 异常=失败)。"""

    def __init__(self, wait_for_result=None, handle=None, name="lc"):
        self._wait_for_result = wait_for_result
        self._handle = handle
        self._first = None
        self.name = name

    @property
    def first(self):
        if self._first is None:
            self._first = FakeLocator(self._wait_for_result, self._handle, f"{self.name}.first")
        return self._first

    async def wait_for(self, state="visible", timeout=None):
        if isinstance(self._wait_for_result, Exception):
            raise self._wait_for_result
        return self._wait_for_result

    async def element_handle(self):
        if self._handle is None:
            raise RuntimeError("no handle")
        return self._handle


class FakeTarget:
    def __init__(self, locator):
        self._lc = locator

    def locator(self, selector):
        return self._lc

    def get_by_role(self, role, name=None):
        return self._lc


class WaitVisibleOrFirstTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_violation_takes_first_and_succeeds(self):
        """多元素匹配 (strict violation) → 自动取 .first 消歧后成功。"""
        strict_err = RuntimeError(
            "locator resolved to 7 elements: strict mode violation"
        )
        lc = FakeLocator(wait_for_result=strict_err, handle=FakeHandle())
        lc.first._wait_for_result = None  # .first 消歧后可见 → 成功
        result = await _wait_visible_or_first(lc, "点击", 6000)
        self.assertIs(result, lc.first)
        self.assertEqual(result.name, "lc.first")

    async def test_first_still_hidden_propagates_error(self):
        """.first 消歧后仍不可见 → 原异常继续抛出。"""
        strict_err = RuntimeError("strict mode violation: resolved to 7 elements")
        hidden_err = TimeoutError("Timeout 6000ms exceeded")
        lc = FakeLocator(wait_for_result=strict_err, handle=FakeHandle())
        lc.first._wait_for_result = hidden_err  # .first 的 wait_for 也失败
        with self.assertRaises(TimeoutError):
            await _wait_visible_or_first(lc, "点击", 6000)

    async def test_non_strict_error_propagates_unchanged(self):
        """非 strict 错误 (如超时) 原样抛出, 不做消歧。"""
        err = TimeoutError("Timeout 6000ms exceeded")
        lc = FakeLocator(wait_for_result=err)
        with self.assertRaises(TimeoutError):
            await _wait_visible_or_first(lc, "点击", 6000)


class VisibleAncestorPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_path_when_ancestor_visible(self):
        lc = FakeLocator(handle=FakeHandle(evaluate_result="div.ant-select-selection"))
        path = await _visible_ancestor_path(FakeTarget(lc), lc)
        self.assertEqual(path, "div.ant-select-selection")

    async def test_returns_none_when_evaluate_returns_none(self):
        lc = FakeLocator(handle=FakeHandle(evaluate_result=None))
        path = await _visible_ancestor_path(FakeTarget(lc), lc)
        self.assertIsNone(path)

    async def test_returns_none_when_no_element_handle(self):
        lc = FakeLocator()  # 无 handle → element_handle 抛错
        path = await _visible_ancestor_path(FakeTarget(lc), lc)
        self.assertIsNone(path)

    async def test_returns_none_when_evaluate_raises(self):
        lc = FakeLocator(handle=FakeHandle(evaluate_exc=RuntimeError("detached")))
        path = await _visible_ancestor_path(FakeTarget(lc), lc)
        self.assertIsNone(path)


if __name__ == "__main__":
    unittest.main()
