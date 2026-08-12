"""Antigravity 视觉模型接入 (参考 oh-my-pi 的 google-antigravity provider 源码)。

授权: Google OAuth2 授权码流 — 打开 Antigravity 授权网页 → 本地回调端口
51121 收 code (或粘贴授权码兜底) → token 交换 → Cloud Code Assist 项目
发现/预置 (loadCodeAssist / onboardUser 拿 projectId) → 凭据持久化
(~/.qa-automation-plugin/antigravity-credentials.json)。

协议: POST {endpoint}/v1internal:streamGenerateContent?alt=sse
(Gemini 原生 generateContent 格式, 非 OpenAI 兼容), 请求携带 antigravity
客户端信封 (sessionId / requestId / labels) 与 userAgent=antigravity。

登录入口: python -m qa_mcp.vision_antigravity login
"""

import base64
import json
import logging
import os
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from qa_mcp.config import PROJECT_DIR

logger = logging.getLogger("mcp_automation.vision.antigravity")

# ==================== OAuth 常量 (与 oh-my-pi google-antigravity.ts 一致) ====================
# 客户端凭证不硬编码提交 (GitHub 推送保护会拦截 OAuth 凭证)。
# 从环境变量读取 (可配置在用户级 ~/.qa-automation-plugin/.env):
#   ANTIGRAVITY_CLIENT_ID / ANTIGRAVITY_CLIENT_SECRET
# 获取方式: Google Cloud Console 创建 OAuth 客户端 (Web 应用,
# 授权回调 http://127.0.0.1:51121/oauth-callback); 或沿用社区公开凭证
# (如 oh-my-pi 所用)。
def _oauth_client_credentials() -> Tuple[str, str]:
    cid = os.getenv("ANTIGRAVITY_CLIENT_ID", "").strip()
    secret = os.getenv("ANTIGRAVITY_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        raise RuntimeError(
            "未配置 ANTIGRAVITY_CLIENT_ID / ANTIGRAVITY_CLIENT_SECRET, "
            "请写入用户级配置 ~/.qa-automation-plugin/.env 后重试"
        )
    return cid, secret


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CLOUD_CODE_ENDPOINT = "https://cloudcode-pa.googleapis.com"
DAILY_ENDPOINT = "https://daily-cloudcode-pa.googleapis.com"
SANDBOX_ENDPOINT = "https://daily-cloudcode-pa.sandbox.googleapis.com"
CALLBACK_PORT = 51121
CALLBACK_PATH = "/oauth-callback"
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]
TIER_LEGACY = "legacy-tier"
PROJECT_ONBOARD_MAX_ATTEMPTS = 5
PROJECT_ONBOARD_INTERVAL_MS = 2.0
LOGIN_TIMEOUT_S = 180

# 端点路由: auto(默认, 带 failover) | production | sandbox | 自定义
ANTIGRAVITY_ENDPOINTS = (DAILY_ENDPOINT, SANDBOX_ENDPOINT)

# 视觉任务输出上限 (pi-catalog wire profile: gemini-3.6-flash maxTokens=65536,
# 后端强制; Claude 系 64000)
MAX_OUTPUT_TOKENS = 65536

# Antigravity 客户端 UA (pi-catalog gemini-headers: antigravity/hub/{version} {os}/{arch})
ANTIGRAVITY_UA = "antigravity/hub/2.1.4 windows/amd64"

# 逻辑模型 ID → effort 路由 wire ID (pi-catalog models.json effortRouting)
ANTIGRAVITY_EFFORT_ROUTING = {
    "gemini-3.6-flash": {
        "minimal": "gemini-3.6-flash-low",
        "low": "gemini-3.6-flash-low",
        "medium": "gemini-3.6-flash-medium",
        "high": "gemini-3.6-flash-high",
    },
}
_EFFORT_SUFFIXES = ("-minimal", "-extra-low", "-low", "-medium", "-high")


def _route_wire_model(model: str, level: str) -> str:
    """逻辑模型 ID → CCA wire ID: 按 thinking level 路由 (无表/已带后缀则透传)。"""
    table = ANTIGRAVITY_EFFORT_ROUTING.get(model)
    if table:
        return table.get(level, table.get("high", model))
    if any(model.endswith(s) for s in _EFFORT_SUFFIXES):
        return model
    return model

THINKING_LEVEL_MAP = {
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "max": "HIGH",
}


@dataclass
class AntigravityCredentials:
    access_token: str
    refresh_token: str
    expires_at: float
    project_id: str


def credentials_path() -> Path:
    override = os.getenv("ANTIGRAVITY_CREDENTIALS_FILE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".qa-automation-plugin" / "antigravity-credentials.json"


def load_credentials() -> Optional[AntigravityCredentials]:
    path = credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AntigravityCredentials(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=float(data["expires_at"]),
            project_id=data["project_id"],
        )
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def save_credentials(creds: AntigravityCredentials) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "access_token": creds.access_token,
                "refresh_token": creds.refresh_token,
                "expires_at": creds.expires_at,
                "project_id": creds.project_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _is_expired(creds: AntigravityCredentials, skew_s: float = 300.0) -> bool:
    return time.time() + skew_s >= creds.expires_at


def refresh_access_token(creds: AntigravityCredentials) -> AntigravityCredentials:
    """用 refresh_token 换新 access_token (过期自动调用)。"""
    cid, secret = _oauth_client_credentials()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Antigravity token 刷新失败: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    new = AntigravityCredentials(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or creds.refresh_token,
        expires_at=time.time() + float(data.get("expires_in", 3600)) - 300.0,
        project_id=creds.project_id,
    )
    save_credentials(new)
    return new


def ensure_valid_credentials() -> AntigravityCredentials:
    """加载凭据, 过期则刷新; 无凭据抛错 (提示先运行登录命令)。"""
    creds = load_credentials()
    if creds is None:
        raise RuntimeError(
            "未找到 Antigravity 凭据。请先运行: python -m qa_mcp.vision_antigravity login"
        )
    if _is_expired(creds):
        return refresh_access_token(creds)
    return creds


# ==================== OAuth 授权码流 ====================
class _CallbackHandler(BaseHTTPRequestHandler):
    code: Optional[str] = None

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_error(404)
            return
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        error = params.get("error", [None])[0]
        if error:
            self._respond(f"授权失败: {error}", 400)
            _CallbackHandler.code = None
        elif code:
            _CallbackHandler.code = code
            self._respond("授权成功! 可以关闭此页面返回终端。", 200)
        else:
            self._respond("缺少 code 参数", 400)

    def _respond(self, text: str, status: int) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102
        pass


def _build_auth_url() -> str:
    cid, _ = _oauth_client_credentials()
    return f"{AUTH_URL}?{urlencode({'client_id': cid, 'redirect_uri': f'http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}', 'response_type': 'code', 'scope': ' '.join(SCOPES), 'access_type': 'offline', 'prompt': 'consent'})}"


def _run_callback_server() -> Optional[str]:
    """本地回调服务器, 等待授权码 (超时 LOGIN_TIMEOUT_S 返回 None)。"""
    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    server.timeout = 1.0
    deadline = time.time() + LOGIN_TIMEOUT_S
    try:
        while time.time() < deadline:
            server.handle_request()
            if _CallbackHandler.code:
                return _CallbackHandler.code
    finally:
        server.server_close()
        _CallbackHandler.code = None
    return None


def _exchange_token(code: str) -> Tuple[str, str, float]:
    cid, secret = _oauth_client_credentials()
    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "code": code,
            "redirect_uri": f"http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH}",
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"token 交换失败: HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return (
        data["access_token"],
        data.get("refresh_token", ""),
        time.time() + float(data.get("expires_in", 3600)) - 300.0,
    )


def _read_project_id(value) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and isinstance(value.get("id"), str) and value["id"]:
        return value["id"]
    return None


def _cca_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": ANTIGRAVITY_UA,
    }


def _discover_project(access_token: str) -> str:
    """Cloud Code Assist 项目发现: loadCodeAssist → 无则 onboardUser 预置。"""
    headers = _cca_headers(access_token)
    load_resp = httpx.post(
        f"{CLOUD_CODE_ENDPOINT}/v1internal:loadCodeAssist",
        headers=headers,
        json={"metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}},
        timeout=30.0,
    )
    if load_resp.status_code != 200:
        raise RuntimeError(
            f"loadCodeAssist 失败: HTTP {load_resp.status_code}: {load_resp.text[:200]}"
        )
    payload = load_resp.json()
    existing = _read_project_id(payload.get("cloudaicompanionProject"))
    if existing:
        return existing

    tiers = payload.get("allowedTiers") or []
    tier_id = TIER_LEGACY
    for tier in tiers:
        if isinstance(tier, dict) and tier.get("isDefault") and isinstance(tier.get("id"), str) and tier["id"]:
            tier_id = tier["id"]
            break

    onboard_body = {
        "tierId": tier_id,
        "metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"},
    }
    for attempt in range(1, PROJECT_ONBOARD_MAX_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(PROJECT_ONBOARD_INTERVAL_MS)
        onboard_resp = httpx.post(
            f"{CLOUD_CODE_ENDPOINT}/v1internal:onboardUser",
            headers=headers,
            json=onboard_body,
            timeout=30.0,
        )
        if onboard_resp.status_code != 200:
            raise RuntimeError(
                f"onboardUser 失败: HTTP {onboard_resp.status_code}: {onboard_resp.text[:200]}"
            )
        operation = onboard_resp.json()
        if operation.get("done"):
            project_id = _read_project_id(
                (operation.get("response") or {}).get("cloudaicompanionProject")
            )
            if project_id:
                return project_id
    raise RuntimeError(
        f"onboardUser 在 {PROJECT_ONBOARD_MAX_ATTEMPTS} 次尝试后未返回 projectId"
    )


def login(paste_fallback: bool = True) -> AntigravityCredentials:
    """交互式登录: 打开 Antigravity 授权网页 → 回调收码 → token → 项目预置 → 持久化。

    paste_fallback=False 时 (MCP 工具场景): 回调超时直接报错, 不做 input() 等待
    (工具进程无终端); CLI 场景保持粘贴授权码兜底。
    """
    url = _build_auth_url()
    print(f"正在打开浏览器进行 Antigravity 授权...\n若浏览器未自动打开, 请手动访问:\n{url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    code = _run_callback_server()
    if code is None:
        if not paste_fallback:
            raise RuntimeError("授权超时 (180s)，请重新调用 vision_login 再试")
        # 兜底: 粘贴授权码 (oh-my-pi pasteCodeFlow 同款)
        code = input("等待回调超时。请粘贴浏览器地址栏中的授权码 (code= 参数值): ").strip()
    if not code:
        raise RuntimeError("未获得授权码, 登录取消")

    access, refresh, expires_at = _exchange_token(code)
    if not refresh:
        raise RuntimeError("授权未返回 refresh_token (需 access_type=offline 同意), 请重试")
    project_id = _discover_project(access)
    creds = AntigravityCredentials(
        access_token=access, refresh_token=refresh, expires_at=expires_at, project_id=project_id
    )
    save_credentials(creds)
    print(f"登录成功! projectId={project_id}, 凭据已保存到 {credentials_path()}")
    return creds


# ==================== CCA 流式调用 (streamGenerateContent) ====================
def _resolve_endpoints(mode: str) -> List[str]:
    mode = (mode or "auto").lower()
    if mode == "sandbox":
        return [SANDBOX_ENDPOINT]
    if mode == "production":
        return [DAILY_ENDPOINT]
    custom = os.getenv("ANTIGRAVITY_ENDPOINT", "").strip()
    if custom:
        if not custom.startswith(("http://", "https://")):
            # 非 URL 的配置值 (如误设成 "auto") 视为默认, 避免把裸值当端点发请求
            logger.warning(f"忽略无效 ANTIGRAVITY_ENDPOINT={custom!r}, 回退默认端点")
            custom = ""
        else:
            return [custom]
    return list(ANTIGRAVITY_ENDPOINTS)


def _parse_sse_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _stream_vision(
    creds: AntigravityCredentials,
    model: str,
    image_urls: List[dict],
    question: str,
    thinking: bool,
    thinking_level: str,
    endpoint_mode: str = "auto",
) -> Tuple[str, str, dict]:
    """CCA streamGenerateContent 流式调用, 返回 (reasoning, content, usage)。

    协议对齐 oh-my-pi: Gemini generateContent 格式 + antigravity 客户端信封
    (requestId/sessionId/labels) + userAgent=antigravity。
    """
    contents = []
    for url_obj in image_urls:
        if url_obj["url"].startswith("data:"):
            header, _, b64 = url_obj["url"].partition(",")
            mime = header[len("data:"):].split(";", 1)[0]
            contents.append(
                {"role": "user", "parts": [{"inlineData": {"mimeType": mime, "data": b64}}]}
            )
        else:
            contents.append({"role": "user", "parts": [{"text": url_obj["url"]}]})
    contents.append({"role": "user", "parts": [{"text": question or "请描述图片中的元素与数据信息内容"}]})

    generation_config: dict = {"maxOutputTokens": MAX_OUTPUT_TOKENS}
    if thinking:
        generation_config["thinkingConfig"] = {"thinkingLevel": THINKING_LEVEL_MAP.get(thinking_level, "MEDIUM")}

    import uuid as _uuid

    wire_model = _route_wire_model(model, thinking_level)
    trajectory_id = str(_uuid.uuid4())
    request_id = f"agent/{_uuid.uuid4()}/{int(time.time() * 1000)}/{trajectory_id}/1"
    body = {
        "project": creds.project_id,
        "requestId": request_id,
        "request": {
            "contents": contents,
            "sessionId": str(_uuid.uuid4()),
            "generationConfig": generation_config,
            "labels": {
                "last_step_index": "0",
                "trajectory_id": trajectory_id,
                "used_claude": "false",
                "used_claude_conservative": "false",
            },
        },
        "model": wire_model,
        "userAgent": "antigravity",
        "requestType": "agent",
    }

    reasoning_parts: List[str] = []
    content_parts: List[str] = []
    usage: dict = {}
    last_error: Optional[Exception] = None

    for endpoint in _resolve_endpoints(endpoint_mode):
        try:
            with httpx.stream(
                "POST",
                f"{endpoint}/v1internal:streamGenerateContent?alt=sse",
                headers=_cca_headers(creds.access_token),
                json=body,
                timeout=httpx.Timeout(connect=10.0, read=120.0, write=60.0, pool=10.0),
            ) as resp:
                if resp.status_code != 200:
                    text = resp.read().decode("utf-8", errors="replace")[:200]
                    raise RuntimeError(f"CCA HTTP {resp.status_code}: {text}")
                for line in resp.iter_lines():
                    event = _parse_sse_line(line)
                    if event is None:
                        continue
                    # CCA 流式事件实际包装在 {"response": {...}} 内 (兼容裸 candidates)
                    response = event.get("response") or event
                    usage = response.get("usageMetadata") or usage
                    candidates = response.get("candidates") or []
                    for cand in candidates:
                        parts = ((cand.get("content") or {}).get("parts")) or []
                        for part in parts:
                            if part.get("thought"):
                                reasoning_parts.append(part.get("text", ""))
                            elif part.get("text"):
                                content_parts.append(part.get("text", ""))
            return "".join(reasoning_parts).strip(), "".join(content_parts).strip(), {
                "prompt_tokens": usage.get("promptTokenCount", 0),
                "completion_tokens": usage.get("candidatesTokenCount", 0),
                "total_tokens": usage.get("totalTokenCount", 0),
            }
        except Exception as e:  # noqa: BLE001 — 端点 failover
            last_error = e
            logger.warning(f"CCA 端点 {endpoint} 失败: {e}, 尝试下一个端点")
    assert last_error is not None
    raise last_error


def _load_api_key() -> str:
    """Antigravity 无 API key, 凭据即认证; 此函数仅为统一调用方接口。"""
    return "antigravity"


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        print(__doc__)
