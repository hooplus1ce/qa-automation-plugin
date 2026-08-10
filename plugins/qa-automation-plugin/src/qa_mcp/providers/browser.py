"""浏览器自动化工具 Provider 扩展 (FastMCP 3.x/4.0 规范)。"""

from collections.abc import Sequence

from fastmcp.server.providers import Provider
from fastmcp.tools import Tool

from qa_mcp.config import INTERACTIVE_UI_ENABLED
from qa_mcp.tools.action_chain import execute_action_chain_impl
from qa_mcp.tools.browser import (
    analyze_elements_impl,
    capture_screenshot_impl,
    click_interact_impl,
    download_file_impl,
    fill_input_impl,
    hover_interact_impl,
    probe_dynamic_layers_impl,
    switch_target_page_impl,
    upload_file_impl,
    wait_for_condition_impl,
)
from qa_mcp.tools.recorder import execute_and_record_impl, start_recording_impl
from qa_mcp.tools.setup import plugin_setup_impl
from qa_mcp.tools.vision import describe_image_impl, vision_login_impl

class BrowserAutomationProvider(Provider):
    """浏览器自动化工具集: 页面分析 / 点击 / 输入 / 截图 / 用例录制 / 条件等待。"""

    def __init__(self) -> None:
        super().__init__()
        self._tools: list[Tool] = [
            Tool.from_function(
                analyze_elements_impl,
                name="analyze_current_page",
                description="分析当前 Chrome 页面。递归提取包括主文档和嵌套 iframe 内的所有可见交互元素并生成定位器。",
            ),
            Tool.from_function(
                start_recording_impl,
                name="start_recording",
                description="初始化一个新的自动化测试用例录制会话。",
            ),
            Tool.from_function(
                execute_and_record_impl,
                name="execute_and_record",
                description="利用 UI 组件适配器智能执行输入/点击等动作，并实时将最优高韧性语义定位步骤记录到用例中。",
            ),
            Tool.from_function(
                probe_dynamic_layers_impl,
                name="probe_dynamic_layers",
                description="探查当前页面或指定 iframe 中交互后出现的可见弹窗、消息气泡、下拉/日期/级联悬浮层，并返回内部文本、属性、HTML 和可交互元素。detail=brief(默认, 剪枝输出)|full(完整 html/文本)。",
            ),
            Tool.from_function(
                click_interact_impl,
                name="click_interact",
                description="统一点击工具：by=css/xpath 传 selector（支持 iframe_selector 链式穿透），by=role 传 role+name（get_by_role 语义定位，适合 Portal 弹层），by=coordinate 传 x/y（coordinate_space=top 为顶层视口坐标，可点 VTable 内部）。click_type=single/double；detail=brief/full 控制观察输出体积。visualize 三态（None=跟随配置，默认关）。返回 visual_effects + observation（dynamic_layers/new_layers 浮层弹窗消息 + summary 摘要 + focus 域隔离 + navigation 的 URL/iframe 跳转对比）。",
            ),
            Tool.from_function(
                fill_input_impl,
                name="fill_input",
                description="文本框输入工具：by=css/xpath + selector（支持 iframe_selector 穿透）；value 为空=清空。input_method=type（逐字键盘，触发键盘事件）/fill（原生填充）；clear_first 默认清空；press_enter 可回车；detail=brief/full。visualize 三态（None=跟随配置，默认关）。返回 visual_effects + observation（浮层/消息 + summary + focus + 跳转）。",
            ),
            Tool.from_function(
                hover_interact_impl,
                name="hover_interact",
                description="通用悬停工具：将鼠标移动到目标元素中心并停留（默认 500ms），触发 CSS :hover 效果（如 antd Select 的 clear 清空图标、tooltip、下拉箭头翻转），随后统一观察浮层/消息/跳转。by=css/xpath 传 selector（支持 iframe_selector 链式穿透），by=role 传 role+name；hold_ms 可调停留时长。悬停后自动返回 revealed_elements：目标元素内 hover 态新出现的可见子元素（clear 图标等）的顶层视口坐标 topX/topY 与相对路径 relPath，可直接用 click_interact(by=coordinate, x=topX, y=topY) 点击，无需截图推断坐标。典型用法：① hover_interact 悬停到 select 本体 → 取返回的 clear 图标 topX/topY ② click_interact by=coordinate 点击清空值。detail=brief/full 控制观察输出体积。",
            ),
            Tool.from_function(
                capture_screenshot_impl,
                name="capture_screenshot",
                description="截取当前页面截图：用 CDP 采集（不会卡在字体加载上），保存到 evidence_assets/ 并返回内联 PNG 图片（支持图片的客户端可直接查看）。filename 可指定输出文件名（默认时间戳，自动补 .png；支持子目录路径如 模块名/文件名.png，自动创建目录，便于按模块组织证据资产）；full_page=True 截整页（含滚动区外内容），False(默认) 只截当前视口。返回文本摘要（文件路径/尺寸/字节数）+ 图片内容。用于视觉证据、页面状态留档、UI 断言前的实况确认。",
            ),
            Tool.from_function(
                describe_image_impl,
                name="describe_image",
                description="仅当当前主模型为纯文本模型（如 DeepSeek-V4、DeepSeek-R1、GLM 纯文本版等）无法识别图片时使用的降级视觉识别工具，实现主模型 + 视觉模型的角色分工（vision role）。将本地图片路径（绝对路径或相对用户项目根目录的相对路径）、公网 URL 或对话框粘贴的图片发送给视觉模型流式解析。视觉模型由 VISION_PROVIDER 配置（auto/tokenhub/antigravity/custom，默认 auto：已登录 Antigravity 走 gemini-3.6-flash，否则腾讯云 TokenHub GLM-5V；VISION_MODEL 可覆盖模型名；antigravity 通道经 OAuth 授权网页登录、凭据自动刷新，首次使用报错时按提示运行 python -m qa_mcp.vision_antigravity login）。thinking=True（默认）开启深度思考（provider 支持时），reasoning_effort 控制思考深度（auto 按问题长度自适应，默认；可选 low/medium/high/max）；interactive=True 时先弹出交互对话框让用户选择识别粒度（快速/标准/深度），适合交互式会话；include_reasoning=False（默认）不返回思考过程以节省主模型上下文，诊断需要时置 True。同图同问结果按图片内容哈希缓存（cached=True 表示命中，不重复调用 API）；失败返回 error_type 分类（missing_key/auth_failed/rate_limited/network/upstream/bad_request）与简短可操作消息。若当前主模型本身具备原生多模态视觉能力（如 GPT-4o、Claude 3.5/3.7 Sonnet、Gemini 系列、Qwen2.5-VL 等），绝对禁止调用本工具，必须由主模型直接看图。",
            ),
            *(
                [
                    Tool.from_function(
                        plugin_setup_impl,
                        name="plugin_setup",
                        description="插件交互式配置向导（MCP elicitation 表单）：弹出表单收集环境变量（用户项目根目录 / Chrome CDP 地址 / 视觉识别通道 / 视觉模型 / 下载目录 / 鼠标高亮，预填当前生效值），校验后写入用户级配置 ~/.qa-automation-plugin/.env，重启客户端后生效。用户明确要求配置插件环境变量、或工具报错提示配置缺失时调用本工具。适用 Claude Code；若客户端返回『不支持表单』错误（如 Claude Desktop 不支持 elicitation 多字段表单），应改用 setup_form 工具（Apps 原生表单 UI）完成同样配置；取消/拒绝或客户端不支持时不会修改任何配置。",
                    )
                ]
                if INTERACTIVE_UI_ENABLED
                else []
            ),
            Tool.from_function(
                vision_login_impl,
                name="vision_login",
                description="Antigravity 视觉通道 OAuth 授权登录（对话内完成）：调用后打开浏览器授权网页，等待用户完成授权（本地回调，约 180s 超时）后返回登录状态与 projectId。当 describe_image 返回 missing_key 错误提示未找到 Antigravity 凭据时，调用本工具完成授权；已登录时直接返回成功。授权一次长期有效（token 自动刷新），无需重复登录。",
            ),
            Tool.from_function(
                switch_target_page_impl,
                name="switch_target_page",
                description="显式切换/重绑 MCP 操作目标标签页（按 URL 子串匹配）并锁定。默认机制：首次调用自动锁定一个标签页，后续操作固定作用于该页，不受新开/切换标签页影响；测试页被误关或需操作另一系统页面时用本工具重绑。返回新目标页 URL/标题。",
            ),
            Tool.from_function(
                execute_action_chain_impl,
                name="execute_action_chain",
                description="批量动作链：一次调用顺序执行 click/fill/select_option/press 多个动作，最后统一观察一次并返回 observation（浮层/消息/跳转）。actions 每项 {action, by, selector, iframe_selector, value, click_type, input_method, clear_first, press_enter, key, description}；stop_on_error=True 遇错即停（默认），False 收集 failed 继续。用于减少 Agent 往返。降级容错：每项可选 fallbacks: [{完整动作参数}] 配置备用定位，主定位失败时按序尝试；执行器还会自动附加兜底变体（antd 常驻 dropdown 的 li[title=...] 选项自动补/去 >> nth=N 变体、role↔css 互退），全部失败才中断，错误信息含已尝试的定位方案数。生成脚本时为易歧义动作（下拉选项点击、弹层按钮）配置 fallbacks 可显著提高整链成功率。",
            ),
            Tool.from_function(
                wait_for_condition_impl,
                name="wait_for_condition",
                description="等待页面条件成立（轮询），超时返回最后一次状态快照、不抛错。condition：element_visible(selector 可见,默认)/element_hidden(selector 不可见或不存在)/element_has_text(selector 可见文本包含 expected_text,exact=True 精确相等)/text_present(目标 iframe 或全部 frame 页面文本出现 expected_text)/url_contains(URL 包含 expected_text)。典型用法：提交表单后 wait_for_condition(condition='text_present', expected_text='新增成功', timeout_ms=10000) 等成功消息出现，再断言收尾。",
            ),
            Tool.from_function(
                download_file_impl,
                name="download_file",
                description="点击触发下载的按钮/链接，将下载文件保存到指定目录并验证落盘。定位参数与 click_interact 一致：by=css/xpath 传 selector（支持 iframe_selector 链式穿透），by=role 传 role+name。download_dir 默认 ./downloads（相对用户项目根目录，可用环境变量 DOWNLOAD_DIR 覆盖）；filename 可指定保存名（默认浏览器提供的文件名，已存在同名文件时覆盖）；wait_timeout_ms 为下载完成等待上限（默认 30s）。实现原理：项目以 no_defaults 接管用户日常浏览器，Playwright download 事件不开启，本工具在动作窗口内通过浏览器级 CDP 会话下发 Browser.setDownloadBehavior（定向到 download_dir + 开启事件流），点击后监听 downloadWillBegin/downloadProgress 直到完成，随后恢复浏览器默认下载行为，不干扰用户日常下载。返回 status：success（文件已落盘并验证，附路径/大小）/timeout/no_download/canceled，便于进一步读取分析（如 xlsx 用 pandas 编辑保存后再上传）。",
            ),
            Tool.from_function(
                upload_file_impl,
                name="upload_file",
                description="点击上传按钮/输入框并注入要上传的文件，可选等待上传成功的页面反馈。两条路径：① 定位到 <input type=file>（含隐藏/antd 包装）→ set_input_files 直接设置；② 定位到普通按钮 → 点击后拦截系统文件选择框（filechooser），不弹原生对话框，直接注入文件路径，页面逻辑照常触发上传。file_paths 为要上传的文件（相对路径基于用户项目根目录，必须存在）。success_text 可选：指定上传成功后页面出现的文本（如“上传成功”），工具轮询等待其出现并返回 success_text_found，用于判断上传是否成功；wait_timeout_ms 为等待上限（默认 15s）。",
            ),
        ]

    async def _list_tools(self) -> Sequence[Tool]:
        return self._tools
