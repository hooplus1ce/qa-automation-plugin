"""VTable 工具 Provider 扩展 (FastMCP 3.x/4.0 规范)。"""

from collections.abc import Sequence

from fastmcp.server.providers import Provider
from fastmcp.tools import Tool

from qa_mcp.config import INTERACTIVE_UI_ENABLED


class VTableAutomationProvider(Provider):
    """VTable (canvas 渲染表格) 工具集: 实例刷新 / 列头分析 / 单元格读取 / 滚动 / 勾选 / 拖拽。"""

    def __init__(self) -> None:
        super().__init__()
        from qa_mcp.tools.vtable import (
            vtable_analyze_headers_impl,
            vtable_drag_column_impl,
            vtable_get_all_records_impl,
            vtable_get_cell_center_impl,
            vtable_get_cell_render_info_impl,
            vtable_get_cell_text_impl,
            vtable_get_column_values_impl,
            vtable_get_row_count_impl,
            vtable_records_view_impl,
            vtable_refresh_instance_impl,
            vtable_resize_column_impl,
            vtable_scan_columns_impl,
            vtable_scroll_to_impl,
            vtable_select_rows_impl,
        )

        self._tools: list[Tool] = [
            Tool.from_function(
                vtable_refresh_instance_impl,
                name="vtable_refresh_instance",
                description="刷新目标 iframe 中最新 VTable 实例（window._vtable），作为其他 vtable 操作的前置条件。",
            ),
            Tool.from_function(
                vtable_analyze_headers_impl,
                name="vtable_analyze_headers",
                description="分析 vtable 列头与单元格内交互图标：header_icons 为表头行真实渲染图标（排序/筛选/下拉/冻结等，含顶层视口坐标可直接点击）；cell_icons 为已渲染 body 单元格内图标（行内按钮/链接/checkbox/开关等，columns 配置中不存在，仅场景图可得）；capabilities 汇总 sortable/filterable/interactiveCell。用于规划列头点击与行内交互。",
            ),
            Tool.from_function(
                vtable_scan_columns_impl,
                name="vtable_scan_columns",
                description="扫描 VTable 全部列（含多级表头）：返回每列标题、body 行为分类（checkbox/button/文本等）及表头图标的顶层视口坐标 viewportX/viewportY，坐标可直接传给 click_interact(by=\"coordinate\", coordinate_space=\"top\")。用于规划列头点击（排序/筛选/下拉）与列交互。",
            ),
            Tool.from_function(
                vtable_get_row_count_impl,
                name="vtable_get_row_count",
                description="读取当前 VTable 纯数据行数（内部记录集合长度，不受屏幕滚动截断影响）。",
            ),
            Tool.from_function(
                vtable_get_all_records_impl,
                name="vtable_get_all_records",
                description="一次性读取表格中全部完整后台行记录（JSON），用于整表断言和宏观数据检查。",
            ),
            *(
                [
                    Tool.from_function(
                        vtable_records_view_impl,
                        name="vtable_records_view",
                        meta={
                            "ui": __import__(
                                "fastmcp.apps.config",
                                fromlist=["app_config_to_meta_dict"],
                            ).app_config_to_meta_dict(True)
                        },
                        description="VTable 全量数据可视化（Claude Desktop Apps UI）：渲染可搜索/排序的 DataTable 供浏览。Claude Code (TUI) 不渲染 Apps UI，降级使用 vtable_get_all_records (JSON)。max_rows 限制渲染行数（默认 1000）。",
                    )
                ]
                if INTERACTIVE_UI_ENABLED
                else []
            ),
            Tool.from_function(
                vtable_get_cell_text_impl,
                name="vtable_get_cell_text",
                description="读取单元格值。row_index 为纯数据行号（0 为第一行），col_field 支持字段名或列标题。visual=True（默认）读渲染层文本，与界面一致——排序/筛选仅在渲染层，须用它才能读真实顺序；visual=False 读数据源原始值（忽略排序/筛选）。视口外的行需先滚动。",
            ),
            Tool.from_function(
                vtable_get_column_values_impl,
                name="vtable_get_column_values",
                description="按列标题读取该列所有单元格值。titles 为列标题数组；raw=false 读渲染层视觉文本（与界面一致），raw=true 读原始字段值。返回每列值列表及缺失列。",
            ),
            Tool.from_function(
                vtable_get_cell_render_info_impl,
                name="vtable_get_cell_render_info",
                description="读取单元格渲染详情：视觉文本、文字/背景/边框色、字体大小及文本/背景节点（detail=\"full\" 含全部节点）。col_field 支持列索引或字段名/列标题，row_index 为纯数据行号（0 为第一行）。用于断言展示样式（标签色、高亮等）。",
            ),
            Tool.from_function(
                vtable_get_cell_center_impl,
                name="vtable_get_cell_center",
                description="读取单元格中心顶层视口坐标 viewportX/viewportY（含 iframe 与容器偏移），可直接作为 click_interact(by=\"coordinate\", coordinate_space=\"top\") 点击坐标。col_field 支持列索引或字段名/列标题，row_index 为纯数据行号（0 为第一行）。",
            ),
            Tool.from_function(
                vtable_scroll_to_impl,
                name="vtable_scroll_to",
                description="滚动 VTable 至目标位置（实例 API scrollToCol/scrollToRow/scrollToCell/setScrollLeft/setScrollTop）。四种用法：1) col_field+row_index 滚到指定单元格；2) 仅 col_field 横向滚到该列；3) 仅 row_index 纵向滚到该行；4) scroll_left/scroll_top 直接设偏移。verify=True（默认）滚动后校验目标进入可视区，返回最新 scrollLeft/scrollTop。滚动后配合 vtable_get_cell_center 取最新坐标再点击。",
            ),
            Tool.from_function(
                vtable_select_rows_impl,
                name="vtable_select_rows",
                description="勾选/取消勾选 VTable 行（canvas 渲染，DOM 无复选框；内部完成实例刷新、checkbox 列定位、坐标合成并发送真实鼠标点击）。row_indexes 为 0 起始纯数据行索引列表；action 可选 check（默认，幂等）/uncheck/toggle。返回勾选前后变化及 observation（dynamic_layers/new_layers、navigation.url_changed、navigation.frames_changed）。",
            ),
            Tool.from_function(
                vtable_drag_column_impl,
                name="vtable_drag_column",
                description="真实鼠标拖拽：把 source 列拖到 target 列前方(before)/后方(after)。先点击源列头中部选中整列（表头未启用整列选中时自动兜底纵向框选整列），再分步拖拽到落点列松开；不使用实例 API 改列位置。source/target 支持列索引或字段名/列标题。返回拖拽前后列顺序、验证结果；未开启列头拖拽（dragHeaderMode）或列级 dragHeader=false 时给出明确报错。",
            ),
            Tool.from_function(
                vtable_resize_column_impl,
                name="vtable_resize_column",
                description="真实鼠标拖拽：把 col 列宽调整到指定像素值 width。采集列头右边界分隔线位置合成顶层视口坐标，分步拖到目标位置后松开；不使用实例 API 改列宽。col 支持列索引或字段名/列标题。拖拽后重读列宽校验（误差≤2px）；未开启 columnResize 或超出 min/max 边界时给出明确报错。",
            ),
        ]

    async def _list_tools(self) -> Sequence[Tool]:
        return self._tools
