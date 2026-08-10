"""带超时自动继续的 Desktop 交互卡片 (Choice/Approval 定制版) + 配置表单。

在 FastMCP 预置 provider 基础上增加倒计时: 用户超时未操作默认选择继续
(INTERACT_TIMEOUT_S 秒, 工具参数可覆盖)。

语义:
- 用户点击选项/按钮 → decided=True → SetInterval 的 while_ 条件失效,
  定时器停止, 超时动作不触发 (用户操作优先);
- 超时未操作 → onComplete 触发 SendMessage 默认选择回对话 → 模型继续下一步。

Claude Code (TUI) 不渲染 Apps UI, 降级走 elicitation 通道 (见各工具描述)。
"""

from typing import Any, Literal, Optional

from prefab_ui.actions import SetState, ShowToast
from prefab_ui.actions.mcp import CallTool, SendMessage
from prefab_ui.actions.timing import SetInterval
from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Button,
    Card,
    CardContent,
    CardFooter,
    CardHeader,
    Column,
    Form,
    H3,
    Heading,
    Muted,
    Text,
)
from prefab_ui.components.control_flow import If
from prefab_ui.rx import STATE

from fastmcp.apps.app import FastMCPApp

from qa_mcp.config import INTERACT_TIMEOUT_S


class ChoiceWithTimeout(FastMCPApp):
    """选择卡片 + 倒计时: 超时未操作默认选择第一个选项 (或 default_option) 继续。"""

    def __init__(
        self,
        name: str = "QA 选择",
        *,
        title: str = "请选择",
        variant: Literal["default", "outline", "destructive", "success", "info"] = "outline",
    ) -> None:
        super().__init__(name)
        self._title = title
        self._variant = variant
        self._register_tools()

    def _register_tools(self) -> None:
        provider = self

        @self.ui()
        def choose(
            prompt: str,
            options: list[str],
            title: str | None = None,
            default_option: str | None = None,
            timeout_s: int | None = None,
        ) -> PrefabApp:
            """让用户在选项卡片中选择 (带倒计时自动继续)。

            每个选项渲染为可点击按钮; 用户点击后选择结果作为消息回对话
            ("... — I selected: X")。用户超时未操作 (默认 10 秒, timeout_s
            可覆盖) 自动按 default_option 继续 (默认第一个选项)。
            IMPORTANT: 调用后必须停止等待 "I selected:" 或超时默认消息。
            """
            _title = title or provider._title
            limit_ms = (timeout_s if timeout_s is not None else INTERACT_TIMEOUT_S) * 1000
            default = default_option if default_option is not None else (options[0] if options else "")

            with Card(
                css_class="max-w-lg mx-auto",
                on_mount=[
                    # 倒计时: 用户已操作 (decided) 时 while_ 失效, 定时器停止
                    SetInterval(
                        duration=limit_ms,
                        count=1,
                        while_=~STATE.decided,
                        onComplete=[
                            SendMessage(
                                f'"{prompt}" — 超时未操作，默认选择: {default}'
                            ),
                            SetState("decided", True),
                        ],
                    )
                ],
            ) as view:
                with CardHeader():
                    H3(_title)
                with CardContent():
                    Text(prompt, css_class="font-medium")
                with CardFooter():
                    with If(STATE.decided):
                        Muted("Response sent.")
                    with If(~STATE.decided):  # noqa: SIM117
                        with Column(gap=2, css_class="w-full"):
                            for option in options:
                                Button(
                                    option,
                                    variant=provider._variant,
                                    css_class="w-full justify-start",
                                    on_click=[
                                        SendMessage(f'"{prompt}" — I selected: {option}'),
                                        SetState("decided", True),
                                    ],
                                )
                            Muted(
                                f"{limit_ms // 1000}s 未操作将默认选择: {default}",
                                css_class="text-xs",
                            )
            return PrefabApp(view=view, state={"decided": False})


class ApprovalWithTimeout(FastMCPApp):
    """审批卡片 + 倒计时: 超时未操作默认按 default_action 继续 (默认批准)。"""

    def __init__(
        self,
        name: str = "QA 审批",
        *,
        title: str = "操作确认",
        approve_text: str = "继续执行",
        reject_text: str = "取消",
    ) -> None:
        super().__init__(name)
        self._title = title
        self._approve_text = approve_text
        self._reject_text = reject_text
        self._register_tools()

    def _register_tools(self) -> None:
        provider = self

        @self.ui()
        def request_approval(
            summary: str,
            details: str | None = None,
            title: str | None = None,
            approve_text: str | None = None,
            reject_text: str | None = None,
            default_action: Literal["approve", "reject"] = "approve",
            timeout_s: int | None = None,
        ) -> PrefabApp:
            """危险操作执行前的人机确认卡片 (带倒计时自动继续)。

            用户点击按钮后决策作为消息回对话 ("... — I selected: 继续执行/取消")。
            用户超时未操作 (默认 10 秒) 按 default_action 自动继续:
            默认 approve (与 elicitation 侧 execute_action_chain confirm 一致)。
            IMPORTANT: 调用后必须停止等待决策消息。
            """
            _title = title or provider._title
            _approve = approve_text or provider._approve_text
            _reject = reject_text or provider._reject_text
            limit_ms = (timeout_s if timeout_s is not None else INTERACT_TIMEOUT_S) * 1000
            default_label = _approve if default_action == "approve" else _reject

            with Card(
                css_class="max-w-lg mx-auto",
                on_mount=[
                    SetInterval(
                        duration=limit_ms,
                        count=1,
                        while_=~STATE.decided,
                        onComplete=[
                            SendMessage(
                                f'"{summary}" — 超时未操作，默认: {default_label}'
                            ),
                            SetState("decided", True),
                        ],
                    )
                ],
            ) as view:
                with CardHeader():
                    H3(_title)
                with CardContent():
                    Text(summary, css_class="font-medium")
                    if details:
                        Text(details, css_class="text-sm text-muted-foreground")
                with CardFooter():
                    with If(STATE.decided):
                        Muted("Response sent.")
                    with If(~STATE.decided):  # noqa: SIM117
                        with Column(gap=2, css_class="w-full"):
                            Button(
                                _approve,
                                variant="default",
                                css_class="w-full",
                                on_click=[
                                    SendMessage(f'"{summary}" — I selected: {_approve}'),
                                    SetState("decided", True),
                                ],
                            )
                            Button(
                                _reject,
                                variant="outline",
                                css_class="w-full",
                                on_click=[
                                    SendMessage(f'"{summary}" — I selected: {_reject}'),
                                    SetState("decided", True),
                                ],
                            )
                            Muted(
                                f"{limit_ms // 1000}s 未操作将默认: {default_label}",
                                css_class="text-xs",
                            )
                    SetInterval(
                        duration=limit_ms,
                        count=1,
                        while_=~STATE.decided,
                        onComplete=[
                            SendMessage(
                                f'"{summary}" — 超时未操作，默认: {default_label}'
                            ),
                            SetState("decided", True),
                        ],
                    )
            return PrefabApp(view=view, state={"decided": False})


class ConfigFormApp(FastMCPApp):
    """插件环境变量配置表单 (Claude Desktop Apps UI, 可定制工具描述)。

    FormInput 的替代: 支持自定义描述 (引导 Desktop 场景模型使用本工具),
    提交回调复用 handle_config_form (校验 + 写入用户级 .env)。
    """

    def __init__(
        self,
        name: str = "QA 配置",
        *,
        title: str = "插件配置",
        submit_text: str = "保存配置",
    ) -> None:
        super().__init__(name)
        self._title = title
        self._submit_text = submit_text
        self._register_tools()

    def _register_tools(self) -> None:
        provider = self
        model_holder: dict = {}

        @self.tool(model=True)
        def submit_config(data: dict[str, Any] | None = None) -> str:
            """校验并保存插件配置 (表单 UI 提交或模型直接调用)。

            当宿主未渲染 setup_form 表单 UI 时, 模型可先向用户展示当前配置
            (从 ~/.qa-automation-plugin/.env 或表单默认值) 并确认需要修改的
            字段, 然后传完整 data (6 个字段: project_dir / cdp_url /
            vision_provider / vision_model / download_dir / visual_effects)
            直接调用本工具完成配置写入。
            """
            from qa_mcp.tools.setup import PluginConfigForm, handle_config_form

            if data is None:
                data = {}
            validated = PluginConfigForm.model_validate(data)
            return handle_config_form(validated)

        model_holder["submit_config"] = submit_config

        @self.ui(
            name="setup_form",
            description=(
                "插件环境变量配置表单（Claude Desktop Apps UI）：表单预填当前"
                "生效配置（用户项目根目录 / Chrome CDP 地址 / 视觉识别通道 / "
                "视觉模型 / 下载目录 / 鼠标点击高亮），填写并提交后校验并写入"
                "用户级配置 ~/.qa-automation-plugin/.env，重启客户端生效。"
                "若宿主未渲染表单 UI（无可见输入控件），不要等待用户填写——"
                "改为向用户展示当前配置并确认需修改的字段，然后调用 "
                "submit_config 直接提交完整配置。Claude Code 请用 plugin_setup。"
            ),
        )
        def setup_form() -> PrefabApp:
            """打开插件配置表单。"""
            from qa_mcp.tools.setup import PluginConfigForm

            with Column(gap=4, css_class="p-6") as view:
                Heading(provider._title)
                Form.from_model(
                    PluginConfigForm,
                    on_submit=CallTool(
                        submit_config,
                        on_success=[
                            ShowToast("配置已保存，重启客户端生效", variant="success"),
                            SendMessage("插件配置已保存到 ~/.qa-automation-plugin/.env，重启客户端生效。"),
                        ],
                        on_error=ShowToast("保存失败，请检查填写内容", variant="error"),
                    ),
                )
            return PrefabApp(view=view)
