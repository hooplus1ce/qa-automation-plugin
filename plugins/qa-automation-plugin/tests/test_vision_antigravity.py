"""vision_antigravity.py 单元测试: OAuth 凭据 / CCA SSE 解析 / describe_image 集成。"""

import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp import vision_antigravity as va  # noqa: E402
from qa_mcp.tools import vision  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self):
        return Path(self.tmp.name) / "creds.json"

    def test_roundtrip(self):
        creds = va.AntigravityCredentials(
            access_token="a", refresh_token="r", expires_at=123.0, project_id="p"
        )
        with patch.object(va, "credentials_path", return_value=self._path()):
            va.save_credentials(creds)
            loaded = va.load_credentials()
        self.assertEqual(loaded.access_token, "a")
        self.assertEqual(loaded.refresh_token, "r")
        self.assertEqual(loaded.project_id, "p")

    def test_missing_returns_none(self):
        with patch.object(va, "credentials_path", return_value=self._path()):
            self.assertIsNone(va.load_credentials())

    def test_expiry_detection(self):
        now = time.time()
        fresh = va.AntigravityCredentials("a", "r", now + 3600, "p")
        stale = va.AntigravityCredentials("a", "r", now - 10, "p")
        self.assertFalse(va._is_expired(fresh))
        self.assertTrue(va._is_expired(stale))

    def test_refresh_token_exchange(self):
        with patch.object(va, "httpx") as fake_httpx, patch.dict(
            os.environ,
            {
                "ANTIGRAVITY_CLIENT_ID": "cid-1",
                "ANTIGRAVITY_CLIENT_SECRET": "sec-1",
            },
            clear=True,
        ):
            fake_httpx.post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"access_token": "new-a", "expires_in": 3600},
            )
            old = va.AntigravityCredentials("old-a", "r", time.time() - 10, "p")
            with patch.object(va, "save_credentials"):
                new = va.refresh_access_token(old)
        self.assertEqual(new.access_token, "new-a")
        self.assertEqual(new.refresh_token, "r")  # 无新 refresh 时保留旧值
        self.assertEqual(new.project_id, "p")
        fake_httpx.post.assert_called_once()

    def test_ensure_valid_requires_login(self):
        with patch.object(va, "load_credentials", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "login"):
                va.ensure_valid_credentials()


    def test_credentials_required_from_env(self):
        """凭证不硬编码: 未配置 ANTIGRAVITY_CLIENT_ID/SECRET 时报错提示。"""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ANTIGRAVITY_CLIENT_ID"):
                va._oauth_client_credentials()
        with patch.dict(
            os.environ,
            {"ANTIGRAVITY_CLIENT_ID": "cid", "ANTIGRAVITY_CLIENT_SECRET": "sec"},
            clear=True,
        ):
            self.assertEqual(va._oauth_client_credentials(), ("cid", "sec"))


class TestSseParsing(unittest.TestCase):
    def test_resolve_endpoints_modes(self):
        """端点解析: 默认/auto 走 failover 端点列表, sandbox/production/自定义 URL 单选。"""
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(va._resolve_endpoints("auto"), list(va.ANTIGRAVITY_ENDPOINTS))
            self.assertEqual(va._resolve_endpoints(""), list(va.ANTIGRAVITY_ENDPOINTS))
            self.assertEqual(va._resolve_endpoints("sandbox"), [va.SANDBOX_ENDPOINT])
            self.assertEqual(va._resolve_endpoints("production"), [va.DAILY_ENDPOINT])

    def test_resolve_endpoints_custom_url(self):
        with patch.dict(
            os.environ, {"ANTIGRAVITY_ENDPOINT": "https://my-cca.example.com"}, clear=True
        ):
            self.assertEqual(va._resolve_endpoints("auto"), ["https://my-cca.example.com"])

    def test_resolve_endpoints_ignores_non_url_env(self):
        """回归: .mcp.json 误设 ANTIGRAVITY_ENDPOINT=auto 时, 不能把裸值当请求 URL。"""
        with patch.dict(os.environ, {"ANTIGRAVITY_ENDPOINT": "auto"}, clear=True), patch.object(
            va, "logger"
        ):
            self.assertEqual(va._resolve_endpoints("auto"), list(va.ANTIGRAVITY_ENDPOINTS))

    def test_parse_sse_line(self):
        self.assertIsNone(va._parse_sse_line(""))
        self.assertIsNone(va._parse_sse_line("event: message"))
        self.assertIsNone(va._parse_sse_line("data: [DONE]"))
        evt = va._parse_sse_line('data: {"candidates": []}')
        self.assertEqual(evt, {"candidates": []})

    def test_stream_vision_collects_thought_and_text(self):
        """CCA 流: thought parts 归 reasoning, text parts 归 content, usage 归一化。"""
        # 真实结构: 事件包装在 {"response": {...}} 内, usageMetadata 在 response 层
        sse = [
            'data: {"response":{"candidates":[{"content":{"parts":[{"thought":true,"text":"思考中"}]}}]}}',
            'data: {"response":{"candidates":[{"content":{"parts":[{"text":"图片里有"}]}}]}}',
            'data: {"response":{"candidates":[{"content":{"parts":[{"text":"红色按钮"}]}}]}}',
            'data: {"response":{"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":32,"totalTokenCount":42}}}',
        ]
        stream_lines = [l + "\n" for l in sse]

        class _FakeStream:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_lines(self):
                return iter(stream_lines)

        creds = va.AntigravityCredentials("a", "r", time.time() + 3600, "proj")
        with patch.object(va, "httpx") as fake_httpx:
            fake_httpx.stream.side_effect = lambda *a, **k: _FakeStream()
            fake_httpx.Timeout = __import__("httpx").Timeout
            reasoning, content, usage = va._stream_vision(
                creds, "gemini-3.6-flash", [{"url": "data:image/png;base64,AAA"}], "q", True, "medium"
            )

        self.assertEqual(reasoning, "思考中")
        self.assertEqual(content, "图片里有红色按钮")
        self.assertEqual(usage, {"prompt_tokens": 10, "completion_tokens": 32, "total_tokens": 42})
        # 请求体信封与 wire 模型路由
        call_kwargs = fake_httpx.stream.call_args
        self.assertEqual(call_kwargs[0][0], "POST")
        self.assertIn("/v1internal:streamGenerateContent?alt=sse", call_kwargs[0][1])
        body = call_kwargs.kwargs["json"]
        self.assertEqual(body["model"], "gemini-3.6-flash-medium")  # medium → wire 路由
        self.assertEqual(body["project"], "proj")
        self.assertEqual(body["userAgent"], "antigravity")
        self.assertEqual(body["request"]["generationConfig"]["maxOutputTokens"], 65536)
        self.assertEqual(
            body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"], "MEDIUM"
        )
        self.assertEqual(body["request"]["contents"][0]["parts"][0]["inlineData"]["mimeType"], "image/png")
        self.assertEqual(body["request"]["contents"][-1]["parts"][0]["text"], "q")

    def test_route_wire_model_by_effort(self):
        self.assertEqual(va._route_wire_model("gemini-3.6-flash", "low"), "gemini-3.6-flash-low")
        self.assertEqual(va._route_wire_model("gemini-3.6-flash", "minimal"), "gemini-3.6-flash-low")
        self.assertEqual(va._route_wire_model("gemini-3.6-flash", "medium"), "gemini-3.6-flash-medium")
        self.assertEqual(va._route_wire_model("gemini-3.6-flash", "high"), "gemini-3.6-flash-high")
        self.assertEqual(va._route_wire_model("gemini-3.6-flash", "max"), "gemini-3.6-flash-high")
        # 已带 effort 后缀透传
        self.assertEqual(va._route_wire_model("gemini-3.6-flash-low", "high"), "gemini-3.6-flash-low")
        # 无路由表的模型透传
        self.assertEqual(va._route_wire_model("claude-sonnet-4-6", "low"), "claude-sonnet-4-6")

    def test_stream_vision_failover_across_endpoints(self):
        """端点 failover: 首个端点异常自动尝试下一端点。"""
        calls = {"n": 0}

        def fake_stream(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first endpoint down")

            class _FakeStream:
                status_code = 200

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def iter_lines(self):
                    return iter(['data: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}' + "\n"])

            return _FakeStream()

        creds = va.AntigravityCredentials("a", "r", time.time() + 3600, "proj")
        with patch.object(va, "httpx") as fake_httpx:
            fake_httpx.stream.side_effect = fake_stream
            fake_httpx.Timeout = __import__("httpx").Timeout
            _, content, _ = va._stream_vision(creds, "m", [], "q", False, "low")
        self.assertEqual(content, "ok")
        self.assertEqual(calls["n"], 2)

    def test_stream_vision_http_error_raises(self):
        fake_resp = MagicMock(status_code=403)
        fake_resp.read.return_value = b"forbidden"

        class _FakeStream:
            status_code = 403

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"forbidden"

        with patch.object(va, "httpx") as fake_httpx:
            fake_httpx.stream.return_value = _FakeStream()
            fake_httpx.Timeout = __import__("httpx").Timeout
            creds = va.AntigravityCredentials("a", "r", time.time() + 3600, "proj")
            with self.assertRaises(RuntimeError):
                va._stream_vision(creds, "m", [], "q", False, "low")


class TestDescribeImageAntigravity(unittest.IsolatedAsyncioTestCase):
    def _make_png(self, tmp: str) -> str:
        p = Path(tmp) / "shot.png"
        p.write_bytes(TINY_PNG)
        return str(p)

    async def test_missing_credentials_returns_actionable_error(self):
        with patch.object(va, "ensure_valid_credentials", side_effect=RuntimeError("未找到 Antigravity 凭据。请先运行: login")), patch.dict(
            os.environ, {"VISION_PROVIDER": "antigravity"}, clear=True
        ):
            with tempfile.TemporaryDirectory() as tmp:
                result = await vision.describe_image_impl(
                    images=[self._make_png(tmp)],
                    reasoning_effort="low",
                )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "missing_key")
        self.assertIn("login", result["message"])

    async def test_antigravity_branch_success(self):
        """VISION_PROVIDER=antigravity: 走 CCA 分支, 返回 provider/model 与内容。"""
        fake_creds = va.AntigravityCredentials("a", "r", time.time() + 3600, "proj")
        with patch.object(va, "ensure_valid_credentials", return_value=fake_creds), patch.object(
            va, "_stream_vision", return_value=("思考", "结论", {"totalTokenCount": 9})
        ) as stream_mock, patch.dict(
            os.environ, {"VISION_PROVIDER": "antigravity"}, clear=True
        ):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(vision, "EVIDENCE_DIR", tmp):
                    result = await vision.describe_image_impl(
                        images=[self._make_png(tmp)],
                        question="图中有什么？",
                        include_reasoning=True,
                    )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider"], "antigravity")
        self.assertEqual(result["model"], "gemini-3.6-flash")
        self.assertEqual(result["description"], "结论")
        self.assertEqual(result["reasoning"], "思考")
        self.assertEqual(result["cached"], False)
        stream_mock.assert_called_once()
        # effort auto: "图中有什么？" (5 字) → low
        self.assertEqual(stream_mock.call_args.args[5], "low")

    async def test_antigravity_cache_hit_skips_stream(self):
        fake_creds = va.AntigravityCredentials("a", "r", time.time() + 3600, "proj")
        with patch.object(va, "ensure_valid_credentials", return_value=fake_creds), patch.object(
            va, "_stream_vision", return_value=("", "第一次", {})
        ) as stream_mock, patch.dict(
            os.environ, {"VISION_PROVIDER": "antigravity"}, clear=True
        ):
            with tempfile.TemporaryDirectory() as tmp:
                p = self._make_png(tmp)
                with patch.object(vision, "EVIDENCE_DIR", tmp):
                    r1 = await vision.describe_image_impl(images=[p], question="q", reasoning_effort="low")
                    r2 = await vision.describe_image_impl(images=[p], question="q", reasoning_effort="low")
        self.assertEqual(r1["cached"], False)
        self.assertEqual(r2["cached"], True)
        self.assertEqual(stream_mock.call_count, 1)


class TestVisionLoginTool(unittest.IsolatedAsyncioTestCase):
    async def test_login_success_returns_project(self):
        fake = va.AntigravityCredentials("a", "r", time.time() + 3600, "proj-123")
        with patch.object(va, "login", return_value=fake) as login_mock:
            result = await vision.vision_login_impl()
        self.assertEqual(result["status"], "success")
        self.assertIn("proj-123", result["message"])
        # 工具模式: 不启用粘贴码 input 兜底
        login_mock.assert_called_once_with(False)

    async def test_login_failure_returns_error(self):
        with patch.object(va, "login", side_effect=RuntimeError("授权超时 (180s)")):
            result = await vision.vision_login_impl()
        self.assertEqual(result["status"], "error")
        self.assertIn("超时", result["message"])

    def test_login_timeout_without_paste_fallback_raises(self):
        """工具模式 (paste_fallback=False): 回调超时不走 input(), 直接报错。"""
        with patch.object(va, "_build_auth_url", return_value="https://auth"), patch.object(
            va, "webbrowser"
        ), patch.object(va, "_run_callback_server", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                va.login(paste_fallback=False)


if __name__ == "__main__":
    unittest.main()
