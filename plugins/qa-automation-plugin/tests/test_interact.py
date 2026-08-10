"""tools/interact.py 单元测试: elicitation 带超时 (超时默认继续下一步)。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp.tools import interact  # noqa: E402


def _ctx(elicit_impl):
    return SimpleNamespace(elicit=elicit_impl)


class TestElicitWithTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_user_accept_returns_data(self):
        from fastmcp.server.elicitation import AcceptedElicitation

        ctx = _ctx(AsyncMock(return_value=AcceptedElicitation(data="quick")))
        result = await interact.elicit_with_timeout(
            ctx, "msg", {"quick": {"title": "快速"}}, default="standard"
        )
        self.assertEqual(result, "quick")

    async def test_base_model_data_dumped(self):
        from fastmcp.server.elicitation import AcceptedElicitation
        from pydantic import BaseModel, Field

        class Form(BaseModel):
            name: str = Field(...)

        ctx = _ctx(AsyncMock(return_value=AcceptedElicitation(data=Form(name="x"))))
        result = await interact.elicit_with_timeout(ctx, "msg", Form, default=None)
        self.assertEqual(result, {"name": "x"})

    async def test_declined_returns_default(self):
        from fastmcp.server.elicitation import DeclinedElicitation

        ctx = _ctx(AsyncMock(return_value=DeclinedElicitation()))
        result = await interact.elicit_with_timeout(
            ctx, "msg", None, default="proceed"
        )
        self.assertEqual(result, "proceed")

    async def test_timeout_returns_default(self):
        async def _slow(*a, **k):
            await asyncio.sleep(30)
            return None

        ctx = _ctx(_slow)
        with patch.object(interact, "INTERACT_TIMEOUT_S", 1):
            result = await interact.elicit_with_timeout(
                ctx, "msg", None, default="proceed"
            )
        self.assertEqual(result, "proceed")

    async def test_unsupported_client_returns_default(self):
        ctx = _ctx(AsyncMock(side_effect=RuntimeError("unsupported")))
        result = await interact.elicit_with_timeout(ctx, "msg", None, default=42)
        self.assertEqual(result, 42)

    async def test_no_ctx_returns_default(self):
        result = await interact.elicit_with_timeout(None, "msg", None, default=7)
        self.assertEqual(result, 7)

    async def test_custom_timeout_override(self):
        async def _slow(*a, **k):
            await asyncio.sleep(30)
            return None

        ctx = _ctx(_slow)
        result = await interact.elicit_with_timeout(
            ctx, "msg", None, default="d", timeout_s=1
        )
        self.assertEqual(result, "d")

    async def test_disabled_switch_short_circuits(self):
        """INTERACTIVE_UI_ENABLED=false: 不弹窗, 直接返回默认值。"""
        from fastmcp.server.elicitation import AcceptedElicitation

        ctx = _ctx(AsyncMock(return_value=AcceptedElicitation(data="quick")))
        with patch.object(interact, "INTERACTIVE_UI_ENABLED", False):
            result = await interact.elicit_with_timeout(
                ctx, "msg", {"quick": {"title": "快速"}}, default="standard"
            )
        self.assertEqual(result, "standard")
        ctx.elicit.assert_not_awaited()  # 弹窗从未被调用


if __name__ == "__main__":
    unittest.main()
