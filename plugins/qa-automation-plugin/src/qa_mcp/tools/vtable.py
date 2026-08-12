import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Callable, Dict, Any, List, Union
from qa_mcp.tools.browser import (
    browser_mgr,
    observe_after_click,
    popup_fingerprint,
    snapshot_navigation,
)

logger = logging.getLogger("mcp_automation.vtable")

# ---- VTable 场景图辅助脚本 (DrissionPage 移植版) ----
# vtable-scanner.js: mountVTable / scanColumns / getCellIconBounds (表头图标坐标)
# vtable-column-values.js: getColumnValuesByTitle / getCellRenderInfo / getCellCenterViewport / scrollToCell
# 坐标体系: 两个脚本均通过 window.frameElement.getBoundingClientRect() 一次性算出
# 【顶层视口坐标】(viewportX/viewportY)，Python 侧点击时不再叠加 iframe 偏移。
_JS_DIR = Path(__file__).resolve().parent.parent / "utils"
_SCANNER_JS = (_JS_DIR / "vtable-scanner.js").read_text(encoding="utf-8")
_VALUES_JS = (_JS_DIR / "vtable-column-values.js").read_text(encoding="utf-8")

# ---- 几何数据防御 (虚拟滚动哨兵值) ----
# VTable 对未渲染的虚拟滚动单元格, scenegraph 会返回 ±Number.MAX_VALUE 哨兵 bounds,
# 经 frame.evaluate 序列化后 Python 侧可能拿到 float('inf') / float('nan')。
# 统一在拿到几何结果后净化: 非有限浮点 → None, 避免后续 int()/坐标求和直接崩溃。
def _finite_num(v, default=None) -> Union[float, None]:
    """若 v 为有限数字返回 float(v), 否则返回 default (None 表示缺失/无效)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _sanitize_geom(geom):
    """把几何结果中的非有限浮点 (NaN/Infinity) 递归归一化为 None。"""
    if isinstance(geom, dict):
        return {k: _sanitize_geom(v) for k, v in geom.items()}
    if isinstance(geom, list):
        return [_sanitize_geom(v) for v in geom]
    if isinstance(geom, float) and not math.isfinite(geom):
        return None
    return geom


def _rect_coords_ok(rect) -> bool:
    """列头/落点矩形四角坐标均需为有限数字, 否则视为无效几何。"""
    if not isinstance(rect, dict):
        return False
    return all(_finite_num(rect.get(k)) is not None for k in ("x1", "x2", "y1", "y2"))


def _point_ok(center) -> bool:
    """点坐标 (x/y) 需为有限数字。"""
    return bool(
        isinstance(center, dict)
        and _finite_num(center.get("x")) is not None
        and _finite_num(center.get("y")) is not None
    )


async def _run_vtable_js(frame, call_expr: str) -> Any:
    """在目标 frame 中执行 挂载 + 调用表达式, 返回其结果。"""
    script = _SCANNER_JS + "\n" + _VALUES_JS + "\n" + call_expr
    return await frame.evaluate(script)


async def _poll_vtable(
    frame,
    call_expr_fn: Callable[[], str],
    predicate: Callable[[Any], bool],
    timeout_ms: float = 3000,
    interval_ms: float = 0.15,
) -> Any:
    """轮询 VTable JS 结果直到谓词成立或超时, 返回最后一次结果。

    canvas 渲染无 DOM 信号, Playwright 无法被动等待; 用"就绪即继续"的
    状态轮询替代固定 sleep —— 条件通常 1~2 轮即命中, 比固定延时更高效。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    last = None
    while True:
        last = await _run_vtable_js(frame, call_expr_fn())
        if predicate(last):
            return last
        if time.monotonic() >= deadline:
            return last
        await asyncio.sleep(interval_ms)

class VTableManager:
    """
    负责处理与页面 VTable 实例的各种交互操作。
    """
    
    async def _get_target_frame(self, iframe_selector: str):
        page = await browser_mgr.get_page()
        if not iframe_selector:
            return page

        iframe_element = await page.query_selector(iframe_selector)
        if not iframe_element:
            raise Exception(f"未找到匹配的 iframe: {iframe_selector}")
        
        frame = await iframe_element.content_frame()
        if not frame:
            raise Exception("无法获取 iframe 的内部上下文(跨域或尚未加载)")
            
        return frame

    async def refresh_instance(self, iframe_selector: str = "div[aria-hidden=false] iframe") -> Dict[str, Any]:
        """
        连接浏览器并在指定的 iframe 下寻址并刷新最新 vtable 实例至 window._vtable。
        使用 vtable-scanner.js 的 mountVTable (含可见性检查 + React fiber 向上回溯)。
        """
        frame = await self._get_target_frame(iframe_selector)
        result = await _run_vtable_js(frame, """
        (() => {
            const m = mountVTable();
            if (!m.ok) return { error: m.reason };
            return { status: "success", levels: m.levels };
        })()
        """)
        if result.get("error"):
            raise Exception(f"挂载 VTable 实例失败: {result.get('error')}")
        return {"status": "success", "message": "最新 VTable 实例已成功抓取并挂载到 window._vtable"}


    async def analyze_headers(
        self,
        iframe_selector: str = "div[aria-hidden=false] iframe",
        max_col: int = 200,
        sample_rows: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        场景图驱动分析列头与单元格内的交互图标组件。

        与旧实现(仅读 columns 配置)不同, 本方法从 VTable 场景图(scenegraph)
        渲染层收集【真实渲染】的图标节点:
          - header_icons: 表头行交互图标 (排序/筛选/下拉/冻结/checkbox 等),
                          含顶层视口坐标 viewportX/viewportY, 可直接配合
                          click_interact(by="coordinate", coordinate_space="top") 点击;
          - cell_icons: 前 sample_rows 个已渲染 body 单元格内的交互图标组件
                        (行内按钮/链接/checkbox/开关/下拉图标等) —— 这些组件
                        由自定义渲染产生, 在 columns 配置中并不存在, 旧实现
                        永远无法获取。
        虚拟滚动视口外的行没有 sceneNode, 采样时自动跳过 (cell_icons 只包含
        已渲染的行)。capabilities.interactiveCell 表示该列单元格内存在可交互图标。
        """
        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        result = await _run_vtable_js(frame, f"""
        (() => {{
            const m = mountVTable();
            if (!m.ok) return {{ error: m.reason }};
            const cols = scanHeaderCellIcons({int(max_col)}, {int(sample_rows)});
            if (!cols) return {{ error: 'scanHeaderCellIcons 返回空' }};
            return {{ ok: true, columns: cols }};
        }})()
        """)
        if result.get("error"):
            raise Exception(f"分析 VTable 列头失败: {result.get('error')}")

        columns = []
        for c in result.get("columns", []):
            header_icons = c.get("headerIcons", [])
            cell_icons = c.get("cellIcons", [])
            funcs = {ic.get("func", "") for ic in header_icons}
            funcs.discard("")
            capabilities = {
                "sortable": any("排序" in f for f in funcs),
                "filterable": any("筛选" in f for f in funcs),
                "hasCustomIcon": len(header_icons) > 0,
                "interactiveCell": len(cell_icons) > 0,
                "cellIconCount": len(cell_icons),
            }
            columns.append({
                "col": c.get("col", 0),
                "field": c.get("field", ""),
                "title": c.get("title", ""),
                "isFrozen": c.get("isFrozen", False),
                "header_icons": header_icons,
                "cell_icons": cell_icons,
                "capabilities": capabilities,
            })
        return columns

    async def scan_columns(self, iframe_selector: str = "div[aria-hidden=false] iframe", max_col: int = 200) -> List[Dict[str, Any]]:
        """
        扫描 VTable 全部列 (含多级表头): 返回每列的标题、body 行为分类
        (checkbox/button/文本等) 以及【表头图标的顶层视口坐标】(viewportX/viewportY)。

        图标坐标由 vtable-scanner.js 通过场景图 (scenegraph) 精确计算,
        viewportX/viewportY 直接是浏览器顶层视口坐标, 无需 Python 侧再叠加
        iframe/canvas 偏移 — 可直接作为 click_interact(by="coordinate", coordinate_space="top") 的点击坐标。
        """
        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        result = await _run_vtable_js(frame, f"""
        (() => {{
            const m = mountVTable();
            if (!m.ok) return {{ error: m.reason }};
            const cols = scanColumns({int(max_col)});
            if (!cols) return {{ error: "scanColumns 返回空" }};
            return {{ ok: true, columns: cols }};
        }})()
        """)
        if result.get("error"):
            raise Exception(f"扫描 VTable 列失败: {result.get('error')}")
        return result.get("columns", [])

    async def get_column_values(
        self,
        titles: List[str],
        iframe_selector: str = "div[aria-hidden=false] iframe",
        raw: bool = False,
    ) -> Dict[str, Any]:
        """
        按中文列标题读取该列所有单元格的值。
        raw=false: 读取场景图渲染后的视觉文本 (与界面显示一致);
        raw=true: 读取原始字段值 (如数值/状态码)。
        """
        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        titles_json = json.dumps(titles, ensure_ascii=False)
        result = await _run_vtable_js(frame, f"""
        (() => {{
            const m = mountVTable();
            if (!m.ok) return {{ error: m.reason }};
            return getColumnsValuesByTitle(window._vtable, {titles_json}, {json.dumps(bool(raw))});
        }})()
        """)
        if result.get("error"):
            raise Exception(f"读取列值失败: {result.get('error')}")
        return result

    async def get_cell_render_info(
        self,
        col_field: Union[int, str],
        row_index: int,
        iframe_selector: str = "div[aria-hidden=false] iframe",
        detail: str = "basic",
    ) -> Dict[str, Any]:
        """
        读取某个单元格的场景图渲染信息: 视觉文本、文字颜色、单元格背景色、
        边框色、字体大小, 以及文本/背景节点明细 (detail="full" 时包含全部节点)。

        col_field: 列索引 (int) 或字段名/列标题 (str); row_index: 纯数据行号 (0 为第一行)。
        """
        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        col_json = json.dumps(col_field)
        result = await _run_vtable_js(frame, f"""
        (() => {{
            const m = mountVTable();
            if (!m.ok) return {{ error: m.reason }};
            const t = window._vtable;
            let colIdx = null;
            if (typeof {col_json} === 'number') {{
                colIdx = {col_json};
            }} else {{
                const cols = t.columns || (t.options && t.options.columns) || [];
                const found = cols.findIndex(c => c.field === {col_json} || c.title === {col_json});
                if (found === -1) return {{ error: '未找到列: ' + {col_json} }};
                colIdx = found;
            }}
            const bodyRow = {int(row_index)} + (t.columnHeaderLevelCount || 1);
            return getCellRenderInfo(colIdx, bodyRow, {json.dumps(detail)});
        }})()
        """)
        if result.get("error"):
            raise Exception(f"读取单元格渲染信息失败: {result.get('error')}")
        return result

    async def get_cell_center(
        self,
        col_field: Union[int, str],
        row_index: int,
        iframe_selector: str = "div[aria-hidden=false] iframe",
    ) -> Dict[str, Any]:
        """
        读取单元格中心的【顶层视口坐标】viewportX/viewportY。
        坐标由 vtable-column-values.js 经场景图 globalAABBBounds 计算,
        可直接作为 click_interact(by="coordinate", coordinate_space="top") 的点击坐标。

        col_field: 列索引 (int) 或字段名/列标题 (str); row_index: 纯数据行号 (0 为第一行)。
        """
        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        col_json = json.dumps(col_field)
        result = await _run_vtable_js(frame, f"""
        (() => {{
            const m = mountVTable();
            if (!m.ok) return {{ error: m.reason }};
            const t = window._vtable;
            let colIdx = null;
            if (typeof {col_json} === 'number') {{
                colIdx = {col_json};
            }} else {{
                const cols = t.columns || (t.options && t.options.columns) || [];
                const found = cols.findIndex(c => c.field === {col_json} || c.title === {col_json});
                if (found === -1) return {{ error: '未找到列: ' + {col_json} }};
                colIdx = found;
            }}
            const bodyRow = {int(row_index)} + (t.columnHeaderLevelCount || 1);
            const pt = getCellCenterViewport(colIdx, bodyRow);
            if (!pt) return {{ error: '无法计算单元格中心坐标 (该行可能未渲染, 尝试 scrollToCell 后重试)' }};
            return {{ ok: true, viewportX: pt.viewportX, viewportY: pt.viewportY, col: colIdx, bodyRow: bodyRow }};
        }})()
        """)
        if result.get("error"):
            raise Exception(f"读取单元格中心坐标失败: {result.get('error')}")
        return result

    async def scroll_to(
        self,
        col_field: Union[int, str, None] = None,
        row_index: Union[int, None] = None,
        scroll_left: Union[int, float, None] = None,
        scroll_top: Union[int, float, None] = None,
        iframe_selector: str = "div[aria-hidden=false] iframe",
        verify: bool = True,
    ) -> Dict[str, Any]:
        """
        滚动 VTable 到目标位置（等价于拖动横/纵向滚动条滑块）。

        三种用法（按优先级）:
          1) col_field + row_index: 滚动到指定单元格 (scrollToCell)
          2) 仅 col_field:         横向滚动到指定列 (scrollToCol)
          3) 仅 row_index:         纵向滚动到指定行 (scrollToRow)
          4) scroll_left / scroll_top: 直接设置滚动偏移 (setScrollLeft / setScrollTop)

        col_field 支持列索引 (int) 或字段名/列标题 (str)。
        verify=True 时滚动后自动校验目标是否已进入可视区。
        """
        if all(v is None for v in (col_field, row_index, scroll_left, scroll_top)):
            raise Exception("至少需要提供 col_field / row_index / scroll_left / scroll_top 之一")

        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)

        # 解析列索引
        col_idx = None
        if col_field is not None:
            if isinstance(col_field, int):
                col_idx = col_field
            else:
                col_json = json.dumps(col_field)
                col_idx = await _run_vtable_js(frame, f"""
                (() => {{
                    const t = window._vtable;
                    if (!t) return {{ error: 'no vtable' }};
                    const cols = t.columns || (t.options && t.options.columns) || [];
                    const found = cols.findIndex(c => c.field === {col_json} || c.title === {col_json});
                    if (found === -1) return {{ error: '未找到列: ' + {col_json} }};
                    return {{ col: found }};
                }})()
                """)
                if col_idx.get("error"):
                    raise Exception(f"解析列索引失败: {col_idx.get('error')}")
                col_idx = col_idx["col"]

        # 构造并执行滚动调用
        if col_idx is not None and row_index is not None:
            call_expr = f"scrollToCellPosition({int(col_idx)}, {int(row_index)})"
        elif col_idx is not None:
            call_expr = f"scrollToColumnByIndex({int(col_idx)})"
        elif row_index is not None:
            call_expr = f"scrollToRowByIndex({int(row_index)})"
        else:
            sl = "null" if scroll_left is None else json.dumps(float(scroll_left))
            st = "null" if scroll_top is None else json.dumps(float(scroll_top))
            call_expr = f"setScrollPosition({sl}, {st})"

        result = await _run_vtable_js(frame, f"""
        (() => {{
            const t = window._vtable;
            if (!t) return {{ error: 'window._vtable 未准备好' }};
            const r = {call_expr};
            return r;
        }})()
        """)
        if result.get("error"):
            raise Exception(f"滚动 VTable 失败: {result.get('error')}")
        if not result.get("ok"):
            raise Exception(f"滚动 VTable 失败: {result.get('reason')}")

        # 等待滚动稳定: 目标可见则轮询可视 (就绪即继续), 否则短暂等待
        # (canvas 无 DOM 信号, Playwright 无法被动等待, 用状态轮询替代固定 sleep)
        if verify and col_idx is not None:
            body_row = int(row_index) + 1 if row_index is not None else 1
            await _poll_vtable(
                frame,
                lambda: f"({{ const v = isCellInViewport({int(col_idx)}, {int(body_row)}); return {{ visible: v }}; }})()",
                lambda r: bool(r.get("visible")),
            )
        else:
            await asyncio.sleep(0.15)

        # 读取滚动后状态
        state = await _run_vtable_js(frame, """
        (() => {
            const s = getScrollStateInfo();
            if (!s) return { error: 'no scroll state' };
            return s;
        })()
        """)

        resp: Dict[str, Any] = {
            "status": "success",
            "api": result.get("api", ""),
            "target": {
                "col": col_idx,
                "col_field": col_field,
                "row_index": row_index,
                "scroll_left": scroll_left,
                "scroll_top": scroll_top,
            },
            "scroll": state,
        }

        # 校验目标是否进入可视区
        if verify and col_idx is not None:
            body_row = int(row_index) + 1 if row_index is not None else 1
            visible = await _run_vtable_js(frame, f"""
            (() => {{
                const v = isCellInViewport({int(col_idx)}, {int(body_row)});
                return {{ visible: v }};
            }})()
            """)
            resp["verification"] = {
                "ok": bool(visible.get("visible")),
                "cell_in_viewport": bool(visible.get("visible")),
            }
            if not visible.get("visible"):
                resp["verification"]["note"] = (
                    "目标单元格未完全落入可视区，可再调用 vtable_get_cell_center "
                    "获取最新坐标后点击"
                )
        return resp

    async def get_row_count(self, iframe_selector: str = "div[aria-hidden=false] iframe") -> int:
        """读取当前表格有多少行"""
        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        get_count_js = """
        () => {
            if (!window._vtable) return { error: "window._vtable 未准备好" };
            const vtable = window._vtable;
            const columns = vtable.columns || (vtable.options && vtable.options.columns) || [];
            
            // 尝试在列中寻找 vtable_aggregator 的 records
            for (let col of columns) {
                if (col.vtable_aggregator && col.vtable_aggregator.records) {
                    return { status: "success", count: col.vtable_aggregator.records.length };
                }
            }
            // 如果没找到 records，尝试读取 vtable 内置的 rowCount 减去表头行数
            const rowCount = vtable.rowCount || 0;
            const headerRowCount = vtable.columnHeaderLevelCount || 1;
            return { status: "success", count: Math.max(0, rowCount - headerRowCount) };
        }
        """
        result = await frame.evaluate(get_count_js)
        if result.get("error"):
            raise Exception(result.get("error"))
        return result.get("count", 0)

    async def get_all_records(self, iframe_selector: str = "div[aria-hidden=false] iframe") -> List[Dict[str, Any]]:
        """一次性读取表格所有的后台完整记录对象。

        优先从 vtable_aggregator.records 读取；若列配置中不存在该结构（如普通
        VTable 数据表），则回退读取 vtable.records。
        """
        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        get_records_js = """
        () => {
            if (!window._vtable) return { error: "window._vtable 未准备好" };
            const vtable = window._vtable;
            const columns = vtable.columns || (vtable.options && vtable.options.columns) || [];
            for (let col of columns) {
                if (col.vtable_aggregator && col.vtable_aggregator.records) {
                    return { status: "success", records: col.vtable_aggregator.records };
                }
            }
            // 回退：直接读取 vtable 实例内置 records
            const records = vtable.records;
            if (Array.isArray(records) && records.length > 0) {
                return { status: "success", records: records };
            }
            return { error: "未能在 vtable 中读取到 records" };
        }
        """
        result = await frame.evaluate(get_records_js)
        if result.get("error"):
            raise Exception(result.get("error"))
        return result.get("records", [])

    async def get_cell_text(
        self,
        row_index: int,
        col_field: str,
        iframe_selector: str = "div[aria-hidden=false] iframe",
        visual: bool = True,
    ) -> Any:
        """
        读取某个具体单元格的值。
        row_index: 行号 (0-indexed，纯数据行，不含表头)
        col_field: 列的 field 名称 或 列标题
        visual: True(默认)= 读取场景图渲染层文本, 与界面显示完全一致
                (重要: VTable 排序/筛选发生在渲染层, 数据源 records 不重排;
                 排序状态下必须用渲染层才能拿到界面真实顺序);
                False = 读取数据源 records 原始值 (原行为, 忽略排序/筛选)
        """
        if visual:
            info = await self.get_cell_render_info(col_field, row_index, iframe_selector)
            if info.get("ok"):
                return info.get("text", info.get("value"))
            # 行未渲染 (虚拟滚动视口外): 回退提示
            if info.get("reason") == "cell not rendered":
                raise Exception(
                    f"第 {row_index} 行不在渲染视口内 (虚拟滚动), 无法读取界面文本。"
                    f"请先滚动到该行, 或使用 visual=False 读取数据源, "
                    f"或使用 vtable_get_column_values / vtable_get_all_records。"
                )
            raise Exception(f"读取单元格渲染信息失败: {info.get('reason')}")

        records = await self.get_all_records(iframe_selector)
        if row_index < 0 or row_index >= len(records):
            raise Exception(f"行索引 {row_index} 越界，当前共 {len(records)} 行。")
        record = records[row_index]
        return record.get(col_field, None)

    async def _get_checked_keys(self, frame) -> List[str]:
        """读取当前已勾选的 checkbox 行 key 列表 (来自 VTable stateManager.checkedState)。"""
        return await frame.evaluate("""() => {
            const vtable = window._vtable;
            if (!vtable || !vtable.stateManager || !vtable.stateManager.checkedState) return [];
            const cs = vtable.stateManager.checkedState;
            const out = [];
            if (typeof cs.forEach === 'function') {
                cs.forEach((val, key) => {
                    if (val && val._vtable_checkbox === true) out.push(String(key));
                });
            }
            return out;
        }""")

    async def _is_checked(self, frame, record_index: int) -> bool:
        """判断指定行当前是否处于勾选状态。"""
        keys = await self._get_checked_keys(frame)
        return str(record_index) in keys

    async def select_rows(
        self,
        row_indexes: List[int],
        iframe_selector: str = "div[aria-hidden=false] iframe",
        action: str = "check",
    ) -> Dict[str, Any]:
        """
        通过真实鼠标点击 VTable canvas 上的复选框来勾选/取消勾选指定行。

        VTable 是 canvas 渲染，DOM 中不存在复选框元素，因此整个交互在一个工具内完成：
          1. 刷新 VTable 实例至 window._vtable (iframe 内)
          2. 在 iframe 上下文定位 checkbox 列、计算目标行 body 行号与单元格矩形；
             目标行不在可视区时自动 scrollToCell 滚动到该行
          3. 计算 iframe 相较于顶层文档的偏移 + canvas 相较于 iframe 视口的偏移，
             两次偏移合成出页面级点击坐标 (不分开，单次调用完成)
          4. 发送真实鼠标点击 (Playwright page.mouse)，命中 canvas 上的复选框
          5. 读取勾选前后 stateManager.checkedState 变化并返回，供调用方确认

        row_indexes: 要操作的行索引列表 (0-indexed 纯数据行，不含表头)。
        action: check=确保勾选(默认, 幂等) | uncheck=确保取消 | toggle=逐行切换。
        """
        if not row_indexes:
            raise Exception("row_indexes 不能为空")
        if action not in ("check", "uncheck", "toggle"):
            raise Exception(f"action 仅支持 check / uncheck / toggle，收到: {action}")

        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        page = await browser_mgr.get_page()

        # 点击前导航快照 (URL + iframe 清单 + 弹层指纹), 全部点击完成后统一观察
        before = await snapshot_navigation(page)

        locate_js = f"""
        () => {{
            const vtable = window._vtable;
            if (!vtable) return {{ error: 'window._vtable 未准备好' }};
            const columns = vtable.columns || (vtable.options && vtable.options.columns) || [];
            let colIdx = columns.findIndex(c =>
                c.field === '_vtable_checkbox' || c.cellType === 'checkbox' || c.headerType === 'checkbox'
            );
            if (colIdx === -1) return {{ error: '未找到复选框列' }};

            const canvas = vtable.container ? vtable.container.querySelector('canvas') : null;
            if (!canvas) return {{ error: '未找到 VTable canvas' }};
            const cr = canvas.getBoundingClientRect();
            const canvasH = cr.height;
            // getCellRect 返回的是表格绝对内容坐标, 需减去滚动偏移才是 canvas 可视区坐标
            const scrollTop = vtable.scrollTop || 0;
            const scrollLeft = vtable.scrollLeft || 0;

            const targets = {json.dumps(row_indexes)};
            const rects = [];
            for (const ri of targets) {{
                let bodyRow = null;
                try {{ bodyRow = vtable.getRecordStartRowByRecordIndex(ri); }} catch (e) {{ bodyRow = null; }}
                if (bodyRow == null) bodyRow = ri + (vtable.columnHeaderLevelCount || 1);
                const rect = vtable.getCellRect(colIdx, bodyRow);
                if (!rect || !rect.bounds) return {{ error: `无法获取第 ${{ri}} 行单元格矩形` }};
                const vx1 = rect.bounds.x1 - scrollLeft;
                const vx2 = rect.bounds.x2 - scrollLeft;
                const vy1 = rect.bounds.y1 - scrollTop;
                const vy2 = rect.bounds.y2 - scrollTop;
                rects.push({{
                    record_index: ri,
                    body_row: bodyRow,
                    rect: {{ x1: vx1, y1: vy1, x2: vx2, y2: vy2 }},
                    visible: vy1 >= 0 && vy2 <= canvasH
                }});
            }}
            return {{
                col_index: colIdx,
                canvas_rect: {{ left: cr.left, top: cr.top }},
                targets: rects
            }};
        }}
        """
        located = await frame.evaluate(locate_js)
        if located.get("error"):
            raise Exception(located["error"])

        # 顶层文档上下文：计算 iframe 相较于顶层视口的偏移 (Playwright 原生, 免手写 JS)
        iframe_box = await page.locator(iframe_selector).bounding_box()
        if not iframe_box:
            raise Exception(f"未找到匹配的 iframe: {iframe_selector}")
        iframe_rect = {"left": iframe_box["x"], "top": iframe_box["y"]}

        # 根据 action 决定每行是否需要点击（check/uncheck 幂等，toggle 全点）
        checked_keys = set(await self._get_checked_keys(frame))
        to_click = []
        for t in located["targets"]:
            key = str(t["record_index"])
            is_checked = key in checked_keys
            if action == "check" and not is_checked:
                to_click.append(t)
            elif action == "uncheck" and is_checked:
                to_click.append(t)
            elif action == "toggle":
                to_click.append(t)

        # 逐行处理: 每行点击前确保其位于可视区 (一次滚动 + 重新定位只针对当前行,
        # 避免多行分布在不同滚动位置时一次性滚动导致后续行坐标错位)
        # 注意: scrollToCell 接收 {row, col} 对象参数 (VTable 源码中按 e.col / e.row 取值)
        clicked = []
        for t in to_click:
            # 该行操作后的预期勾选状态 (用于点击后验证, 应对渲染时序竞态)
            if action == "check":
                expected = True
            elif action == "uncheck":
                expected = False
            else:
                expected = not (str(t["record_index"]) in checked_keys)

            for attempt in range(3):
                # 确保目标行位于可视区, 不可见则滚动并重新定位当前行
                if not t["visible"]:
                    await frame.evaluate("""(args) => {
                        const vtable = window._vtable;
                        if (typeof vtable.scrollToCell === 'function') {
                            vtable.scrollToCell({ row: args.bodyRow, col: args.colIndex });
                        }
                        return true;
                    }""", {"bodyRow": t["body_row"], "colIndex": located["col_index"]})
                    # 轮询滚动后该行进入可视区 (canvas 无 DOM 信号, 状态轮询替代固定 sleep)
                    current = None
                    for _ in range(20):
                        current = await frame.evaluate("""(args) => {
                            const vtable = window._vtable;
                            const rect = vtable.getCellRect(args.colIndex, args.bodyRow);
                            const cr = vtable.container.querySelector('canvas').getBoundingClientRect();
                            const scrollTop = vtable.scrollTop || 0;
                            const scrollLeft = vtable.scrollLeft || 0;
                            const vy1 = rect.bounds.y1 - scrollTop;
                            const vy2 = rect.bounds.y2 - scrollTop;
                            return {
                                rect: { x1: rect.bounds.x1 - scrollLeft, y1: vy1, x2: rect.bounds.x2 - scrollLeft, y2: vy2 },
                                canvas_rect: { left: cr.left, top: cr.top },
                                visible: vy1 >= 0 && vy2 <= cr.height
                            };
                        }""", {"bodyRow": t["body_row"], "colIndex": located["col_index"]})
                        if current.get("visible"):
                            break
                        await asyncio.sleep(0.1)
                    t["rect"] = current["rect"]
                    located["canvas_rect"] = current["canvas_rect"]
                    t["visible"] = current["visible"]

                # 两次偏移合成页面级坐标 (iframe 相对顶层 + canvas 相对 iframe), 发送真实鼠标点击
                cx = iframe_rect["left"] + located["canvas_rect"]["left"] + (t["rect"]["x1"] + t["rect"]["x2"]) / 2
                cy = iframe_rect["top"] + located["canvas_rect"]["top"] + (t["rect"]["y1"] + t["rect"]["y2"]) / 2
                logger.info(f"[select_rows] click row={t['record_index']} attempt={attempt + 1} visible={t['visible']} coord=({cx:.1f}, {cy:.1f})")
                await page.mouse.click(cx, cy)

                # 点击后验证: 轮询勾选状态直至预期 (canvas 无 DOM 信号, 用
                # "就绪即继续"的状态轮询替代固定 sleep; 未达成视为渲染竞态)
                checked_ok = False
                for _ in range(20):
                    if await self._is_checked(frame, t["record_index"]) == expected:
                        checked_ok = True
                        break
                    await asyncio.sleep(0.15)
                if checked_ok:
                    break

            clicked.append({"record_index": t["record_index"], "body_row": t["body_row"]})

        # 轮询勾选状态直至稳定，返回前后对比
        checked_after = await self._get_checked_keys(frame)
        for _ in range(6):
            if checked_after != sorted(checked_keys) or not clicked:
                break
            await asyncio.sleep(0.25)
            checked_after = await self._get_checked_keys(frame)

        # 点击后观察: 浮层/消息弹窗 + tab 页跳转 (URL) + iframe 跳转
        observation = await observe_after_click(page, before)

        return {
            "action": action,
            "clicked": clicked,
            "checked_before": sorted(checked_keys),
            "checked_after": sorted(checked_after),
            "added": sorted(set(checked_after) - checked_keys),
            "removed": sorted(checked_keys - set(checked_after)),
            "observation": observation,
        }

    # 点击前后统一观察 (浮窗/弹窗/消息提示/下拉浮层 + tab 页跳转 + iframe 跳转)
    # 已抽象至 qa_mcp.tools.browser 公共机制: snapshot_navigation / observe_after_click /
    # popup_fingerprint, 本类与 click_interact / execute_and_record 共用同一套逻辑。

    async def click_at(
        self,
        x: float,
        y: float,
        iframe_selector: str = "div[aria-hidden=false] iframe",
        coordinate_space: str = "viewport",
        click_type: str = "single",
    ) -> Dict[str, Any]:
        """
        在指定坐标处发送真实鼠标点击 (canvas 渲染的 VTable 内部元素只能基于坐标)。

        本工具是坐标点击的通用原语:
          - top 空间: 坐标即【浏览器顶层视口坐标】, 直接点击, 不依赖 VTable 实例
            —— 页面任意元素/区域 (普通 DOM 浮层选项、按钮等) 均可点击, 页面上
            没有 VTable 时同样成立; 配合 vtable_scan_columns /
            vtable_get_cell_center 返回的 viewportX/viewportY 点击 VTable
            内部图标/单元格时也直接可用, 无需任何偏移叠加。
          - viewport/content 空间: 坐标相对 VTable canvas (0,0 = canvas 左上角,
            与 getCellRect 返回的坐标同空间; content 额外自动扣除
            scrollLeft/scrollTop)。此二空间必须挂载 VTable 实例:
            1. 刷新实例并读取 canvas 可视区矩形与当前滚动偏移 (iframe 内)
            2. 合成 iframe 相对顶层偏移 + canvas 相对 iframe 偏移, 得到页面级坐标
            3. 发送真实鼠标点击 (单/双击), 返回实际页面坐标供调用方确认

        x/y: 目标坐标 (由 coordinate_space 决定参照系)
        coordinate_space: top(默认) | viewport | content
        click_type: single(默认) | double
        """
        space = coordinate_space.lower()
        if space not in ("top", "viewport", "content"):
            raise Exception(f"coordinate_space 仅支持 top / viewport / content, 收到: {coordinate_space}")
        click_kind = click_type.lower()
        if click_kind not in ("single", "double"):
            raise Exception(f"click_type 仅支持 single / double, 收到: {click_type}")

        page = await browser_mgr.get_page()

        # 点击前导航快照 (URL + iframe 清单 + 弹层指纹), 点击后统一观察对比
        before = await snapshot_navigation(page)

        # top 空间: 坐标即顶层视口坐标, 直接点击 (场景图脚本已算好偏移)。
        # 本空间不挂载 VTable 实例 —— 点击普通 DOM 浮层选项/按钮等非 VTable
        # 区域时, 不应强制要求页面上存在 VTable。
        if space == "top":
            px, py = x, y
            logger.info(f"[click_at] space=top coord=({x}, {y})")
            if click_kind == "double":
                await page.mouse.dblclick(px, py)
            else:
                await page.mouse.click(px, py)
            observation = await observe_after_click(page, before)
            return {
                "status": "success",
                "coordinate_space": space,
                "click_type": click_kind,
                "input_coord": {"x": x, "y": y},
                "page_coords": {"x": round(px, 2), "y": round(py, 2)},
                "vtable_mounted": False,
                # 点击后观察: 浮层/消息弹窗 + tab 页跳转 (URL) + iframe 跳转
                "observation": observation,
            }

        # viewport/content 空间: 坐标相对 VTable canvas (可视区/内容区),
        # 必须挂载实例读取 canvas 矩形与滚动偏移
        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)

        locate_js = """
        () => {
            const vtable = window._vtable;
            if (!vtable) return { error: 'window._vtable 未准备好' };
            const canvas = vtable.container ? vtable.container.querySelector('canvas') : null;
            if (!canvas) return { error: '未找到 VTable canvas' };
            const cr = canvas.getBoundingClientRect();
            return {
                canvas_rect: { left: cr.left, top: cr.top },
                scroll_left: vtable.scrollLeft || 0,
                scroll_top: vtable.scrollTop || 0
            };
        }
        """
        located = await frame.evaluate(locate_js)
        if located.get("error"):
            raise Exception(located["error"])

        # 顶层文档上下文: 计算 iframe 相较于顶层视口的偏移 (Playwright 原生, 免手写 JS)
        # (iframe_selector="" 表示 VTable 直接渲染在主文档, 偏移为 0)
        if iframe_selector:
            iframe_box = await page.locator(iframe_selector).bounding_box()
            if not iframe_box:
                raise Exception(f"未找到匹配的 iframe: {iframe_selector}")
            iframe_rect = {"left": iframe_box["x"], "top": iframe_box["y"]}
        else:
            iframe_rect = {"left": 0.0, "top": 0.0}

        # 坐标换算: content 空间扣除滚动偏移, viewport 空间即 canvas 可视区坐标
        if space == "content":
            canvas_x = x - located["scroll_left"]
            canvas_y = y - located["scroll_top"]
        else:
            canvas_x, canvas_y = x, y

        px = iframe_rect["left"] + located["canvas_rect"]["left"] + canvas_x
        py = iframe_rect["top"] + located["canvas_rect"]["top"] + canvas_y
        logger.info(f"[click_at] space={space} coord=({x}, {y}) -> page=({px:.1f}, {py:.1f})")

        if click_kind == "double":
            await page.mouse.dblclick(px, py)
        else:
            await page.mouse.click(px, py)

        # 点击后观察: 浮层/消息弹窗 + tab 页跳转 (URL) + iframe 跳转
        observation = await observe_after_click(page, before)

        return {
            "status": "success",
            "coordinate_space": space,
            "click_type": click_kind,
            "input_coord": {"x": x, "y": y},
            "page_coords": {"x": round(px, 2), "y": round(py, 2)},
            "vtable_mounted": True,
            "canvas_rect": located["canvas_rect"],
            "scroll": {"left": located.get("scroll_left", 0), "top": located.get("scroll_top", 0)},
            "iframe_rect": iframe_rect,
            "observation": observation,
        }

    async def drag_column(
        self,
        source: Union[int, str],
        target: Union[int, str],
        position: str = "after",
        iframe_selector: str = "div[aria-hidden=false] iframe",
    ) -> Dict[str, Any]:
        """
        通过真实鼠标拖拽 VTable 列头，把 source 列移动到 target 列的前方/后方。

        完全复刻人工操作 (canvas 渲染, 只能基于坐标 + 真实鼠标事件):
          1. 让源列进入整列选中状态 (VTable 要求整列选中才能启动列拖拽,
             即 select ranges 覆盖到全局最后一行, 否则 _canDragHeaderPosition 拒绝):
             先点击 source 列头中部 (headerSelectMode 为 single/body 等时一次点击即整列选中);
             若表头未启用整列选中 (headerSelectMode='cell', 点击只选中表头单元格),
             则兜底真实鼠标纵向框选: 在列头按下 → 拖到源列 body 最后一行松开 → 整列选中
          2. 再次按下鼠标不松 → 分步移动指针到落点 → 松开 (page.mouse down/move/up)
          3. 读取拖拽后的列顺序验证结果

        落点语义 (由 VTable 源码 dragHeader 机制决定, 落点列 = 指针所在列):
          - position="before": 落点列 T = target (当 target 在 source 左侧) 或 target-1 (右侧);
          - position="after":  落点列 T = target (当 target 在 source 右侧) 或 target+1 (左侧);
          - 指针在 T 上松开 → T.col < source.col 时 source 插到 T 前方, 否则插到 T 后方。

        不使用任何 VTable 实例 API 修改列位置 (changeHeaderPosition / updateColumns 等),
        仅用实例内部 API 读取坐标/属性/顺序用于定位与验证。

        source/target: 列索引 (int) 或字段名/列标题 (str)
        position: before=拖到 target 前方 | after=拖到 target 后方 (默认)
        """
        pos = (position or "after").lower()
        if pos not in ("before", "after"):
            raise Exception(f"position 仅支持 before / after，收到: {position}")

        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        page = await browser_mgr.get_page()

        # ---- 1. 解析源列/目标列索引 (当前可见顺序) ----
        resolve_js = f"""
        () => {{
            const t = window._vtable;
            if (!t) return {{ error: 'window._vtable 未准备好' }};
            const headerRow = Math.max((t.columnHeaderLevelCount || 1) - 1, 0);
            const fieldOf = (c) => {{ try {{ const f = t.getHeaderField ? t.getHeaderField(c, headerRow) : null; return f === null || f === undefined ? '' : String(f); }} catch (e) {{ return ''; }} }};
            const titleOf = (c) => {{ let s = ''; try {{ const v = t.getCellValue ? t.getCellValue(c, headerRow) : null; if (v !== null && v !== undefined) s = v; }} catch (e) {{}} if (!s) {{ try {{ const d = t.getHeaderDefine ? t.getHeaderDefine(c, headerRow) : null; if (d) s = d.title || d.caption || ''; }} catch (e) {{}} }} return typeof s === 'string' ? s : String(s); }};
            const colCount = t.colCount || 0;
            const resolve = (ref) => {{
                if (typeof ref === 'number') {{
                    return (ref >= 0 && ref < colCount) ? ref : {{ error: '列索引越界: ' + ref + ' (colCount=' + colCount + ')' }};
                }}
                const s = String(ref);
                for (let c = 0; c < colCount; c++) {{
                    if (fieldOf(c) === s || titleOf(c) === s) return c;
                }}
                return {{ error: '未找到列: ' + s + ' (可尝试用列索引, 或先 vtable_scan_columns 查看实际列标题)' }};
            }};
            const src = resolve({json.dumps(source)});
            if (src && src.error) return src;
            const tgt = resolve({json.dumps(target)});
            if (tgt && tgt.error) return tgt;
            return {{ ok: true, sourceCol: src, targetCol: tgt, fieldOf: fieldOf(src), titleOf: titleOf(src), targetField: fieldOf(tgt), targetTitle: titleOf(tgt) }};
        }}
        """
        resolved = await frame.evaluate(resolve_js)
        if resolved.get("error"):
            raise Exception(resolved["error"])
        source_col = resolved["sourceCol"]
        target_col = resolved["targetCol"]

        # ---- 2. 计算落点列 (VTable 原生语义: 指针所在列决定前后) ----
        if pos == "before":
            drop_col = target_col if target_col < source_col else target_col - 1
        else:
            drop_col = target_col if target_col > source_col else target_col + 1
        if drop_col == source_col:
            return {
                "status": "noop",
                "reason": f"源列 {resolved['fieldOf']} 已在目标列 {resolved['targetField']} 的{('前方' if pos == 'before' else '后方')}, 无需拖拽",
                "source": {"col": source_col, "field": resolved["fieldOf"], "title": resolved["titleOf"]},
                "target": {"col": target_col, "field": resolved["targetField"], "title": resolved["targetTitle"]},
                "position": pos,
            }

        # ---- 3. 采集拖拽几何信息 (仅读) ----
        geom = await _run_vtable_js(frame, f"getHeaderDragGeometry({int(source_col)}, {int(drop_col)})")
        if geom.get("error"):
            raise Exception(f"采集列头几何信息失败: {geom['error']}")
        geom = _sanitize_geom(geom)

        drag_mode = geom.get("dragHeaderMode")
        if drag_mode not in ("all", "column"):
            raise Exception(
                f"VTable 未开启列头拖拽: dragHeaderMode={drag_mode} "
                f"(需 'all' 或 'column', 前端需配置 dragHeaderMode 或 dragOrder.dragHeaderMode)"
            )
        if geom.get("sourceCanDragByDefine") is False and not geom.get("sourceIsFrozen"):
            # 列级 dragHeader:false 会直接禁用该列拖拽
            try:
                define_ok = await frame.evaluate(
                    """(args) => {
                        const t = window._vtable;
                        try {
                            const d = t.getHeaderDefine ? t.getHeaderDefine(args.col, args.row) : null;
                            return d ? !(d.dragHeader === false) : true;
                        } catch (e) { return true; }
                    }""",
                    {"col": source_col, "row": geom["headerRow"]},
                )
            except Exception:
                define_ok = True
            if not define_ok:
                raise Exception(f"源列 {resolved['fieldOf']} 配置了 dragHeader:false, 禁止拖拽换位")

        src_h = geom.get("sourceHeader")
        drop_h = geom.get("dropHeader")
        if not src_h or not _rect_coords_ok(src_h):
            raise Exception(
                "无法获取源列表头矩形 (该列可能未渲染或坐标无效): "
                "请先横向滚动使源列可见后重试"
            )
        if not drop_h or not _rect_coords_ok(drop_h):
            raise Exception(
                f"无法获取落点列 (col={drop_col}) 表头矩形: 该列可能未渲染 (虚拟滚动), "
                f"请先横向滚动使源列与目标列同时可见后重试"
            )
        if not src_h.get("visible"):
            raise Exception("源列表头当前不可见 (横向视口外), 请先横向滚动使源列可见后重试")
        if not drop_h.get("visible"):
            raise Exception(
                f"落点列 (col={drop_col}) 表头当前不可见 (横向视口外), "
                f"请先横向滚动使源列与目标列同时可见后重试"
            )

        frozen_mode = geom.get("frozenColDragHeaderMode")
        if frozen_mode == "disabled":
            if geom.get("sourceIsFrozen"):
                raise Exception(
                    f"源列 {resolved['fieldOf']} 为冻结列且 frozenColDragHeaderMode=disabled, 禁止拖拽"
                )
            if geom.get("dropIsFrozen"):
                raise Exception(
                    f"落点列 (col={drop_col}) 为冻结列且 frozenColDragHeaderMode=disabled, 无法拖入冻结区"
                )

        src_cx = (src_h["x1"] + src_h["x2"]) / 2
        src_cy = (src_h["y1"] + src_h["y2"]) / 2
        drop_cx = (drop_h["x1"] + drop_h["x2"]) / 2
        drop_cy = (drop_h["y1"] + drop_h["y2"]) / 2

        # ---- 4. 真实交互: 点击列头中部选中整列 (VTable 拖拽启动前提) ----
        # 列头单元格内可能渲染交互图标 (排序/筛选/冻结/下拉等), pointerdown 命中
        # 图标时 VTable 会消费事件、跳过选中与拖拽启动, 因此点击点需避开图标。
        header_row_num = _finite_num(geom.get("headerRow"), 0)
        header_row_num = int(header_row_num)
        icon_info = await _run_vtable_js(
            frame,
            f"getCellIconsViewport({int(source_col)}, {header_row_num}, '', 'basic')",
        )
        blockers = []
        for ic in (icon_info or {}).get("icons", []) or []:
            name = str(ic.get("name") or "")
            if name in ("content", "text", ""):
                continue  # 文本节点不拦截交互
            w, h = ic.get("width") or 0, ic.get("height") or 0
            if 0 < w <= 500 and 0 < h <= 500:
                blockers.append({"x": ic.get("viewportX"), "y": ic.get("viewportY"), "w": w, "h": h})

        def _icon_free_point() -> tuple:
            width = src_h["x2"] - src_h["x1"]
            py = (src_h["y1"] + src_h["y2"]) / 2
            best = None
            for frac in (0.5, 0.25, 0.75, 0.12, 0.88, 0.38, 0.62):
                px = src_h["x1"] + width * frac
                free = all(
                    abs(px - b["x"]) > b["w"] / 2 + 6 or abs(py - b["y"]) > b["h"] / 2 + 6
                    for b in blockers
                )
                if free:
                    return px, py
                if best is None:
                    best = (px, py)
            return best if best is not None else (src_cx, src_cy)

        click_points = [_icon_free_point()]
        if click_points[0] != (src_cx, src_cy):
            click_points.append((src_cx, src_cy))

        async def _select_source_column() -> bool:
            for px, py in click_points:
                await page.mouse.click(px, py)
                # 轮询选中状态直至就绪 (替代固定 sleep)
                for _ in range(24):
                    sel = await _run_vtable_js(
                        frame, f"getColumnSelectionState({int(source_col)})"
                    )
                    if isinstance(sel, dict) and sel.get("selected"):
                        return True, px, py
                    await asyncio.sleep(0.1)
            return False, None, None

        # ---- 4b. 兜底: 真实鼠标纵向框选整列 ----
        # 表头未启用整列选中 (headerSelectMode='cell') 时, 点击列头只选中表头单元格,
        # 无法满足 VTable 的拖拽启动前提 (整列选中到全局最后一行)。此时复刻人工操作:
        # 在源列头按下 → 纵向拖到源列 body 最后一行松开 → 形成覆盖整列的选中范围,
        # 之后再次按下列头即可正常启动列头拖拽。全程真实鼠标事件, 不使用实例 API 改状态。
        async def _select_source_column_by_drag() -> bool:
            last_row = geom.get("lastBodyRowGlobal")
            last_center = geom.get("sourceLastBodyCenter")
            if _finite_num(last_row) is None:
                return False, None, None
            last_row = int(_finite_num(last_row))
            # 几何无效 (哨兵值 → 净化后 None, 或坐标非有限) 视为"该行未渲染", 需先滚动再重采
            if not _point_ok(last_center):
                last_center = None
            # 源列 body 最后一行不在视口内 (虚拟滚动未渲染 → 无几何) → 先纵向滚动到该行。
            # 滚动仅移动视图, 不改列顺序; 表头行不随纵向滚动, 起点坐标仍有效。
            if not geom.get("sourceLastBodyVisible", True) or not last_center:
                try:
                    await frame.evaluate(
                        """(args) => {
                            const t = window._vtable;
                            if (!t || typeof t.scrollToRow !== 'function') return false;
                            t.scrollToRow(args.row);
                            return true;
                        }""",
                        {"row": int(last_row)},
                    )
                    # 轮询滚动后重采几何, 直至源列最后一行几何有效 (替代固定 sleep)
                    last_center = None
                    for _ in range(20):
                        geom2 = await _run_vtable_js(
                            frame,
                            f"getHeaderDragGeometry({int(source_col)}, {int(drop_col)})",
                        )
                        geom2 = _sanitize_geom(geom2 or {})
                        last_center = (geom2 or {}).get("sourceLastBodyCenter")
                        if _point_ok(last_center):
                            break
                        last_center = None
                        await asyncio.sleep(0.15)
                except Exception:
                    last_center = None
                if not last_center:
                    return False, None, None
            for cx, cy in click_points:
                await page.mouse.move(cx, cy)
                await asyncio.sleep(0.08)
                await page.mouse.down()
                await asyncio.sleep(0.1)
                # 坐标已在上方校验为有限数字, 这里再兜底一次, 防止异常数据触发 int(NaN) 崩溃
                tx = _finite_num(last_center.get("x"), 0.0)
                ty = _finite_num(last_center.get("y"), 0.0)
                # 一次 move + steps: 与主拖拽一致, 由 Playwright 内部连续派发 mousemove,
                # 不依赖 ease-in-out 轨迹; 步数与纵向距离成正比 (上限 32)
                steps = max(8, min(32, int(abs(ty - cy) / 50) + 1))
                await page.mouse.move(tx, ty, steps=steps)
                # 终点悬停稳定 (让 VTable 渲染选中范围)
                await asyncio.sleep(0.15)
                await page.mouse.up()
                # 轮询选中状态直至就绪 (替代固定 sleep)
                for _ in range(24):
                    sel = await _run_vtable_js(
                        frame, f"getColumnSelectionState({int(source_col)})"
                    )
                    if isinstance(sel, dict) and sel.get("selected"):
                        return True, cx, cy
                    await asyncio.sleep(0.1)
            return False, None, None

        # ---- 4c. 兜底: 编程式整列选中 ----
        # headerSelectMode=None 且表格禁用鼠标框选 (select.disableDragSelect) 时, 点击与
        # 真实框选都无法让源列整列进入选中状态 (VTable 拖拽启动前提: ranges 覆盖全局最后一行)。
        # 此兜底通过 VTable 公开 API selectCells / selectRanges (或写 stateManager.select.ranges)
        # 将源列整列设为选中范围 —— 仅设置选中状态, 不修改任何列位置, 列顺序变更仍由真实
        # 鼠标拖拽触发。即使该兜底也失败, 也不中断执行: 继续完整动作链, 是否生效交给最终验证。
        async def _select_source_column_programmatic() -> bool:
            try:
                prog = await frame.evaluate(
                    """(args) => {
                        const t = window._vtable;
                        const sm = t && t.stateManager;
                        if (!sm) return { ok: false, reason: '无 stateManager' };
                        const lastRow = (t.rowCount || 1) - 1;
                        const range = { start: { col: args.col, row: 0 }, end: { col: args.col, row: lastRow } };
                        let method = '';
                        if (typeof t.selectCells === 'function') { t.selectCells({ range, add: false }); method = 'selectCells'; }
                        else if (typeof t.selectRanges === 'function') { t.selectRanges([range]); method = 'selectRanges'; }
                        else if (sm.select) { sm.select.ranges = [range]; method = 'stateManager'; }
                        else return { ok: false, reason: '无 selectCells/selectRanges/stateManager.select' };
                        return { ok: true, method };
                    }""",
                    {"col": int(source_col)},
                )
                # 轮询选中状态直至就绪 (替代固定 sleep)
                for _ in range(20):
                    sel = await _run_vtable_js(
                        frame, f"getColumnSelectionState({int(source_col)})"
                    )
                    if isinstance(sel, dict) and sel.get("selected"):
                        return True
                    await asyncio.sleep(0.1)
                return False
            except Exception as e:
                logger.warning(f"[drag_column] 编程式整列选中失败: {e}")
                return False

        selected, sel_x, sel_y = await _select_source_column()
        if not selected:
            # 重试一轮 (个别表格首次点击有渲染时序/需先收起浮层)
            selected, sel_x, sel_y = await _select_source_column()
        if not selected:
            # 点击无法整列选中 (如 headerSelectMode='cell') → 真实鼠标框选整列兜底
            selected, sel_x, sel_y = await _select_source_column_by_drag()
        if not selected:
            # 框选也未生效 (headerSelectMode=None / 框选禁用) → 编程式整列选中兜底;
            # 不中断执行, 若兜底也失败则记录警告后继续完整拖拽动作链
            if await _select_source_column_programmatic():
                selected, sel_x, sel_y = True, None, None
            else:
                logger.warning(
                    f"[drag_column] 源列 {resolved['fieldOf']} 未能进入整列选中状态 "
                    f"(headerSelectMode={geom.get('headerSelectMode')}), 继续执行拖拽动作链, "
                    f"是否生效由最终验证判定"
                )

        # 选中后再确认拖拽启动条件 (列级 dragHeader 配置)
        can_drag = await frame.evaluate(
            """(args) => {
                const t = window._vtable;
                try { return !!(t._canDragHeaderPosition && t._canDragHeaderPosition(args.col, args.row)); }
                catch (e) { return false; }
            }""",
            {"col": source_col, "row": header_row_num},
        )
        if not can_drag:
            # 不中断: 拖拽启动条件未满足时仍执行完整动作链, 是否生效交给最终验证
            logger.warning(
                f"[drag_column] 源列 {resolved['fieldOf']} 未满足拖拽启动条件 "
                f"(dragHeaderMode={drag_mode}, headerSelectMode={geom.get('headerSelectMode')}, "
                f"frozenColDragHeaderMode={frozen_mode}), 继续执行拖拽动作链"
            )

        # ---- 5. 分步真实拖拽 (按下 → 缓动移动 → 松开) ----
        press_x = sel_x if sel_x is not None else src_cx
        press_y = sel_y if sel_y is not None else src_cy
        logger.info(
            f"[drag_column] src=col{source_col}({resolved['fieldOf']}) "
            f"target=col{target_col}({resolved['targetField']}) position={pos} "
            f"drop=col{drop_col} coord=({drop_cx:.1f}, {drop_cy:.1f}) press=({press_x:.1f}, {press_y:.1f})"
        )
        await page.mouse.move(press_x, press_y)
        await asyncio.sleep(0.08)
        await page.mouse.down()
        await asyncio.sleep(0.1)
        # 一次 move + steps: 由 Playwright 内部连续派发 mousemove (无逐步 await/sleep),
        # 事件节奏交给浏览器按帧 coalesce —— 比"逐步 await + sleep(0.045)" (约 20fps) 更平滑,
        # 接近真实鼠标拖拽。VTable 拖拽只关心按下状态与落点, 不依赖 ease-in-out 轨迹, 线性插值即可。
        await page.mouse.move(drop_cx, drop_cy, steps=14)
        # 落点悬停稳定 (让 VTable 渲染拖拽指示线并更新目标列)
        await asyncio.sleep(0.15)
        await page.mouse.up()
        # 轮询拖拽结果: 列顺序已变则立即继续 (canvas 无 DOM 信号, 状态轮询替代固定 sleep)
        after_geom = None
        for _ in range(10):
            after_geom = await _run_vtable_js(
                frame,
                f"getHeaderDragGeometry({int(source_col)}, {int(drop_col)})",
            )
            after_geom = _sanitize_geom(after_geom or {})
            if (
                isinstance(after_geom.get("fields"), list)
                and after_geom["fields"] != geom.get("fields", [])
            ):
                break
            await asyncio.sleep(0.05)

        # ---- 6. 读取拖拽后的列顺序并验证 ----
        after = after_geom.get("fields")
        if not isinstance(after, list):
            raise Exception(f"拖拽后读取列顺序失败: {after_geom}")
        fields_before = geom.get("fields", [])
        fields_after = after
        titles_after = after_geom.get("titles", [])
        src_field = resolved["fieldOf"]
        tgt_field = resolved["targetField"]
        src_new = fields_after.index(src_field) if src_field in fields_after else -1
        tgt_new = fields_after.index(tgt_field) if tgt_field in fields_after else -1
        src_old = fields_before.index(src_field) if src_field in fields_before else -1

        moved = fields_after != fields_before
        if pos == "before":
            ok = moved and src_new + 1 == tgt_new
        else:
            ok = moved and src_new == tgt_new + 1

        if not ok:
            # 不中断: 动作链已完整执行, 返回明确诊断结果而非抛异常
            return {
                "status": "not_effective",
                "reason": (
                    f"列拖拽动作链已执行但列顺序未变化: 期望 {resolved['titleOf']} "
                    f"在 {resolved['targetTitle']} 的{('前方' if pos == 'before' else '后方')} "
                    f"(新位置 src={src_new}, target={tgt_new}, moved={moved}). "
                    f"诊断: dragHeaderMode={drag_mode}, headerSelectMode={geom.get('headerSelectMode')}, "
                    f"frozenColDragHeaderMode={frozen_mode}. "
                    f"可能原因: 目标与源列跨分组/层级 (VTable 默认禁止跨父级移动), "
                    f"整列选中未达成 (选中机制被禁用), 或前端 validateDragOrderOnEnd 拒绝了本次移动。"
                ),
                "source": {"col": source_col, "field": src_field, "title": resolved["titleOf"]},
                "target": {"col": target_col, "field": tgt_field, "title": resolved["targetTitle"]},
                "position": pos,
                "selection": {"selected_full_column": selected, "header_select_mode": geom.get("headerSelectMode")},
                "drag_options": {
                    "dragHeaderMode": drag_mode,
                    "frozenColDragHeaderMode": frozen_mode,
                },
                "verification": {
                    "ok": False,
                    "source_index_before": src_old,
                    "source_index_after": src_new,
                    "target_index_after": tgt_new,
                },
            }

        return {
            "status": "success",
            "source": {"col": source_col, "field": src_field, "title": resolved["titleOf"]},
            "target": {"col": target_col, "field": tgt_field, "title": resolved["targetTitle"]},
            "position": pos,
            "drop": {
                "col": drop_col,
                "field": fields_before[drop_col] if drop_col < len(fields_before) else None,
                "page_coords": {"x": round(drop_cx, 2), "y": round(drop_cy, 2)},
            },
            "selection": {"selected_full_column": True, "header_select_mode": geom.get("headerSelectMode")},
            "drag_options": {
                "dragHeaderMode": drag_mode,
                "frozenColDragHeaderMode": frozen_mode,
            },
            "verification": {
                "ok": True,
                "source_index_before": src_old,
                "source_index_after": src_new,
                "target_index_after": tgt_new,
            },
            "columns_before": [
                {"field": f, "title": t} for f, t in zip(fields_before, geom.get("titles", []))
            ],
            "columns_after": [
                {"field": f, "title": t} for f, t in zip(fields_after, titles_after)
            ],
        }

    async def resize_column(
        self,
        col: Union[int, str],
        width: int,
        iframe_selector: str = "div[aria-hidden=false] iframe",
    ) -> Dict[str, Any]:
        """
        通过真实鼠标拖拽 VTable 列头分隔线，把指定列宽调整到目标像素值。

        完全复刻人工操作 (canvas 渲染, 只能基于坐标 + 真实鼠标事件):
          1. 采集列头矩形 (scenegraph 优先 / getCellRect 兜底, 合成顶层视口坐标)
             → 分隔线 = 列头右边界 x2, 拖拽线 = 表头行中线 y
          2. 悬停分隔线 → 按下鼠标不松 → 分步缓动移动到目标位置 (x1 + width) → 松开
             (page.mouse down/move/up, 拖距 = 目标列宽 - 当前列宽)
          3. 拖拽后重读列宽验证结果 (误差 ≤ 2px)

        不使用任何 VTable 实例 API 修改列宽 (resizeColumn / updateColumns 等)，
        仅用实例内部 API 读取坐标/属性/配置用于定位与验证 —— 与 vtable_drag_column
        同一设计原则, 真实 UI 操作可复现到任何启用 columnResize 的 VTable。

        col: 列索引 (int) 或字段名/列标题 (str)
        width: 目标列宽 (px, 正数)
        """
        if not width or int(width) <= 0:
            raise Exception(f"width 必须为正数 (px), 收到: {width}")
        target_width = int(width)

        await self.refresh_instance(iframe_selector)
        frame = await self._get_target_frame(iframe_selector)
        page = await browser_mgr.get_page()

        # ---- 1. 采集列头几何 + 能力开关 (仅读) ----
        geom = await _run_vtable_js(frame, f"getHeaderResizeGeometry({json.dumps(col)})")
        if geom.get("error"):
            raise Exception(geom["error"])
        geom = _sanitize_geom(geom)

        h = geom.get("header")
        if not h or not _rect_coords_ok(h):
            raise Exception(
                "无法获取列头矩形 (该列可能未渲染或坐标无效): "
                "请先横向滚动使该列可见后重试"
            )
        if not h.get("visible"):
            raise Exception(
                f"列头当前不可见 (横向视口外): col={geom['col']}({geom['field']}), "
                f"请先横向滚动使该列可见后重试"
            )

        resize_cfg = geom.get("resize") or {}
        if resize_cfg.get("resizeEnabled") is False:
            raise Exception(
                f"VTable 未开启列宽调整: columnResize.resizable=false "
                f"(resize 配置={resize_cfg.get('resize')}), 无法拖拽调整列宽"
            )
        min_w = resize_cfg.get("minColumnWidth")
        max_w = resize_cfg.get("maxColumnWidth")
        if isinstance(min_w, (int, float)) and target_width < min_w:
            raise Exception(f"目标宽度 {target_width}px 小于配置的最小列宽 {min_w}px")
        if isinstance(max_w, (int, float)) and target_width > max_w:
            raise Exception(f"目标宽度 {target_width}px 大于配置的最大列宽 {max_w}px")

        cur_width = _finite_num(h.get("width"), 0.0)
        start_x = _finite_num(h.get("x2"), 0.0)  # 分隔线 = 列头右边界
        start_y = (_finite_num(h.get("y1"), 0.0) + _finite_num(h.get("y2"), 0.0)) / 2
        target_x = _finite_num(h.get("x1"), 0.0) + target_width
        delta = target_x - start_x

        # ---- 2. 真实交互: 悬停分隔线 → 按下 → 分步缓动拖动 → 松开 ----
        logger.info(
            f"[resize_column] col={geom['col']}({geom['field']}) "
            f"width {cur_width}px -> {target_width}px "
            f"drag ({start_x:.1f}, {start_y:.1f}) -> ({target_x:.1f}, {start_y:.1f})"
        )
        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(0.12)  # 悬停稳定, 让 VTable 进入 resize 判定区
        await page.mouse.down()
        await asyncio.sleep(0.12)
        # 一次 move + steps: 与 vtable_drag_column 一致, 由 Playwright 内部连续派发
        # mousemove, 不依赖 ease-in-out 轨迹, 平滑度接近真实鼠标拖拽
        await page.mouse.move(target_x, start_y, steps=18)
        await asyncio.sleep(0.15)  # 落点悬停稳定 (VTable 渲染拖拽反馈)
        await page.mouse.up()
        # 轮询列宽直至接近目标 (canvas 无 DOM 信号, 状态轮询替代固定 sleep)
        after = None
        for _ in range(10):
            after = await _run_vtable_js(frame, f"getColumnWidth({json.dumps(geom['col'])})")
            w = (after or {}).get("width") if isinstance(after, dict) else None
            if w is not None and abs(w - target_width) <= 2:
                break
            await asyncio.sleep(0.05)

        # ---- 3. 重读列宽验证 ----
        after_w = (after or {}).get("width") if isinstance(after, dict) else None
        if after_w is None:
            raise Exception(f"拖拽后读取列宽失败: {after}")
        ok = abs(after_w - target_width) <= 2
        return {
            "status": "success" if ok else "partial",
            "col": geom["col"],
            "field": geom["field"],
            "title": geom["title"],
            "width_before": cur_width,
            "width_target": target_width,
            "width_after": after_w,
            "delta": round(delta, 1),
            "drag_points": {
                "start": {"x": round(start_x, 2), "y": round(start_y, 2)},
                "end": {"x": round(target_x, 2), "y": round(start_y, 2)},
            },
            "resize_config": {
                "columnResize": resize_cfg.get("columnResize"),
                "resize": resize_cfg.get("resize"),
                "columnResizeMode": resize_cfg.get("columnResizeMode"),
            },
            "verified": ok,
            "message": (
                f"列 [{geom['title']}] 列宽 {cur_width}px → {after_w}px "
                f"(目标 {target_width}px): "
                f"{'✅ 已生效' if ok else '⚠️ 未完全命中, 请检查 columnResize 配置/该列是否可调整'}"
            ),
        }

vtable_mgr = VTableManager()

# -------- 用于 Provider 注册的纯函数外壳 --------

async def vtable_refresh_instance_impl(iframe_selector: str = "div[aria-hidden=false] iframe") -> Dict[str, Any]:
    return await vtable_mgr.refresh_instance(iframe_selector)

async def vtable_analyze_headers_impl(iframe_selector: str = "div[aria-hidden=false] iframe") -> List[Dict[str, Any]]:
    return await vtable_mgr.analyze_headers(iframe_selector)

async def vtable_get_row_count_impl(iframe_selector: str = "div[aria-hidden=false] iframe") -> int:
    return await vtable_mgr.get_row_count(iframe_selector)

async def vtable_get_all_records_impl(iframe_selector: str = "div[aria-hidden=false] iframe") -> List[Dict[str, Any]]:
    return await vtable_mgr.get_all_records(iframe_selector)


async def vtable_records_view_impl(
    iframe_selector: str = "div[aria-hidden=false] iframe", max_rows: int = 1000
):
    """VTable 全量数据可视化 (Claude Desktop Apps UI): 返回可搜索/排序的 DataTable。

    Claude Code (TUI) 不渲染 Apps UI, 降级使用 vtable_get_all_records (JSON)。
    """
    from prefab_ui.app import PrefabApp
    from prefab_ui.components import Column, DataTable, DataTableColumn, Heading, Text

    records = await vtable_mgr.get_all_records(iframe_selector)
    if not records:
        return PrefabApp(state={}, view=Column(gap=2, children=[Text("表格为空")]))
    if max_rows and len(records) > max_rows:
        records = records[:max_rows]
    keys = list(records[0].keys())
    columns = [
        DataTableColumn(key=k, header=k, sortable=True) for k in keys[:20]
    ]
    with PrefabApp(state={}) as app:
        with Column(gap=3, css_class="p-4"):
            Heading(f"VTable 数据 ({len(records)} 行)")
            DataTable(columns=columns, rows=records, search=True)
    return app

async def vtable_get_cell_text_impl(row_index: int, col_field: str, iframe_selector: str = "div[aria-hidden=false] iframe", visual: bool = True) -> Any:
    return await vtable_mgr.get_cell_text(row_index, col_field, iframe_selector, visual)

async def vtable_select_rows_impl(row_indexes: List[int], iframe_selector: str = "div[aria-hidden=false] iframe", action: str = "check") -> Dict[str, Any]:
    return await vtable_mgr.select_rows(row_indexes, iframe_selector, action)

async def vtable_scan_columns_impl(iframe_selector: str = "div[aria-hidden=false] iframe", max_col: int = 200) -> List[Dict[str, Any]]:
    return await vtable_mgr.scan_columns(iframe_selector, max_col)

async def vtable_get_column_values_impl(titles: List[str], iframe_selector: str = "div[aria-hidden=false] iframe", raw: bool = False) -> Dict[str, Any]:
    return await vtable_mgr.get_column_values(titles, iframe_selector, raw)

async def vtable_get_cell_render_info_impl(col_field: Union[int, str], row_index: int, iframe_selector: str = "div[aria-hidden=false] iframe", detail: str = "basic") -> Dict[str, Any]:
    return await vtable_mgr.get_cell_render_info(col_field, row_index, iframe_selector, detail)

async def vtable_get_cell_center_impl(col_field: Union[int, str], row_index: int, iframe_selector: str = "div[aria-hidden=false] iframe") -> Dict[str, Any]:
    return await vtable_mgr.get_cell_center(col_field, row_index, iframe_selector)

async def vtable_scroll_to_impl(
    col_field: Union[int, str, None] = None,
    row_index: Union[int, None] = None,
    scroll_left: Union[int, float, None] = None,
    scroll_top: Union[int, float, None] = None,
    iframe_selector: str = "div[aria-hidden=false] iframe",
    verify: bool = True,
) -> Dict[str, Any]:
    return await vtable_mgr.scroll_to(col_field, row_index, scroll_left, scroll_top, iframe_selector, verify)

async def vtable_drag_column_impl(
    source: Union[int, str],
    target: Union[int, str],
    position: str = "after",
    iframe_selector: str = "div[aria-hidden=false] iframe",
) -> Dict[str, Any]:
    return await vtable_mgr.drag_column(source, target, position, iframe_selector)

async def vtable_resize_column_impl(
    col: Union[int, str],
    width: int,
    iframe_selector: str = "div[aria-hidden=false] iframe",
) -> Dict[str, Any]:
    return await vtable_mgr.resize_column(col, width, iframe_selector)
