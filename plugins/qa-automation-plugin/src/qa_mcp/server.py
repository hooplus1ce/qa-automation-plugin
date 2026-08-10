"""QA 自动化 MCP 服务装配入口 (对齐 FastMCP 3.x 官方架构, gofastmcp.com)。

架构对齐点:
- **Providers**: 工具按领域拆分为官方 ``fastmcp.server.providers.Provider`` 子类,
  经 ``FastMCP(providers=[...])`` 注册 —— 取代旧式 "MCPTool 包装 + 手工注册循环";
- **Middleware**: 基于官方 ``Middleware`` 基类钩子 (``on_call_tool``), 构造时装配;
- **Lifespan**: 官方生命周期模式 (async context manager) 取代已废弃的 on_shutdown
  钩子, 负责关闭时释放 CDP 物理连接;
- **Session state**: 录制会话经 ``Context.set_state/get_state`` 官方会话态存取,
  取代进程级全局变量;
- **元数据**: ``instructions`` / ``version`` 帮助客户端理解服务用途。
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import Context, FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from pathlib import Path

from fastmcp.server.providers.skills import SkillsDirectoryProvider
from qa_mcp.apps_interactive import ApprovalWithTimeout, ChoiceWithTimeout, ConfigFormApp
from qa_mcp.config import (
    EVIDENCE_DIR,
    INTERACTIVE_UI_ENABLED,
    OUTPUT_DIR,
)
from qa_mcp.providers import BrowserAutomationProvider, VTableAutomationProvider
from qa_mcp.tools.browser import browser_mgr
from qa_mcp.tools.recorder import SESSION_KEY
from qa_mcp.utils.excel_render import render_shadcn_excel
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mcp_server")


class ToolLoggingMiddleware(Middleware):
    """工具调用日志中间件: 经 FastMCP Context 官方日志通道发给客户端 (logging/info)。"""

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> ToolResult:
        # tools/call 请求的 message 为 CallToolRequestParams (含 name/arguments)
        tool_name = getattr(context.message, "name", "N/A")

        if context.fastmcp_context and context.fastmcp_context.request_context:
            await context.fastmcp_context.info(f"正在调起底层 UI 驱动: [{tool_name}]...")
        else:
            logger.info(f"正在调起底层 UI 驱动: [{tool_name}]...")

        try:
            return await call_next(context)
        except Exception as e:
            if context.fastmcp_context and context.fastmcp_context.request_context:
                await context.fastmcp_context.error(f"驱动执行异常报错: {str(e)}")
            else:
                logger.error(f"驱动执行异常报错: {str(e)}")
            raise e


class ToolSerializationMiddleware(Middleware):
    """全局动作串行锁: MCP 客户端并发/并行调起多个工具时, 共享的 Chrome
    页面上下文会被并发操作撕裂 (如两个 fill 同时打字、select 与观察交错)。
    串行化后任意时刻只有一个工具在操作页面, 杜绝页面级竞态导致的间歇性失败。
    只读工具同样串行 (成本可忽略, 换取执行确定性)。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> ToolResult:
        async with self._lock:
            return await call_next(context)


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """服务生命周期: 启动即绪; 关闭时释放 CDP 物理连接 (官方 lifespan 模式)。"""
    logger.info("QA 自动化 MCP 服务启动 (transport 就绪)...")
    try:
        yield {}
    finally:
        logger.info("清理并关闭 CDP 会话...")
        await browser_mgr.close()


# 装配: 中间件 (日志在前, 串行锁包住整个工具执行, 日志仍按实际调用记录)
# + 官方 Provider 组件集 (包含领域 Tools Provider 及 Agent Skills Resources Provider)
skills_dir = Path(__file__).resolve().parents[2] / "skills"

mcp = FastMCP(
    "QA Automated Orchestrator",
    instructions=(
        "企业级 Web 系统 (SCM/MOM/WMS/ERP) 自动化测试 MCP 服务: 通过 Playwright CDP "
        "接管本地 Chrome (需以 --remote-debugging-port=9222 启动), 提供页面元素分析、"
        "点击/输入/动作链执行、动态层探查、VTable 场景图交互、用例录制与 Shadcn "
        "风格 Excel 资产导出。所有工具共享同一浏览器页面并已串行化; 录制类工具 "
        "需先调用 start_recording 开启会话, 结束用 export_session 一键落盘。"
    ),
    version="0.2.0",
    middleware=[ToolLoggingMiddleware(), ToolSerializationMiddleware()],
    providers=[
        BrowserAutomationProvider(),
        VTableAutomationProvider(),
        SkillsDirectoryProvider(roots=skills_dir),
        # Claude Desktop 交互式 UI (FastMCP Apps): 配置表单 + 选择/审批卡片。
        # 由 INTERACTIVE_UI_ENABLED 总开关控制: 关闭时不注册 (工具描述不进
        # 上下文, 省 token), 所有 elicitation 交互点直接走默认值。
        # Claude Code (TUI) 不渲染 Apps UI, 自动降级走 elicitation 通道。
        *(
            [
                ConfigFormApp(name="QA 配置", title="插件配置", submit_text="保存配置"),
                ChoiceWithTimeout(name="QA 选择", title="请选择"),
                ApprovalWithTimeout(
                    name="QA 审批",
                    title="操作确认",
                    approve_text="继续执行",
                    reject_text="取消",
                ),
            ]
            if INTERACTIVE_UI_ENABLED
            else []
        ),
    ],
    lifespan=server_lifespan,
)

# 用例一键落盘导出工具 (读取官方会话态中的录制数据, 采用 Shadcn 引擎)
@mcp.tool(name="export_session")
async def export_session(ctx: Context) -> str:
    """
    结束录制会话，一键打包生成高质量的测试资产 (JSON) 及极致精美的 Shadcn 风格 Excel 用例。
    """
    data_dict = await ctx.get_state(SESSION_KEY)
    if not data_dict or not data_dict["steps"]:
        return "❌ 导出失败: 活跃的录制会话中无步骤数据。"

    filename = data_dict["flow_name"].replace(" ", "_")

    # 导出 JSON 证据资产
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    json_path = f"{EVIDENCE_DIR}/{filename}_asset.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data_dict, f, ensure_ascii=False, indent=2)

    # 导出 Excel (Shadcn Slate 双色调极简主题)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    excel_path = f"{OUTPUT_DIR}/{filename}.xlsx"
    render_shadcn_excel(data_dict, excel_path)

    await ctx.delete_state(SESSION_KEY)
    return f"🎉 测试资产导出成功!\n- 资产 JSON: {json_path}\n- Excel 用例: {excel_path}"


if __name__ == "__main__":
    mcp.run()
