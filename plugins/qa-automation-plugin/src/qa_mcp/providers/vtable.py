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
                description="强制连接浏览器，在目标 iframe 中寻址并刷新最新 vtable 实例至 window._vtable。通常作为其他 VTable 操作的前置条件。",
            ),
            Tool.from_function(
                vtable_analyze_headers_impl,
                name="vtable_analyze_headers",
                description="【场景图驱动】分析 vtable 列头与单元格内的交互图标组件：header_icons 为表头行真实渲染的交互图标（排序/筛选/下拉/冻结等，含顶层视口坐标可直接点击）；cell_icons 为已渲染 body 单元格内的交互图标组件（行内按钮/链接/checkbox/开关等，columns 配置中不存在、只能从场景图获取）；capabilities 汇总 sortable/filterable/interactiveCell。用于规划列头点击与行内交互。",
            ),
            Tool.from_function(
                vtable_scan_columns_impl,
                name="vtable_scan_columns",
                description="【推荐】扫描 VTable 全部列（含多级表头）：返回每列标题、body 行为分类（checkbox/button/文本等），以及【表头图标的顶层视口坐标 viewportX/viewportY】（经场景图 scenegraph 精确计算）。坐标可直接传给 click_interact(by=\"coordinate\", coordinate_space=\"top\") 点击，无需叠加偏移。用于规划列头点击（排序/筛选/下拉图标）与列交互。",
            ),
            Tool.from_function(
                vtable_get_row_count_impl,
                name="vtable_get_row_count",
                description="读取当前 VTable 表格有多少行纯数据。直接通过读取内部记录集合长度，不受屏幕滚动截断影响。",
            ),
            Tool.from_function(
                vtable_get_all_records_impl,
                name="vtable_get_all_records",
                description="一次性无损读取表格中所有的完整后台行记录对象 (JSON)。可用于整表断言和宏观数据检查，无需操作 DOM。",
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
                        description="VTable 全量数据可视化 (Claude Desktop Apps UI): 渲染可搜索/排序的 DataTable 供直接浏览。Claude Code (TUI) 不渲染 Apps UI, 降级使用 vtable_get_all_records (JSON)。max_rows 限制渲染行数 (默认 1000)。",
                    )
                ]
                if INTERACTIVE_UI_ENABLED
                else []
            ),
            Tool.from_function(
                vtable_get_cell_text_impl,
                name="vtable_get_cell_text",
                description="读取某个具体单元格的值。row_index 为纯数据行号 (0为第一行)，col_field 支持字段名或列标题。visual=True(默认) 读取场景图渲染层文本，与界面显示完全一致——重要：VTable 排序/筛选发生在渲染层，数据源 records 不重排，排序状态下必须用渲染层才能读到界面真实顺序；visual=False 读取数据源 records 原始值（忽略排序/筛选）。渲染视口外的行需先滚动。",
            ),
            Tool.from_function(
                vtable_get_column_values_impl,
                name="vtable_get_column_values",
                description="按中文列标题读取该列所有单元格的值。titles 为列标题数组（如 [\"商品名称\",\"商品编码\"]）；raw=false 读取场景图渲染后的视觉文本（与界面显示一致），raw=true 读取原始字段值（如数字码/状态码）。返回每列值列表及缺失列。",
            ),
            Tool.from_function(
                vtable_get_cell_render_info_impl,
                name="vtable_get_cell_render_info",
                description="读取某个单元格的场景图渲染详情：视觉文本、文字颜色、单元格背景色、边框色、字体大小及文本/背景节点（detail=\"full\" 时含全部节点）。col_field 支持列索引或字段名/列标题，row_index 为纯数据行号（0 为第一行）。用于断言单元格的展示样式（标签色、高亮等）。",
            ),
            Tool.from_function(
                vtable_get_cell_center_impl,
                name="vtable_get_cell_center",
                description="读取单元格中心的【顶层视口坐标】viewportX/viewportY（经场景图 globalAABBBounds 精确计算，含 iframe 与容器偏移），可直接作为 click_interact(by=\"coordinate\", coordinate_space=\"top\") 的点击坐标。col_field 支持列索引或字段名/列标题，row_index 为纯数据行号（0 为第一行）。",
            ),
            Tool.from_function(
                vtable_scroll_to_impl,
                name="vtable_scroll_to",
                description="滚动 VTable 到目标位置（等价于拖动横/纵向滚动条滑块，直接调用实例 API scrollToCol/scrollToRow/scrollToCell/setScrollLeft/setScrollTop，比真实鼠标拖拽滚动条更稳定）。四种用法按优先级：1) col_field+row_index 滚动到指定单元格；2) 仅 col_field 横向滚动到该列；3) 仅 row_index 纵向滚动到该行；4) scroll_left/scroll_top 直接设置滚动偏移。col_field 支持列索引(int)或字段名/列标题(str)。verify=True(默认) 滚动后自动校验目标是否已进入可视区，返回最新 scrollLeft/scrollTop 及单元格可见性。滚动到目标后应配合 vtable_get_cell_center 获取最新坐标再点击。",
            ),
            Tool.from_function(
                vtable_select_rows_impl,
                name="vtable_select_rows",
                description="勾选/取消勾选 VTable 表格中的行。VTable 是 canvas 渲染，DOM 中无复选框，本工具在内部一次性完成实例刷新、checkbox 列定位、目标行坐标计算(iframe 相对顶层偏移 + canvas 相对 iframe 偏移合成)并发送真实鼠标点击，返回勾选前后变化。row_indexes 为 0 起始的纯数据行索引列表；action 可选 check(确保勾选,默认,幂等)/uncheck(确保取消)/toggle(逐行切换)。点击后自动观察并返回 observation：dynamic_layers/new_layers(浮层与消息弹窗)、navigation.url_changed(tab 页跳转)、navigation.frames_changed(iframe 跳转)。",
            ),
            Tool.from_function(
                vtable_drag_column_impl,
                name="vtable_drag_column",
                description="【真实鼠标拖拽】把 vtable 的 source 列拖到 target 列的前方(before)/后方(after)。完全复刻人工操作：先点击源列头中部使整列选中（VTable 拖拽启动前提），再按下鼠标分步拖拽到落点列松开。不使用任何实例 API 改列位置，仅用实例内部 API 读坐标与顺序做定位和验证；落点列由 VTable 原生语义自动计算（向右拖→目标列后方，向左拖→目标列前方）。source/target 支持列索引或字段名/列标题。返回拖拽前后列顺序、验证结果及 dragHeaderMode 等诊断信息；VTable 未开启列头拖拽(dragHeaderMode)或列级 dragHeader=false 时给出明确报错。",
            ),
            Tool.from_function(
                vtable_resize_column_impl,
                name="vtable_resize_column",
                description="【真实鼠标拖拽】把 vtable 的 col 列宽调整到指定像素值 width。完全复刻人工操作：采集列头右边界分隔线位置（scenegraph 优先/getCellRect 兜底合成顶层视口坐标），按下鼠标分步缓动拖到目标位置后松开。不使用任何实例 API 改列宽（resizeColumn/updateColumns 等），仅用实例内部 API 读坐标/属性/配置做定位与验证；拖拽后自动重读列宽校验（误差≤2px）。col 支持列索引或字段名/列标题；自动校验 columnResize 能力开关与 min/max 列宽边界。返回拖拽前后列宽、拖拽点坐标及 resize 配置等诊断信息；VTable 未开启列宽调整(columnResize.resizable=false)时给出明确报错。",
            ),
        ]

    async def _list_tools(self) -> Sequence[Tool]:
        return self._tools
