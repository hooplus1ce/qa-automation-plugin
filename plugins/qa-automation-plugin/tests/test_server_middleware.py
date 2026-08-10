"""server.py ToolSerializationMiddleware 单元测试: 执行看门狗 (工具卡死时释放串行队列)。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastmcp.server.middleware.middleware import MiddlewareContext
from fastmcp.tools import ToolResult

import qa_mcp.server as server


def _ctx() -> MiddlewareContext:
    return MiddlewareContext(message=SimpleNamespace(name="fill_input"))


async def _hung_call_next(ctx):
    await asyncio.Event().wait()  # 模拟 CDP 挂死: 永不返回


async def _fast_call_next(ctx):
    return ToolResult(content="ok")


class TestWatchdogTimeout(unittest.TestCase):
    def test_hung_tool_releases_queue_with_error(self):
        """超时: 返回 is_error 结果、触发浏览器重置、串行锁释放 (后续调用立即执行)。"""

        async def scenario():
            mw = server.ToolSerializationMiddleware()
            with patch.object(server, "TOOL_MAX_EXECUTION_MS", 300), patch.object(
                server, "browser_mgr"
            ) as bm:
                bm.close = AsyncMock()
                bm.recover = AsyncMock()
                r1 = await mw.on_call_tool(_ctx(), _hung_call_next)
                self.assertIsInstance(r1, ToolResult)
                self.assertTrue(r1.is_error)
                self.assertIn("超时", r1.content[0].text)
                await asyncio.sleep(0)  # 让 create_task(_safe_close_browser) 有机会执行
                # 队列已释放: 紧接着的调用不等待、立即返回
                r2 = await mw.on_call_tool(_ctx(), _fast_call_next)
                self.assertFalse(r2.is_error)
                self.assertEqual(r2.content[0].text, "ok")
                # 看门狗重置走 recover() (有界 stop + 重建连接) 而非 close()
                # (无界 stop 会在半开连接上挂死并堵死后续调用)
                bm.recover.assert_awaited()

        asyncio.run(scenario())

    def test_normal_tool_passthrough(self):
        """未超时的正常调用原样透传。"""

        async def scenario():
            mw = server.ToolSerializationMiddleware()
            with patch.object(server, "TOOL_MAX_EXECUTION_MS", 300000):
                r = await mw.on_call_tool(_ctx(), _fast_call_next)
                self.assertFalse(r.is_error)
                self.assertEqual(r.content[0].text, "ok")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
