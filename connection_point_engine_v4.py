# -*- coding: utf-8 -*-
"""ODN multi-node naming engine V4.

Topology and naming engine for FDT/FAT/CL/BB. Optional Cable naming uses the
same resolved topology and never depends on Cable geometry direction.
"""

from collections import defaultdict

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QVariant
from qgis.core import QgsField, QgsProject

from .connection_point_engine_v3 import (
    ENDPOINT_TOLERANCE_M,
    _build_graph,
    _count_fats,
    _edges,
    _other,
    _angle,
    _part,
)


def _tree_from_fdt(root_edge, fdt_key, nodes, adj, direction, log, seen_errors):
    root = _other(root_edge, fdt_key)
    children = defaultdict(list)
    parent = {root: fdt_key}
    parent_edge = {root: root_edge}
    stack = [root]

    def err(key, msg):
        if key not in seen_errors:
            seen_errors.add(key)
            log.append(msg)

    while stack:
        k = stack.pop()
        typ = nodes[k]['type']
        incident = _edges(k, nodes, adj, direction)
        pedge = parent_edge.get(k)
        downstream = [e for e in incident if e['id'] != (pedge or {}).get('id')]

        if typ == 'FAT':
            if len(incident) > 2:
                err(('degree', k), f'Topology error: FAT {nodes[k]["fid"]} has {len(incident)} connected Cables; FAT cannot be a branch node')
            if len(downstream) > 1:
                err(('branch', k), f'Topology error: FAT {nodes[k]["fid"]} has {len(downstream)} downstream Cables; check Cable endpoints')
                continue
        elif typ == 'CL':
            if len(incident) > 2:
                err(('degree', k), f'Topology error: CL {nodes[k]["fid"]} has {len(incident)} connected Cables; CL cannot be a branch node')
            if len(downstream) > 1:
                err(('branch', k), f'Topology error: CL {nodes[k]["fid"]} has {len(downstream)} downstream Cables; check Cable endpoints')
                continue
        elif typ == 'BB':
            downstream = [e for e in downstream if nodes[_other(e, k)]['type'] != 'FDT']
            if len(incident) != 3:
                err(('degree', k), f'Topology error: BB {nodes[k]["fid"]} has {len(incident)} connected Cables; expected 1-in-2-out')
            if len(downstream) > 2:
                err(('branch', k), f'Topology error: BB {nodes[k]["fid"]} has {len(downstream)} downstream Cables; expected at most 2')
                continue

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

    return root, children, parent, parent_edge


def _write_checked(layer, assigns, field, log, label):
    """Write values through the edit buffer when possible and verify them."""
    idx = layer.fields().indexOf(field)
    if idx < 0:
        return False, f"field {field!r} not found"
    if not assigns:
        log.append(f"{label}命名写入：0/0")
        return True, ''

    own_edit = not layer.isEditable()
    if own_edit and not layer.startEditing():
        return False, f"cannot edit {layer.name()}"

    failed = []
    try:
        for fid, value in assigns.items():
            ok = False
            try:
                ok = bool(layer.changeAttributeValue(fid, idx, value))
            except Exception:
                ok = False
            if not ok:
                # Fallback for providers that reject edit-buffer writes.
                try:
                    ok = bool(layer.dataProvider().changeAttributeValues({fid: {idx: value}}))
                except Exception:
                    ok = False
            if not ok:
                failed.append((fid, value, 'write failed'))

        if failed:
            if own_edit:
                layer.rollBack()
            return False, f"{len(failed)} feature(s) failed to write"

        if own_edit and not layer.commitChanges():
            layer.rollBack()
            return False, 'commitChanges failed'

        layer.updateFields()
        layer.triggerRepaint()

        verified = 0
        for fid, expected in assigns.items():
            try:
                feat = layer.getFeature(fid)
                actual = feat.attribute(field)
                if actual is not None and str(actual) == str(expected):
                    verified += 1
                else:
                    failed.append((fid, expected, f'actual={actual!r}'))
            except Exception as exc:
                failed.append((fid, expected, f'verify error: {exc}'))

        log.append(f"{label}命名写入：{verified}/{len(assigns)} 成功")
        if failed:
            log.append(f"{label}命名校验失败：{len(failed)}")
            for fid, expected, reason in failed[:10]:
                log.append(f"  Feature {fid}: expected={expected!r}; {reason}")
            return False, f"{len(failed)} feature(s) failed verification"
        return True, ''
    except Exception as exc:
        if own_edit:
            try:
                layer.rollBack()
            except Exception:
                pass
        return False, str(exc)


def run_connection_point_naming_v2(params, iface=None):
    project = QgsProject.instance()
    log = [
        '============================',
        'ODN Multi-Node Naming Start',
        '============================',
    ]
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

    direction = 'clockwise' if params.get('point_name_directions_enum') == 'clockwise' else 'counter'
    reverse = params.get('point_name_sort_enum', 'fromConv') != 'fromConv'
    lp = params.get('l_prefix', '_L')
    lm = params.get('l_suffix', 'num')
    sp = params.get('fat_prefix', '_S')
    sm = params.get('fat_suffix', 'num')
    cp = params.get('cl_prefix', '_CL')
    cm = params.get('cl_suffix', 'num')
    bp = params.get('bb_prefix', '_BB')
    auto = bool(params.get('bb_auto_type', True))
    prefix = params.get('prefix', '') or ''

    line_sync = bool(params.get('line_name_enabled', True))
    line_field = (params.get('line_name_field') or '').strip()
    line_direction = params.get('line_name_direction', 'fromConv')
    if line_sync:
        if not line_field:
            QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', '已勾选“同时命名连接线”，但未指定连接线输出字段')
            return False
        if line.fields().indexOf(line_field) < 0:
            QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'连接线图层缺少输出字段 {line_field}')
            return False

    nodes, adj = _build_graph(
        fdt,
        (params.get('conv_point_layer_field_name') or '').strip(),
        node_layers,
        line,
        ENDPOINT_TOLERANCE_M,
        log,
    )

    assigns = {t: {} for t in node_layers}
    claimed = set()
    line_assignments = {}
    tree_edges = {}

    for fk, fdt_node in nodes.items():
        if fdt_node['type'] != 'FDT':
            continue

        root_edges = _edges(fk, nodes, adj, direction)
        for li, root_edge in enumerate(root_edges, 1):
            root = _other(root_edge, fk)
            if root is None or root in claimed:
                continue

            root, children, parent, parent_edge = _tree_from_fdt(root_edge, fk, nodes, adj, direction, log, errors)
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
                    suffix = '3070'
                    out = children.get(k, [])
                    if auto and len(out) == 2:
                        counts = [_count_fats(c, children, nodes, {}) for c, _ in out]
                        suffix = '5050' if counts[0] == counts[1] and counts[0] > 0 else '3070'
                    assigns['BB'][nodes[k]['fid']] = f'{base}{bp}{suffix}'

            tree_edges[root_edge['id']] = (fk, root, root_edge)
            for child_key, parent_key in parent.items():
                if child_key == root and parent_key == fk:
                    continue
                edge = parent_edge.get(child_key)
                if edge is not None:
                    tree_edges[edge['id']] = (parent_key, child_key, edge)

            log.append(f"{fdt_node['name']} {_part(lp, li, lm)}: FAT={fat_counter}, CL={cl_counter}, BB={bb_count}")

    for k, node in nodes.items():
        if node['type'] != 'FDT' and k not in claimed:
            log.append(f"Unassigned: {node['type']} {node['fid']} is not reachable from any FDT through 1 mm endpoint connections")

    total_node_assignments = sum(len(assigns.get(t, {})) for t in node_layers)
    log.append(f'接入节点命名计划：{total_node_assignments} 个')
    for typ, layer in node_layers.items():
        ok, msg = _write_checked(layer, assigns.get(typ, {}), field, log, typ)
        if not ok:
            QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'{typ} 命名写入失败：{msg}')
            return False

    if line_sync:
        node_name_map = {}
        for key, node in nodes.items():
            typ = node['type']
            if typ == 'FDT':
                node_name_map[key] = node['name']
            else:
                node_name_map[key] = assigns.get(typ, {}).get(node['fid'])

        for parent_key, child_key, edge in tree_edges.values():
            parent_name = node_name_map.get(parent_key)
            child_name = node_name_map.get(child_key)
            if not parent_name or not child_name:
                log.append(f"Cable name skipped: line {edge.get('line_id')} has endpoint without final Name")
                continue
            cable_name = f'{child_name}-{parent_name}' if line_direction == 'toConv' else f'{parent_name}-{child_name}'
            line_id = edge.get('line_id')
            if line_id is None:
                continue
            if line_id in line_assignments and line_assignments[line_id] != cable_name:
                log.append(f"Cable naming conflict: line {line_id}: '{line_assignments[line_id]}' vs '{cable_name}'")
                continue
            line_assignments[line_id] = cable_name

        log.append(f'连接线命名计划：{len(line_assignments)} 条')
        ok, msg = _write_checked(line, line_assignments, line_field, log, '连接线')
        if not ok:
            QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'连接线写入失败：{msg}')
            return False

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
