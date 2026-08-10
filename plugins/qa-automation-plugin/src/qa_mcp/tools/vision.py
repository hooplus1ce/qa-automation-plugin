"""视觉理解降级工具模块 — 参考 oh-my-pi 的 role 设计: 主模型之外提供可配置的
"vision role" (视觉识别模型角色), 支持任意 OpenAI 兼容视觉端点。

provider (VISION_PROVIDER):
- antigravity (推荐): Antigravity 平台 OAuth 授权登录, 默认模型 gemini-3.6-flash
- tokenhub: 腾讯云 TokenHub GLM-5V, 支持 thinking/reasoning_content
- custom: 任意 OpenAI 兼容端点 (VISION_API_BASE + VISION_MODEL + VISION_API_KEY)
- auto (默认): 已登录 Antigravity (凭据文件存在) 走 antigravity, 否则走 tokenhub

可靠性: 请求超时 + 临时错误指数退避重试 + 错误分类;
成本: 同图同问 sha256 内容寻址缓存; 上下文友好: include_reasoning 默认关闭。
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import struct
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import List, Optional, Tuple

from httpx import Timeout
from fastmcp import Context
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from qa_mcp.config import EVIDENCE_DIR, PROJECT_DIR

logger = logging.getLogger("mcp_automation.vision")

MAX_TOKENS = 2048
MAX_IMAGE_BYTES = 50 * 1024 * 1024
SUPPORTED_MIME = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
}
MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}


@dataclass(frozen=True)
class VisionProvider:
    """vision role: 一个可配置的视觉识别模型角色 (参考 oh-my-pi models.yml)。"""

    name: str
    base_url: str
    model: str
    key_env: str
    supports_thinking: bool


TOKENHUB_BASE = "https://tokenhub.tencentmaas.com/v1"
ANTIGRAVITY_DAILY = "https://daily-cloudcode-pa.googleapis.com"

PROVIDERS = {
    "tokenhub": VisionProvider(
        name="tokenhub",
        base_url=TOKENHUB_BASE,
        model="glm-5v-turbo",
        key_env="VISION_API_KEY",
        supports_thinking=True,
    ),
    "antigravity": VisionProvider(
        name="antigravity",
        base_url=ANTIGRAVITY_DAILY,
        model="gemini-3.6-flash",
        key_env="ANTIGRAVITY_CREDENTIALS_FILE",
        supports_thinking=True,
    ),
}


def _select_provider() -> VisionProvider:
    """解析生效的 vision role: VISION_PROVIDER 显式选择 > auto。

    auto 语义: 已登录 Antigravity (凭据文件存在) 走 antigravity (gemini-3.6-flash),
    否则回退 tokenhub (腾讯云 GLM-5V)。
    """
    explicit = os.getenv("VISION_PROVIDER", "").strip().lower()
    if explicit == "custom":
        base = os.getenv("VISION_API_BASE", "").strip()
        model = os.getenv("VISION_MODEL", "").strip()
        if not base or not model:
            raise RuntimeError(
                "VISION_PROVIDER=custom 需要同时配置 VISION_API_BASE 与 VISION_MODEL"
            )
        return VisionProvider(
            name="custom", base_url=base, model=model,
            key_env="VISION_API_KEY", supports_thinking=True,
        )
    if explicit and explicit in PROVIDERS:
        provider = PROVIDERS[explicit]
    elif _antigravity_credentials_exist():
        provider = PROVIDERS["antigravity"]
    else:
        provider = PROVIDERS["tokenhub"]

    model_override = os.getenv("VISION_MODEL", "").strip()
    if model_override:
        provider = replace(provider, model=model_override)
    return provider


def _antigravity_credentials_exist() -> bool:
    """Antigravity 是否已登录 (凭据文件存在; 延迟导入避免循环依赖)。"""
    try:
        from qa_mcp import vision_antigravity as va

        return va.credentials_path().is_file()
    except (ImportError, OSError):
        return False


def _load_api_key(provider: VisionProvider) -> str:
    """读取 provider 对应的 API Key: 环境变量 > 用户项目 .env > 插件 .env。"""
    key_env = provider.key_env
    key = os.environ.get(key_env, "").strip()
    if key:
        return key
    bases: List[Path] = []
    for base in (Path(PROJECT_DIR), Path.cwd(), Path(__file__).resolve().parents[3]):
        if base not in bases:
            bases.append(base)
    for base in bases:
        env_file = base / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{key_env}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

# ==================== 可靠性: 超时 / 重试 / 错误分类 ====================
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
WRITE_TIMEOUT_S = 60.0
POOL_TIMEOUT_S = 10.0
RETRY_ATTEMPTS = 2
RETRY_BACKOFF_S = 1.0
# 流式中途断开/限流/5xx 视为可重试的临时错误
_RETRYABLE_EXC = (APIConnectionError, APITimeoutError, RateLimitError, InternalServerError)

# ==================== 缓存: sha256 内容寻址, 同图同问直接命中 ====================
CACHE_DIR_NAME = ".vision_cache"
CACHE_MAX_FILES = 200
# 粘贴图提取时读取的会话文件尾部窗口 (base64 图片单行可能很大)
SESSION_TAIL_BYTES = 4 * 1024 * 1024


def _resolve_image_url(image_arg: str) -> dict:
    """将图片文件路径或 URL 解析为 Base64 data URI 对象。

    相对路径优先基于用户项目根目录 (PROJECT_DIR, 插件化部署时由客户端注入的
    CLAUDE_PROJECT_DIR 指向用户项目) 解析, 其次回退进程 cwd (本地直跑),
    保证粘贴图片/截图等相对地址在任何部署形态下都能命中。
    """
    if image_arg.startswith(("http://", "https://")):
        return {"url": image_arg}

    expanded = os.path.expanduser(image_arg)
    path = Path(expanded)
    if not path.is_file():
        for base in (Path(PROJECT_DIR), Path.cwd()):
            candidate = base / expanded
            if candidate.is_file():
                path = candidate
                break

    if not path.is_file():
        raise RuntimeError(f"图片文件不存在: {image_arg}")

    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise RuntimeError(f"图片 {image_arg} 超过 50MB 限制")

    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime not in SUPPORTED_MIME and not mime.startswith("image/"):
        raise RuntimeError(f"不支持的图片格式: {path.name}")

    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"url": f"data:{mime};base64,{b64}"}


def _is_remote_url(image_arg: str) -> bool:
    return image_arg.startswith(("http://", "https://"))


def _resolve_local_path(image_arg: str) -> Optional[Path]:
    """仅解析本地图片路径 (远程 URL 返回 None)。与 _resolve_image_url 同一套定位规则。"""
    if _is_remote_url(image_arg):
        return None
    expanded = os.path.expanduser(image_arg)
    path = Path(expanded)
    if not path.is_file():
        for base in (Path(PROJECT_DIR), Path.cwd()):
            candidate = base / expanded
            if candidate.is_file():
                path = candidate
                break
    return path if path.is_file() else None


def _image_dimensions(path: Path) -> Optional[dict]:
    """轻量解析图片像素尺寸 (PNG/GIF/JPEG/BMP), 不引入 PIL。其余格式返回 None。"""
    try:
        data = path.read_bytes()[: 64 * 1024]
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return {"width": w, "height": h}
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return {"width": w, "height": h}
    if data[:2] == b"BM" and len(data) >= 26:
        w, h = struct.unpack("<ii", data[18:26])
        return {"width": abs(w), "height": abs(h)}
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            ):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return {"width": w, "height": h}
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
            else:
                seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg_len
    return None


def _tail_lines(path: Path, max_bytes: int = SESSION_TAIL_BYTES) -> List[str]:
    """读文件尾部窗口的行列表 (避免全量读入大会话 jsonl)。"""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        fh.seek(max(0, size - max_bytes))
        data = fh.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 窗口起点可能切破一行, 丢弃首行
    return lines


def _session_jsonl_candidates() -> List[Path]:
    """收集候选会话 jsonl 文件 (Claude Code, 按最近修改时间优先)。

    会话目录名由用户项目路径 (PROJECT_DIR) 生成; 插件化部署时进程 cwd 是
    插件目录, 不能用作会话定位依据。Claude Desktop 的会话记录延迟写盘且
    不注入项目信息, 粘贴图提取不覆盖 (使用 PROJECT_DIR 显式配置)。
    """
    project_parts = Path(PROJECT_DIR).resolve().parts
    drive = project_parts[0][0]
    rest_path = "-".join(project_parts[1:])

    candidates: List[Path] = []
    projects_base = Path.home() / ".claude" / "projects"
    for d_prefix in [drive.lower(), drive.upper()]:
        session_dir = projects_base / f"{d_prefix}--{rest_path}"
        if session_dir.is_dir():
            candidates.extend(session_dir.glob("*.jsonl"))

    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def _extract_latest_pasted_image(index: int = 0) -> Optional[Path]:
    """从 Claude Code 会话记录 jsonl (尾部窗口) 提取粘贴的图片并保存到 EVIDENCE_DIR。

    index=0 取最近一张, 1 取次近一张, 依此类推; 越界返回 None。
    落盘 evidence_assets/pasted/{时间戳}_{index}.{ext}, 提取时清理旧文件
    (粘贴图属临时输入, 不长期保留)。
    """
    session_files = _session_jsonl_candidates()
    if not session_files:
        return None

    found: List[dict] = []  # (media_type, data)
    for s_file in session_files:
        try:
            lines = _tail_lines(s_file)
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line or '"type":"image"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "user":
                continue
            content = (rec.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "image":
                    src = blk.get("source") or {}
                    if src.get("type") == "base64" and src.get("data"):
                        found.append((src.get("media_type", "image/png"), src["data"]))
            if len(found) > index:
                break
        if len(found) > index:
            break

    if index >= len(found):
        return None

    media_type, data = found[index]
    ext = MIME_EXT.get(media_type, "png")
    pasted_dir = Path(EVIDENCE_DIR) / "pasted"
    pasted_dir.mkdir(parents=True, exist_ok=True)
    # 粘贴图属临时输入: 每次提取清理旧文件, 目录只保留本批
    for old in pasted_dir.glob("*"):
        try:
            old.unlink()
        except OSError:
            pass
    out_path = pasted_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{index}.{ext}"
    out_path.write_bytes(base64.b64decode(data))
    return out_path


def _resolve_effort(question: str, reasoning_effort: str) -> str:
    """auto 模式: 按问题长度自适应思考深度 (短问答降级, 省时省钱省上下文)。"""
    if reasoning_effort != "auto":
        return reasoning_effort
    n = len(question or "")
    if n <= 20:
        return "low"
    if n <= 60:
        return "medium"
    return "high"


def _classify_error(exc: Exception, provider: VisionProvider) -> dict:
    """把上游/网络异常分类为简短可操作消息, 避免原始长文本污染上下文。"""
    if isinstance(exc, AuthenticationError):
        return {
            "error_type": "auth_failed",
            "message": f"视觉识别鉴权失败({provider.name}): 请检查 {provider.key_env} 是否正确有效",
        }
    if isinstance(exc, RateLimitError):
        return {"error_type": "rate_limited", "message": f"视觉识别限流(429, {provider.name}): 请稍后重试"}
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return {
            "error_type": "network",
            "message": f"视觉识别网络/超时({provider.name}): {str(exc)[:140]}",
        }
    if isinstance(exc, InternalServerError):
        return {"error_type": "upstream", "message": f"视觉识别服务异常(5xx, {provider.name}): 请稍后重试"}
    if isinstance(exc, BadRequestError):
        return {
            "error_type": "bad_request",
            "message": f"视觉识别请求被拒(图片/参数非法, {provider.name}): {str(exc)[:140]}",
        }
    return {"error_type": "unknown", "message": f"视觉识别失败: {str(exc)[:200]}"}


def _stream_vision_completion(
    provider: VisionProvider,
    api_key: str,
    image_urls: List[dict],
    question: str,
    thinking: bool,
    reasoning_effort: str,
) -> Tuple[str, str, Optional[dict]]:
    """同步执行视觉模型流式理解 (在独立线程中运行)。

    返回 (reasoning, content, usage)。超时有界 (连接/读), 限流/5xx/网络断开
    按指数退避重试; 流式 usage 经 stream_options.include_usage 收集。
    thinking 仅在 provider.supports_thinking 时透传 (Gemini 兼容端点不接受
    thinking extra_body, 其思考由模型自身控制)。
    """
    client = OpenAI(
        api_key=api_key,
        base_url=provider.base_url,
        timeout=Timeout(
            connect=CONNECT_TIMEOUT_S,
            read=READ_TIMEOUT_S,
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        ),
    )

    user_content = [
        {"type": "image_url", "image_url": url_obj} for url_obj in image_urls
    ]
    if question:
        user_content.append({"type": "text", "text": question})

    messages = [
        {"role": "system", "content": "你是多模态视觉助手，请基于图片内容准确回答用户的问题。"},
        {"role": "user", "content": user_content},
    ]

    extra_body = {}
    if thinking and provider.supports_thinking:
        extra_body["thinking"] = {"type": "enabled"}
        extra_body["reasoning_effort"] = reasoning_effort

    last_exc: Optional[Exception] = None
    for attempt in range(RETRY_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=provider.model,
                messages=messages,
                max_tokens=MAX_TOKENS,
                stream=True,
                stream_options={"include_usage": True},
                extra_body=extra_body,
            )
            reasoning_parts: List[str] = []
            content_parts: List[str] = []
            usage: Optional[dict] = None
            for chunk in response:
                if not chunk.choices:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = {
                            "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0),
                            "completion_tokens": getattr(
                                chunk_usage, "completion_tokens", 0
                            ),
                            "total_tokens": getattr(chunk_usage, "total_tokens", 0),
                        }
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_parts.append(reasoning)
                content = getattr(delta, "content", None)
                if content:
                    content_parts.append(content)
            return "".join(reasoning_parts).strip(), "".join(content_parts).strip(), usage
        except _RETRYABLE_EXC as e:
            last_exc = e
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


# ==================== 缓存: sha256 内容寻址 ====================
def _cache_dir() -> Path:
    return Path(EVIDENCE_DIR) / CACHE_DIR_NAME


def _cache_key(
    image_paths: List[Path], question: str, thinking: bool, effort: str,
    include_reasoning: bool, provider_name: str,
) -> str:
    hasher = hashlib.sha256()
    for p in image_paths:
        hasher.update(p.read_bytes())
    img_digest = hasher.hexdigest()[:16]
    q_digest = hashlib.sha256(
        f"{provider_name}|{question}|{thinking}|{effort}|{include_reasoning}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{img_digest}_{q_digest}.json"


def _load_cache(key: str) -> Optional[dict]:
    try:
        path = _cache_dir() / key
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_cache(key: str, result: dict) -> None:
    try:
        cache_dir = _cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        # 上限保护: 超过 CACHE_MAX_FILES 按修改时间清理最旧一半
        files = sorted(cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if len(files) >= CACHE_MAX_FILES:
            for old in files[: CACHE_MAX_FILES // 2]:
                try:
                    old.unlink()
                except OSError:
                    pass
        (cache_dir / key).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"写入视觉缓存失败: {e}")


async def _describe_image_antigravity(
    provider: VisionProvider,
    images: Optional[List[str]],
    question: str,
    thinking: bool,
    reasoning_effort: str,
    extract_pasted: bool,
    pasted_index: int,
    include_reasoning: bool,
) -> dict:
    """Antigravity 分支: OAuth 凭据 + CCA streamGenerateContent (非 OpenAI 兼容)。

    认证 = 登录命令产出的凭据文件 (python -m qa_mcp.vision_antigravity login);
    无凭据/过期刷新失败时返回可操作错误, 提示先登录。
    """
    from qa_mcp import vision_antigravity as va

    try:
        creds = va.ensure_valid_credentials()
    except RuntimeError as e:
        return {
            "status": "error",
            "error_type": "missing_key",
            "message": (
                f"{e}。请先调用 vision_login 工具完成 Antigravity 授权"
                "(会打开浏览器授权网页), 授权成功后再重试本工具。"
            ),
        }

    target_images: List[str] = list(images) if images else []

    if extract_pasted or not target_images:
        pasted_path = _extract_latest_pasted_image(index=pasted_index)
        if pasted_path:
            target_images.append(str(pasted_path))

    if not target_images:
        return {
            "status": "error",
            "error_type": "no_images",
            "message": "未指定图片路径/URL，且未找到会话中提取的粘贴图片。",
        }

    try:
        effective_effort = _resolve_effort(question, reasoning_effort)

        local_paths = [p for p in (_resolve_local_path(img) for img in target_images) if p]
        cache_key = (
            _cache_key(
                local_paths, question, thinking, effective_effort,
                include_reasoning, provider.name,
            )
            if local_paths and len(local_paths) == len(target_images)
            else None
        )
        if cache_key:
            cached = _load_cache(cache_key)
            if cached is not None:
                cached["cached"] = True
                return cached

        image_urls = [_resolve_image_url(img) for img in target_images]

        reasoning, content, usage = await asyncio.to_thread(
            va._stream_vision,
            creds,
            provider.model,
            image_urls,
            question,
            thinking,
            effective_effort,
        )

        result: dict = {
            "status": "success",
            "images_processed": target_images,
            "image_details": [
                {
                    "path": str(p),
                    "size_bytes": p.stat().st_size,
                    "dimensions": _image_dimensions(p),
                }
                for p in local_paths
            ],
            "question": question,
            "provider": provider.name,
            "provider_base": provider.base_url,
            "model": provider.model,
            "thinking": thinking,
            "reasoning_effort": effective_effort,
            "description": content,
            "usage": usage or {},
            "cached": False,
        }
        if include_reasoning:
            result["reasoning"] = reasoning

        if cache_key:
            _save_cache(cache_key, result)
        return result

    except Exception as e:
        logger.error(f"视觉识别异常 ({provider.name}): {e}")
        return {"status": "error", **_classify_error(e, provider)}


_EFFORT_BY_GRANULARITY = {"quick": "low", "standard": "medium", "deep": "high"}


async def vision_login_impl() -> dict:
    """Antigravity 视觉通道 OAuth 授权登录 (MCP 工具)。

    打开浏览器授权网页 → 等待用户完成授权 (本地回调) → token 交换 → 项目预置
    → 凭据持久化 (~/.qa-automation-plugin/)。describe_image 报错提示未找到
    凭据时, 调用本工具完成授权后重试即可; 已登录时直接返回成功。
    """
    from qa_mcp import vision_antigravity as va

    try:
        creds = await asyncio.to_thread(va.login, False)
        return {
            "status": "success",
            "message": (
                f"Antigravity 登录成功 (projectId={creds.project_id}), "
                "凭据已保存, 现在可以调用 describe_image 识别图片了。"
            ),
        }
    except RuntimeError as e:
        return {"status": "error", "message": f"{e}"}


async def describe_image_impl(
    images: Optional[List[str]] = None,
    question: str = "请描述图片中的元素与数据信息内容",
    thinking: bool = True,
    reasoning_effort: str = "auto",
    extract_pasted: bool = False,
    pasted_index: int = 0,
    include_reasoning: bool = False,
    interactive: bool = False,
    ctx: Context = None,
) -> dict:
    """调用视觉模型 (vision role) 对图片进行流式理解, provider 由 VISION_PROVIDER 决定。

    thinking=True (默认): 开启深度思考 (仅支持思考透传的 provider);
    reasoning_effort 控制思考深度 (auto/low/medium/high/max), auto 按问题长度自适应。
    interactive=True: 调用前弹出交互对话框 (MCP elicitation) 让用户选择识别粒度
    (快速/标准/深度), 选择结果覆盖 reasoning_effort; 用户取消或客户端不支持
    elicitation 时自动降级为 auto, 不阻塞流程。
    include_reasoning=False (默认): 不返回 reasoning 思考过程, 节省主模型上下文;
    诊断需要时置 True 返回完整思考链。
    同图同问结果按 sha256 内容寻址缓存 (本地图片), 命中直接返回 cached=True,
    不重复调用 API。
    """
    if interactive and ctx is not None:
        from qa_mcp.tools.interact import elicit_with_timeout

        override = await elicit_with_timeout(
            ctx,
            "请选择图片识别粒度 (超时默认标准):",
            {
                "quick": {"title": "快速", "description": "低思考深度：适合 OCR、颜色、按钮状态等简单判断"},
                "standard": {"title": "标准", "description": "中等思考深度：默认选项"},
                "deep": {"title": "深度", "description": "高思考深度：适合复杂场景、表格数据理解"},
            },
            default="standard",
            response_title="识别粒度",
        )
        if override in _EFFORT_BY_GRANULARITY:
            reasoning_effort = _EFFORT_BY_GRANULARITY[override]

    try:
        provider = _select_provider()
    except RuntimeError as e:
        return {"status": "error", "error_type": "bad_config", "message": str(e)}

    if provider.name == "antigravity":
        return await _describe_image_antigravity(
            provider,
            images,
            question,
            thinking,
            reasoning_effort,
            extract_pasted,
            pasted_index,
            include_reasoning,
        )

    api_key = _load_api_key(provider)
    if not api_key:
        return {
            "status": "error",
            "error_type": "missing_key",
            "message": (
                f"未配置 {provider.key_env} 环境变量 (vision provider={provider.name}), "
                "无法调起视觉识别接口。"
            ),
        }

    target_images: List[str] = list(images) if images else []

    if extract_pasted or not target_images:
        pasted_path = _extract_latest_pasted_image(index=pasted_index)
        if pasted_path:
            target_images.append(str(pasted_path))

    if not target_images:
        return {
            "status": "error",
            "error_type": "no_images",
            "message": "未指定图片路径/URL，且未找到会话中提取的粘贴图片。",
        }

    try:
        effective_effort = _resolve_effort(question, reasoning_effort)

        # 本地图 (非 URL) 参与缓存键; 全部为本地图时才启用缓存
        local_paths = [p for p in (_resolve_local_path(img) for img in target_images) if p]
        cache_key = (
            _cache_key(
                local_paths, question, thinking, effective_effort,
                include_reasoning, provider.name,
            )
            if local_paths and len(local_paths) == len(target_images)
            else None
        )
        if cache_key:
            cached = _load_cache(cache_key)
            if cached is not None:
                cached["cached"] = True
                return cached

        image_urls = [_resolve_image_url(img) for img in target_images]

        reasoning, content, usage = await asyncio.to_thread(
            _stream_vision_completion,
            provider,
            api_key,
            image_urls,
            question,
            thinking,
            effective_effort,
        )

        result: dict = {
            "status": "success",
            "images_processed": target_images,
            "image_details": [
                {
                    "path": str(p),
                    "size_bytes": p.stat().st_size,
                    "dimensions": _image_dimensions(p),
                }
                for p in local_paths
            ],
            "question": question,
            "provider": provider.name,
            "provider_base": provider.base_url,
            "model": provider.model,
            "thinking": thinking,
            "reasoning_effort": effective_effort,
            "description": content,
            "usage": usage or {},
            "cached": False,
        }
        if include_reasoning:
            result["reasoning"] = reasoning

        if cache_key:
            _save_cache(cache_key, result)
        return result

    except Exception as e:
        logger.error(f"视觉识别异常 ({provider.name}): {e}")
        return {"status": "error", **_classify_error(e, provider)}
