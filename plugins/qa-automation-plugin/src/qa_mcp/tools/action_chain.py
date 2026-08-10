import asyncio
import logging

from fastmcp import Context
from typing import Any, Dict, List, Optional

from qa_mcp.config import ACTION_STEP_TIMEOUT_MS, ELEMENT_WAIT_TIMEOUT_MS
from qa_mcp.tools.browser import (
    browser_mgr,
    _do_click,
    _do_fill,
    _resolve_frame_target,
    _enhance_locator_timeout,
    _wait_visible_or_first,
    retry_ui_action,
    observe_after_click,
    snapshot_navigation,
)
from qa_mcp.utils.ui_adapters import adapter_registry

logger = logging.getLogger("mcp_automation.action_chain")


def _action_key(act: Dict[str, Any]) -> str:
    """动作定位指纹: 相同 (by, selector | role+name) 且相同 iframe 域视为同一尝试。

    iframe_selector 是定位的一部分: 同一 selector 在顶层与 iframe 内是两个
    不同尝试 (规则 0 的 iframe 穿透变体), 不含 iframe 维度会把兜底变体去重吞掉。
    """
    by = str(act.get("by", "css")).lower()
    frame = str(act.get("iframe_selector") or "")
    if by == "role":
        return f"{frame}|role|{act.get('role')}|{act.get('name')}"
    return f"{frame}|{by}|{act.get('selector')}"


def build_action_fallbacks(act: Dict[str, Any]) -> List[Dict[str, Any]]:
    """为动作自动生成降级定位方案 (确定性规则, 低风险, 只产出语义明确的变体)。

    供两条路径使用:
      1) 生成动作链脚本时调用, 预生成降级并写入动作的 fallbacks 字段;
      2) execute_action_chain_impl 执行时自动附加 (显式 fallbacks 之后)。

    规则:
      0. 未指定 iframe_selector: 自动生成「可见 iframe 内定位」变体。业务页
         (analyze 的元素 frame_path 均在 iframe 内) 顶层定位必然失败, 这是
         动作链最常见失败源; 带 iframe 穿透的兜底让链路不再依赖调用方记忆。
      1. antd 下拉选项点击 (click/press + css 含 li 与 [title=]):
         生成全部 nth 位次变体 (nth=0..3, 排除主定位已用的位次): 多 select 的
         dropdown 常驻 DOM (modal 关闭不卸载), 激活层位次不定, 逐个位次尝试。
         执行器对 nth 变体做可见性预检, hidden 层直接跳过 (不耗等待超时)。
         主定位已带 nth 时, 额外生成去 nth 的原 selector (旧层卸载后目标唯一)。
      2. by=role 且附带 selector: 退回 CSS 精确定位 (语义名歧义时)。
      3. by=css/xpath 且附带 role+name: 退回语义定位 (选择器失效/重渲染时)。
    """
    action = str(act.get("action", "")).lower()
    by = str(act.get("by", "css")).lower()
    selector = act.get("selector")
    fallbacks: List[Dict[str, Any]] = []

    # 规则 0: iframe 穿透兜底 (对所有动作生效, 优先级最高 — 多数业务页元素在 iframe 内)
    if not act.get("iframe_selector"):
        fallbacks.append({**act, "iframe_selector": "div[aria-hidden=false] iframe"})

    if action not in ("click", "press"):
        return fallbacks

    if by == "role" and act.get("role") and selector:
        fallbacks.append({**act, "by": "css", "role": None, "name": None})
        return fallbacks

    if by in ("css", "xpath") and selector:
        sel = str(selector)
        if "li" in sel and "[title=" in sel:
            # 主定位已用的 nth 位次不再重复生成
            used = set()
            if ">> nth=" in sel:
                base = sel.split(">> nth=")[0].rstrip()
                if base:
                    fallbacks.append({**act, "selector": base})
                try:
                    used.add(int(sel.split(">> nth=")[1].strip()))
                except ValueError:
                    pass
            else:
                base = sel
            for idx in range(4):
                if idx in used:
                    continue
                fallbacks.append({**act, "selector": f"{base} >> nth={idx}"})
        elif act.get("role") and act.get("name"):
            fallbacks.append({**act, "by": "role", "selector": None})

    return fallbacks


class _OptionNotVisibleError(RuntimeError):
    """下拉选项快速失败异常: 目标下拉未展开/已关闭, li 不存在或不可见。

    抛出后执行器不等待 10s 可见超时, 立即尝试下一定位方案, 全部失败时
    错误信息保留该原因, 引导调用方先展开对应下拉再点击。
    """


async def _precheck_li_option(
    page,
    attempt: Dict[str, Any],
) -> None:
    """下拉选项可见性预检: 对 click/press + css `li[title=...]` 定位生效。

    场景: antd dropdown 关闭时 li 随层卸载 (或隐藏), 若调用方未先展开对应
    下拉就点击选项, 元素不存在, 每个定位方案都会干等 10s visible 超时,
    整条链 30s 看门狗强杀 (观感=卡死)。预检在超时看门狗之外运行, 用独立
    短超时 (5s) 保护 CDP 半开; 元素不存在/不可见时秒级抛 _OptionNotVisibleError,
    不耗等待超时。

    覆盖两层场景:
      1) 主定位 li[title=...] 无 nth: 下拉未展开 → 快速失败 (原逻辑直接等 10s);
      2) nth 变体: antd 常驻 dropdown 中多数层隐藏 → 快速跳过 (替代原 _is_css_nth_visible)。

    预检自身异常 (CDP 半开/解析错误) 时放行, 交由真实定位决定成败,
    不因预检引入新失败。
    """
    action = str(attempt.get("action", "")).lower()
    if action not in ("click", "press"):
        return
    if str(attempt.get("by", "css")).lower() != "css":
        return
    selector = attempt.get("selector")
    if not selector:
        return
    sel = str(selector)
    if "li" not in sel or "[title=" not in sel:
        return
    try:
        target, _ = await asyncio.wait_for(
            _resolve_frame_target(page, attempt.get("iframe_selector")), timeout=5
        )
        locator = target.locator(sel)
        if await locator.count() == 0:
            raise _OptionNotVisibleError(
                f"下拉选项 {sel} 不存在: 对应下拉未展开或已关闭"
            )
        if not await asyncio.wait_for(locator.first.is_visible(), timeout=2):
            raise _OptionNotVisibleError(
                f"下拉选项 {sel} 不可见: 对应下拉未展开或已关闭"
            )
    except _OptionNotVisibleError:
        raise
    except Exception:
        # 预检异常放行: 交由真实定位 (10s 等待 + 重试) 决定成败
        return


async def _do_select_option(
    page,
    by: str,
    selector: Optional[str],
    iframe_selector: Optional[str],
    value: str,
    visualize: Optional[bool],
    description: Optional[str],
) -> dict:
    """下拉选择执行体: 复用 UIAdapterRegistry 的框架适配器 (Ant Design/Element Plus 等)。

    说明: 与 recorder.execute_and_record 的 select_option 走同一适配器机制,
    但为纯执行语义 (不沉淀录制步骤), 故独立实现而非共享 (录制含会话状态写入)。
    """
    framework = await adapter_registry.detect_framework(page)
    adapter = adapter_registry.get_adapter(framework)
    by_lower = (by or "").lower()
    full_selector = f"xpath={selector}" if by_lower == "xpath" else selector
    action_label = description or f"选择下拉: {value}"

    async def _select_once():
        # 每次执行/重试重新解析目标与 locator (同 _do_click 的重试语义);
        # 适配器内部自带下拉轮询, 外层重试覆盖定位段与 trigger 点击竞态。
        target, frame_path_list = await _resolve_frame_target(page, iframe_selector)
        locator = target.locator(full_selector)
        try:
            locator = await _wait_visible_or_first(locator, action_label, ELEMENT_WAIT_TIMEOUT_MS)
        except Exception as e:
            raise await _enhance_locator_timeout(e, locator, action_label) from e
        await locator.scroll_into_view_if_needed()
        # 适配器内部: 点击触发框 → 等待 portal 下拉层 → 按选项文本点击
        await adapter.select_option(target, locator, value)
        return frame_path_list

    frame_path_list = await retry_ui_action(action_label, _select_once)
    return {
        "status": "success",
        "action": "select_option",
        "by": by_lower,
        "selector": full_selector,
        "frame_path": frame_path_list,
        "value": value,
        "ui_framework": framework,
        "description": description,
    }


async def _do_press(
    page,
    by: str,
    selector: Optional[str],
    iframe_selector: Optional[str],
    key: str,
    visualize: Optional[bool],
    description: Optional[str],
) -> dict:
    """按键执行体: 定位后向元素发送键盘按键。"""
    if not key:
        raise ValueError("press 动作必须提供 key")
    by_lower = (by or "").lower()
    full_selector = f"xpath={selector}" if by_lower == "xpath" else selector
    action_label = description or f"按键: {key}"

    async def _press_once():
        target, frame_path_list = await _resolve_frame_target(page, iframe_selector)
        locator = target.locator(full_selector)
        try:
            locator = await _wait_visible_or_first(locator, action_label, ELEMENT_WAIT_TIMEOUT_MS)
        except Exception as e:
            raise await _enhance_locator_timeout(e, locator, action_label) from e
        await locator.press(key)
        return frame_path_list

    frame_path_list = await retry_ui_action(action_label, _press_once)
    return {
        "status": "success",
        "action": "press",
        "by": by_lower,
        "selector": full_selector,
        "frame_path": frame_path_list,
        "key": key,
        "description": description,
    }


async def execute_action_chain_impl(
    actions: List[Dict[str, Any]],
    stop_on_error: bool = True,
    visualize: Optional[bool] = None,
    detail: str = "brief",
    confirm: bool = False,
    ctx: Context = None,
) -> dict:
    """批量动作链: 一次调用顺序执行多个动作, 全部完成后统一观察一次。

    confirm=True 时先弹出交互确认 (elicitation), 用户批准才执行;
    超时未操作默认继续执行 (INTERACT_TIMEOUT_S, 可配置)。
    Desktop 端模型也可改用 request_approval 卡片确认。
    若用户拒绝: 返回 cancelled, 不执行任何动作。

    actions 每项支持:
      click:         {action, by(css/xpath/role/coordinate), selector, iframe_selector, x, y, role, name, click_type, description}
      fill:          {action, by, selector, iframe_selector, value, input_method, clear_first, press_enter, description}
      select_option: {action, by, selector, iframe_selector, value, description} —— 经 UI 适配器 (Ant/ElementPlus) 选择下拉项
      press:         {action, by, selector, iframe_selector, key, description}

    降级 (容错): 每项可选 fallbacks: [{…完整动作参数}] —— 主定位失败时按序尝试的
    备用定位 (生成脚本时配置); 其后自动附加 build_action_fallbacks 生成的兜底变体
    (antd 常驻 dropdown 的 nth 变体 / role↔css 互退), 全部失败才记为该步失败。
    同一 (by, selector|role+name) 的重复尝试自动去重。

    stop_on_error: True(默认)=首步失败即抛 RuntimeError(含已完成步数与尝试的定位方案数);
                   False=收集 failed 继续执行。
    返回: {status, executed, failed, observation} —— observation 为链尾统一观察
          (dynamic_layers/new_layers/summary/focus + navigation), 只做一次, 显著减少往返。
    """
    if not actions:
        raise ValueError("actions 不能为空")
    if detail not in ("brief", "full"):
        raise ValueError(f"detail 仅支持 brief / full, 收到: {detail}")

    # 执行前交互确认 (confirm=True): 展示动作摘要, 用户批准才执行;
    # 超时未操作默认继续 (安全提示: 批量动作可能包含不可逆操作)
    if confirm:
        from qa_mcp.tools.interact import elicit_with_timeout

        summary = "\n".join(
            f"{i + 1}. {a.get('description') or a.get('action')} "
            f"({a.get('selector') or a.get('by') or ''})"
            for i, a in enumerate(actions[:10])
        )
        if len(actions) > 10:
            summary += f"\n... 共 {len(actions)} 步"
        decision = await elicit_with_timeout(
            ctx,
            f"即将执行动作链 ({len(actions)} 步), 是否继续?\n{summary}",
            {"proceed": {"title": "继续执行"}, "abort": {"title": "取消"}},
            default="proceed",  # 超时未操作默认继续
            response_title="动作链确认",
        )
        if decision != "proceed":
            return {
                "status": "cancelled",
                "message": "用户取消了动作链执行, 未执行任何动作。",
            }

    page = await browser_mgr.get_page()
    # 链首统一导航快照 (URL + iframe 清单 + 弹层指纹)
    before = await snapshot_navigation(page)

    async def _run_single(act: Dict[str, Any]) -> None:
        """单个动作执行体: 校验 + 分发到各执行函数。

        每次动作实时从 manager 获取 page (而非闭包捕获): 前序动作被超时强杀并
        recover 重建连接后, 后续动作自动切到新连接, 避免旧 page 引用半开挂死。
        """
        nonlocal page
        page = await browser_mgr.get_page()
        action = str(act.get("action", "")).lower()
        by = str(act.get("by", "css")).lower()
        selector = act.get("selector")
        iframe_selector = act.get("iframe_selector")
        description = act.get("description")
        if action not in ("click", "fill", "select_option", "press"):
            raise ValueError(
                f"action 仅支持 click / fill / select_option / press, 收到: {action}"
            )
        if action == "click":
            await _do_click(
                page, by, selector, iframe_selector,
                x=act.get("x"), y=act.get("y"),
                coordinate_space=str(act.get("coordinate_space", "top")).lower(),
                click_type=str(act.get("click_type", "single")).lower(),
                visualize=visualize, description=description,
                role=act.get("role"), name=act.get("name"),
            )
        elif action == "fill":
            await _do_fill(
                page, by, selector, iframe_selector,
                value=str(act.get("value", "")),
                input_method=str(act.get("input_method", "type")).lower(),
                clear_first=bool(act.get("clear_first", True)),
                press_enter=bool(act.get("press_enter", False)),
                visualize=visualize, description=description,
            )
        elif action == "select_option":
            if not act.get("value"):
                raise ValueError("select_option 动作必须提供 value")
            await _do_select_option(
                page, by, selector, iframe_selector,
                str(act["value"]), visualize, description,
            )
        elif action == "press":
            await _do_press(
                page, by, selector, iframe_selector,
                str(act.get("key", "")), visualize, description,
            )

    executed = 0
    failed: List[Dict[str, Any]] = []
    for i, act in enumerate(actions):
        action = str(act.get("action", "")).lower()
        try:
            # 尝试序列 = 主定位 + 显式 fallbacks + 自动生成兜底 (按定位指纹去重)
            attempts: List[Dict[str, Any]] = [
                act,
                *(act.get("fallbacks") or []),
                *build_action_fallbacks(act),
            ]
            last_err: Optional[Exception] = None
            tried = 0
            seen: set[str] = set()
            for attempt in attempts:
                key = _action_key(attempt)
                if key in seen:
                    continue
                seen.add(key)
                # 下拉选项预检: 对应下拉未展开/已关闭时 li 不存在或不可见,
                # 秒级快速失败并记录原因, 避免每个方案干等 10s 把整条链堵死
                # (30s 看门狗强杀 + CDP 重建, 观感=卡死)。
                try:
                    await _precheck_li_option(page, attempt)
                except _OptionNotVisibleError as e:
                    last_err = e
                    tried += 1
                    continue
                tried += 1
                try:
                    # 单步硬上限: CDP 挂死时 Playwright 动作级 timeout 不生效,
                    # 用外层 wait_for 兜底, 防止一个死动作把整条链堵死。
                    await asyncio.wait_for(
                        _run_single(attempt), timeout=ACTION_STEP_TIMEOUT_MS / 1000
                    )
                    break
                except asyncio.TimeoutError:
                    last_err = RuntimeError(
                        f"动作执行超过 {ACTION_STEP_TIMEOUT_MS}ms 上限, 已强制中断该步"
                    )
                    # 强杀一个 CDP 请求半开的协程后, Playwright 底层可能残留 pending
                    # 协议请求, 后续所有工具调用会排队挂死 (典型症状: 单独调用成功、
                    # 紧随失败动作之后调用超时)。立即重建连接自愈, 不阻断原错误抛出。
                    try:
                        await browser_mgr.recover(preferred_url=page.url)
                        logger.warning("动作超时强杀后已重建 CDP 连接")
                    except Exception as rec_exc:
                        logger.warning(f"动作超时后连接重建失败: {rec_exc}")
                except Exception as e:
                    last_err = e
            else:
                if last_err is None:  # 防御: 全部尝试被去重跳过
                    last_err = RuntimeError("动作缺少有效定位参数")
                raise last_err
            executed += 1
        except Exception as e:
            if stop_on_error:
                raise RuntimeError(
                    f"动作链第 {i + 1} 步 ({action}) 失败, 已完成 {executed} 步, "
                    f"已尝试 {tried} 个定位方案: {e}"
                ) from e
            failed.append({
                "index": i + 1, "action": action,
                "attempts": tried, "error": str(e),
            })

    # 链尾统一观察: 浮层/弹窗/消息提示 + tab 页跳转 + iframe 跳转
    # (重新获取 page: 链内若发生过 recover, 旧 page 引用已失效)
    page = await browser_mgr.get_page()
    observation = await observe_after_click(page, before, detail=detail)
    return {
        "status": "success" if not failed else "partial",
        "executed": executed,
        "failed": failed,
        "observation": observation,
    }
