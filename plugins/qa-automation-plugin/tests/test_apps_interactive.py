"""apps_interactive.py 单元测试: Desktop 卡片倒计时自动继续 (SetInterval on_mount)。"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastmcp import FastMCP  # noqa: E402

from qa_mcp.apps_interactive import (  # noqa: E402
    ApprovalWithTimeout,
    ChoiceWithTimeout,
    ConfigFormApp,
)


class TestInteractiveApps(unittest.IsolatedAsyncioTestCase):
    async def _run(self, mcp, tool_name, args) -> str:
        tool = await mcp.get_tool(tool_name)
        result = await tool.run(args)
        return json.dumps(result.structured_content, ensure_ascii=False)

    async def test_choice_card_has_countdown_timer(self):
        """choose 卡片: on_mount 挂 SetInterval (10s + while 守卫 + 超时默认消息)。"""
        mcp = FastMCP("t", providers=[ChoiceWithTimeout(name="QA 选择")])
        s = await self._run(
            mcp,
            "choose",
            {"prompt": "选择粒度", "options": ["快速", "标准", "深度"], "default_option": "标准"},
        )
        self.assertEqual(s.count("setInterval"), 1)  # on_mount 定时器
        self.assertEqual(s.count("10000"), 1)  # 默认 10 秒
        self.assertIn("未操作将默认选择: 标准", s)  # 超时默认文案
        # while 守卫 (用户已操作则定时器停止)
        self.assertIn("while", s)
        # 每个选项都是可点击按钮
        for opt in ("快速", "标准", "深度"):
            self.assertIn(opt, s)

    async def test_approval_card_has_countdown_timer(self):
        """request_approval 卡片: 同样带倒计时, 超时默认继续执行。"""
        mcp = FastMCP("t", providers=[ApprovalWithTimeout(name="QA 审批")])
        s = await self._run(
            mcp, "request_approval", {"summary": "执行 3 步动作链", "timeout_s": 5}
        )
        self.assertEqual(s.count("setInterval"), 1)
        self.assertEqual(s.count("5000"), 1)  # timeout_s 覆盖
        self.assertIn("未操作将默认: 继续执行", s)
        self.assertIn("继续执行", s)
        self.assertIn("取消", s)

    async def test_choice_reject_default_option(self):
        """default_option 覆盖默认选择。"""
        mcp = FastMCP("t", providers=[ChoiceWithTimeout(name="QA 选择")])
        s = await self._run(
            mcp,
            "choose",
            {"prompt": "p", "options": ["A", "B"], "default_option": "B", "timeout_s": 3},
        )
        self.assertIn("未操作将默认选择: B", s)
        self.assertIn("3000", s)

    async def test_setup_form_registered_with_guidance(self):
        """setup_form: Desktop 配置表单, 描述引导模型正确选择通道。"""
        mcp = FastMCP("t", providers=[ConfigFormApp(name="QA 配置")])
        tools = await mcp.list_tools()
        names = [t.name for t in tools]
        self.assertIn("setup_form", names)
        t = next(x for x in tools if x.name == "setup_form")
        self.assertIn("Claude Desktop", t.description)
        self.assertIn("plugin_setup", t.description)  # 降级通道指引
        # 后端提交工具模型可见 (UI 不渲染时的降级提交通道)
        self.assertIn("submit_config", names)

    def test_ui_tools_switch_by_env(self):
        """INTERACTIVE_UI_ENABLED 控制 UI 工具注册 (子进程, 环境变量优先于 .env)。"""
        import os
        import subprocess

        code = f"""
import asyncio, sys
sys.path.insert(0, {str(PROJECT_ROOT / 'src')!r})
from qa_mcp.server import mcp
async def main():
    names = [t.name for t in await mcp.list_tools()]
    print(" ".join(sorted(names)))
asyncio.run(main())
"""
        base_env = {
            k: v for k, v in os.environ.items() if k != "INTERACTIVE_UI_ENABLED"
        }

        env_false = dict(base_env)
        env_false["INTERACTIVE_UI_ENABLED"] = "false"
        r = subprocess.run(
            [sys.executable, "-c", code],
            env=env_false, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        names_false = set(r.stdout.strip().split())
        for hidden in (
            "setup_form", "choose", "request_approval",
            "vtable_records_view", "plugin_setup",
        ):
            self.assertNotIn(hidden, names_false)
        self.assertIn("describe_image", names_false)  # 核心工具不受影响

        env_true = dict(base_env)
        env_true["INTERACTIVE_UI_ENABLED"] = "true"
        r2 = subprocess.run(
            [sys.executable, "-c", code],
            env=env_true, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(r2.returncode, 0, r2.stderr[-500:])
        names_true = set(r2.stdout.strip().split())
        for registered in (
            "setup_form", "choose", "request_approval",
            "vtable_records_view", "plugin_setup",
        ):
            self.assertIn(registered, names_true)

    async def test_submit_config_direct_call(self):
        """UI 不渲染时: 模型直接调 submit_config 提交完整配置。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        mcp = FastMCP("t", providers=[ConfigFormApp(name="QA 配置")])
        tool = await mcp.get_tool("submit_config")
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".env"
            with patch("qa_mcp.tools.setup.user_env_path", return_value=target):
                result = await tool.run(
                    {
                        "data": {
                            "project_dir": str(Path(proj)),
                            "cdp_url": "http://127.0.0.1:9222",
                            "vision_provider": "antigravity",
                            "vision_model": "gemini-3.6-flash",
                            "download_dir": "downloads",
                            "visual_effects": True,
                        }
                    }
                )
            content = target.read_text(encoding="utf-8")
        self.assertIn("重启客户端", result.content[0].text)
        self.assertIn("VISION_PROVIDER=antigravity", content)
        self.assertIn(f"PROJECT_DIR={Path(proj)}", content)


if __name__ == "__main__":
    unittest.main()
