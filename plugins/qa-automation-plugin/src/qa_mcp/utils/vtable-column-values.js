// VTable 工具：按中文列标题取列值 + 单元格坐标/滚动（DrissionPage 移植版）
// 在 *iframe(frame) 上下文* 通过 frame.run_js 执行，依赖 window._vtable（先 mountVTable()）。

// ---- 按中文列标题获取该列所有单元格值 ----
// raw=false：场景图视觉文本（与界面一致）；raw=true：原始字段值（如数字码）
function getColumnValuesByTitle(vtable, title, raw) {
  if (!vtable || !title) return null;
  var headerLevelCount = vtable.columnHeaderLevelCount || vtable.frozenRowCount || 1;
  var exactCols = [];
  var partialCols = [];
  for (var col = 0; col < vtable.colCount; col++) {
    var exact = false;
    var partial = false;
    for (var row = 0; row < headerLevelCount; row++) {
      var headerValue = '';
      try { headerValue = String(vtable.getCellValue(col, row) || '').trim(); } catch (e) {}
      if (headerValue === title) exact = true;
      else if (headerValue.indexOf(title) !== -1) partial = true;
    }
    if (exact) exactCols.push(col);
    else if (partial) partialCols.push(col);
  }
  var matches = exactCols.length ? exactCols : partialCols;
  if (matches.length !== 1) return null;
  var targetCol = matches[0];

  var values = [];
  for (var r = headerLevelCount; r < vtable.rowCount; r++) {
    var val = null;
    try {
      if (raw) {
        val = vtable.getCellRawValue ? vtable.getCellRawValue(targetCol, r) : vtable.getCellOriginValue(targetCol, r);
      } else {
        val = getVisualCellText(vtable, targetCol, r);
        if (val === null || val === undefined) { val = vtable.getCellValue(targetCol, r); }
      }
    } catch (e) {}
    values.push(val);
  }
  return values;
}

function getColumnsValuesByTitle(vtable, titles, raw) {
  var values = {};
  var missing = [];
  (titles || []).forEach(function(title) {
    var columnValues = getColumnValuesByTitle(vtable, title, raw);
    if (columnValues === null) missing.push(title);
    else values[title] = columnValues;
  });
  return {
    values: values,
    missing: missing,
    headerRows: vtable ? (vtable.columnHeaderLevelCount || vtable.frozenRowCount || 1) : 1
  };
}

// ---- 场景图渲染后视觉文本 ----
function getVisualCellText(vtable, col, row) {
  try {
    var cellGroup = vtable.scenegraph && vtable.scenegraph.getCell ? vtable.scenegraph.getCell(col, row) : null;
    if (!cellGroup) return null;
    var textNode = null;
    (function walk(node, depth) {
      if (!node || depth > 5 || textNode) return;
      if (node.type === 'text' && node.attribute && node.attribute.text) { textNode = node; return; }
      if (node.children) { for (var i = 0; i < node.children.length; i++) walk(node.children[i], depth + 1); }
    })(cellGroup, 0);
    if (textNode && textNode.attribute && textNode.attribute.text !== undefined) return textNode.attribute.text;
    return null;
  } catch (e) { return null; }
}

function _duVisibleVTableElement(el) {
  if (!el || !el.isConnected) return false;
  var style = window.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden') return false;
  var rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  var left = Math.max(0, rect.left), right = Math.min(window.innerWidth, rect.right);
  var top = Math.max(0, rect.top), bottom = Math.min(window.innerHeight, rect.bottom);
  if (right <= left || bottom <= top) return false;
  var hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
  return !!(hit && (hit === el || el.contains(hit)));
}

function _duVTableElement() {
  var cached = window._vtableElement;
  if (_duVisibleVTableElement(cached)) return cached;
  var selectors = ['.vtable', '[class*="vtable"]'];
  for (var si = 0; si < selectors.length; si++) {
    var candidates = [].slice.call(document.querySelectorAll(selectors[si]));
    for (var ci = 0; ci < candidates.length; ci++) {
      var candidate = candidates[ci];
      if (_duVisibleVTableElement(candidate) &&
          (candidate.tagName === 'CANVAS' || candidate.querySelector('canvas'))) {
        window._vtableElement = candidate;
        return candidate;
      }
    }
  }
  return null;
}

// ---- 单元格中心【顶层视口坐标】（供 click_xy / actions.move_to 直接使用）----
// 内部通过 window.frameElement.getBoundingClientRect() 一次算到顶层视口，
// Python 侧不再叠加 iframe 偏移。
function getCellCenterViewport(col, row) {
  var t = window._vtable;
  if (!t) return null;
  var ifrRect = window.frameElement ? window.frameElement.getBoundingClientRect() : { left: 0, top: 0 };
  var vtEl = _duVTableElement();
  if (!vtEl) return null;
  var vtRect = vtEl.getBoundingClientRect();
  var cx = null, cy = null;
  // 优先用 scenegraph 的 globalAABBBounds
  try {
    var cell = t.scenegraph && t.scenegraph.getCell ? t.scenegraph.getCell(col, row) : null;
    if (cell && cell.globalAABBBounds) {
      var b = cell.globalAABBBounds;
      cx = (b.x1 + b.x2) / 2; cy = (b.y1 + b.y2) / 2;
    }
  } catch (e) {}
  // 降级用 getCellRect
  if (cx === null) {
    try {
      var rect = t.getCellRect ? t.getCellRect(col, row) : null;
      if (rect) {
        var r0 = rect.bounds || rect;
        cx = (r0.x1 + r0.x2) / 2; cy = (r0.y1 + r0.y2) / 2;
      }
    } catch (e) {}
  }
  if (cx === null) return null;
  return {
    viewportX: Math.round((ifrRect.left + vtRect.left + cx) * 10) / 10,
    viewportY: Math.round((ifrRect.top + vtRect.top + cy) * 10) / 10
  };
}

function _duRound(n) {
  return Math.round(n * 10) / 10;
}

function _duSafeValue(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  try { return JSON.parse(JSON.stringify(value)); } catch (e) { return String(value); }
}

function _duBounds(node) {
  var b = node && node.globalAABBBounds;
  if (!b || typeof b.x1 !== 'number') return null;
  var w = b.x2 - b.x1, h = b.y2 - b.y1;
  return {
    x1: _duRound(b.x1), y1: _duRound(b.y1),
    x2: _duRound(b.x2), y2: _duRound(b.y2),
    width: _duRound(w), height: _duRound(h),
    area: _duRound(w * h),
    centerX: _duRound((b.x1 + b.x2) / 2),
    centerY: _duRound((b.y1 + b.y2) / 2)
  };
}

function _duViewportOrigin() {
  var vtEl = _duVTableElement();
  if (!vtEl) return null;
  var ifrRect = window.frameElement ? window.frameElement.getBoundingClientRect() : { left: 0, top: 0 };
  var vtRect = vtEl.getBoundingClientRect();
  return { left: ifrRect.left + vtRect.left, top: ifrRect.top + vtRect.top };
}

function _duTextColor(attr) {
  if (!attr) return null;
  if (attr.fill || attr.color || attr.textFill || attr.fontColor) {
    return attr.fill || attr.color || attr.textFill || attr.fontColor;
  }
  var cfg = attr.textConfig;
  if (cfg && cfg.length) {
    for (var i = 0; i < cfg.length; i++) {
      if (cfg[i] && (cfg[i].fill || cfg[i].color)) return cfg[i].fill || cfg[i].color;
    }
  }
  return null;
}

function _duIntersects(a, b) {
  if (!a || !b) return false;
  return !(a.x2 < b.x1 || a.x1 > b.x2 || a.y2 < b.y1 || a.y1 > b.y2);
}

function _duContainsPoint(b, x, y) {
  return !!b && x >= b.x1 && x <= b.x2 && y >= b.y1 && y <= b.y2;
}

function getCellRenderInfo(col, row, detail) {
  var t = window._vtable;
  if (!t || !t.scenegraph || !t.scenegraph.getCell) return { ok: false, reason: 'no vtable scenegraph' };
  var cell = t.scenegraph.getCell(col, row);
  if (!cell) return { ok: false, reason: 'cell not rendered', col: col, row: row };

  var cellBounds = _duBounds(cell);
  var textNodes = [], bgNodes = [], allNodes = [];
  function walk(node, depth) {
    if (!node || depth > 10) return;
    var attr = node.attribute || {};
    var bounds = _duBounds(node);
    var hasText = attr.text !== undefined && String(attr.text || '').trim() !== '';
    var isText = node.type === 'text' || attr.text !== undefined;
    var info = {
      depth: depth,
      type: node.type || '',
      name: node.name || '',
      text: attr.text === undefined ? null : String(attr.text),
      textColor: _duSafeValue(isText ? _duTextColor(attr) : null),
      fill: _duSafeValue(attr.fill),
      background: _duSafeValue(attr.background || attr.backgroundColor),
      stroke: _duSafeValue(attr.stroke),
      fontSize: attr.fontSize || null,
      fontWeight: attr.fontWeight || null,
      bounds: bounds
    };
    allNodes.push(info);
    if (isText && hasText) textNodes.push(info);

    var bg = isText ? (attr.background || attr.backgroundColor) : (attr.background || attr.backgroundColor || attr.fill);
    if (bg && bounds && bounds.area > 0) {
      var copy = {};
      for (var k in info) copy[k] = info[k];
      copy.backgroundPaint = _duSafeValue(bg);
      bgNodes.push(copy);
    }

    var children = node.children || [];
    for (var i = 0; i < children.length; i++) walk(children[i], depth + 1);
  }
  walk(cell, 0);

  var primaryText = textNodes[0] || null;
  var textBounds = primaryText ? primaryText.bounds : null;
  var cellBg = null, tagBg = null;
  for (var i = 0; i < bgNodes.length; i++) {
    var n = bgNodes[i];
    var areaRatio = cellBounds && n.bounds ? n.bounds.area / cellBounds.area : null;
    n.areaRatio = areaRatio === null ? null : Math.round(areaRatio * 1000) / 1000;
    if (areaRatio !== null && areaRatio >= 0.75) {
      if (!cellBg || Math.abs(1 - areaRatio) < Math.abs(1 - cellBg.areaRatio)) cellBg = n;
    }
    var textRelated = textBounds && (_duContainsPoint(n.bounds, textBounds.centerX, textBounds.centerY) || _duIntersects(n.bounds, textBounds));
    if (areaRatio !== null && areaRatio > 0.03 && areaRatio < 0.75 && textRelated) {
      if (!tagBg || n.bounds.area > tagBg.bounds.area) tagBg = n;
    }
  }
  if (!cellBg) {
    var ca = cell.attribute || {};
    var paint = ca.background || ca.backgroundColor || ca.fill;
    if (paint) cellBg = { backgroundPaint: _duSafeValue(paint), type: cell.type || '', name: cell.name || '', bounds: cellBounds, areaRatio: 1, stroke: _duSafeValue(ca.stroke) };
  }
  if (!tagBg && primaryText && primaryText.background) {
    tagBg = {
      backgroundPaint: primaryText.background,
      type: primaryText.type,
      name: primaryText.name,
      bounds: primaryText.bounds,
      areaRatio: primaryText.bounds && cellBounds ? Math.round((primaryText.bounds.area / cellBounds.area) * 1000) / 1000 : null
    };
  }

  var result = {
    ok: true,
    col: col,
    row: row,
    value: (function () { try { return t.getCellValue ? t.getCellValue(col, row) : null; } catch (e) { return null; } })(),
    cellType: (function () { try { return t.getCellType ? t.getCellType(col, row) : null; } catch (e) { return null; } })(),
    text: primaryText ? primaryText.text : null,
    fontColor: primaryText ? primaryText.textColor : null,
    tagBackgroundColor: tagBg ? tagBg.backgroundPaint : null,
    cellBackgroundColor: cellBg ? cellBg.backgroundPaint : null,
    cellBorderColor: cellBg ? (cellBg.stroke || null) : _duSafeValue((cell.attribute || {}).stroke),
    cellBounds: cellBounds,
    tagBackgroundNode: tagBg ? { type: tagBg.type, name: tagBg.name, bounds: tagBg.bounds, areaRatio: tagBg.areaRatio, backgroundPaint: tagBg.backgroundPaint } : null,
    cellBackgroundNode: cellBg ? { type: cellBg.type, name: cellBg.name, bounds: cellBg.bounds, areaRatio: cellBg.areaRatio, backgroundPaint: cellBg.backgroundPaint, stroke: cellBg.stroke || null } : null
  };
  if (detail === 'full') {
    result.textNodes = textNodes;
    result.backgroundNodes = bgNodes;
    result.nodes = allNodes;
  } else {
    result.textNodes = textNodes.slice(0, 3);
    result.backgroundNodes = bgNodes.slice(0, 5);
    result.nodeCount = allNodes.length;
  }
  return result;
}

function getCellIconsViewport(col, row, iconName, detail) {
  var t = window._vtable;
  if (!t || !t.scenegraph || !t.scenegraph.getCell) return { ok: false, reason: 'no vtable scenegraph' };
  var cell = t.scenegraph.getCell(col, row);
  if (!cell) return { ok: false, reason: 'cell not rendered', col: col, row: row };
  var origin = _duViewportOrigin();
  if (!origin) return { ok: false, reason: 'visible vtable root not found', col: col, row: row };
  var low = (iconName || '').toLowerCase();
  var icons = [], all = [];
  var MAX_COORD = 1e15;

  function pushIcon(node, depth) {
    var attr = node.attribute || {};
    var bounds = _duBounds(node);
    if (!bounds || Math.abs(bounds.x1) >= MAX_COORD || Math.abs(bounds.y1) >= MAX_COORD) return;
    if (bounds.width <= 0 || bounds.width > 500 || bounds.height <= 0 || bounds.height > 500) return;
    var type = node.type || '';
    var name = node.name || '';
    var isText = type === 'text' || attr.text !== undefined || name === 'text';
    var hasIconIdentity = (!!name && name !== 'text') || type === 'image' || type === 'symbol' || type === 'icon' || type === 'path' || !!attr.symbolType || !!attr.image;
    if (isText || !hasIconIdentity) return;
    var text = [name, type, attr.symbolType || '', attr.id || '', attr.role || ''].join(' ').toLowerCase();
    if (low && text.indexOf(low) === -1) return;
    var icon = {
      index: icons.length,
      depth: depth,
      name: name,
      type: type,
      symbolType: attr.symbolType || null,
      width: bounds.width,
      height: bounds.height,
      centerX: bounds.centerX,
      centerY: bounds.centerY,
      viewportX: _duRound(origin.left + bounds.centerX),
      viewportY: _duRound(origin.top + bounds.centerY),
      bounds: bounds,
      coordinate_space: 'top_viewport'
    };
    if (detail === 'full') {
      icon.fill = _duSafeValue(attr.fill);
      icon.background = _duSafeValue(attr.background || attr.backgroundColor);
      icon.stroke = _duSafeValue(attr.stroke);
      icon.attributeKeys = Object.keys(attr).slice(0, 30);
    }
    icons.push(icon);
  }

  function walk(node, depth) {
    if (!node || depth > 10) return;
    if (depth > 0) {
      if (detail === 'full') {
        all.push({ depth: depth, type: node.type || '', name: node.name || '', bounds: _duBounds(node), attributeKeys: Object.keys(node.attribute || {}).slice(0, 30) });
      }
      pushIcon(node, depth);
    }
    var children = node.children || [];
    for (var i = 0; i < children.length; i++) walk(children[i], depth + 1);
  }
  walk(cell, 0);
  return {
    ok: true,
    col: col,
    row: row,
    iconName: iconName || null,
    count: icons.length,
    icons: icons,
    nodes: detail === 'full' ? all : undefined
  };
}

// ---- 滚动到目标单元格（确保在视口内再取坐标）----
function scrollToCell(col, row) {
  var t = window._vtable;
  if (!t || !t.scrollToCell) return { ok: false, reason: 'no scrollToCell' };
  try { t.scrollToCell({ col: col, row: row }); return { ok: true }; }
  catch (e) { return { ok: false, reason: String(e) }; }
}

// ---- 滚动控制（实例 API 直调: scrollToCol / scrollToRow / scrollToCell / setScrollLeft / setScrollTop）----
// 这些 API 等价于拖动横向/纵向滚动条滑块后的结果，比真实鼠标拖拽滚动条更稳定可靠。
function scrollToColumnByIndex(col) {
  var t = window._vtable;
  if (!t) return { ok: false, reason: 'no vtable' };
  if (typeof t.scrollToCol === 'function') { t.scrollToCol(col); return { ok: true, api: 'scrollToCol' }; }
  if (typeof t.scrollToCell === 'function') { t.scrollToCell({ col: col, row: 0 }); return { ok: true, api: 'scrollToCell' }; }
  return { ok: false, reason: 'no scroll API' };
}

function scrollToRowByIndex(row) {
  var t = window._vtable;
  if (!t) return { ok: false, reason: 'no vtable' };
  if (typeof t.scrollToRow === 'function') { t.scrollToRow(row); return { ok: true, api: 'scrollToRow' }; }
  if (typeof t.scrollToCell === 'function') { t.scrollToCell({ col: 0, row: row }); return { ok: true, api: 'scrollToCell' }; }
  return { ok: false, reason: 'no scroll API' };
}

function scrollToCellPosition(col, row) {
  var t = window._vtable;
  if (!t) return { ok: false, reason: 'no vtable' };
  if (typeof t.scrollToCell === 'function') { t.scrollToCell({ col: col, row: row }); return { ok: true, api: 'scrollToCell' }; }
  return { ok: false, reason: 'no scrollToCell' };
}

function setScrollPosition(scrollLeft, scrollTop) {
  var t = window._vtable;
  if (!t) return { ok: false, reason: 'no vtable' };
  var applied = [];
  if (scrollLeft !== null && scrollLeft !== undefined && typeof t.setScrollLeft === 'function') {
    t.setScrollLeft(scrollLeft); applied.push('setScrollLeft');
  }
  if (scrollTop !== null && scrollTop !== undefined && typeof t.setScrollTop === 'function') {
    t.setScrollTop(scrollTop); applied.push('setScrollTop');
  }
  if (!applied.length) return { ok: false, reason: 'no setScroll API' };
  return { ok: true, applied: applied };
}

function getScrollStateInfo() {
  var t = window._vtable;
  if (!t) return null;
  return {
    scrollLeft: t.scrollLeft || 0,
    scrollTop: t.scrollTop || 0,
    colCount: t.colCount,
    rowCount: t.rowCount,
    frozenColCount: t.frozenColCount || 0,
    frozenRowCount: t.frozenRowCount || 0
  };
}

// 目标单元格是否已全部落入可视区（横向/纵向都可见）
function isCellInViewport(col, row) {
  var t = window._vtable;
  if (!t) return false;
  try {
    var canvas = t.container ? t.container.querySelector('canvas') : null;
    if (!canvas) return false;
    var cr = canvas.getBoundingClientRect();
    if (!cr.width || !cr.height) return false;
    var rect = t.getCellRect ? t.getCellRect(col, row) : null;
    if (!rect) return false;
    var r = rect.bounds || rect;
    var vx1 = r.x1 - (t.scrollLeft || 0), vx2 = r.x2 - (t.scrollLeft || 0);
    var vy1 = r.y1 - (t.scrollTop || 0), vy2 = r.y2 - (t.scrollTop || 0);
    return vx1 >= -0.5 && vx2 <= cr.width + 0.5 && vy1 >= -0.5 && vy2 <= cr.height + 0.5;
  } catch (e) { return false; }
}

// ---- 列头拖拽换位：几何信息 + 能力开关 + 当前列顺序 (供 vtable_drag_column 使用) ----
// 全部通过实例内部 API 只读采集 (scenegraph / getCellRect / options / stateManager),
// 不修改任何列位置 —— 列顺序变更完全交给真实鼠标拖拽触发。
// 返回顶层视口坐标 (含 iframe + .vtable 偏移), Python 侧可直接驱动 page.mouse。
function getHeaderDragGeometry(sourceCol, dropCol) {
  var t = window._vtable;
  if (!t) return { error: 'window._vtable 未准备好' };
  var headerLevel = t.columnHeaderLevelCount || 1;
  var headerRow = Math.max(headerLevel - 1, 0);
  var ifrRect = window.frameElement ? window.frameElement.getBoundingClientRect() : { left: 0, top: 0 };
  var vtEl = _duVTableElement();
  if (!vtEl) return { error: '未找到可见的 VTable 根元素' };
  var vtRect = vtEl.getBoundingClientRect();
  var canvas = t.container ? t.container.querySelector('canvas') : null;
  if (!canvas) return { error: '未找到 VTable canvas' };
  var cr = canvas.getBoundingClientRect();
  var canvasViewportWidth = cr.width;
  var scrollLeft = t.scrollLeft || 0;
  var scrollTop = t.scrollTop || 0;

  // 单元格在【顶层视口】的矩形 (场景图优先, getCellRect 兜底)
  function cellViewport(col, row) {
    try {
      var cell = t.scenegraph && t.scenegraph.getCell ? t.scenegraph.getCell(col, row) : null;
      if (cell && cell.globalAABBBounds && typeof cell.globalAABBBounds.x1 === 'number') {
        var b = cell.globalAABBBounds;
        var vx1 = ifrRect.left + vtRect.left + b.x1;
        var vx2 = ifrRect.left + vtRect.left + b.x2;
        var vy1 = ifrRect.top + vtRect.top + b.y1;
        var vy2 = ifrRect.top + vtRect.top + b.y2;
        return {
          x1: _duRound(vx1), x2: _duRound(vx2), y1: _duRound(vy1), y2: _duRound(vy2),
          visible: vx1 >= ifrRect.left + cr.left - 0.5 && vx2 <= ifrRect.left + cr.left + canvasViewportWidth + 0.5,
          source: 'scenegraph'
        };
      }
    } catch (e) {}
    try {
      var rect = t.getCellRect ? t.getCellRect(col, row) : null;
      if (rect) {
        var r = rect.bounds || rect;
        var vx1 = ifrRect.left + cr.left + r.x1 - scrollLeft;
        var vx2 = ifrRect.left + cr.left + r.x2 - scrollLeft;
        var vy1 = ifrRect.top + cr.top + r.y1 - scrollTop;
        var vy2 = ifrRect.top + cr.top + r.y2 - scrollTop;
        return {
          x1: _duRound(vx1), x2: _duRound(vx2), y1: _duRound(vy1), y2: _duRound(vy2),
          visible: vx1 >= ifrRect.left + cr.left - 0.5 && vx2 <= ifrRect.left + cr.left + canvasViewportWidth + 0.5,
          source: 'cellRect'
        };
      }
    } catch (e) {}
    return null;
  }

  function headerField(col) {
    try { var f = t.getHeaderField ? t.getHeaderField(col, headerRow) : null; if (f !== null && f !== undefined) return String(f); } catch (e) {}
    return '';
  }
  function headerTitle(col) {
    var title = '';
    try { var v = t.getCellValue ? t.getCellValue(col, headerRow) : null; if (v !== null && v !== undefined) title = v; } catch (e) {}
    if (!title) { try { var d = t.getHeaderDefine ? t.getHeaderDefine(col, headerRow) : null; if (d) title = d.title || d.caption || ''; } catch (e) {} }
    return typeof title === 'string' ? title : String(title);
  }

  var colCount = t.colCount || 0;
  var fields = [], titles = [];
  for (var c = 0; c < colCount; c++) {
    fields.push(headerField(c));
    titles.push(headerTitle(c));
  }
  var opts = t.options || {};
  var dragOrderOpt = opts.dragOrder || {};
  var src = cellViewport(sourceCol, headerRow);
  var drop = (dropCol === null || dropCol === undefined) ? null : cellViewport(dropCol, headerRow);
  var result = {
    ok: true,
    headerRow: headerRow,
    headerLevel: headerLevel,
    colCount: colCount,
    rowCount: t.rowCount || 0,
    canvasViewportWidth: _duRound(canvasViewportWidth),
    scrollLeft: _duRound(scrollLeft),
    // 能力开关 (供前置校验与失败诊断)
    dragHeaderMode: opts.dragHeaderMode !== undefined ? opts.dragHeaderMode
      : (dragOrderOpt.dragHeaderMode !== undefined ? dragOrderOpt.dragHeaderMode : null),
    frozenColDragHeaderMode: opts.frozenColDragHeaderMode !== undefined ? opts.frozenColDragHeaderMode
      : (dragOrderOpt.frozenColDragHeaderMode !== undefined ? dragOrderOpt.frozenColDragHeaderMode : null),
    headerSelectMode: (opts.select && opts.select.headerSelectMode) || null,
    // 源列/落点列头矩形 (顶层视口坐标)
    sourceHeader: src ? { x1: src.x1, x2: src.x2, y1: src.y1, y2: src.y2, visible: src.visible, source: src.source } : null,
    dropHeader: drop ? { x1: drop.x1, x2: drop.x2, y1: drop.y1, y2: drop.y2, visible: drop.visible, source: drop.source } : null,
    // 当前可见列顺序 (字段 + 标题)
    fields: fields,
    titles: titles
  };
  try { result.sourceIsFrozen = !!(t.isFrozenColumn && t.isFrozenColumn(sourceCol)); } catch (e) { result.sourceIsFrozen = false; }
  try { result.dropIsFrozen = !!(dropCol !== null && dropCol !== undefined && t.isFrozenColumn && t.isFrozenColumn(dropCol)); } catch (e) { result.dropIsFrozen = false; }
  try {
    result.sourceCanDragByDefine = !!(t._canDragHeaderPosition && t._canDragHeaderPosition(sourceCol, headerRow));
  } catch (e) { result.sourceCanDragByDefine = false; }
  // 源列 body 最后一行 (全局行号 = rowCount - 1, 与 VTable select ranges / _canDragHeaderPosition 判定一致):
  // 供"表头未启用整列选中时纵向框选整列"兜底使用 —— 框选终点必须真实命中该行单元格。
  // 虚拟滚动未渲染该行时 cellViewport 返回 null → center=null, visible=false (需要先滚动再重采)。
  var lastBodyRowGlobal = (t.rowCount || 1) - 1;
  var lastBody = cellViewport(sourceCol, lastBodyRowGlobal);
  result.lastBodyRowGlobal = lastBodyRowGlobal;
  result.sourceLastBodyCenter = lastBody
    ? { x: _duRound((lastBody.x1 + lastBody.x2) / 2), y: _duRound((lastBody.y1 + lastBody.y2) / 2) }
    : null;
  result.sourceLastBodyVisible = !!(lastBody && lastBody.visible);
  return result;
}

// ---- 整列选中状态检查 (列头拖拽前置条件: 拖拽启动要求整列选中) ----
function getColumnSelectionState(col) {
  var t = window._vtable;
  if (!t || !t.stateManager) return { selected: false };
  var ranges = (t.stateManager.select && t.stateManager.select.ranges) || [];
  var lastRow = (t.rowCount || 1) - 1;
  for (var i = 0; i < ranges.length; i++) {
    var r = ranges[i];
    if (!r || !r.start || !r.end) continue;
    if (r.start.col <= col && col <= r.end.col && r.end.row >= lastRow) {
      return {
        selected: true,
        range: { start: [r.start.col, r.start.row], end: [r.end.col, r.end.row] }
      };
    }
  }
  return {
    selected: false,
    ranges: ranges.map(function (r) {
      return { start: [r.start.col, r.start.row], end: [r.end.col, r.end.row] };
    })
  };
}

// ---- 列宽调整 (resize)：列头几何 + 能力开关 + 当前列宽 (供 vtable_resize_column 使用) ----
// 与 getHeaderDragGeometry 同体系：全部通过实例内部 API 只读采集 (scenegraph 优先 /
// getCellRect 兜底, 含 iframe + .vtable 容器偏移合成顶层视口坐标)，
// 列宽变更完全交给真实鼠标拖拽触发 —— 分隔线 = 列头矩形右边界 (x2)。
function getHeaderResizeGeometry(col) {
  var t = window._vtable;
  if (!t) return { error: 'window._vtable 未准备好' };
  var headerLevel = t.columnHeaderLevelCount || 1;
  var headerRow = Math.max(headerLevel - 1, 0);
  var ifrRect = window.frameElement ? window.frameElement.getBoundingClientRect() : { left: 0, top: 0 };
  var vtEl = _duVTableElement();
  if (!vtEl) return { error: '未找到可见的 VTable 根元素' };
  var vtRect = vtEl.getBoundingClientRect();
  var canvas = t.container ? t.container.querySelector('canvas') : null;
  if (!canvas) return { error: '未找到 VTable canvas' };
  var cr = canvas.getBoundingClientRect();
  var canvasViewportWidth = cr.width;

  function cellViewport(c, row) {
    try {
      var cell = t.scenegraph && t.scenegraph.getCell ? t.scenegraph.getCell(c, row) : null;
      if (cell && cell.globalAABBBounds && typeof cell.globalAABBBounds.x1 === 'number') {
        var b = cell.globalAABBBounds;
        return {
          x1: ifrRect.left + vtRect.left + b.x1,
          x2: ifrRect.left + vtRect.left + b.x2,
          y1: ifrRect.top + vtRect.top + b.y1,
          y2: ifrRect.top + vtRect.top + b.y2,
          width: b.x2 - b.x1,
          source: 'scenegraph'
        };
      }
    } catch (e) {}
    try {
      var rect = t.getCellRect ? t.getCellRect(c, row) : null;
      if (rect) {
        var r = rect.bounds || rect;
        return {
          x1: ifrRect.left + cr.left + r.x1 - (t.scrollLeft || 0),
          x2: ifrRect.left + cr.left + r.x2 - (t.scrollLeft || 0),
          y1: ifrRect.top + cr.top + r.y1 - (t.scrollTop || 0),
          y2: ifrRect.top + cr.top + r.y2 - (t.scrollTop || 0),
          width: r.x2 - r.x1,
          source: 'cellRect'
        };
      }
    } catch (e) {}
    return null;
  }

  function headerField(c) {
    try { var f = t.getHeaderField ? t.getHeaderField(c, headerRow) : null; if (f !== null && f !== undefined) return String(f); } catch (e) {}
    return '';
  }
  function headerTitle(c) {
    var title = '';
    try { var v = t.getCellValue ? t.getCellValue(c, headerRow) : null; if (v !== null && v !== undefined) title = v; } catch (e) {}
    if (!title) { try { var d = t.getHeaderDefine ? t.getHeaderDefine(c, headerRow) : null; if (d) title = d.title || d.caption || ''; } catch (e) {} }
    return typeof title === 'string' ? title : String(title);
  }

  var colCount = t.colCount || 0;
  var resolved = null;
  if (typeof col === 'number') {
    resolved = (col >= 0 && col < colCount) ? col : null;
  } else {
    var s = String(col);
    for (var c = 0; c < colCount; c++) {
      if (headerField(c) === s || headerTitle(c) === s) { resolved = c; break; }
    }
  }
  if (resolved === null) return { error: '未找到列: ' + col + ' (colCount=' + colCount + ', 可尝试列索引或先 vtable_scan_columns 查看列标题)' };

  var h = cellViewport(resolved, headerRow);
  if (!h) return { error: '无法获取列头矩形 (col=' + resolved + ')' };

  var opts = t.options || {};
  var colResizeOpt = opts.columnResize || {};
  var resizeOpt = opts.resize || {};
  var resizeEnabled = (typeof colResizeOpt.resizable === 'boolean') ? colResizeOpt.resizable
    : ((typeof colResizeOpt.enable === 'boolean') ? colResizeOpt.enable : null);

  return {
    ok: true,
    col: resolved,
    field: headerField(resolved),
    title: headerTitle(resolved),
    headerRow: headerRow,
    colCount: colCount,
    canvasViewportWidth: _duRound(canvasViewportWidth),
    // 列头矩形 (顶层视口坐标, 分隔线 = x2; visible 判定与 getHeaderDragGeometry 一致)
    header: {
      x1: _duRound(h.x1), x2: _duRound(h.x2),
      y1: _duRound(h.y1), y2: _duRound(h.y2),
      width: _duRound(h.width),
      visible: h.x1 >= ifrRect.left + cr.left - 0.5 && h.x2 <= ifrRect.left + cr.left + canvasViewportWidth + 0.5,
      source: h.source
    },
    // 列宽调整能力开关 (兼容新旧 VTable 配置: columnResize / resize)
    resize: {
      columnResize: colResizeOpt,
      resize: resizeOpt,
      resizeEnabled: resizeEnabled,
      columnResizeMode: (resizeOpt.columnResizeMode !== undefined) ? resizeOpt.columnResizeMode : null,
      canResize: (resizeOpt.canResize !== undefined) ? resizeOpt.canResize : null,
      minColumnWidth: (colResizeOpt.minColumnWidth !== undefined) ? colResizeOpt.minColumnWidth
        : ((resizeOpt.minColumnWidth !== undefined) ? resizeOpt.minColumnWidth : null),
      maxColumnWidth: (colResizeOpt.maxColumnWidth !== undefined) ? colResizeOpt.maxColumnWidth
        : ((resizeOpt.maxColumnWidth !== undefined) ? resizeOpt.maxColumnWidth : null)
    }
  };
}

// ---- 读取当前列宽 (拖拽后验证用, 与 getHeaderResizeGeometry.header.width 一致) ----
function getColumnWidth(col) {
  var g = getHeaderResizeGeometry(col);
  if (g.error) return g;
  return { ok: true, col: g.col, field: g.field, title: g.title, width: g.header.width, header: g.header };
}
