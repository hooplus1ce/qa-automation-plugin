import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def _plugin_root() -> Path:
    """插件根目录: src/qa_mcp/config.py 向上两级 (parents[2])。"""
    return Path(__file__).resolve().parents[2]


# 项目根特征标志: 候选目录命中任一即视为项目根 (防止误判系统目录/home)
_PROJECT_MARKERS = (".git", ".gitignore", "pyproject.toml", "package.json")


def _looks_like_project_root(path: Path) -> bool:
    """候选目录是否像项目根: 含常见项目标志文件。"""
    return any((path / marker).exists() for marker in _PROJECT_MARKERS)


def _detect_project_dir_from_process_tree() -> Optional[str]:
    """回溯进程树, 自动识别客户端的工作目录 (即用户项目根)。

    原理: 插件化部署时仅最内层 fastmcp 子进程被 `uv run --directory <插件根>`
    切到插件目录, 外层客户端进程 (claude / cursor / code / Claude Desktop 等)
    的 cwd 仍是用户项目目录——沿父进程链上溯, 跳过"插件目录及其祖先链"
    (如 ~/.claude/plugins/cache/...、%LOCALAPPDATA%\\Claude-3p\\...、仓库根),
    第一个无关 cwd 且**含项目标志文件** (.git/.gitignore/pyproject.toml/
    package.json) 的目录即客户端工作目录 = 用户项目。

    特征过滤是防误判的关键: 无标志的系统目录 (System32/home/盘符根) 不命中,
    宁可回退 cwd, 也不把资产写进系统目录。

    psutil 延迟导入: 未安装/权限失败 (跨用户进程) 时静默返回 None,
    由调用方回退, 不构成硬依赖。
    """
    try:
        import psutil
    except ImportError:
        return None

    root = _plugin_root()
    proc = psutil.Process()
    while proc is not None:
        try:
            # Windows 8.3 短路径 (如 HOOPLU~1) 会破坏 is_relative_to 文本比较,
            # 统一 resolve 规范化后再判断, 防止把插件根祖先误判为用户项目。
            cwd = Path(proc.cwd()).resolve()
        except (psutil.Error, PermissionError, OSError):
            return None
        # 跳过插件目录本身及其祖先链 (root.is_relative_to(cwd)) 与插件目录
        # 内部路径 (cwd.is_relative_to(root)); 首个无关且像项目根的 cwd 即命中。
        if not (root.is_relative_to(cwd) or cwd.is_relative_to(root)):
            if cwd.is_dir() and _looks_like_project_root(cwd):
                return str(cwd.resolve())
        try:
            proc = proc.parent()
        except (psutil.Error, PermissionError):
            break
    return None


def _resolve_project_dir() -> str:
    """定位用户项目根目录。

    优先级链 (覆盖插件化部署与任意 MCP 客户端):
    1. PROJECT_DIR 环境变量   — 任何客户端可显式配置, 最高优先 (可在
       .mcp.json / 客户端 MCP 配置的 env / 用户级 ~/.qa-automation-plugin/.env
       中设置; Claude Desktop 不注入项目信息, 必须走此通道)
    2. CLAUDE_PROJECT_DIR     — Claude Code 插件化部署时注入的用户项目根
    3. 进程树回溯嗅探          — 其他 CLI/IDE 客户端 (Cursor/VS Code 等):
       沿父进程链自动识别客户端工作目录
    4. 进程 cwd                — 本地直跑最终回退

    使相对路径 (粘贴图片/截图/下载/导出目录) 始终落在用户自己的项目里,
    任何部署形态都不写进插件安装目录。
    """
    for var in ("PROJECT_DIR", "CLAUDE_PROJECT_DIR"):
        proj = os.getenv(var, "").strip().strip('"').strip("'")
        if proj and os.path.isdir(proj):
            return os.path.abspath(proj)
    detected = _detect_project_dir_from_process_tree()
    if detected:
        return detected
    return os.getcwd()


def user_env_path() -> Path:
    """用户级全局 .env 路径 (跨客户端唯一稳定配置位)。"""
    return Path.home() / ".qa-automation-plugin" / ".env"


def _load_env_files(probe_dir: str) -> None:
    """加载 .env 进插件进程, 优先级: 用户项目根 > 用户级全局 > 进程 cwd。

    - 用户项目根 .env: 环境变量 (CDP_URL / VISION_PROVIDER / GEMINI_API_KEY 等)
      配置在**用户自己的项目根 .env** 中, 而不是共享的插件安装目录
      (插件由多人共用, 插件目录的配置会互相污染, 且插件更新时被重置)
    - 用户级 ~/.qa-automation-plugin/.env: 跨项目固定配置 (如 PROJECT_DIR),
      适合 Claude Desktop 等不注入项目信息、会话记录又延迟写盘的客户端
    - 进程 cwd (插件目录/本地项目) .env: 最后兜底
    已存在的进程环境变量始终优先, 不会被 .env 覆盖 (override=False)。
    """
    load_dotenv(os.path.join(probe_dir, ".env"))
    load_dotenv(user_env_path())
    load_dotenv()


# ===== 启动顺序 =====
# 1. 初步定位用户项目 (不依赖 .env): 显式 env > CLAUDE_PROJECT_DIR > 嗅探 > cwd
_probe_dir = _resolve_project_dir()
# 2. 加载 .env: 用户项目根优先, 用户级全局次之, 插件目录兜底
#    (override=False, 环境变量优先)
_load_env_files(_probe_dir)
# 3. 最终解析 — .env 中若定义了 PROJECT_DIR 可覆盖探测结果
PROJECT_DIR = _resolve_project_dir()


PROJECT_DIR = _resolve_project_dir()


def project_path(path: str) -> str:
    """将相对路径锚定到用户项目根目录 (绝对路径/空串原样返回)。"""
    if not path or os.path.isabs(path):
        return path
    return os.path.join(PROJECT_DIR, os.path.expanduser(path))


CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222")
# 鼠标光标可视化 + 目标高亮 服务级默认开关 (visualize=None 时生效, 默认关闭)
VISUAL_EFFECTS = os.getenv("VISUAL_EFFECTS", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# 以下相对目录统一锚定用户项目根: 插件化部署时进程 cwd 是插件目录,
# 若保持相对路径会把证据/导出/下载写进插件安装目录, 用户侧无法访问。
EVIDENCE_DIR = project_path("evidence_assets")
OUTPUT_DIR = project_path("output_testcases")
# download_file 工具默认下载保存目录 (相对用户项目根, 可环境变量覆盖)
DOWNLOAD_DIR = project_path(os.getenv("DOWNLOAD_DIR", "downloads"))


# ==================== 时序/等待参数 (统一调优成功率) ====================
# 全部可用环境变量覆盖; 慢环境/慢页面整体放大时改一处即可, 无需逐文件改魔法数字。
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return default


# 元素定位等待超时 (click/fill/select_option/press 的 wait_for visible)
ELEMENT_WAIT_TIMEOUT_MS = _env_int("ELEMENT_WAIT_TIMEOUT_MS", 6000)
# 动作链单步执行上限: 单个 click/fill/select/press 超过即记为失败,
# 防止链中一个死动作把整条链及后续所有工具调用堵死 (看门狗配套, 恢复自
# 远程 revert 前版本; CDP 挂死时 Playwright 动作级 timeout 不生效, 需外层限时)
ACTION_STEP_TIMEOUT_MS = _env_int("ACTION_STEP_TIMEOUT_MS", 15000)
# 全局工具执行看门狗: 任何工具调用超过该上限即强制中断并释放串行队列
TOOL_MAX_EXECUTION_MS = _env_int("TOOL_MAX_EXECUTION_MS", 300000)
# 点击/输入后的统一观察轮询窗口 (动态层/消息捕获)
OBSERVE_WAIT_MS = _env_int("OBSERVE_WAIT_MS", 1500)
# 交互式 UI (elicitation 弹窗/Apps 卡片) 等待用户操作的超时秒数;
# 超时未操作默认按"直接进入下一步"处理 (危险确认类可在工具层配置)
INTERACT_TIMEOUT_S = _env_int("INTERACT_TIMEOUT_S", 10)
# 交互式 UI 工具总开关: false (默认) 时不注册 setup_form/choose/request_approval/
# vtable_records_view/plugin_setup 等 UI 工具 (描述不进入上下文, 省 token),
# 且所有 elicitation 交互点直接走默认值 (不弹窗)。需要交互式表单/卡片时
# 在 .env 设置 INTERACTIVE_UI_ENABLED=true 并重启客户端。
INTERACTIVE_UI_ENABLED = os.getenv("INTERACTIVE_UI_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Ant Design 下拉: 首次等待新下拉挂载; 后续每轮重试等待; 重试总轮数; 重试间隔
SELECT_WAIT_FIRST_MS = _env_int("SELECT_WAIT_FIRST_MS", 5000)
SELECT_WAIT_RETRY_MS = _env_int("SELECT_WAIT_RETRY_MS", 1000)
SELECT_RETRY_ATTEMPTS = _env_int("SELECT_RETRY_ATTEMPTS", 6)
SELECT_POLL_INTERVAL_MS = _env_int("SELECT_POLL_INTERVAL_MS", 200)
# 统一"定位-执行"重试 (SPA 重渲染/元素 detach/短暂遮挡): 尝试次数与间隔
ACTION_RETRY_ATTEMPTS = _env_int("ACTION_RETRY_ATTEMPTS", 3)
ACTION_RETRY_BACKOFF_MS = _env_int("ACTION_RETRY_BACKOFF_MS", 500)
# CDP 首次连接失败退避重试: 次数与初始间隔 (指数退避 x2)
CONNECT_RETRY_ATTEMPTS = _env_int("CONNECT_RETRY_ATTEMPTS", 3)
CONNECT_RETRY_BACKOFF_MS = _env_int("CONNECT_RETRY_BACKOFF_MS", 1500)
