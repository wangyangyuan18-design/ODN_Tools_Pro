# -*- coding: utf-8 -*-
"""ODN multi-node naming engine.

Topology rule (authoritative):
- A connection exists ONLY when an FDT/FAT/CL/BB point is within 0.001 m
  (1 mm) of a Cable START or END point.
- A node lying in the middle of a Cable is NOT connected by that Cable.
- 0.001 is always a real length in metres; geographic CRS is measured with
  QgsDistanceArea, not by comparing degrees.
- If more than one node matches one cable endpoint within 1 mm, the endpoint
  is ambiguous and no arbitrary node is selected.
- Duplicate NodeA-NodeB edges are collapsed.
- L is created only by a real FDT -> first-node Cable edge.
- FAT count per L has NO maximum. S starts at 1 independently for each L.
- CL is transparent to L and has its own CL counter.
- BB is inside the current L and creates downstream branches; it does not
  create a new L. BB type is inferred from the two downstream FAT counts.
"""

import math
from collections import defaultdict

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSpatialIndex,
    QgsWkbTypes,
)

ENDPOINT_TOLERANCE_M = 0.001


def _pt(g):
    if g is None or g.isEmpty():
        return None
    try:
        if QgsWkbTypes.geometryType(g.wkbType()) != QgsWkbTypes.PointGeometry:
            return None
        if g.isMultipart():
            pts = g.asMultiPoint()
            return QgsPointXY(pts[0]) if pts else None
        return QgsPointXY(g.asPoint())
    except Exception:
        return None


def _ntext(n, mode):
    if mode == '':
        return ''
    if mode != 'letter':
        return str(int(n))
    n = int(n)
    out = ''
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _part(prefix, n, mode):
    return f'{prefix}{_ntext(n, mode)}'


def _angle(a, b):
    return math.atan2(b.y() - a.y(), b.x() - a.x())


def _normalize_angle(a):
    while a <= -math.pi:
        a += 2.0 * math.pi
    while a > math.pi:
        a -= 2.0 * math.pi
    return a


def _payloads(layers, crs):
    ctx = QgsProject.instance().transformContext()
    out = {}
    for typ, layer in layers.items():
        try:
            tr = QgsCoordinateTransform(layer.crs(), crs, ctx)
        except Exception:
            tr = None
        idx = QgsSpatialIndex()
        feats = {}
        pts = {}
        for f in layer.getFeatures():
            p = _pt(f.geometry())
            if p is None:
                continue
            if tr is not None and layer.crs() != crs:
                try:
                    g = QgsGeometry(f.geometry())
                    g.transform(tr)
                    p = _pt(g)
                except Exception:
                    continue
            if p is None:
                continue
            feats[f.id()] = f
            pts[f.id()] = p
            temp = QgsFeature()
            temp.setId(f.id())
            temp.setGeometry(QgsGeometry.fromPointXY(p))
            idx.addFeature(temp)
        out[typ] = (layer, idx, feats, pts)
    return out


def _distance_area(crs):
    da = QgsDistanceArea()
    try:
        da.setSourceCrs(crs, QgsProject.instance().transformContext())
    except Exception:
        pass
    try:
        da.setEllipsoid('WGS84')
    except Exception:
        pass
    return da


def _distance_m(da, a, b):
    try:
        return float(da.measureLine(a, b))
    except Exception:
        return float('inf')


def _find_endpoint_matches(endpoint, payloads, da, tolerance_m):
    """Return all node candidates within the strict metre tolerance of endpoint."""
    result = []
    for typ, (_, idx, _, pts) in payloads.items():
        # Use a small coordinate bbox only as an index prefilter. The actual
        # acceptance test is always the metre distance calculated by QgsDistanceArea.
        try:
            bbox = QgsGeometry.fromPointXY(endpoint).boundingBox()
            # Broad enough for angular/projected CRS; exact test below decides.
            pad = 1e-4 if not QgsProject.instance().crs().isGeographic() else 1e-5
            bbox.setXMinimum(bbox.xMinimum() - pad)
            bbox.setXMaximum(bbox.xMaximum() + pad)
            bbox.setYMinimum(bbox.yMinimum() - pad)
            bbox.setYMaximum(bbox.yMaximum() + pad)
            ids = idx.intersects(bbox)
        except Exception:
            ids = list(pts.keys())
        for fid in ids:
            p = pts.get(fid)
            if p is None:
                continue
            d = _distance_m(da, endpoint, p)
            if d <= tolerance_m:
                result.append((d, typ, fid))
    result.sort(key=lambda x: (x[0], x[1], x[2]))
    return result


def _line_parts(geometry):
    if geometry is None or geometry.isEmpty():
        return []
    try:
        if geometry.isMultipart():
            return [part for part in geometry.asMultiPolyline() if len(part) >= 2]
        part = geometry.asPolyline()
        return [part] if len(part) >= 2 else []
    except Exception:
        return []


def _build_nodes(layers, line_crs, fdt_field):
    payloads = _payloads(layers, line_crs)
    nodes = {}
    for typ, (_, _, feats, pts) in payloads.items():
        for fid, feat in feats.items():
            name = str(fid)
            if typ == 'FDT':
                try:
                    value = feat.attribute(fdt_field)
                    if value is not None and str(value).strip():
                        name = str(value).strip()
                except Exception:
                    pass
            nodes[(typ, fid)] = {
                'type': typ,
                'fid': fid,
                'name': name,
                'point': pts[fid],
            }
    return payloads, nodes


def _build_graph(fdt, fdt_field, node_layers, line, tolerance_m, log):
    """Build graph ONLY from cable endpoints touching nodes within tolerance_m."""
    layers = {'FDT': fdt}
    layers.update(node_layers)
    payloads, nodes = _build_nodes(layers, line.crs(), fdt_field)
    da = _distance_area(line.crs())
    adjacency = defaultdict(list)
    edge_by_pair = {}
    edge_id = 0
    cables_without_valid_endpoints = 0

    for lf in line.getFeatures():
        parts = _line_parts(lf.geometry())
        if not parts:
            cables_without_valid_endpoints += 1
            continue
        for part_index, part in enumerate(parts):
            start = QgsPointXY(part[0])
            end = QgsPointXY(part[-1])
            start_matches = _find_endpoint_matches(start, payloads, da, tolerance_m)
            end_matches = _find_endpoint_matches(end, payloads, da, tolerance_m)

            if len(start_matches) == 0 or len(end_matches) == 0:
                cables_without_valid_endpoints += 1
                missing = []
                if not start_matches:
                    missing.append('start')
                if not end_matches:
                    missing.append('end')
                log.append(
                    f"Endpoint not connected: Cable {lf.id()} part {part_index + 1}; "
                    f"missing {', '.join(missing)} within {tolerance_m:.3f} m"
                )
                continue

            if len(start_matches) > 1:
                names = ', '.join(f'{t} {fid} ({d:.6f} m)' for d, t, fid in start_matches)
                log.append(f"Endpoint ambiguity: Cable {lf.id()} part {part_index + 1} START matches {names}")
                continue
            if len(end_matches) > 1:
                names = ', '.join(f'{t} {fid} ({d:.6f} m)' for d, t, fid in end_matches)
                log.append(f"Endpoint ambiguity: Cable {lf.id()} part {part_index + 1} END matches {names}")
                continue

            a = (start_matches[0][1], start_matches[0][2])
            b = (end_matches[0][1], end_matches[0][2])
            if a == b:
                cables_without_valid_endpoints += 1
                log.append(f"Endpoint invalid: Cable {lf.id()} part {part_index + 1} start and end resolve to the same node {nodes[a]['name']}")
                continue

            pair = tuple(sorted((a, b), key=lambda x: (x[0], x[1])))
            if pair in edge_by_pair:
                continue

            e = {
                'id': f'E{edge_id}',
                'line_id': lf.id(),
                'part_index': part_index,
                'node_a': a,
                'node_b': b,
            }
            edge_id += 1
            edge_by_pair[pair] = e
            adjacency[a].append(e)
            adjacency[b].append(e)

    log.append(f'Nodes: {len(nodes)}')
    log.append(f'Graph edges: {edge_id}')
    if cables_without_valid_endpoints:
        log.append(f'Cable endpoints not forming valid graph edges: {cables_without_valid_endpoints}')
    log.append(f'Endpoint tolerance: {tolerance_m:.3f} m (1 mm)')
    return nodes, adjacency


def _other(e, k):
    if e['node_a'] == k:
        return e['node_b']
    if e['node_b'] == k:
        return e['node_a']
    return None


def _edges(k, nodes, adj, direction):
    p = nodes[k]['point']
    arr = []
    for e in adj.get(k, []):
        o = _other(e, k)
        if o in nodes:
            arr.append((_angle(p, nodes[o]['point']), e))
    arr.sort(key=lambda x: (x[0], x[1]['id']), reverse=(direction == 'clockwise'))
    return [e for _, e in arr]


def _tree(root, nodes, adj, direction, log, seen_errors):
    """Build one rooted tree from already validated endpoint-to-endpoint edges."""
    children = defaultdict(list)
    parent = {root: None}
    parent_edge = {}
    stack = [root]

    def err(key, msg):
        if key not in seen_errors:
            seen_errors.add(key)
            log.append(msg)

    while stack:
        k = stack.pop()
        typ = nodes[k]['type']
        incident = _edges(k, nodes, adj, direction)
        pkey = parent.get(k)
        pedge = parent_edge.get(k)

        if typ == 'FAT' and len(incident) > 2:
            err(('degree', k), f'Topology error: FAT {nodes[k]["fid"]} has {len(incident)} connected Cables; FAT cannot be a branch node')
            continue
        if typ == 'CL' and len(incident) > 2:
            err(('degree', k), f'Topology error: CL {nodes[k]["fid"]} has {len(incident)} connected Cables; CL cannot be a branch node')
            continue
        if typ == 'BB' and len(incident) != 3:
            err(('degree', k), f'Topology error: BB {nodes[k]["fid"]} has {len(incident)} connected Cables; expected 1-in-2-out')

        if typ in ('FAT', 'CL'):
            downstream = [e for e in incident if e['id'] != (pedge or {}).get('id')]
            if len(downstream) > 1:
                err(('branch', k), f'Topology error: {typ} {nodes[k]["fid"]} has {len(downstream)} downstream Cables; check Cable endpoints')
                continue
        elif typ == 'BB':
            downstream = [e for e in incident if e['id'] != (pedge or {}).get('id')]
            downstream = [e for e in downstream if nodes[_other(e, k)]['type'] != 'FDT']
        else:
            downstream = [e for e in incident if e['id'] != (pedge or {}).get('id')]

        for e in downstream:
            o = _other(e, k)
            if o is None:
                continue
            if nodes[o]['type'] == 'FDT':
                err(('crossfdt', k, o), f'Topology warning: {typ} {nodes[k]["fid"]} connects to FDT {nodes[o]["name"]}; boundary edge ignored')
                continue
            if o in parent:
                err(('cycle', tuple(sorted((k, o)))), f'Topology warning: cycle or duplicate connectivity involving {nodes[k]["name"]} and {nodes[o]["name"]}')
                continue
            parent[o] = k
            parent_edge[o] = e
            children[k].append((o, e))
            stack.append(o)
    return children, parent


def _count_fats(k, children, nodes, memo):
    if k in memo:
        return memo[k]
    total = 1 if nodes[k]['type'] == 'FAT' else 0
    for child, _ in children.get(k, []):
        total += _count_fats(child, children, nodes, memo)
    memo[k] = total
    return total


def _write(layer, assigns, field):
    ix = layer.fields().indexOf(field)
    if ix < 0:
        return False, f'field {field!r} not found'
    own = not layer.isEditable()
    if own and not layer.startEditing():
        return False, f'cannot edit {layer.name()}'
    try:
        changes = {fid: {ix: value} for fid, value in assigns.items()}
        if changes and not layer.dataProvider().changeAttributeValues(changes):
            raise RuntimeError('changeAttributeValues failed')
        if own and not layer.commitChanges():
            raise RuntimeError('commitChanges failed')
        layer.triggerRepaint()
        return True, ''
    except Exception as exc:
        if own:
            try:
                layer.rollBack()
            except Exception:
                pass
        return False, str(exc)


def run_connection_point_naming_v2(params, iface=None):
    project = QgsProject.instance()
    log = ['============================', 'ODN Multi-Node Naming Start', '============================']
    errors = set()

    def get(layer_id, layer_name):
        layer = project.mapLayer(str(layer_id)) if layer_id else None
        if layer is not None:
            return layer
        items = project.mapLayersByName(str(layer_name)) if layer_name else []
        return items[0] if items else None

    fdt = get(params.get('conv_point_layer_id'), params.get('conv_point_layer_name'))
    line = get(params.get('line_layer_id'), params.get('line_layer_name'))
    if fdt is None or line is None:
        QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', '未找到汇聚点或连接线图层')
        return False

    node_layers = {}
    for typ in ('FAT', 'CL', 'BB'):
        if params.get(f'{typ.lower()}_enabled'):
            layer = get(params.get(f'{typ.lower()}_layer_id'), params.get(f'{typ.lower()}_layer_name'))
            if layer is None:
                QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'未找到 {typ} 图层')
                return False
            node_layers[typ] = layer

    write_mode = params.get('data_write_mode', 'addAttr')
    field = params.get('add_attr_name') if write_mode == 'addAttr' else params.get('modify_attr_name')
    if not field:
        QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', '未指定输出字段')
        return False

    if write_mode == 'addAttr':
        for layer in node_layers.values():
            if layer.fields().indexOf(field) < 0:
                if not layer.startEditing():
                    QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'无法编辑图层 {layer.name()}')
                    return False
                layer.dataProvider().addAttributes([QgsField(field, QVariant.String)])
                layer.updateFields()
                if not layer.commitChanges():
                    layer.rollBack()
                    QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'无法创建字段 {field}')
                    return False
    else:
        for typ, layer in node_layers.items():
            if layer.fields().indexOf(field) < 0:
                QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'{typ} 图层缺少字段 {field}')
                return False

    tolerance_m = ENDPOINT_TOLERANCE_M
    try:
        if params.get('connection_tolerance_m') is not None:
            tolerance_m = float(params.get('connection_tolerance_m'))
    except Exception:
        tolerance_m = ENDPOINT_TOLERANCE_M
    # The business rule is strict 1 mm. Ignore any legacy larger value.
    tolerance_m = ENDPOINT_TOLERANCE_M

    direction = 'clockwise' if params.get('point_name_directions_enum') == 'clockwise' else 'counter'
    reverse = params.get('point_name_sort_enum', 'fromConv') != 'fromConv'
    lp = params.get('l_prefix', 'L')
    lm = params.get('l_suffix', 'num')
    sp = params.get('fat_prefix', 'S')
    sm = params.get('fat_suffix', 'num')
    cp = params.get('cl_prefix', '_CL')
    cm = params.get('cl_suffix', 'num')
    bp = params.get('bb_prefix', '_BB')
    auto = bool(params.get('bb_auto_type', True))
    prefix = params.get('prefix', '') or ''

    nodes, adj = _build_graph(
        fdt,
        (params.get('conv_point_layer_field_name') or '').strip(),
        node_layers,
        line,
        tolerance_m,
        log,
    )
    assigns = {t: {} for t in node_layers}
    claimed = set()

    # Each valid FDT endpoint edge starts exactly one L.
    for fk, fdt_node in nodes.items():
        if fdt_node['type'] != 'FDT':
            continue
        root_edges = _edges(fk, nodes, adj, direction)
        for li, root_edge in enumerate(root_edges, 1):
            root = _other(root_edge, fk)
            if root is None:
                continue
            if root in claimed:
                errors.add(('duplicate-root', fk, root))
                continue

            children, parent = _tree(root, nodes, adj, direction, log, errors)
            order = []

            def walk(k):
                if k in order:
                    return
                order.append(k)
                child_items = list(children.get(k, []))
                if nodes[k]['type'] == 'BB':
                    child_items.sort(
                        key=lambda x: _angle(nodes[k]['point'], nodes[x[0]]['point']),
                        reverse=(direction == 'clockwise'),
                    )
                else:
                    child_items.sort(key=lambda x: x[1]['id'])
                for child, _ in child_items:
                    walk(child)

            walk(root)

            fresh = []
            for k in order:
                if k in claimed:
                    errors.add(('shared', k))
                    continue
                claimed.add(k)
                fresh.append(k)
            if not fresh:
                continue

            base = f'{prefix}{fdt_node["name"]}{_part(lp, li, lm)}'
            seq = list(reversed(fresh)) if reverse else fresh
            fat_counter = 0
            cl_counter = 0
            bb_count = 0

            for k in seq:
                typ = nodes[k]['type']
                if typ == 'FAT':
                    fat_counter += 1
                    assigns['FAT'][nodes[k]['fid']] = f'{base}{_part(sp, fat_counter, sm)}'
                elif typ == 'CL':
                    cl_counter += 1
                    assigns['CL'][nodes[k]['fid']] = f'{base}{_part(cp, cl_counter, cm)}'
                elif typ == 'BB':
                    bb_count += 1
                    name = 'BB3070'
                    out = children.get(k, [])
                    if auto and len(out) == 2:
                        counts = [_count_fats(c, children, nodes, {}) for c, _ in out]
                        name = 'BB5050' if counts[0] == counts[1] and counts[0] > 0 else 'BB3070'
                    assigns['BB'][nodes[k]['fid']] = f'{base}{bp}{name}'

            log.append(
                f"{fdt_node['name']} {_part(lp, li, lm)}: "
                f"FAT={fat_counter}, CL={cl_counter}, BB={bb_count}"
            )

    for k, node in nodes.items():
        if node['type'] != 'FDT' and k not in claimed:
            log.append(f"Unassigned: {node['type']} {node['fid']} is not reachable from any FDT through 1 mm endpoint connections")

    for typ, layer in node_layers.items():
        ok, msg = _write(layer, assigns.get(typ, {}), field)
        if not ok:
            QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'{typ} 写入失败：{msg}')
            return False

    log.append(
        f"Assignments: FAT={len(assigns.get('FAT', {}))}, "
        f"CL={len(assigns.get('CL', {}))}, BB={len(assigns.get('BB', {}))}"
    )
    log.append('============================')
    log.append('ODN Multi-Node Naming End')

    try:
        dialog = QtWidgets.QDialog()
        dialog.setWindowTitle('ODN Tools Pro - 接入点命名日志')
        dialog.resize(820, 580)
        layout = QtWidgets.QVBoxLayout(dialog)
        edit = QtWidgets.QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText('\n'.join(log))
        layout.addWidget(edit)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()
    except Exception:
        pass

    return True
