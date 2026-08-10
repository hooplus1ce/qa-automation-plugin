"""vision.py 单元测试: API Key 加载 / 图片解析 / GLM-5V 流式思考解析 (describe_image)。"""

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qa_mcp.tools import vision  # noqa: E402

# 1x1 红色 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _FakeDelta:
    def __init__(self, reasoning=None, content=None):
        self.reasoning_content = reasoning
        self.content = content


class _FakeChunk:
    def __init__(self, delta=None):
        self.choices = [SimpleNamespace(delta=delta)] if delta is not None else []


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return iter(self._chunks)


def _make_fake_openai(chunks):
    class _FakeOpenAI:
        latest = None

        def __init__(self, api_key, base_url, **kwargs):
            self.api_key = api_key
            self.base_url = base_url
            self.timeout = kwargs.get("timeout")
            self.chat = SimpleNamespace(completions=_FakeCompletions(chunks))
            type(self).latest = self

    return _FakeOpenAI


class TestLoadApiKey(unittest.TestCase):
    def test_env_var_wins(self):
        with patch.dict(os.environ, {"VISION_API_KEY": "from-env"}, clear=True):
            self.assertEqual(vision._load_api_key(vision.PROVIDERS["tokenhub"]), "from-env")

    def test_dotenv_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text('VISION_API_KEY=from-dotenv\n', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch(
                "qa_mcp.tools.vision.Path.cwd", return_value=Path(tmp)
            ), patch.object(vision, "PROJECT_DIR", tmp), patch.object(
                vision, "__file__", str(Path(tmp) / "vision.py")
            ):
                self.assertEqual(vision._load_api_key(vision.PROVIDERS["tokenhub"]), "from-dotenv")

    def test_dotenv_in_project_dir_wins_over_plugin_dir(self):
        """插件部署: VISION_API_KEY 配置在用户项目 .env, 进程 cwd 是插件目录。"""
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as plug:
            (Path(proj) / ".env").write_text(
                'VISION_API_KEY=from-project\n', encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True), patch(
                "qa_mcp.tools.vision.Path.cwd", return_value=Path(plug)
            ), patch.object(vision, "PROJECT_DIR", proj), patch.object(
                vision, "__file__", str(Path(plug) / "vision.py")
            ):
                self.assertEqual(vision._load_api_key(vision.PROVIDERS["tokenhub"]), "from-project")

    def test_missing_returns_empty(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "qa_mcp.tools.vision.Path.cwd", return_value=Path(tempfile.gettempdir())
        ), patch.object(vision, "PROJECT_DIR", tempfile.gettempdir()), patch.object(
            vision, "__file__", str(Path(tempfile.gettempdir()) / "vision.py")
        ):
            self.assertEqual(vision._load_api_key(vision.PROVIDERS["tokenhub"]), "")


class TestResolveImageUrl(unittest.TestCase):
    def test_http_url_passthrough(self):
        self.assertEqual(
            vision._resolve_image_url("https://example.com/a.png"),
            {"url": "https://example.com/a.png"},
        )

    def test_local_file_becomes_data_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "shot.png"
            p.write_bytes(TINY_PNG)
            obj = vision._resolve_image_url(str(p))
            self.assertTrue(obj["url"].startswith("data:image/png;base64,"))
            self.assertEqual(
                base64.b64decode(obj["url"].split(",", 1)[1]), TINY_PNG
            )

    def test_relative_path_resolves_in_project_dir(self):
        """插件部署: 相对图片路径相对用户项目根 (PROJECT_DIR), 与进程 cwd 无关。"""
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as plug:
            (Path(proj) / "shot.png").write_bytes(TINY_PNG)
            with patch.object(vision, "PROJECT_DIR", proj), patch(
                "qa_mcp.tools.vision.Path.cwd", return_value=Path(plug)
            ):
                obj = vision._resolve_image_url("shot.png")
                self.assertTrue(obj["url"].startswith("data:image/png;base64,"))

    def test_relative_path_falls_back_to_cwd(self):
        """本地直跑: 项目根没有该文件时回退进程 cwd。"""
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as plug:
            (Path(plug) / "shot.png").write_bytes(TINY_PNG)
            with patch.object(vision, "PROJECT_DIR", proj), patch(
                "qa_mcp.tools.vision.Path.cwd", return_value=Path(plug)
            ):
                obj = vision._resolve_image_url("shot.png")
                self.assertTrue(obj["url"].startswith("data:image/png;base64,"))

    def test_missing_file_raises(self):
        with self.assertRaisesRegex(RuntimeError, "图片文件不存在"):
            vision._resolve_image_url("D:/no/such/file.png")

    def test_oversized_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "big.png"
            p.write_bytes(b"\x89PNG" + b"0" * (vision.MAX_IMAGE_BYTES + 1))
            with self.assertRaisesRegex(RuntimeError, "50MB"):
                vision._resolve_image_url(str(p))


class TestExtractLatestPastedImage(unittest.TestCase):
    def test_session_located_by_project_dir_not_cwd(self):
        """插件部署: 会话 jsonl 目录按用户项目路径 (PROJECT_DIR) 定位, 而非进程 cwd。"""
        with tempfile.TemporaryDirectory() as home_tmp, tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as plug, tempfile.TemporaryDirectory() as evid:
            home = Path(home_tmp)
            project = Path(proj) / "my_app"
            project.mkdir()
            parts = project.resolve().parts
            drive = parts[0][0]
            rest = "-".join(parts[1:])
            session_dir = home / ".claude" / "projects" / f"{drive.lower()}--{rest}"
            session_dir.mkdir(parents=True)
            rec = {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": base64.b64encode(TINY_PNG).decode("ascii"),
                            },
                        }
                    ]
                },
            }
            # 与 Claude Code 会话 jsonl 一致的紧凑分隔符 (行内需含 '"type":"image"')
            (session_dir / "20260807.jsonl").write_text(
                json.dumps(rec, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ, {"LOCALAPPDATA": str(Path(home_tmp))}, clear=True
            ), patch.object(vision, "PROJECT_DIR", str(project)), patch(
                "qa_mcp.tools.vision.Path.home", return_value=home
            ), patch("qa_mcp.tools.vision.Path.cwd", return_value=Path(plug)), patch.object(
                vision, "EVIDENCE_DIR", evid
            ):
                out = vision._extract_latest_pasted_image()
            self.assertIsNotNone(out)
            self.assertEqual(out.read_bytes(), TINY_PNG)
            self.assertEqual(out.parent.name, "pasted")  # 落盘 pasted/ 子目录 (临时输入)

    def test_no_session_for_cwd_returns_none(self):
        """插件部署且无注入项目: 按进程 cwd 找不到会话时返回 None 而非报错。"""
        with tempfile.TemporaryDirectory() as home_tmp, tempfile.TemporaryDirectory() as plug:
            home = Path(home_tmp)
            parts = Path(plug).resolve().parts
            drive = parts[0][0]
            rest = "-".join(parts[1:])
            session_dir = home / ".claude" / "projects" / f"{drive.lower()}--{rest}"
            session_dir.mkdir(parents=True)  # cwd 会话目录存在但无 jsonl
            with patch.object(vision, "PROJECT_DIR", plug), patch(
                "qa_mcp.tools.vision.Path.home", return_value=home
            ):
                self.assertIsNone(vision._extract_latest_pasted_image())

    def setUp(self):
        # 隔离: 默认无 antigravity 凭据且强制 tokenhub 通道 (用户级 .env 可能
        # 配置了 VISION_PROVIDER=antigravity, 会绕过 auto 逻辑走真实调用)
        self._ag = patch(
            "qa_mcp.tools.vision._antigravity_credentials_exist", return_value=False
        )
        self._ag.start()
        self._env = patch.dict(
            os.environ, {"VISION_PROVIDER": "tokenhub"}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._ag.stop()

    def _make_png(self, tmp: str) -> str:
        p = Path(tmp) / "shot.png"
        p.write_bytes(TINY_PNG)
        return str(p)

    async def test_error_without_api_key(self):
        with patch("qa_mcp.tools.vision._load_api_key", return_value=""):
            result = await vision.describe_image_impl(images=["a.png"])
        self.assertEqual(result["status"], "error")
        self.assertIn("VISION_API_KEY", result["message"])

    async def test_error_without_images(self):
        with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
            "qa_mcp.tools.vision._extract_latest_pasted_image", return_value=None
        ):
            result = await vision.describe_image_impl(images=[])
        self.assertEqual(result["status"], "error")
        self.assertIn("未指定图片", result["message"])

    async def test_streaming_success_splits_reasoning_and_content(self):
        """流式块解析: reasoning_content 归 reasoning, content 归 description;
        空 choices 块跳过; include_reasoning=True 时返回思考链;
        请求参数与参考示例一致 (stream + thinking + reasoning_effort)。"""
        chunks = [
            _FakeChunk(),  # 无 choices 的块应跳过
            _FakeChunk(_FakeDelta(reasoning="第一步：识别图片", content=None)),
            _FakeChunk(_FakeDelta(reasoning="第二步：分析布局", content="图中有一个")),
            _FakeChunk(_FakeDelta(reasoning=None, content="红色按钮")),
        ]
        fake_cls = _make_fake_openai(chunks)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("qa_mcp.tools.vision._load_api_key", return_value="test-key"), patch(
                "qa_mcp.tools.vision.OpenAI", fake_cls
            ), patch.object(vision, "EVIDENCE_DIR", tmp):
                result = await vision.describe_image_impl(
                    images=[self._make_png(tmp)],
                    question="图中有什么？",
                    reasoning_effort="high",
                    include_reasoning=True,
                )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["reasoning"], "第一步：识别图片第二步：分析布局")
        self.assertEqual(result["description"], "图中有一个红色按钮")
        self.assertTrue(result["thinking"])

        client = fake_cls.latest.chat.completions
        kwargs = client.last_kwargs
        self.assertEqual(kwargs["model"], "glm-5v-turbo")
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["max_tokens"], 2048)
        self.assertEqual(
            kwargs["extra_body"],
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        )
        user_content = kwargs["messages"][1]["content"]
        self.assertTrue(user_content[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(user_content[1]["text"], "图中有什么？")

    async def test_thinking_disabled_skips_reasoning_extra_body(self):
        chunks = [_FakeChunk(_FakeDelta(content="直接回答"))]
        fake_cls = _make_fake_openai(chunks)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("qa_mcp.tools.vision._load_api_key", return_value="test-key"), patch(
                "qa_mcp.tools.vision.OpenAI", fake_cls
            ), patch.object(vision, "EVIDENCE_DIR", tmp):
                result = await vision.describe_image_impl(
                    images=[self._make_png(tmp)], thinking=False
                )

        self.assertEqual(result["status"], "success")
        self.assertNotIn("reasoning", result)  # include_reasoning 默认关闭
        self.assertEqual(result["description"], "直接回答")
        self.assertEqual(fake_cls.latest.chat.completions.last_kwargs["extra_body"], {})

    async def test_reasoning_omitted_by_default(self):
        """默认 include_reasoning=False: 不返回思考链, 节省主模型上下文。"""
        chunks = [
            _FakeChunk(_FakeDelta(reasoning="思考中", content="结论")),
        ]
        fake_cls = _make_fake_openai(chunks)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("qa_mcp.tools.vision._load_api_key", return_value="test-key"), patch(
                "qa_mcp.tools.vision.OpenAI", fake_cls
            ), patch.object(vision, "EVIDENCE_DIR", tmp):
                result = await vision.describe_image_impl(
                    images=[self._make_png(tmp)], question="q"
                )
        self.assertEqual(result["status"], "success")
        self.assertNotIn("reasoning", result)
        self.assertEqual(result["description"], "结论")

    async def test_api_exception_returns_error_status(self):
        def _boom(*args, **kwargs):
            raise RuntimeError("network down")

        fake_cls = MagicMock(side_effect=_boom)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("qa_mcp.tools.vision._load_api_key", return_value="test-key"), patch(
                "qa_mcp.tools.vision.OpenAI", fake_cls
            ), patch.object(vision, "EVIDENCE_DIR", tmp):
                result = await vision.describe_image_impl(images=[self._make_png(tmp)])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "unknown")
        self.assertIn("network down", result["message"])


class TestVisionEnhancements(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._ag = patch(
            "qa_mcp.tools.vision._antigravity_credentials_exist", return_value=False
        )
        self._ag.start()
        self._env = patch.dict(
            os.environ, {"VISION_PROVIDER": "tokenhub"}, clear=False
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._ag.stop()

    def _make_png(self, tmp: str) -> str:
        p = Path(tmp) / "shot.png"
        p.write_bytes(TINY_PNG)
        return str(p)

    def test_effort_auto_adapts_to_question_length(self):
        self.assertEqual(vision._resolve_effort("看", "auto"), "low")
        self.assertEqual(vision._resolve_effort("x" * 25, "auto"), "medium")
        self.assertEqual(vision._resolve_effort("x" * 61, "auto"), "high")
        self.assertEqual(vision._resolve_effort("x", "max"), "max")  # 非 auto 直通

    def test_dimensions_png_parsed_without_pil(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.png"
            p.write_bytes(TINY_PNG)
            self.assertEqual(vision._image_dimensions(p), {"width": 1, "height": 1})

    def test_dimensions_unknown_format_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(b"\x00\x01\x02")
            self.assertIsNone(vision._image_dimensions(p))

    def _make_openai_error(self, exc_cls, status: int):
        from httpx import Request, Response

        req = Request("POST", "https://tokenhub.tencentmaas.com/v1")
        return exc_cls("boom", response=Response(status, request=req), body=None)

    def test_classify_openai_errors_short_and_actionable(self):
        cases = [
            (self._make_openai_error(vision.AuthenticationError, 401), "auth_failed"),
            (self._make_openai_error(vision.RateLimitError, 429), "rate_limited"),
            (self._make_openai_error(vision.InternalServerError, 500), "upstream"),
            (self._make_openai_error(vision.BadRequestError, 400), "bad_request"),
        ]
        provider = vision.PROVIDERS["tokenhub"]
        for exc, expected_type in cases:
            d = vision._classify_error(exc, provider)
            self.assertEqual(d["error_type"], expected_type)
            self.assertLess(len(d["message"]), 120)  # 消息简短, 不污染上下文

    def test_classify_unknown_error(self):
        d = vision._classify_error(RuntimeError("boom"), vision.PROVIDERS["tokenhub"])
        self.assertEqual(d["error_type"], "unknown")

    async def test_cache_hit_skips_second_api_call(self):
        """同图同问: 第二次命中 sha256 缓存, 不重复调用上游。"""
        calls = {"n": 0}

        def _counting_openai(chunks):
            class _Fake:
                def __init__(self, api_key, base_url, **kwargs):
                    self.chat = SimpleNamespace(completions=_FakeCompletions(chunks))
                    calls["n"] += 1

            return _Fake

        with tempfile.TemporaryDirectory() as tmp:
            with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
                "qa_mcp.tools.vision.OpenAI",
                _counting_openai([_FakeChunk(_FakeDelta(content="A"))]),
            ), patch.object(vision, "EVIDENCE_DIR", tmp):
                r1 = await vision.describe_image_impl(images=[self._make_png(tmp)], question="q")
                r2 = await vision.describe_image_impl(images=[self._make_png(tmp)], question="q")

        self.assertEqual(r1["status"], "success")
        self.assertEqual(r1["cached"], False)
        self.assertEqual(r2["status"], "success")
        self.assertEqual(r2["cached"], True)
        self.assertEqual(r2["description"], "A")
        self.assertEqual(calls["n"], 1)

    async def test_cache_miss_on_different_question(self):
        """同图不同问: 缓存键含 question, 不命中。"""
        calls = {"n": 0}

        def _counting_openai(chunks):
            class _Fake:
                def __init__(self, api_key, base_url, **kwargs):
                    self.chat = SimpleNamespace(completions=_FakeCompletions(chunks))
                    calls["n"] += 1

            return _Fake

        with tempfile.TemporaryDirectory() as tmp:
            with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
                "qa_mcp.tools.vision.OpenAI",
                _counting_openai([_FakeChunk(_FakeDelta(content="B"))]),
            ), patch.object(vision, "EVIDENCE_DIR", tmp):
                p = self._make_png(tmp)
                await vision.describe_image_impl(images=[p], question="q1")
                await vision.describe_image_impl(images=[p], question="q2")

        self.assertEqual(calls["n"], 2)

    async def test_retry_then_success(self):
        """限流后指数退避重试成功: 首次 429, 重试返回正常结果。"""
        attempts = {"n": 0}
        rate_err = self._make_openai_error(vision.RateLimitError, 429)

        class _FlakyCompletions:
            def create(self, **kwargs):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise rate_err
                return iter([_FakeChunk(_FakeDelta(content="ok"))])

        class _Flaky:
            def __init__(self, api_key, base_url, **kwargs):
                self.chat = SimpleNamespace(completions=_FlakyCompletions())

        with tempfile.TemporaryDirectory() as tmp:
            with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
                "qa_mcp.tools.vision.OpenAI", _Flaky
            ), patch.object(vision, "EVIDENCE_DIR", tmp), patch("qa_mcp.tools.vision.time.sleep") as sleep:
                result = await vision.describe_image_impl(images=[self._make_png(tmp)], question="q")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["description"], "ok")
        self.assertEqual(attempts["n"], 2)
        sleep.assert_called_once()

    async def test_retry_exhausted_returns_error(self):
        """重试耗尽仍失败: 返回限流错误分类。"""
        rate_err = self._make_openai_error(vision.RateLimitError, 429)

        class _AlwaysFail:
            def __init__(self, api_key, base_url, **kwargs):
                self.chat = SimpleNamespace(completions=_AlwaysFailCompletions())

        class _AlwaysFailCompletions:
            def create(self, **kwargs):
                raise rate_err

        with tempfile.TemporaryDirectory() as tmp:
            with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
                "qa_mcp.tools.vision.OpenAI", _AlwaysFail
            ), patch.object(vision, "EVIDENCE_DIR", tmp), patch("qa_mcp.tools.vision.time.sleep"):
                result = await vision.describe_image_impl(images=[self._make_png(tmp)], question="q")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_type"], "rate_limited")


class TestVisionProviderRole(unittest.TestCase):
    """vision role (参考 oh-my-pi models.yml): provider 选择与模型覆盖。

    仅 3 通道: antigravity (OAuth 凭据) / tokenhub (腾讯 GLM-5V) / custom。
    """

    def test_auto_prefers_antigravity_when_logged_in(self):
        with patch.object(vision, "_antigravity_credentials_exist", return_value=True), patch.dict(
            os.environ, {}, clear=True
        ):
            provider = vision._select_provider()
        self.assertEqual(provider.name, "antigravity")
        self.assertEqual(provider.model, "gemini-3.6-flash")
        self.assertEqual(provider.base_url, vision.ANTIGRAVITY_DAILY)

    def test_auto_falls_back_to_tokenhub_without_credentials(self):
        with patch.object(vision, "_antigravity_credentials_exist", return_value=False), patch.dict(
            os.environ, {"VISION_API_KEY": "t-key"}, clear=True
        ):
            provider = vision._select_provider()
        self.assertEqual(provider.name, "tokenhub")
        self.assertEqual(provider.model, "glm-5v-turbo")

    def test_explicit_provider_wins(self):
        with patch.object(vision, "_antigravity_credentials_exist", return_value=True), patch.dict(
            os.environ, {"VISION_PROVIDER": "tokenhub"}, clear=True
        ):
            provider = vision._select_provider()
        self.assertEqual(provider.name, "tokenhub")

    def test_vision_model_override(self):
        with patch.object(vision, "_antigravity_credentials_exist", return_value=True), patch.dict(
            os.environ,
            {"VISION_PROVIDER": "antigravity", "VISION_MODEL": "gemini-3.6-flash-lite"},
            clear=True,
        ):
            provider = vision._select_provider()
        self.assertEqual(provider.model, "gemini-3.6-flash-lite")

    def test_custom_provider_requires_base_and_model(self):
        with patch.dict(
            os.environ,
            {"VISION_PROVIDER": "custom", "VISION_API_BASE": "https://x/v1", "VISION_MODEL": "m"},
            clear=True,
        ):
            provider = vision._select_provider()
        self.assertEqual(provider.name, "custom")
        self.assertEqual(provider.base_url, "https://x/v1")
        with patch.dict(os.environ, {"VISION_PROVIDER": "custom"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "VISION_API_BASE"):
                vision._select_provider()

    def test_load_api_key_uses_provider_key_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 隔离: 避免读取用户真实 PROJECT_DIR/.env 与用户级 ~/.qa-automation-plugin/.env
            with patch.object(vision, "PROJECT_DIR", tmp), patch(
                "qa_mcp.tools.vision.Path.cwd", return_value=Path(tmp)
            ), patch.object(vision, "__file__", str(Path(tmp) / "vision.py")), patch(
                "qa_mcp.tools.vision.Path.home", return_value=Path(tmp) / "home"
            ):
                with patch.dict(os.environ, {"VISION_API_KEY": "t-key"}, clear=True):
                    self.assertEqual(
                        vision._load_api_key(vision.PROVIDERS["tokenhub"]), "t-key"
                    )
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(
                        vision._load_api_key(vision.PROVIDERS["tokenhub"]), ""
                    )


class TestInteractiveElicitation(unittest.IsolatedAsyncioTestCase):
    """describe_image interactive=True: 识别粒度单选弹窗与自动降级。"""

    def setUp(self):
        self._ag = patch(
            "qa_mcp.tools.vision._antigravity_credentials_exist", return_value=False
        )
        self._ag.start()

    def tearDown(self):
        self._ag.stop()

    def _make_png(self, tmp: str) -> str:
        p = Path(tmp) / "shot.png"
        p.write_bytes(TINY_PNG)
        return str(p)

    def _ctx_with_elicit(self, result):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        return SimpleNamespace(elicit=AsyncMock(return_value=result))

    async def test_interactive_quick_selects_low_effort(self):
        """用户选"快速" → reasoning_effort 覆盖为 low, 并传入流式调用。"""
        from fastmcp.server.elicitation import AcceptedElicitation

        ctx = self._ctx_with_elicit(AcceptedElicitation(data="quick"))
        with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
            "qa_mcp.tools.vision.OpenAI",
            _make_fake_openai([_FakeChunk(_FakeDelta(content="结果"))]),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(vision, "EVIDENCE_DIR", tmp):
                    result = await vision.describe_image_impl(
                        images=[self._make_png(tmp)],
                        question="图中有什么？",
                        interactive=True,
                        ctx=ctx,
                    )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["reasoning_effort"], "low")
        ctx.elicit.assert_awaited_once()

    async def test_interactive_declined_keeps_auto(self):
        """用户拒绝 → 保持 auto, 不阻塞识别。"""
        from fastmcp.server.elicitation import DeclinedElicitation

        ctx = self._ctx_with_elicit(DeclinedElicitation())
        with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
            "qa_mcp.tools.vision.OpenAI",
            _make_fake_openai([_FakeChunk(_FakeDelta(content="结果"))]),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(vision, "EVIDENCE_DIR", tmp):
                    result = await vision.describe_image_impl(
                        images=[self._make_png(tmp)],
                        question="图中有什么？",
                        interactive=True,
                        ctx=ctx,
                    )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["reasoning_effort"], "medium")  # 拒绝 → 默认 standard

    async def test_interactive_unsupported_client_degrades(self):
        """客户端不支持 elicitation (抛异常) → 自动降级 auto, 识别继续。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        ctx = SimpleNamespace(elicit=AsyncMock(side_effect=RuntimeError("unsupported")))
        with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
            "qa_mcp.tools.vision.OpenAI",
            _make_fake_openai([_FakeChunk(_FakeDelta(content="结果"))]),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(vision, "EVIDENCE_DIR", tmp):
                    result = await vision.describe_image_impl(
                        images=[self._make_png(tmp)],
                        question="图中有什么？",
                        interactive=True,
                        ctx=ctx,
                    )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["reasoning_effort"], "medium")  # 降级 → 默认 standard

    async def test_interactive_false_skips_elicit(self):
        """interactive=False: 不弹窗。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        ctx = SimpleNamespace(elicit=AsyncMock())
        with patch("qa_mcp.tools.vision._load_api_key", return_value="k"), patch(
            "qa_mcp.tools.vision.OpenAI",
            _make_fake_openai([_FakeChunk(_FakeDelta(content="结果"))]),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(vision, "EVIDENCE_DIR", tmp):
                    await vision.describe_image_impl(
                        images=[self._make_png(tmp)],
                        question="图中有什么？",
                        interactive=False,
                        ctx=ctx,
                    )
        ctx.elicit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
