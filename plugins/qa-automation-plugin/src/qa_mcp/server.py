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
from fastmcp.apps.choice import Choice
from fastmcp.apps.form import FormInput
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from pathlib import Path

from fastmcp.server.providers.skills import SkillsDirectoryProvider
from qa_mcp.config import EVIDENCE_DIR, OUTPUT_DIR, TOOL_MAX_EXECUTION_MS
from qa_mcp.providers import BrowserAutomationProvider, VTableAutomationProvider
from qa_mcp.tools.browser import browser_mgr
from qa_mcp.tools.recorder import SESSION_KEY
from qa_mcp.tools.setup import PluginConfigForm, handle_config_form
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
    """全局动作串行锁 + 执行看门狗: 串行化共享 Chrome 页面操作 (并发工具调用
    会撕裂页面上下文), 同时给每次工具调用加上 TOOL_MAX_EXECUTION_MS 硬上限。

    看门狗必要性: Chrome 假死 / CDP 连接半开时, Playwright 协议调用会无限
    等待响应, 动作级 timeout 不会触发; 若无看门狗, 一个卡死的动作会持锁
    把后续所有工具调用 (probe/读表等) 永久堵在串行队列里。超时后强制取消、
    释放队列并后台重置 CDP 连接, 下一次调用可自动重连。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> ToolResult:
        tool_name = getattr(context.message, "name", "N/A")
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    call_next(context), timeout=TOOL_MAX_EXECUTION_MS / 1000
                )
            except asyncio.TimeoutError:
                logger.error(
                    f"工具 [{tool_name}] 执行超过 {TOOL_MAX_EXECUTION_MS}ms, "
                    "强制中断并重置浏览器连接 (释放串行队列)"
                )
                # 后台重置 CDP 连接: 取消的调用可能把连接留在半死状态,
                # 下一次工具调用通过 BrowserManager 重连逻辑恢复。
                asyncio.create_task(_safe_close_browser())
                return ToolResult(
                    content=(
                        f"工具执行超时 (>{TOOL_MAX_EXECUTION_MS}ms), 已强制中断并"
                        "释放串行队列, 浏览器连接已重置。请检查浏览器/页面状态后重试。"
                    ),
                    is_error=True,
                )


async def _safe_close_browser() -> None:
    """看门狗超时后的连接重置 (异常只记录, 不阻塞响应)。

    用 recover() 而非 close(): recover 内部对半开连接的 stop() 有 5s 上限,
    并立即重建全新 CDP 连接; close() 的 stop() 无超时保护, 半开连接上会永久
    挂住并持有 browser_mgr._lock, 把后续所有工具调用堵死在 get_page 队列里
    (现象: 一次卡死动作后, 后续所有调用全部"不执行")。
    """
    try:
        await asyncio.wait_for(browser_mgr.recover(), timeout=10)
    except Exception:
        logger.exception("看门狗重置浏览器连接失败 (忽略)")


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
        # Claude Desktop 交互式 UI (FastMCP Apps): 配置表单 + 选择卡片。
        # Claude Code (TUI) 不渲染 Apps UI, 自动降级走 elicitation 通道
        # (plugin_setup / describe_image interactive=True)。
        FormInput(
            model=PluginConfigForm,
            name="QA 配置",
            title="插件配置",
            tool_name="setup_form",
            submit_text="保存配置",
            on_submit=handle_config_form,
            send_message=True,
        ),
        Choice(name="QA 选择", title="请选择"),
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
