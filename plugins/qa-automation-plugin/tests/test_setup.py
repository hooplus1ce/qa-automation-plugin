"""tools/setup.py 单元测试: 交互式配置表单 → 用户级 .env 写入与校验。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp.tools import setup  # noqa: E402
from qa_mcp.tools.setup import PluginConfigForm  # noqa: E402


def _make_form(**overrides):
    values = {
        "project_dir": "D:/MyProject",
        "cdp_url": "http://127.0.0.1:9222",
        "vision_provider": "antigravity",
        "vision_model": "gemini-3.6-flash",
        "download_dir": "downloads",
        "visual_effects": True,
    }
    values.update(overrides)
    return PluginConfigForm(**values)


class TestUpdateEnvFile(unittest.TestCase):
    def test_update_preserves_comment_and_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text(
                "# 注释保留\nPROJECT_DIR=D:/Old\nKEEP_ME=1\n", encoding="utf-8"
            )
            setup._update_env_file(p, {"PROJECT_DIR": "D:/New", "NEW_KEY": "v"})
            content = p.read_text(encoding="utf-8")
        self.assertIn("# 注释保留", content)
        self.assertIn("PROJECT_DIR=D:/New", content)
        self.assertIn("KEEP_ME=1", content)
        self.assertIn("NEW_KEY=v", content)
        self.assertNotIn("D:/Old", content)

    def test_create_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            setup._update_env_file(p, {"A": "1"})
            self.assertEqual(p.read_text(encoding="utf-8").strip(), "A=1")


class TestPluginSetup(unittest.IsolatedAsyncioTestCase):
    async def test_submit_writes_env_and_returns_saved(self):
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as tmp:
            form = _make_form(project_dir=str(Path(proj)))
            from fastmcp.server.elicitation import AcceptedElicitation

            ctx = SimpleNamespace(
                elicit=AsyncMock(return_value=AcceptedElicitation(data=form))
            )
            target = Path(tmp) / ".env"
            with patch.object(setup, "user_env_path", return_value=target):
                result = await setup.plugin_setup_impl(ctx=ctx)
            content = target.read_text(encoding="utf-8")
        self.assertEqual(result["status"], "success")
        self.assertIn("重启客户端", result["message"])
        self.assertEqual(result["saved"]["PROJECT_DIR"], str(Path(proj)))
        self.assertIn("PROJECT_DIR=", content)
        self.assertIn("VISION_PROVIDER=antigravity", content)
        self.assertIn("VISUAL_EFFECTS=true", content)

    async def test_invalid_project_dir_rejected(self):
        form = _make_form(project_dir="D:/no/such/project")
        from fastmcp.server.elicitation import AcceptedElicitation

        ctx = SimpleNamespace(
            elicit=AsyncMock(return_value=AcceptedElicitation(data=form))
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".env"
            with patch.object(setup, "user_env_path", return_value=target):
                result = await setup.plugin_setup_impl(ctx=ctx)
        self.assertEqual(result["status"], "error")
        self.assertIn("不存在", result["message"])
        self.assertFalse(target.exists())  # 未写入

    async def test_cancel_keeps_config_unchanged(self):
        from fastmcp.server.elicitation import DeclinedElicitation

        ctx = SimpleNamespace(
            elicit=AsyncMock(return_value=DeclinedElicitation())
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".env"
            with patch.object(setup, "user_env_path", return_value=target):
                result = await setup.plugin_setup_impl(ctx=ctx)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(target.exists())

    async def test_unsupported_client_degrades(self):
        ctx = SimpleNamespace(
            elicit=AsyncMock(side_effect=RuntimeError("unsupported"))
        )
        with patch.object(setup, "user_env_path", return_value=Path("x") / ".env"):
            result = await setup.plugin_setup_impl(ctx=ctx)
        self.assertEqual(result["status"], "error")
        self.assertIn("手动编辑", result["message"])

    async def test_no_ctx_returns_error(self):
        result = await setup.plugin_setup_impl(ctx=None)
        self.assertEqual(result["status"], "error")
        self.assertIn("手动编辑", result["message"])

    def test_form_constructible_with_defaults(self):
        """表单默认值齐全 (导入时与配置同步), 可无参构造。"""
        form = PluginConfigForm()
        self.assertTrue(form.project_dir)
        self.assertTrue(form.cdp_url)
        self.assertIn(form.vision_provider, ("auto", "antigravity", "tokenhub", "custom"))
        self.assertTrue(form.vision_model)
        self.assertTrue(form.download_dir)
        self.assertIsInstance(form.visual_effects, bool)

    def test_handle_config_form_writes_env(self):
        """Desktop FormInput 回调: 校验后写入用户级 .env, 返回重启提示。"""
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as tmp:
            form = _make_form(project_dir=str(Path(proj)))
            target = Path(tmp) / ".env"
            with patch.object(setup, "user_env_path", return_value=target):
                message = setup.handle_config_form(form)
            content = target.read_text(encoding="utf-8")
        self.assertIn("重启客户端", message)
        self.assertIn("PROJECT_DIR=", content)
        self.assertIn("VISION_PROVIDER=antigravity", content)

    def test_handle_config_form_rejects_bad_project(self):
        form = _make_form(project_dir="D:/no/such/project")
        with patch.object(setup, "user_env_path", return_value=Path("x") / ".env"):
            with self.assertRaisesRegex(ValueError, "不存在"):
                setup.handle_config_form(form)


if __name__ == "__main__":
    unittest.main()
