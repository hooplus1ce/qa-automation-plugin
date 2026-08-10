"""交互式 UI 通用辅助: elicitation 带超时 (超时默认继续下一步)。

双轨策略:
- Claude Code / Claude Desktop 均支持 MCP elicitation → 统一用 ctx.elicit
- 用户超时未操作 → asyncio.wait_for 超时 → 按 default 值继续 (不阻塞流程)
- 桌面端 Apps UI (Choice/FormInput/Approval) 由模型按宿主选择, 见各工具描述
"""

import asyncio
import logging
from typing import Any, Optional

from fastmcp import Context

from qa_mcp.config import INTERACT_TIMEOUT_S, INTERACTIVE_UI_ENABLED

logger = logging.getLogger("mcp_automation.interact")


class InteractTimeout(Exception):
    """用户未在超时窗口内操作, 已按默认值继续。"""


async def elicit_with_timeout(
    ctx: Optional[Context],
    message: str,
    response_type: Any = None,
    default: Any = None,
    *,
    timeout_s: Optional[int] = None,
    response_title: Optional[str] = None,
    response_description: Optional[str] = None,
) -> Any:
    """弹出交互对话框并等待用户操作, 超时返回 default (直接进入下一步)。

    返回: 用户接受时的 data (dict/标量); 用户拒绝/取消/客户端不支持/超时
    均返回 default —— 调用方以 default 继续, 保证交互永不阻塞流程。
    """
    from fastmcp.server.elicitation import AcceptedElicitation

    if ctx is None:
        return default
    if not INTERACTIVE_UI_ENABLED:
        # 交互 UI 总开关关闭: 不弹窗, 直接走默认逻辑
        return default
    limit = timeout_s if timeout_s is not None else INTERACT_TIMEOUT_S
    try:
        result = await asyncio.wait_for(
            ctx.elicit(
                message,
                response_type,
                response_title=response_title,
                response_description=response_description,
            ),
            timeout=limit,
        )
    except asyncio.TimeoutError:
        logger.info("交互等待超时 (%ss), 按默认值继续: %r", limit, default)
        return default
    except Exception as e:  # noqa: BLE001 — 客户端不支持 elicitation
        logger.warning("elicitation 不可用, 按默认值继续: %s", e)
        return default
    if isinstance(result, AcceptedElicitation):
        data = result.data
        if hasattr(data, "model_dump"):
            return data.model_dump()
        return data
    return default  # declined / cancelled → 默认继续
