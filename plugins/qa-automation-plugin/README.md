# QA Automation Plugin (Claude Code / Desktop 插件)

[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.4.4-green)](https://gofastmcp.com/)
[![Playwright](https://img.shields.io/badge/Playwright-CDP-orange)](https://playwright.dev/)
[![License](https://img.shields.io/badge/license-MIT-brightgreen)](#)

企业级 Web 系统（SCM / MOM / WMS / ERP）自动化测试 **Claude Code & Claude Desktop 插件**。

通过 Playwright CDP 接管本地物理 Chrome 浏览器，提供 DOM/iframe 语义分析、高韧性点击输入、批量动作链、VTable 场景图渲染层交互、动态浮层探查、测试用例实时录制、文件下载/上传控制以及 Shadcn 极简风格 Excel 报表与证据 JSON 一键落盘导出（内置 **27 个 MCP 工具** + **测试设计 SOP 技能指南**）。

---

## 目录

- [核心架构设计与原理总结](#核心架构设计与原理总结)
- [项目目录结构](#项目目录结构)
- [前置条件与环境准备](#前置条件与环境准备)
- [插件安装与使用 SOP](#插件安装与使用-sop)
  - [方式一：Claude Desktop 导入 ZIP 插件包（推荐生产使用）](#方式一claude-desktop-导入-zip-插件包推荐生产使用)
  - [方式二：Claude Code 插件加载与市场安装](#方式二claude-code-插件加载与市场安装)
  - [方式三：常规 MCP 客户端直接接入（Cursor / VS Code / Claude Desktop 手动配置）](#方式三常规-mcp-客户端直接接入cursor--vs-code--claude-desktop-手动配置)
- [MCP 工具与 SOP 技能清单](#mcp-工具与-sop-技能清单)
- [开发验证与单元测试](#开发验证与单元测试)

---

## 核心架构设计与原理总结

在项目设计与优化过程中，针对 MCP 插件的生命周期、环境打包、变量注入与启动性能得出了以下核心架构原理与结论：

### 1. 无需打包 `.venv` 的“现场自动建环”机制
- **零体积发布**：分发 Zip 插件包时**绝对不需要**包含庞大的 `.venv` 虚拟环境，打出的插件 Zip 包体积仅 **~200 KB**。
- **外层 `uv run` 负责环境感知与现场构建**：当 MCP 客户端启动插件时，命令最外层的 `uv run` 会首先检查插件目录下是否存在 `.venv`。若不存在，`uv` 会读取 `fastmcp.json` / `pyproject.toml` 依赖声明，在目标机器上自动下载 Python 环境并瞬间构建虚拟环境、安装依赖。
- **内层 `--skip-env` 防止二次建环死循环**：子命令 `fastmcp run --skip-env fastmcp.json` 中的 `--skip-env` 标志用于告知 FastMCP 内部 CLI 引擎：*“外层 `uv` 已经完成了虚拟环境的创建与激活，FastMCP 无需在内部重复拉起 `uv` 嵌套构建”*。此举杜绝了死循环，并将服务启动耗时缩短至毫秒级。
- **`uv.lock` 随包分发锁定依赖**：依赖解析结果（含全部传递依赖）提交在 `uv.lock` 中并随 Zip 包分发，用户机器首次 `uv run` 严格按锁定版本建环。杜绝 `pyproject.toml` 范围依赖（如 `playwright>=1.60`）在用户侧解析到新版本导致的“开发环境正常、用户环境异常”。（修改 `pyproject.toml` 依赖后需重新生成：`uv lock` 或直接 `uv add`。）

### 2. 插件全局挂载与 `${CLAUDE_PLUGIN_ROOT}` 路径寻址
- **解决 `not loaded` 的关键**：在 Claude Code / Claude Desktop 插件体系中，用户安装插件后，插件文件解压挂载在插件系统的全局路径下（如 `~/.claude/plugins/qa-automation-plugin/`）。当用户在任意其他工作区目录使用该插件时，如果没有指定 `--directory "${CLAUDE_PLUGIN_ROOT}"`，`uv` 会在用户当前工作区寻找 `fastmcp.json`，从而导致找不到配置文件并引发 **`qa-automation-mcp: not loaded`** 加载失败。
- **`${CLAUDE_PLUGIN_ROOT}` 自动注入与挂载**：在 `.claude-plugin/plugin.json` 中配置 `--directory "${CLAUDE_PLUGIN_ROOT}"`，确保了无论用户在电脑上的哪个项目路径下触发插件，`uv` 都能准确跳至插件的实际安装根目录去加载 `fastmcp.json` 并激活环境，实现跨目录、跨项目的全局无缝调用。
### 3. 用户项目根目录 (`PROJECT_DIR`) 与相对路径锚定
- **进程 cwd ≠ 用户项目**：插件化部署时 MCP 服务进程 cwd 是插件安装目录（见第 2 点），若相对路径按 cwd 解析，`describe_image` 的图片入参（粘贴图片、`capture_screenshot` 落盘的 `evidence_assets/` 截图地址）以及 `download_file` / `upload_file` / `export_session` 的文件路径都会解析到插件目录，导致"找不到图片/文件"。
- **自动识别用户项目根（四级优先级链）**：`src/qa_mcp/config.py` 按以下顺序解析
  `PROJECT_DIR`，保证资产（证据/截图/下载/导出）在任何部署形态都落在用户项目：
  1. `PROJECT_DIR` 环境变量 —— 任何客户端可显式配置（`.mcp.json` / 客户端 MCP
     配置的 `env` / 用户级 `~/.qa-automation-plugin/.env`），最高优先
  2. `CLAUDE_PROJECT_DIR` —— Claude Code 插件化部署时注入的用户项目根
  3. **进程树回溯嗅探** —— 其他客户端（Cursor / VS Code 等 CLI/IDE）：沿父进程
     链跳过插件目录，自动识别客户端工作目录（需目录含 `.git`/`.gitignore`/
     `pyproject.toml`/`package.json` 等标志，防止误判系统目录）
  4. 进程 cwd —— 本地直跑最终回退
- **Claude Desktop 用户请显式配置 `PROJECT_DIR`**（实测结论：Desktop 启动 MCP
  server 时不注入项目目录环境变量，会话记录延迟写盘，无法自动识别活跃项目）。
  在用户级配置文件 `~/.qa-automation-plugin/.env` 中指定（用户自己的目录，非共享
  插件目录）：
  ```bash
  # ~/.qa-automation-plugin/.env
  PROJECT_DIR=D:\Developer\Hoolinks\APS
  ```
  加载优先级：用户项目根 `.env` > 用户级 `~/.qa-automation-plugin/.env` > 插件目录 `.env`；
  换项目时改这一行即可（或在 Desktop 的 MCP 配置 `env` 中设置，随项目走）。
- 所有相对目录（`evidence_assets/`、`output_testcases/`、`downloads/`）与工具的相对路径入参统一锚定解析结果，保证在任意客户端/工作区使用插件时图片识别与文件读写都能正确命中。
### 4. 全面支持 Python 3.14 稳定版与向下兼容
- 项目依赖规范配置为 `requires-python = ">=3.11"`（`pyproject.toml`）与 `"python": ">=3.11"`（`fastmcp.json`）。
- 完全支持已正式发布的 **Python 3.14 稳定版**，同时对 Python 3.11 / 3.12 / 3.13 保持向下兼容。

---

## 项目目录结构

```text
仓库根 (Marketplace)
├── .claude-plugin/
│   └── marketplace.json        # 市场清单 (name/owner/plugins[].source 相对路径)
├── plugins/
│   └── qa-automation-plugin/   # ← 本插件根目录 (可独立分发/打包)
│       ├── .claude-plugin/
│       │   └── plugin.json     # 插件主清单 (定义名称、版本、mcpServers 与 skills 显式映射)
│       ├── fastmcp.json        # FastMCP 声明式服务、入口与依赖配置
│       ├── pyproject.toml      # Hatchling 构建与项目依赖声明 (包含 pytest 开发依赖组)
│       ├── .env.example        # 环境变量配置模板
│       ├── skills/
│       │   ├── qa-automation-guide/
│       │   │   └── SKILL.md    # 测试设计 SOP 技能 (模型可自动调用, 正文按需加载)
│       │   └── ui-automation-test/
│       │       └── SKILL.md    # 用例复验场景技能 (disable-model-invocation, 仅显式调用)
│       ├── src/qa_mcp/         # FastMCP 3.x 服务源码
│       │   ├── server.py       # 服务装配入口 (Lifespan, Middleware, Provider 与导出工具)
│       │   ├── config.py       # 统一超时、轮询与环境变量配置
│       │   ├── providers/      # FastMCP Provider 扩展 (BrowserProvider, VTableProvider)
│       │   ├── tools/          # 27 个 MCP 核心工具实现 (browser, vtable, recorder, vision 等)
│       │   └── utils/          # UI 组件适配器、场景图 JS 注入脚本与 Shadcn Excel 渲染器
│       └── tests/              # 单元测试套件
└── .mcp.json.example           # 工作区 MCP 配置示例 (开发者在本仓库根直接调试用)
```

---

## 前置条件与环境准备

1. **安装必要工具**：
   - 已安装 [Claude Code](https://code.claude.com) 或 [Claude Desktop](https://claude.ai/download)。
   - 已安装 [uv](https://docs.astral.sh/uv/)（Python 包与环境管理工具）。
   - 环境支持 Python 3.11、3.12、3.13 或 **3.14+**。

2. **启动本地物理 Chrome 远程调试端口**：
   Playwright CDP 需连接至开启调试端口的 Chrome 实例，在终端运行：
   - **Windows**:
     ```cmd
     chrome.exe --remote-debugging-port=9222 --user-data-dir="C:\Temp\ChromeDebugProfile"
     ```
   - **macOS**:
     ```bash
     /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir="/tmp/ChromeDebugProfile"
     ```

3. **配置环境变量（可选，推荐放你自己的项目根）**：
   **不要**在插件安装目录配置环境变量（多人共享 + 更新即重置）。插件启动时会
   自动定位你当前的项目根并加载其 `.env` 注入插件进程（定位链:
   `CLAUDE_PROJECT_DIR` > 进程树嗅探 > `PROJECT_DIR` 环境变量 > 进程 cwd；
   也可以在客户端 MCP 配置的 `env` 中直接设置）。在你的项目根执行：
   ```bash
   cp <插件目录>/.env.example .env
   ```
   常用变量：
   - `CDP_URL`: Chrome CDP 调试地址（默认 `http://127.0.0.1:9222`）。
   - `VISUAL_EFFECTS`: 是否开启鼠标点击与定位框可视化高亮（默认 `true`）。
   - `VISION_PROVIDER`: 视觉识别模型通道（`auto`/`tokenhub`/`gemini`/`antigravity`/
     `custom`，默认 `auto`）。`antigravity` 通道免 API Key，走 OAuth 授权登录
     （见下节"视觉模型（vision role）配置与使用"）。
   - `VISION_MODEL`: 覆盖视觉模型名（默认 `gemini-3.6-flash`）。
   - `VISION_API_KEY`: 腾讯云 TokenHub GLM-5V 视觉 API Key（tokenhub 通道）。
   - `ELEMENT_WAIT_TIMEOUT_MS`: 元素定位等待超时（click/fill/select/press 的 wait_for visible，默认 `6000`ms）。

### 视觉模型（vision role）配置与使用

参考 oh-my-pi 的 role 设计：主模型负责逻辑与交互，图片视觉识别由独立的 **vision role**
承担——纯文本主模型（如 DeepSeek-V4）通过 `describe_image` 工具获得多模态能力。

**通道对比**（`VISION_PROVIDER`）：

| 通道 | 认证方式 | 默认模型 | 适用场景 |
|---|---|---|---|
| `antigravity`（推荐） | OAuth 授权网页登录，免 API Key | gemini-3.6-flash | Antigravity 订阅用户，配额制 |
| `tokenhub` | `VISION_API_KEY` | glm-5v-turbo | 腾讯云 TokenHub |
| `custom` | `VISION_API_BASE` + `VISION_MODEL` + Key | 自定义 | 任意 OpenAI 兼容端点 |

**Antigravity 通道使用（推荐，免 API Key）**：

1. **首次使用（对话内完成授权，无需终端命令）**：
   直接在对话中说"识别一下 xxx 图片"——主模型调 `describe_image` 发现未授权时，
   会自动改调 **`vision_login` 工具**：浏览器弹出 Antigravity 授权网页 → 点击允许
   → 授权完成 → 主模型重试识别。全程无需离开对话，授权一次长期有效
   （token 自动刷新，`vision_login` 已登录时直接返回成功）。
   备用（命令行方式）：`uv run --directory <插件目录> python -m qa_mcp.vision_antigravity login`
2. **启用通道**：`.env`（或客户端 MCP 配置的 `env`）中设置 `VISION_PROVIDER=antigravity`
   （默认 `auto` 在已登录时也会自动走 antigravity，可不设置）。
3. **使用**：主模型为纯文本时，直接要求它识别图片即可——主模型会自动调
   `describe_image`（支持截图路径 / 粘贴图片 / URL，`thinking`/`reasoning_effort`/
   `include_reasoning` 可调）。例如：
   "识别 evidence_assets/基础配置/20260808_TC001_02_保存成功.png，核对保存提示"
   同图同问命中 sha256 缓存（返回 `cached=True`），不重复消耗配额。

**注意事项**：
- Antigravity 按日配额（daily quota）；429 限流时稍后重试，缓存可显著降低消耗。
- 多客户端（Claude Code / Cursor 等）：凭据用户级共享，但**每个客户端的 MCP
  配置都要各自设 `VISION_PROVIDER=antigravity`**。
- 未登录/凭据失效时，`describe_image` 返回可操作错误（含登录命令提示）。

---

## 插件安装与使用 SOP

### 方式一：Claude Desktop 导入 ZIP 插件包（推荐生产使用）

1. **打包插件（开发者）**：
   直接将插件根目录 `plugins/qa-automation-plugin/` 下除 `.venv`、`__pycache__` 外的文件打包为 `qa-automation-plugin.zip`（体积约 200KB）。
2. **导入 Claude Desktop**：
   - 打开 Claude Desktop $\rightarrow$ 设置 $\rightarrow$ Plugins / MCP Servers $\rightarrow$ 选择导入 `qa-automation-plugin.zip`。
3. **自动运行**：
   - Claude Desktop 会解压插件到本地插件目录。
   - 首次触发时外层 `uv run` 自动建环并安装依赖，内层 `--skip-env` 快速调起 MCP 服务。
4. **首次启用预热（重要，避免"连接中"卡死）**：
   - 插件首次启用时，客户端调用 `uv run` 现场构建虚拟环境并安装依赖（
     fastmcp/playwright 等，首次约 20-60 秒）。部分客户端（如 Claude Desktop
     cowork）对 MCP 服务启动存在超时窗口，若首次依赖安装未完成即被中断，
     服务会一直显示"连接中"、工具加载不出来。
   - **遇到"连接中"时**：在插件安装目录执行一次预热，再重启客户端：
     ```bash
     uv sync --no-dev --directory "<插件安装目录>"
     ```
     插件安装目录示例（Claude Desktop）：`%LOCALAPPDATA%\Claude-3p\
     local-agent-mode-sessions\<账号>\00000000\cowork_plugins\cache\
     hoolinks\qa-automation-plugin\<版本>\`
   - 预热幂等（依赖已装时秒级完成）；**每次通过 Update 更新插件后建议重新
     预热一次**——更新会重置插件目录，依赖需重装，uv 全局缓存使重装仅需
     数秒到数十秒，远快于首次。

### 方式二：Claude Code 插件加载与市场安装

- **本地开发调试**：
  在 Claude Code 中直接指定插件目录运行：
  ```bash
  claude --plugin-dir ./plugins/qa-automation-plugin
  ```
- **通过 Marketplace 安装**：
  在 Claude Code 中添加市场并安装（仓库根即市场根，source 指向 `./plugins/qa-automation-plugin`）：
  ```bash
  /plugin marketplace add hooplus1ce/qa-automation-plugin
  /plugin install qa-automation-plugin@hoolinks
  ```
- **Claude Desktop 安装**（桌面应用 Code 标签页）：
  1. 先在 Claude Code CLI 中执行 `/plugin marketplace add hooplus1ce/qa-automation-plugin`，将该市场加入已配置列表（Desktop 与 CLI 共享市场配置）。
  2. 桌面会话中点击输入框旁 **+** → **Plugins** → **Add plugin**，在插件浏览器中安装 **QA Automation Plugin**。
  3. 云会话不支持插件浏览器，需在仓库 `.claude/settings.json` 的 `enabledPlugins` 中声明。

### 方式三：常规 MCP 客户端直接接入（Cursor / VS Code / Claude Desktop 手动配置）

若不使用插件包装机制，可以直接在客户端配置（如 `~/.claude/claude_desktop_config.json` 或 `.cursor/mcp.json`）中添加 `mcpServers`：

```json
{
  "mcpServers": {
    "qa-automation-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/绝对路径/到/qa-automation-plugin",
        "fastmcp",
        "run",
        "--skip-env",
        "fastmcp.json"
      ],
      "env": {
        "CDP_URL": "http://127.0.0.1:9222",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

---

## MCP 工具与 SOP 技能清单

项目共装配 **27 个 MCP 核心工具** 及 **2 个技能（设计 SOP + 用例复验场景）**：

### 1. 基础页面分析与交互工具 (13 个)
- `analyze_current_page`: 递归分析 DOM 及嵌套 iframe，生成可见交互元素定位器。
- `click_interact`: 统一点击工具（支持 CSS/XPath、get_by_role 语义定位、视口坐标点击，附带弹窗浮层与跳转观察）。
- `fill_input`: 文本框填充（支持清空、逐字模拟键盘输入、回车触发）。
- `execute_action_chain`: 批量动作链顺序执行（含 fallback 变体容错与降级机制）。
- `probe_dynamic_layers`: 探查页面/iframe 出现的可见弹窗、下拉悬浮层及消息气泡。
- `wait_for_condition`: 页面条件轮询等待（文本出现/元素可见/URL跳转）。
- `download_file`: 点击触发下载的按钮/链接，下载文件落盘到指定目录（默认 ./downloads）并验证，供后续读取分析（如 xlsx 用 pandas 编辑）。
- `upload_file`: 点击上传按钮/输入框注入指定文件（input 直设或 filechooser 拦截），可选等待"上传成功"反馈验证。
- `capture_screenshot`: CDP 原生无卡顿截屏（支持整页或视口，生成 PNG 及文件凭证）。
- `switch_target_page`: 显式重绑/锁定 MCP 操作的目标标签页。
- `describe_image`: 纯文本主模型环境下的视觉理解降级工具，实现**主模型 + 视觉模型
  角色分工**（vision role，参考 oh-my-pi 的 role 设计）。视觉模型由 `VISION_PROVIDER`
  配置（auto/tokenhub/antigravity/custom；auto 默认：已登录 Antigravity 走
  `gemini-3.6-flash`，否则腾讯云 TokenHub GLM-5V；`VISION_MODEL` 可覆盖模型名）。**antigravity 通道**：OAuth 授权网页登录（`python -m
  qa_mcp.vision_antigravity login`，凭据存 `~/.qa-automation-plugin/`，过期自动刷新），
  经 Cloud Code Assist 协议（streamGenerateContent）调用 Antigravity 的
  gemini-3.6-flash。`interactive=True` 时通过**交互对话框（MCP Elicitation）**
  让用户单选识别粒度（快速/标准/深度），客户端不支持时自动降级 auto（后续完善
  双轨自动降级）。带超时/重试/错误分类，同图同问 sha256 缓存命中免重复调用；
  `reasoning_effort` 默认 auto 按问题长度自适应；`include_reasoning` 默认关闭，
  思考链按需获取。
- `plugin_setup`: **交互式配置向导**（MCP elicitation 表单）——表单收集环境变量
  （项目根目录/CDP 地址/视觉通道/视觉模型/下载目录/鼠标高亮，预填当前生效值），
  校验后写入用户级 `~/.qa-automation-plugin/.env`，**重启客户端生效**。用户要求
  配置或工具报配置缺失时由主模型调用；取消/客户端不支持时零修改。
  **Claude Desktop 另有 Apps 表单**（`setup_form`，定制版 ConfigFormApp）：同一
  Pydantic 模型渲染为原生表单 UI，提交回调写入同一 .env——Claude Code (TUI)
  自动降级走 elicitation 表单，Desktop 走原生 UI（注意：Claude Desktop 不支持
  elicitation 多字段表单，配置请用 `setup_form`；`plugin_setup` 报"不支持表单"
  时应切换）。
- `choose`: **Desktop 选择卡片**（FastMCP Apps Choice provider）——可点击选项
  按钮替代文本回复，选择结果作为消息回对话。Claude Code (TUI) 不渲染 Apps UI，
  降级用 `describe_image(interactive=True)` 的 elicitation 单选。
- `request_approval`: **Desktop 审批卡片**（FastMCP Apps Approval provider）——
  危险操作执行前展示摘要与继续/取消按钮。Claude Code 降级用
  `execute_action_chain(confirm=True)` 的 elicitation 确认。
- `vtable_records_view`: **VTable 数据可视化**（Apps DataTable，可搜索/排序）——
  Claude Code 降级用 `vtable_get_all_records` (JSON)。

### 交互式 UI 超时策略（10 秒默认）
所有交互点（识别粒度/动作链确认/选页/录制参数）等待用户操作默认 **10 秒**
（`INTERACT_TIMEOUT_S` 可配置）；超时未操作**默认选择直接进入下一步**
（如动作链确认超时默认继续执行、粒度超时默认标准）——交互永不阻塞流程；
用户显式拒绝/取消则按拒绝处理。

**Claude Desktop 模式差异（实测）**：Apps UI（配置表单 `setup_form`、选择卡片
`choose`、审批卡片 `request_approval`、VTable DataTable）仅在 **cowork 模式**
渲染；**code 模式不渲染** Apps UI——此时模型自动降级为「展示当前配置 → 向用户
确认字段 → 直接调用 `submit_config` 提交」完成同样配置（`plugin_setup` 的
elicitation 多字段表单在 Desktop 不受支持，会报"不支持表单"并引导切换）。

**交互 UI 总开关 `INTERACTIVE_UI_ENABLED`**（默认 `false`）：关闭时不注册
`setup_form`/`choose`/`request_approval`/`plugin_setup`/`vtable_records_view`
等长描述工具（**描述不进入上下文，节省 token**），且所有 elicitation 交互点
直接走默认值（不弹窗、不阻塞）。需要交互式表单/卡片时在用户级
`~/.qa-automation-plugin/.env` 设 `INTERACTIVE_UI_ENABLED=true` 并重启客户端。

双轨实现：
- **Claude Code / Desktop（elicitation）**：`asyncio.wait_for` 超时 → 默认值继续
- **Desktop Apps 卡片**（`choose` / `request_approval` 定制版）：Prefab
  `SetInterval(count=1, while_=未操作, onComplete=超时默认消息)` 挂于卡片
  `on_mount`——10 秒倒计时（卡片显示文案），用户点击选项后定时器立即停止
  （用户操作优先），超时未操作自动回传默认选择消息驱动模型继续。
- 工具参数 `timeout_s` / `default_option` / `default_action` 可按次覆盖。
- `start_recording`: 初始化测试用例录制会话。
- `execute_and_record`: 执行动作并自动记录最优高韧性语义定位步骤。

### 2. VTable 场景图表格交互工具 (13 个)
- `vtable_refresh_instance`: 挂载并刷新 Canvas 渲染表格的 `window._vtable` 实例。
- `vtable_analyze_headers`: 【场景图驱动】分析列头图标与单元格交互组件。
- `vtable_scan_columns`: 【推荐】扫描全部列头及视口坐标（直接传给坐标点击）。
- `vtable_get_row_count`: 获取表格纯数据总行数。
- `vtable_get_all_records`: 一次性读取整表后台行记录 JSON。
- `vtable_get_cell_text`: 读取指定单元格展示文本（可读取场景图渲染层）。
- `vtable_get_column_values`: 按中文列标题批量提取列数据。
- `vtable_get_cell_render_info`: 读取单元格场景图渲染详情（颜色/背景色/字体/节点）。
- `vtable_get_cell_center`: 计算单元格中心顶层视口坐标。
- `vtable_scroll_to`: 精确滚动 VTable 表格到指定行/列/坐标。
- `vtable_select_rows`: 勾选/取消勾选 Canvas 表格多行复选框。
- `vtable_drag_column`: 复刻真实鼠标拖拽移动 VTable 列位置。
- `vtable_resize_column`: 复刻真实鼠标拖拽 VTable 列头分隔线调整列宽（拖后自动校验）。

### 3. 会话导出工具 (1 个)
- `export_session`: 结束录制，生成证据 JSON 资产并落盘 Shadcn 极简风格 Excel 报表。

### 4. Agent SOP 技能
- `qa-automation-guide`: 提供 SCM/MOM/WMS/ERP 测试矩阵设计模式（Pattern A~E）及 UI 框架穿透路由标准。

### 5. 用例复验场景技能
- `ui-automation-test`：针对已有测试用例文档（Excel/子表）执行 UI 自动化测试与
  回归复验的场景指令，覆盖**资产全生命周期**：资产生成 SOP（录制/截图/导出/下载）、
  资产管理（追加合并/模块目录/基线/失败留档）、资产复用（录制的高韧性定位器直接
  驱动回归，免重新分析）、回归对比（状态翻转/新增失败）与命名规范（模块/用例ID/
  日期/步骤/状态，AI 与人均可一眼分辨）。
  frontmatter 设置 `disable-model-invocation: true`，**模型不会自动加载正文，
  仅用户显式调用** `/qa-automation-plugin:ui-automation-test` 时内容才进入
  上下文——场景指令不进入通用自动化框架的上下文背景，避免污染。
- 参数化占位符（用例文档/子表/过滤字段/过滤值/系统 URL/账号/执行人/
  证据根目录/报告路径/Bug 系统/回归模式）全部可选，未提供的参数保留占位符
  原文交由用户确认。
- 新增场景约定：一个场景一个 `SKILL.md` 放 `skills/<场景名>/`，frontmatter
  保留 `disable-model-invocation: true`；显式调用时在对话中提供占位符实际值。

### 6. 其他 Agent 平台加载技能（SKILL.md 开放标准）
- 本插件技能文件遵循 [Agent Skills 开放标准](https://agentskills.io)（必填 `name` +
  `description` frontmatter 齐全），可被 Cursor / Windsurf / GitHub Copilot /
  OpenAI Codex / Cline 等支持 `SKILL.md` 的平台直接加载，无需 Claude 生态。
- 加载方式：将 `skills/<技能名>/` 目录**复制**（或符号链接，Windows `mklink /J`）
  到平台约定位置：
  - 跨平台通用：`.agents/skills/<技能名>/`（项目）或 `~/.agents/skills/`（用户级）
  - Cursor：`.cursor/skills/`；Windsurf：`.windsurf/skills/`
  - Codex CLI：`.codex/skills/`；GitHub Copilot：`.github/skills/`
- 差异注意：`disable-model-invocation` 为 Claude 生态字段，其他平台可能忽略，
  `ui-automation-test` 可能被模型自动调用；如需严格"仅显式"需在目标平台另行配置。
- MCP 工具不受平台限制：任意 MCP 客户端按 `.mcp.json.example` 直接接入。

---

## 开发验证与单元测试

在项目修改或扩展后，请执行以下命令进行完整验证（仓库根与插件目录分离）：

```bash
# 1. 验证清单 (仓库根: Marketplace 清单; 插件目录: plugin.json)
claude plugin validate .
claude plugin validate plugins/qa-automation-plugin

# 2. 检查 FastMCP 服务工具装配与状态 (自动在插件目录建环)
uv run --directory plugins/qa-automation-plugin fastmcp list src/qa_mcp/server.py

# 3. 运行自动化单元测试套件 (自动在插件目录建环)
uv run --directory plugins/qa-automation-plugin pytest
```
