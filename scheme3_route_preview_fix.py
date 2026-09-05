# -*- coding: utf-8 -*-
"""Runtime fixes for Scheme 3 route preview and distance display.

This keeps the existing manual Link Design implementation intact while making
three details explicit:
1. every clicked FAT immediately produces a real Pole Edge route preview;
2. the route includes the endpoint-to-Pole Edge attachment pieces;
3. the UI shows both current-segment length and cumulative Link length.
"""

from math import inf

from qgis.PyQt import QtWidgets
from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject, QgsWkbTypes


def _same_point(a, b):
    return abs(a.x() - b.x()) < 1e-10 and abs(a.y() - b.y()) < 1e-10


def _point_in_edge_crs(engine, info):
    point = QgsPointXY(info['point'])
    source_crs = info['layer'].crs()
    target_crs = engine.edge_layer.crs()
    if source_crs != target_crs:
        point = QgsCoordinateTransform(
            source_crs, target_crs, QgsProject.instance().transformContext()
        ).transform(point)
    return point


def _route_to_canvas(engine, points):
    src = engine.edge_layer.crs()
    dst = engine.iface.mapCanvas().mapSettings().destinationCrs()
    if src == dst:
        return [QgsPointXY(p) for p in points]
    transform = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
    return [transform.transform(QgsPointXY(p)) for p in points]


def _build_attached_points(engine, start_info, end_info, start_node, end_node, graph_nodes):
    start_point = _point_in_edge_crs(engine, start_info)
    end_point = _point_in_edge_crs(engine, end_info)
    points = [start_point]
    points.append(engine.node_points[start_node])
    for node in graph_nodes[1:-1]:
        points.append(engine.node_points[node])
    points.append(engine.node_points[end_node])
    points.append(end_point)

    deduped = []
    for point in points:
        if not deduped or not _same_point(deduped[-1], point):
            deduped.append(QgsPointXY(point))
    return deduped


def _cns_attach(self, label):
    info = self._point_for(label)
    if not info:
        return None
    point = _point_in_edge_crs(self, info)
    best = None
    for node, node_point in self.node_points.items():
        distance = self._point_distance(point, node_point)
        if best is None or distance < best[0]:
            best = (distance, node)
    limit = self.params['attach'] if info['typ'] == 'FAT' else max(self.params['attach'], 10.0)
    if best is None or best[0] > limit:
        return None
    return best[0], best[1], point


def _cns_shortest(self, start_label, end_label):
    start_info = self._point_for(start_label)
    end_info = self._point_for(end_label)
    if not start_info or not end_info:
        return None

    start_attach = self._cns_attach(self, start_label)
    end_attach = self._cns_attach(self, end_label)
    if not start_attach or not end_attach:
        return None

    start_connector, start_node, _ = start_attach
    end_connector, end_node, _ = end_attach

    dist = {start_node: 0.0}
    previous = {}
    heap = [(0.0, start_node)]
    while heap:
        current_distance, node = heap.pop()
        # The graph is small enough here that using a sorted insertion is
        # simpler and more robust across QGIS/Python builds than relying on
        # a heap implementation imported from the original module.
        if current_distance != dist.get(node):
            continue
        if node == end_node:
            break
        for neighbour, weight, edge_id in self.graph.get(node, []):
            new_distance = current_distance + weight
            if new_distance < dist.get(neighbour, inf):
                dist[neighbour] = new_distance
                previous[neighbour] = (node, edge_id)
                heap.append((new_distance, neighbour))
                heap.sort(key=lambda item: item[0])

    if end_node not in dist:
        return None

    graph_nodes = []
    edges = []
    current = end_node
    while current != start_node:
        previous_node, edge_id = previous[current]
        graph_nodes.append(current)
        edges.append(edge_id)
        current = previous_node
    graph_nodes.append(start_node)
    graph_nodes.reverse()
    edges.reverse()

    total = start_connector + dist[end_node] + end_connector
    points = _build_attached_points(
        self, start_info, end_info, start_node, end_node, graph_nodes
    )
    return dict(
        from_label=start_label,
        to_label=end_label,
        distance=total,
        edge_sequence=edges,
        points=points,
        start_connector=start_connector,
        end_connector=end_connector,
        pole_edge_distance=dist[end_node],
    )


def _cns_segment_total(self, dialog):
    if not dialog._current or not dialog._current_fdt:
        return 0.0, []
    labels = [dialog._current_fdt] + [item[1] for item in dialog._current]
    infos = []
    total = 0.0
    for start, end in zip(labels[:-1], labels[1:]):
        info = self.segment_info(start, end)
        if info:
            infos.append(info)
            total += info['distance']
    return total, infos


def _patched_set_segment_info(self, info, total_length=None):
    if not info:
        self.segment_label.setText('当前段：—')
        self.route_label.setText('Pole Edge：— | 当前 Link 累计：0.0m')
        return

    limit = self._params()['max_seg']
    flag = f'⚠ >{limit:.0f}m' if info['distance'] > limit else '✓'
    self.segment_label.setText(
        f"当前段：{info['from_label']} → {info['to_label']}  "
        f"实际路径 {info['distance']:.1f}m  {flag}"
    )
    if total_length is None:
        total_length = info['distance']
    self.route_label.setText(
        f"Pole Edge 实际路径：{info['distance']:.1f}m  |  "
        f"当前 Link 累计：{total_length:.1f}m  |  "
        f"Pole Edge 边段：{len(info['edge_sequence'])}"
    )


def _patched_update_current(self):
    path = (
        [self._current_fdt] + [item[1] for item in self._current]
        if self._current_fdt else [item[1] for item in self._current]
    )
    self.current_path_label.setText('     ↓\n'.join(path) if path else '等待开始')
    self.current_label.setText(
        f"{self._current_fdt or 'FDT—'} / {self._current_link or self.link_combo.currentText()}"
    )

    if self._current and self._engine:
        total, infos = _cns_segment_total(self._engine, self)
        if infos:
            self._set_segment_info(infos[-1], total)
        else:
            self._set_segment_info(None)
    else:
        self._set_segment_info(None)
    self._refresh_tree()


def _patched_refresh_route_preview(self):
    if self._hover_band:
        try:
            self.iface.mapCanvas().scene().removeItem(self._hover_band)
        except Exception:
            pass
        self._hover_band = None

    if not self.dialog._current or not self.dialog._current_fdt:
        return

    labels = [self.dialog._current_fdt] + [item[1] for item in self.dialog._current]
    all_points = []
    for start, end in zip(labels[:-1], labels[1:]):
        info = self.engine.segment_info(start, end)
        if not info:
            continue
        display_points = _route_to_canvas(self.engine, info['points'])
        if all_points and display_points and _same_point(all_points[-1], display_points[0]):
            all_points.extend(display_points[1:])
        else:
            all_points.extend(display_points)

    if len(all_points) >= 2:
        self._hover_band = QgsRubberBand(
            self.iface.mapCanvas(), QgsWkbTypes.LineGeometry
        )
        self._hover_band.setWidth(4)
        self._hover_band.setColor(QtWidgets.QApplication.palette().highlight().color())
        self._hover_band.setToGeometry(
            QgsGeometry.fromPolylineXY(all_points), None
        )


def _patched_draw_route(self, info):
    display_points = _route_to_canvas(self, info['points'])
    if len(display_points) < 2:
        return
    rb = self._new_route_band()
    rb.setToGeometry(QgsGeometry.fromPolylineXY(display_points), None)
    self.route_bands.append(rb)


def _new_route_band(self):
    # Keep the visual treatment consistent with the existing temporary route.
    from qgis.PyQt.QtGui import QColor
    from qgis.gui import QgsRubberBand

    rb = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.LineGeometry)
    rb.setWidth(4)
    rb.setColor(QColor(0, 120, 255, 180))
    return rb


def _patched_hover_set(self, info):
    total = None
    if info and self.dialog._engine:
        total, _ = _cns_segment_total(self.dialog._engine, self.dialog)
    self.dialog._set_segment_info(info, total)


def install_scheme3_route_preview_fix(dialog_class, engine_class, map_tool_class):
    """Install the route preview/distance fixes once per QGIS session."""
    if getattr(dialog_class, '_odn_route_preview_fixed', False):
        return

    engine_class._attach = _cns_attach
    engine_class.shortest = _cns_shortest
    engine_class.draw_route = _patched_draw_route
    engine_class._new_route_band = _new_route_band

    dialog_class._set_segment_info = _patched_set_segment_info
    dialog_class._update_current = _patched_update_current
    map_tool_class.refresh_route_preview = _patched_refresh_route_preview

    # Mouse-hover feedback should keep using the same distance presentation.
    map_tool_class.canvasMoveEvent = _patched_canvas_move_event(map_tool_class)
    dialog_class._odn_route_preview_fixed = True


def _patched_canvas_move_event(map_tool_class):
    def canvasMoveEvent(self, event):
        if not self.dialog._current:
            return
        hit = self._nearest(event.pos())
        if not hit:
            return
        _, _, info = hit
        if info['typ'] != 'FAT':
            return
        previous = (
            self.dialog._current[-1][1]
            if self.dialog._current else self.dialog._current_fdt
        )
        route = self.engine.segment_info(previous, info['label'])
        if route:
            total = sum(
                x['distance']
                for x in _cns_segment_total(self.engine, self.dialog)[1][:-1]
            ) + route['distance']
            self.dialog._set_segment_info(route, total)
    return canvasMoveEvent
