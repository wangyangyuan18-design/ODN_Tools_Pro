# -*- coding: utf-8 -*-
"""ODN Project physical-route services.

The active ODN Project is the sole source of operational layers and semantic
field bindings.  This module provides reusable Pole Edge routing services to
Link Design and future ODN functions.
"""
from collections import defaultdict
from heapq import heappop, heappush
from math import inf

from qgis.core import (
    QgsCoordinateTransform, QgsDistanceArea, QgsGeometry,
    QgsPointXY, QgsProject,
)


def project_layer(payload, role):
    """Return the QGIS layer bound to an ODN Project role."""
    entry = (payload or {}).get("layer_registry", {}).get(role, {})
    return QgsProject.instance().mapLayer(entry.get("layer_id", ""))


def _node_key(point):
    return (round(float(point.x()), 8), round(float(point.y()), 8))


def _edge_key(a, b):
    return tuple(sorted((a, b)))


def feature_name(payload, layer, role, feature):
    """Resolve a feature's display name from the project's field registry."""
    mapping = (payload or {}).get("field_registry", {}).get(role, {})
    configured = mapping.get("名称")
    if configured and configured in layer.fields().names():
        value = feature[configured]
        if value not in (None, ""):
            return str(value)
    for field_name in ("Name", "NAME", "name"):
        if field_name in layer.fields().names() and feature[field_name] not in (None, ""):
            return str(feature[field_name])
    return str(feature.id())


class OdnProjectRouteEngine:
    """Weighted shortest-path engine over the active project's Pole Edge graph."""

    def __init__(self, iface, payload, attach_distance):
        self.iface = iface
        self.project = QgsProject.instance()
        self.payload = payload or {}
        self.attach_distance = float(attach_distance)
        if self.attach_distance <= 0:
            raise ValueError("FAT 挂杆最大距离必须大于 0。")

        self.fdt_layer = project_layer(self.payload, "FDT")
        self.fat_layer = project_layer(self.payload, "FAT")
        self.existing_pole_layer = project_layer(self.payload, "Existing Pole")
        self.new_pole_layer = project_layer(self.payload, "New Pole")
        self.edge_layer = project_layer(self.payload, "Pole Edge")

        self.graph = defaultdict(list)
        self.edge_len = {}
        self.edge_geom = {}
        self.node_points = {}
        self.points = {}
        self._build_graph()
        self._index_design_points()

    def ready(self):
        return (
            self.fdt_layer is not None
            and self.fat_layer is not None
            and self.edge_layer is not None
            and bool(self.graph)
        )

    def _measure(self, geometry):
        distance = QgsDistanceArea()
        distance.setSourceCrs(
            self.edge_layer.crs(), self.project.transformContext()
        )
        return distance.measureLength(geometry)

    def _build_graph(self):
        if self.edge_layer is None:
            return
        for feature in self.edge_layer.getFeatures():
            geometry = feature.geometry()
            if geometry.isEmpty():
                continue
            parts = geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]
            for points in parts:
                if len(points) < 2:
                    continue
                for a, b in zip(points[:-1], points[1:]):
                    start, end = _node_key(a), _node_key(b)
                    if start == end:
                        continue
                    edge_id = (int(feature.id()), _edge_key(start, end))
                    segment = QgsGeometry.fromPolylineXY([QgsPointXY(a), QgsPointXY(b)])
                    length = self._measure(segment)
                    self.graph[start].append((end, length, edge_id))
                    self.graph[end].append((start, length, edge_id))
                    self.edge_len[edge_id] = length
                    self.edge_geom[edge_id] = segment
                    self.node_points[start] = QgsPointXY(a)
                    self.node_points[end] = QgsPointXY(b)

    def _index_design_points(self):
        for role, typ in (("FDT", "FDT"), ("FAT", "FAT")):
            layer = project_layer(self.payload, role)
            if layer is None:
                continue
            for feature in layer.getFeatures():
                geometry = feature.geometry()
                if geometry.isEmpty():
                    continue
                point = QgsPointXY(geometry.centroid().asPoint())
                key = (layer.id(), int(feature.id()))
                self.points[key] = {
                    "typ": typ,
                    "label": feature_name(self.payload, layer, role, feature),
                    "point": point,
                    "layer": layer,
                    "feature_id": int(feature.id()),
                    "role": role,
                }

    def points_of_type(self, typ):
        return [(key, info) for key, info in self.points.items() if info["typ"] == typ]

    def point_by_id(self, typ, feature_id):
        feature_id = int(feature_id)
        for key, info in self.points.items():
            if info["typ"] == typ and info["feature_id"] == feature_id:
                return key, info
        return None, None

    def _point_in_edge_crs(self, info):
        point = QgsPointXY(info["point"])
        source, target = info["layer"].crs(), self.edge_layer.crs()
        if source != target:
            point = QgsCoordinateTransform(
                source, target, self.project.transformContext()
            ).transform(point)
        return point

    def _point_distance(self, a, b):
        return self._measure(
            QgsGeometry.fromPolylineXY([QgsPointXY(a), QgsPointXY(b)])
        )

    def attach(self, typ, feature_id):
        _, info = self.point_by_id(typ, feature_id)
        if info is None or not self.node_points:
            return None

        point = self._point_in_edge_crs(info)
        best = None
        for node, node_point in self.node_points.items():
            distance = self._point_distance(point, node_point)
            if best is None or distance < best[0]:
                best = (distance, node)
        if best is None or best[0] > self.attach_distance:
            return None
        return {
            "connector": best[0],
            "node": best[1],
            "point": point,
            "typ": typ,
            "feature_id": info["feature_id"],
            "label": info["label"],
        }

    def route(self, start_typ, start_id, end_typ, end_id):
        start = self.attach(start_typ, start_id)
        end = self.attach(end_typ, end_id)
        if not start or not end:
            return None

        start_node, end_node = start["node"], end["node"]
        distances = {start_node: 0.0}
        previous = {}
        heap = [(0.0, start_node)]

        while heap:
            current_distance, node = heappop(heap)
            if current_distance != distances.get(node):
                continue
            if node == end_node:
                break
            for neighbour, weight, edge_id in self.graph.get(node, []):
                new_distance = current_distance + weight
                if new_distance < distances.get(neighbour, inf):
                    distances[neighbour] = new_distance
                    previous[neighbour] = (node, edge_id)
                    heappush(heap, (new_distance, neighbour))

        if end_node not in distances:
            return None

        graph_nodes, edge_sequence = [], []
        node = end_node
        while node != start_node:
            parent, edge_id = previous[node]
            graph_nodes.append(node)
            edge_sequence.append(edge_id)
            node = parent
        graph_nodes.append(start_node)
        graph_nodes.reverse()
        edge_sequence.reverse()

        graph_points = [self.node_points[n] for n in graph_nodes]
        attached_points = [start["point"]] + graph_points + [end["point"]]
        points = []
        for point in attached_points:
            point = QgsPointXY(point)
            if not points or _node_key(points[-1]) != _node_key(point):
                points.append(point)

        total = start["connector"] + distances[end_node] + end["connector"]
        return {
            "from_type": start_typ,
            "from_id": start_id,
            "from_label": start["label"],
            "to_type": end_typ,
            "to_id": end_id,
            "to_label": end["label"],
            "distance": total,
            "pole_edge_distance": distances[end_node],
            "start_connector": start["connector"],
            "end_connector": end["connector"],
            "edge_sequence": edge_sequence,
            "graph_nodes": graph_nodes,
            "points": points,
        }
