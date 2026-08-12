import asyncio
import logging
from typing import List, Optional
from pydantic import BaseModel, Field
from fastmcp import Context
from qa_mcp.config import ELEMENT_WAIT_TIMEOUT_MS, OBSERVE_WAIT_MS
from qa_mcp.tools.browser import browser_mgr, get_frame_path, _resolve_frame_target
from qa_mcp.utils.dynamic_layers import scan_dynamic_layers
from qa_mcp.utils.ui_adapters import adapter_registry

logger = logging.getLogger("mcp_automation.recorder")

# 录制会话状态键: 经 FastMCP 官方会话态 (Context.set_state/get_state) 存取,
# 取代进程级全局变量 —— 会话态按 MCP session 隔离, HTTP/多会话部署下互不串扰。
SESSION_KEY = "recording_session"

class FlowStep(BaseModel):
    step_number: int = Field(..., description="步骤编号")
    action: str = Field(..., description="动作类型: click, fill, select_option, fill_date")
    locator_type: str = Field(..., description="定位策略")
    locator_value: str = Field(..., description="定位主参数值")
    locator_extra: Optional[str] = Field(default=None, description="可访问名称")
    frame_path: Optional[List[str]] = Field(default_factory=list, description="嵌套 iframe 的链式路径")
    value: Optional[str] = Field(default=None, description="输入或配置的参数")
    description: str = Field(..., description="步骤描述")
    expected_result: Optional[str] = Field(default=None, description="预期结果")

class RecordingSession(BaseModel):
    flow_name: str
    system_under_test: str
    description: str
    steps: List[FlowStep] = []

async def start_recording_impl(
    flow_name: str,
    system_under_test: str,
    description: str,
    ctx: Context,
    interactive: bool = False,
) -> str:
    """开启录制会话。

    interactive=True 时先弹出交互表单收集录制参数 (未提供值的字段),
    超时未操作默认使用当前传入值继续。
    """
    if interactive:
        from pydantic import BaseModel, Field

        from qa_mcp.tools.interact import elicit_with_timeout

        class RecordingForm(BaseModel):
            flow_name: str = Field(..., title="场景名称", description="如 基础配置_新增字典项")
            system_under_test: str = Field(
                ..., title="被测系统", description="如 WMS"
            )
            description: str = Field(..., title="场景描述", description="本次录制内容说明")

        collected = await elicit_with_timeout(
            ctx,
            "请确认录制会话参数 (超时默认使用当前传入值):",
            RecordingForm,
            default=None,
        )
        if collected:
            flow_name = str(collected.get("flow_name") or flow_name)
            system_under_test = str(
                collected.get("system_under_test") or system_under_test
            )
            description = str(collected.get("description") or description)

    await ctx.set_state(
        SESSION_KEY,
        RecordingSession(
            flow_name=flow_name,
            system_under_test=system_under_test,
            description=description,
        ).model_dump(),
    )
    return f"🎬 录制会话已成功开启！当前目标系统为: [{system_under_test}]，场景为: [{flow_name}]"

async def execute_and_record_impl(
    action: str, 
    element_css: str, 
    description: str,
    value: Optional[str] = None, 
    expected_result: Optional[str] = None,
    iframe_selector: Optional[str] = None,
    ctx: Context = None,
) -> dict:
    session_data = await ctx.get_state(SESSION_KEY)
    if not session_data:
        raise RuntimeError("请先调用 start_recording 开启会话！")
    session = RecordingSession.model_validate(session_data)
        
    page = await browser_mgr.get_page()
    framework = await adapter_registry.detect_framework(page)
    adapter = adapter_registry.get_adapter(framework)
    
    target_context, frame_path_list = await _resolve_frame_target(page, iframe_selector)

    locator = target_context.locator(element_css)
    await locator.wait_for(state="visible", timeout=ELEMENT_WAIT_TIMEOUT_MS)
    # 滚动降级: 持续动画页面 (VTable 重绘/antd 动效) 会使 stable 等待超时
    # (Playwright 默认 30s), 此时元素往往已在视口内, 点击/输入内部自带滚动,
    # 无需硬等稳定 —— 与 click_interact 的 _do_click 滚动降级保持一致。
    try:
        await asyncio.wait_for(locator.scroll_into_view_if_needed(), timeout=5)
    except (asyncio.TimeoutError, Exception):
        pass
    
    # 动态语义分析 JS 提纯算法
    semantic_info = await locator.evaluate("""el => {
        const testId = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa');
        if (testId) return { type: 'test_id', value: testId, extra: null };
        
        let labelText = '';
        if (el.id) {
            const labelEl = document.querySelector(`label[for="${el.id}"]`);
            if (labelEl) labelText = labelEl.innerText.trim();
        }
        if (!labelText) {
            const parentLabel = el.closest('label');
            if (parentLabel) labelText = parentLabel.innerText.trim();
        }
        labelText = labelText.replace(/[ \\t\\r\\n]+/g, ' ').trim();
        
        let role = el.getAttribute('role');
        const antComponent = el.closest('.ant-select, .ant-picker, .ant-cascader, .ant-tree-select');
        const ariaLabelledBy = el.getAttribute('aria-labelledby');
        let labelledByText = '';
        if (ariaLabelledBy) {
            labelledByText = ariaLabelledBy
                .split(/\\s+/)
                .map(id => document.getElementById(id)?.innerText || '')
                .join(' ')
                .trim();
        }

        if (!role) {
            const tag = el.tagName.toLowerCase();
            if (tag === 'button') role = 'button';
            else if (tag === 'a') role = 'link';
            else if (tag === 'textarea') role = 'textbox';
            else if (tag === 'select') role = 'combobox';
            else if (tag === 'input') {
                const type = el.type || 'text';
                if (['text', 'search', 'tel', 'url', 'email', 'number'].includes(type)) role = 'textbox';
                else if (type === 'button' || type === 'submit') role = 'button';
            }
        }

        if (!role && antComponent) {
            role = antComponent.matches('.ant-picker') ? 'textbox' : 'combobox';
        }

        if (role) {
            let name = el.getAttribute('aria-label') || labelledByText || el.title || '';
            if (!name && (role === 'button' || role === 'link')) name = el.innerText.trim();
            if (!name && (role === 'textbox' || role === 'combobox')) name = labelText || el.placeholder || '';
            if (!name && antComponent) {
                const antInput = antComponent.querySelector('input[aria-label], input[placeholder], input');
                name = antInput?.getAttribute('aria-label') || antInput?.placeholder || '';
                if (!name && (role === 'textbox' || role === 'combobox')) {
                    name = (antComponent.innerText || '').replace(/[ \\t\\r\\n]+/g, ' ').trim();
                }
            }
            name = name.replace(/[ \\t\\r\\n]+/g, ' ').trim();
            if (name) return { type: 'role', value: role, extra: name };
        }
        
        if (labelText) return { type: 'label', value: labelText, extra: null };
        if (el.placeholder) return { type: 'placeholder', value: el.placeholder, extra: null };
        
        let textVal = el.innerText ? el.innerText.trim() : '';
        if (textVal && textVal.length < 30) {
            return { type: 'text', value: textVal.replace(/[ \\t\\r\\n]+/g, ' '), extra: null };
        }
        
        return { type: 'css', value: 'CSS_FALLBACK', extra: null };
    }""")
    
    if semantic_info["type"] == "css":
        semantic_info["value"] = element_css

    # 执行物理操作与高亮闪烁
    await locator.evaluate("el => { el.style.outline = '3px solid #EF4444'; el.style.transition = 'outline 0.1s'; }")
    
    # 路由动作执行 (显式 timeout: 官方 actionability 智能等待, 持续动画页面
    # 的 stable 等待可能较长, 显式限时避免默认 30s 卡死录制会话)
    _action_timeout = max(ELEMENT_WAIT_TIMEOUT_MS, 5000)
    if action.lower() == "click":
        await locator.click(timeout=_action_timeout)
    elif action.lower() == "fill":
        await locator.fill(value or "", timeout=_action_timeout)
    elif action.lower() == "select_option":
        await adapter.select_option(target_context, locator, value or "")
    elif action.lower() == "fill_date":
        await adapter.fill_date(target_context, locator, value or "")
        
    dynamic_layer_probe = await scan_dynamic_layers(
        page,
        get_frame_path,
        iframe_selector=iframe_selector,
        wait_ms=OBSERVE_WAIT_MS,
    )
    try:
        await locator.evaluate("el => el.style.outline = ''")
    except Exception:
        pass

    # 封装并存储
    step_num = len(session.steps) + 1
    new_step = FlowStep(
        step_number=step_num,
        action=action.lower(),
        locator_type=semantic_info["type"],
        locator_value=semantic_info["value"],
        locator_extra=semantic_info["extra"],
        frame_path=frame_path_list,
        value=value,
        description=description,
        expected_result=expected_result
    )
    session.steps.append(new_step)
    await ctx.set_state(SESSION_KEY, session.model_dump())
    
    return {
        "step_number": step_num,
        "locator_type": semantic_info["type"],
        "locator_value": semantic_info["value"],
        "ui_framework": framework,
        "dynamic_layers": dynamic_layer_probe["layers"],
        "dynamic_layer_count": dynamic_layer_probe["layer_count"],
        "dynamic_layer_probe": dynamic_layer_probe
    }
