"""plugin_setup 工具: 交互式表单 (MCP elicitation) 收集环境变量并写入用户级 .env。

Claude Code 原生支持 elicitation 表单 (Form mode); 客户端不支持时自动降级
(返回错误并提示手动编辑)。配置写入 ~/.qa-automation-plugin/.env —— 跨客户端
唯一稳定配置位; 重启客户端后生效 (config 常量在进程启动时求值, 不做动态注入)。
"""

import os
from pathlib import Path
from typing import Literal, Optional

from fastmcp import Context
from fastmcp.server.elicitation import AcceptedElicitation
from pydantic import BaseModel, Field

from qa_mcp.config import CDP_URL, PROJECT_DIR, VISUAL_EFFECTS, user_env_path


class PluginConfigForm(BaseModel):
    """交互式配置表单 (字段 title/description 渲染为表单标签与说明)。

    默认值在模块导入时求值 (与 config 加载的 .env 同步), 重启客户端后自动
    反映最新配置; elicitation 只接受 BaseModel 类 (实例会触发 unhashable)。
    """

    project_dir: str = Field(
        default=PROJECT_DIR,
        title="用户项目根目录",
        description="截图/证据/下载等资产落盘的项目绝对路径 (如 D:\\MyProject)",
    )
    cdp_url: str = Field(
        default=CDP_URL,
        title="Chrome CDP 地址",
        description="Chrome 远程调试端口 (需以 --remote-debugging-port=9222 启动)",
    )
    vision_provider: Literal["auto", "antigravity", "tokenhub", "custom"] = Field(
        default=os.getenv("VISION_PROVIDER", "auto").strip() or "auto",
        title="视觉识别通道",
        description="auto: 已登录 Antigravity 走 antigravity, 否则 tokenhub",
    )
    vision_model: str = Field(
        default=os.getenv("VISION_MODEL", "gemini-3.6-flash").strip()
        or "gemini-3.6-flash",
        title="视觉模型名",
        description="覆盖默认模型 (如 gemini-3.6-flash)",
    )
    download_dir: str = Field(
        default=os.getenv("DOWNLOAD_DIR", "downloads").strip() or "downloads",
        title="下载目录",
        description="相对项目根",
    )
    visual_effects: bool = Field(
        default=VISUAL_EFFECTS, title="鼠标点击高亮", description="点击与定位框可视化"
    )


# 表单字段名 → .env 键名
_CONFIG_KEYS = {
    "project_dir": "PROJECT_DIR",
    "cdp_url": "CDP_URL",
    "vision_provider": "VISION_PROVIDER",
    "vision_model": "VISION_MODEL",
    "download_dir": "DOWNLOAD_DIR",
    "visual_effects": "VISUAL_EFFECTS",
}


def _update_env_file(path: Path, values: dict) -> None:
    """更新 .env: 保留注释与未涉及的行, 更新已知键, 追加新键。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    written: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                written.add(key)
                continue
        out.append(line)
    for key, value in values.items():
        if key not in written:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


async def plugin_setup_impl(ctx: Context = None) -> dict:
    """交互式配置向导: 表单收集环境变量 → 校验 → 写入用户级 .env。

    保存后**重启客户端生效** (config 常量在进程启动时求值, 不做动态注入)。
    取消/拒绝或客户端不支持 elicitation 时不修改任何配置。
    """
    if ctx is None:
        return {
            "status": "error",
            "message": (
                "当前客户端不支持交互式表单。Claude Desktop 请改用 setup_form "
                "工具 (Apps 原生表单); 或手动编辑 "
                f"{user_env_path()} 并重启客户端。"
            ),
        }
    try:
        result = await ctx.elicit(
            "请填写插件环境变量配置 (保存后重启客户端生效)",
            PluginConfigForm,
        )
    except Exception as e:  # noqa: BLE001 — 非交互客户端降级
        return {
            "status": "error",
            "message": (
                f"交互式表单不可用 ({e})。"
                "若当前为 Claude Desktop, 请改用 setup_form 工具 "
                "(Apps 原生表单 UI); "
                f"或手动编辑 {user_env_path()} 并重启客户端。"
            ),
        }
    if not isinstance(result, AcceptedElicitation):
        return {"status": "cancelled", "message": "未修改任何配置"}

    data = result.data
    if hasattr(data, "model_dump"):
        data = data.model_dump()

    try:
        values, target = _validate_and_build_values(data)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    _update_env_file(target, values)
    return {
        "status": "success",
        "message": (
            f"配置已保存到 {target}，请重启客户端生效。"
            "当前已保存: " + ", ".join(f"{k}={v}" for k, v in values.items())
        ),
        "saved": values,
    }


def _validate_and_build_values(data: dict) -> tuple[dict, Path]:
    """校验表单数据并构造 .env 键值 (plugin_setup 与 FormInput 共用)。"""
    project_dir = str(data.get("project_dir", "")).strip().strip('"')
    if not project_dir or not Path(project_dir).is_dir():
        raise ValueError(f"项目根目录不存在或不可访问: {project_dir}")
    values = {}
    for field, key in _CONFIG_KEYS.items():
        value = data.get(field)
        if isinstance(value, bool):
            value = "true" if value else "false"
        values[key] = str(value)
    values["PROJECT_DIR"] = project_dir
    return values, user_env_path()


def handle_config_form(form: PluginConfigForm) -> str:
    """FastMCP FormInput 提交回调 (Claude Desktop Apps 表单 UI)。

    校验后写入用户级 .env, 返回消息 (send_message=True 时推回对话)。
    """
    values, target = _validate_and_build_values(form.model_dump())
    _update_env_file(target, values)
    return (
        f"配置已保存到 {target}，请重启客户端生效。"
        "已保存: " + ", ".join(f"{k}={v}" for k, v in values.items())
    )
