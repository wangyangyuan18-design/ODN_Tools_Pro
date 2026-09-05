# -*- coding: utf-8 -*-
"""Multi-node ODN connection-point naming engine.

Business model:
- FDT creates first-level L branches.
- FAT consumes a continuous S counter within each FDT-L branch.
- CL is transparent in topology and uses an independent CL counter.
- BB is a 1-in/2-out branch node; BB5050/BB3070 is inferred from the
  FAT count of its two outgoing subtrees.
"""

import math
from collections import defaultdict

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateTransform,
    QgsField,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsSpatialIndex,
    QgsWkbTypes,
)


def _point_from_geometry(geometry):
    if geometry is None or geometry.isEmpty():
        return None
    try:
        if QgsWkbTypes.geometryType(geometry.wkbType()) == QgsWkbTypes.PointGeometry:
            if geometry.isMultipart():
                points = geometry.asMultiPoint()
                return QgsPointXY(points[0]) if points else None
            return QgsPointXY(geometry.asPoint())
    except Exception:
        pass
    return None


def _connection_tolerance(line_layer):
    try:
        return 1e-5 if line_layer.crs().isGeographic() else 0.5
    except Exception:
        return 0.5


def _number_text(index, mode):
    if mode == 'letter':
        n = int(index)
        result = ''
        while n > 0:
            n, rem = divmod(n - 1, 26)
            result = chr(ord('A') + rem) + result
        return result
    if mode == '':
        return ''
    return str(int(index))


def _part(prefix, index, mode):
    return f"{prefix}{_number_text(index, mode)}"


def _angle(origin, point):
    return math.atan2(point.y() - origin.y(), point.x() - origin.x())


def _build_node_layers(layers_by_type, line_crs):
    payloads = {}
    context = QgsProject.instance().transformContext()
    for node_type, layer in layers_by_type.items():
        try:
            transform = QgsCoordinateTransform(layer.crs(), line_crs, context)
        except Exception:
            transform = None
        index = QgsSpatialIndex()
        features = {}
        points = {}
        for feature in layer.getFeatures():
            geometry = feature.geometry()
            point = _point_from_geometry(geometry)
            if point is None:
                continue
            if transform is not None and layer.crs() != line_crs:
                try:
                    transformed = QgsGeometry(geometry)
                    transformed.transform(transform)
                    point = _point_from_geometry(transformed)
                except Exception:
                    continue
            if point is None:
                continue
            features[feature.id()] = feature
            points[feature.id()] = point
            try:
                temp = QgsFeature(feature)
                temp.setGeometry(QgsGeometry.fromPointXY(point))
                index.addFeature(temp)
            except Exception:
                try:
                    index.addFeature(feature)
                except Exception:
                    pass
        payloads[node_type] = {'layer': layer, 'index': index, 'features': features, 'points': points}
    return payloads


def _build_graph(fdt_layer, fdt_field, node_layers, line_layer, tolerance, log):
    layer_map = {'FDT': fdt_layer}
    layer_map.update(node_layers)
    payloads = _build_node_layers(layer_map, line_layer.crs())
    nodes = {}
    for node_type, payload in payloads.items():
        for fid, feature in payload['features'].items():
            name = str(fid)
            if node_type == 'FDT':
                try:
                    value = feature.attribute(fdt_field)
                    if value is not None and str(value).strip():
                        name = str(value).strip()
                except Exception:
                    pass
            nodes[(node_type, fid)] = {
                'key': (node_type, fid),
                'type': node_type,
                'fid': fid,
                'feature': feature,
                'point': payload['points'][fid],
                'name': name,
            }

    adjacency = defaultdict(list)
    edge_counter = 0
    for line_feature in line_layer.getFeatures():
        geometry = line_feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        bbox = geometry.boundingBox()
        bbox.setXMinimum(bbox.xMinimum() - tolerance)
        bbox.setXMaximum(bbox.xMaximum() + tolerance)
        bbox.setYMinimum(bbox.yMinimum() - tolerance)
        bbox.setYMaximum(bbox.yMaximum() + tolerance)
        positions = {}
        for node_type, payload in payloads.items():
            try:
                candidate_ids = payload['index'].intersects(bbox)
            except Exception:
                candidate_ids = []
            for fid in candidate_ids:
                point = payload['points'].get(fid)
                if point is None:
                    continue
                try:
                    pgeom = QgsGeometry.fromPointXY(point)
                    if geometry.distance(pgeom) > tolerance:
                        continue
                    position = geometry.lineLocatePoint(pgeom)
                except Exception:
                    continue
                if position is None or position < 0:
                    continue
                positions[(node_type, fid)] = float(position)

        ordered = sorted(positions.items(), key=lambda item: (item[1], item[0][0], item[0][1]))
        for (node_a, pos_a), (node_b, pos_b) in zip(ordered[:-1], ordered[1:]):
            if node_a == node_b:
                continue
            edge = {
                'id': f'E{edge_counter}',
                'line_id': line_feature.id(),
                'node_a': node_a,
                'node_b': node_b,
                'length': max(0.0, pos_b - pos_a),
            }
            edge_counter += 1
            adjacency[node_a].append(edge)
            adjacency[node_b].append(edge)

    log.append(f'Nodes: {len(nodes)}')
    log.append(f'Graph edges: {edge_counter}')
    return nodes, adjacency


def _other(edge, node_key):
    if edge['node_a'] == node_key:
        return edge['node_b']
    if edge['node_b'] == node_key:
        return edge['node_a']
    return None


def _ordered_edges(node_key, nodes, adjacency, direction):
    origin = nodes[node_key]['point']
    items = []
    for edge in adjacency.get(node_key, []):
        other = _other(edge, node_key)
        if other not in nodes:
            continue
        items.append((_angle(origin, nodes[other]['point']), edge))
    items.sort(key=lambda item: item[0], reverse=(direction == 'clockwise'))
    return [edge for _, edge in items]


def _prepare_tree(root_key, nodes, adjacency, direction, log):
    parent = {root_key: None}
    children = defaultdict(list)
    stack = [root_key]
    while stack:
        current = stack.pop()
        node = nodes[current]
        incident = list(adjacency.get(current, []))
        degree = len(incident)
        if node['type'] == 'FAT' and degree > 2:
            log.append(f"Topology error: FAT {node['fid']} has {degree} connected cables")
        if node['type'] == 'CL' and degree != 2:
            log.append(f"Topology warning: CL {node['fid']} has degree {degree}, expected 2")
        if node['type'] == 'BB' and degree != 3:
            log.append(f"Topology error: BB {node['fid']} has degree {degree}, expected 3")

        candidates = _ordered_edges(current, nodes, adjacency, direction) if current == root_key or node['type'] == 'BB' else incident
        for edge in candidates:
            other = _other(edge, current)
            if other is None or other == parent.get(current):
                continue
            if nodes.get(other, {}).get('type') == 'FDT':
                log.append(f"Topology error: FDT {nodes[current]['name']} directly connects FDT {nodes[other]['name']}")
                continue
            if other in parent:
                log.append(f"Topology error: cycle or duplicate connectivity involving {nodes[current]['name']} and {nodes[other]['name']}")
                continue
            parent[other] = current
            children[current].append((other, edge))
            stack.append(other)
    return parent, children


def _count_fats(key, children, nodes, cache):
    if key in cache:
        return cache[key]
    total = 1 if nodes[key]['type'] == 'FAT' else 0
    for child, _ in children.get(key, []):
        total += _count_fats(child, children, nodes, cache)
    cache[key] = total
    return total


def _write_layer_names(layer, assignments, field_name):
    field_idx = layer.fields().indexOf(field_name)
    if field_idx < 0:
        return False, f"field '{field_name}' not found"
    was_editable = layer.isEditable()
    if not was_editable and not layer.startEditing():
        return False, 'layer could not enter edit mode'
    try:
        changes = {fid: {field_idx: value} for fid, value in assignments.items()}
        if changes and not layer.dataProvider().changeAttributeValues(changes):
            raise RuntimeError('changeAttributeValues returned False')
        if not was_editable and not layer.commitChanges():
            raise RuntimeError('commitChanges failed')
        return True, ''
    except Exception as exc:
        if not was_editable:
            try:
                layer.rollBack()
            except Exception:
                pass
        return False, str(exc)


def run_connection_point_naming_v2(params, iface=None):
    """Name FAT/CL/BB using one rooted FDT topology while preserving L/S conventions."""
    project = QgsProject.instance()
    log = ['============================', 'ODN Multi-Node Naming Start', '============================']

    def get_layer(layer_id, layer_name):
        if layer_id:
            layer = project.mapLayer(str(layer_id))
            if layer is not None:
                return layer
        layers = project.mapLayersByName(str(layer_name)) if layer_name else []
        return layers[0] if layers else None

    fdt = get_layer(params.get('conv_point_layer_id'), params.get('conv_point_layer_name'))
    line = get_layer(params.get('line_layer_id'), params.get('line_layer_name'))
    if fdt is None or line is None:
        QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', '未找到汇聚点或连接线图层')
        return False

    node_layers = {}
    for node_type in ('FAT', 'CL', 'BB'):
        if params.get(f'{node_type.lower()}_enabled'):
            layer = get_layer(params.get(f'{node_type.lower()}_layer_id'), params.get(f'{node_type.lower()}_layer_name'))
            if layer is None:
                QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'未找到 {node_type} 图层')
                return False
            node_layers[node_type] = layer
    if not node_layers:
        QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', '至少启用一个节点图层')
        return False

    fdt_field = (params.get('conv_point_layer_field_name') or '').strip()
    write_mode = params.get('data_write_mode', 'addAttr')
    field_name = params.get('add_attr_name') if write_mode == 'addAttr' else params.get('modify_attr_name')
    if not fdt_field or not field_name:
        QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', '汇聚点字段或输出字段未指定')
        return False

    if write_mode == 'addAttr':
        for layer in node_layers.values():
            if layer.fields().indexOf(field_name) < 0:
                was_editable = layer.isEditable()
                if not was_editable and not layer.startEditing():
                    QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'无法编辑图层 {layer.name()}')
                    return False
                layer.dataProvider().addAttributes([QgsField(field_name, QVariant.String)])
                layer.updateFields()
                if not was_editable and not layer.commitChanges():
                    QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'无法创建字段 {field_name}：{layer.name()}')
                    return False
    else:
        missing = [node_type for node_type, layer in node_layers.items() if layer.fields().indexOf(field_name) < 0]
        if missing:
            QtWidgets.QMessageBox.critical(None, 'ODN Tools Pro', f'所选节点图层缺少公共字段：{field_name} ({", ".join(missing)})')
            return False

    tolerance = float(params.get('connection_tolerance') or _connection_tolerance(line))
    direction = 'clockwise' if params.get('point_name_directions_enum') == 'clockwise' else 'counter'
    naming_direction = params.get('point_name_sort_enum', 'fromConv')
    l_prefix = params.get('l_prefix', params.get('variableL', 'L'))
    l_suffix = params.get('l_suffix', params.get('variableLSuf', 'num'))
    fat_prefix = params.get('fat_prefix', params.get('variableS', 'S'))
    fat_suffix = params.get('fat_suffix', params.get('variableSSuf', 'num'))
    cl_prefix = params.get('cl_prefix', '_CL')
    cl_suffix = params.get('cl_suffix', 'num')
    bb_prefix = params.get('bb_prefix', '_BB')
    fdt_prefix = params.get('prefix', '') or ''

    nodes, adjacency = _build_graph(fdt, fdt_field, node_layers, line, tolerance, log)
    fdt_keys = [key for key, node in nodes.items() if node['type'] == 'FDT']
    assignments = {node_type: {} for node_type in node_layers}
    assigned_nodes = set()

    for fdt_key in fdt_keys:
        _, children = _prepare_tree(fdt_key, nodes, adjacency, direction, log)
        root_edges = _ordered_edges(fdt_key, nodes, adjacency, direction)
        child_by_edge = {edge['id']: child for child, edge in children.get(fdt_key, [])}

        for lidx, root_edge in enumerate(root_edges, start=1):
            root_child = child_by_edge.get(root_edge['id'])
            if root_child is None:
                continue

            subtree = []
            stack = [root_child]
            seen = set()
            while stack:
                key = stack.pop()
                if key in seen:
                    continue
                seen.add(key)
                subtree.append(key)
                stack.extend(child for child, _ in children.get(key, []))

            bb_keys = [key for key in subtree if nodes[key]['type'] == 'BB']
            if len(bb_keys) > 1:
                log.append(f"Topology error: FDT {nodes[fdt_key]['name']} L{lidx} contains {len(bb_keys)} BB; expected at most 1")

            bb_types = {}
            for bb_key in bb_keys:
                bb_children = children.get(bb_key, [])
                counts = [_count_fats(child, children, nodes, {}) for child, _ in bb_children]
                if len(bb_children) != 2:
                    log.append(f"Topology error: BB {nodes[bb_key]['fid']} is not 1-in-2-out")
                    bb_types[bb_key] = 'BB3070'
                elif min(counts) <= 0:
                    log.append(f"Topology error: BB {nodes[bb_key]['fid']} has a branch with zero FAT")
                    bb_types[bb_key] = 'BB3070'
                else:
                    bb_types[bb_key] = 'BB5050' if counts[0] == counts[1] else 'BB3070'

            base = f"{fdt_prefix}{nodes[fdt_key]['name']}{_part(l_prefix, lidx, l_suffix)}"
            ordered_nodes = []

            def collect(key):
                if key in assigned_nodes:
                    return
                node = nodes[key]
                ordered_nodes.append(key)
                children_items = list(children.get(key, []))
                if node['type'] == 'BB':
                    children_items.sort(
                        key=lambda item: _angle(node['point'], nodes[item[0]]['point']),
                        reverse=(direction == 'clockwise'),
                    )
                for child_key, _ in children_items:
                    collect(child_key)

            collect(root_child)
            numbering_nodes = ordered_nodes if naming_direction == 'fromConv' else list(reversed(ordered_nodes))
            fat_counter = 0
            cl_counter = 0

            # BB names are independent of the FAT/CL counters, so every BB is assigned
            # from the topology regardless of numbering direction.
            for key in ordered_nodes:
                node = nodes[key]
                if node['type'] == 'BB':
                    assignments['BB'][node['fid']] = f"{base}{bb_prefix}{bb_types.get(key, 'BB3070')}"

            for key in numbering_nodes:
                node = nodes[key]
                if node['type'] == 'FAT':
                    fat_counter += 1
                    assignments['FAT'][node['fid']] = f"{base}{_part(fat_prefix, fat_counter, fat_suffix)}"
                elif node['type'] == 'CL':
                    cl_counter += 1
                    assignments['CL'][node['fid']] = f"{base}{_part(cl_prefix, cl_counter, cl_suffix)}"

            # Add nodes to the global seen set only after all names for this L are generated.
            assigned_nodes.update(ordered_nodes)
            log.append(f"{nodes[fdt_key]['name']} L{lidx}: FAT={fat_counter}, CL={cl_counter}, BB={len(bb_keys)}")

    for key, node in nodes.items():
        if node['type'] != 'FDT' and key not in assigned_nodes:
            log.append(f"Topology warning: {node['type']} {node['fid']} is not reachable from any FDT")

    success_layers = 0
    for node_type, layer in node_layers.items():
        ok, error = _write_layer_names(layer, assignments[node_type], field_name)
        if ok:
            success_layers += 1
        else:
            log.append(f"Write error: {node_type}: {error}")

    counts = ', '.join(f"{node_type}={len(values)}" for node_type, values in assignments.items())
    topology_errors = sum(1 for text in log if 'Topology error:' in text)
    log.append(f'Assignments: {counts}')
    log.append(f'Topology errors: {topology_errors}')
    try:
        QtWidgets.QMessageBox.information(
            None,
            'ODN Tools Pro - 接入点命名',
            f"命名完成：{counts}\n拓扑错误：{topology_errors}\n\n" + '\n'.join(log),
        )
    except Exception:
        pass
    return success_layers > 0
