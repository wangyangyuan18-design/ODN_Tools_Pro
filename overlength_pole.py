# -*- coding: utf-8 -*-
"""Project-driven overlength pole insertion tool."""

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.core import (
    QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsField,
    QgsGeometry, QgsMapLayerType, QgsPointXY, QgsProject, QgsSpatialIndex,
    QgsVectorLayer, QgsWkbTypes,
)

from . import odn_project_context as context


class OverlengthPoleDialog(QtWidgets.QDialog):
    """Insert poles using only the active ODN Project's pole layers and spacing rule."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("超距增点")
        self.setMinimumWidth(470)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setSpacing(9)

        title = QtWidgets.QLabel("超距增点")
        font = title.font(); font.setBold(True); font.setPointSize(12); title.setFont(font)
        lay.addWidget(title)

        lay.addWidget(QtWidgets.QLabel("杆子图层（项目配置）"))
        self.pole_label = QtWidgets.QLabel("—")
        self.pole_label.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        lay.addWidget(self.pole_label)

        lay.addWidget(QtWidgets.QLabel("连线图层（项目配置）"))
        self.line_label = QtWidgets.QLabel("—")
        self.line_label.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        lay.addWidget(self.line_label)

        self.rule_label = QtWidgets.QLabel("杆间距最大值（项目配置）：—")
        self.rule_label.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        lay.addWidget(self.rule_label)

        tip = QtWidgets.QLabel(
            "新增杆位直接写入当前 ODN Project 绑定的 New Pole 图层；"
            "Pole Edge 也只使用当前项目配置的图层。工具不提供独立工程距离参数。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#666; padding:4px 0;")
        lay.addWidget(tip)

        buttons = QtWidgets.QDialogButtonBox()
        ok = buttons.addButton("开始", QtWidgets.QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton("取消", QtWidgets.QDialogButtonBox.RejectRole)
        ok.clicked.connect(self._start)
        cancel.clicked.connect(self.reject)
        lay.addWidget(buttons)
        self._ok_button = ok
        self._load_project_layers()

    def _load_project_layers(self):
        payload = context.require_project(self, "超距增点")
        if not payload:
            self._ok_button.setEnabled(False)
            return

        existing = context.project_layer(payload, "Existing Pole")
        new = context.project_layer(payload, "New Pole")
        edge = context.project_layer(payload, "Pole Edge")
        params = payload.get("parameters", {}) or {}
        max_spacing = params.get("pole_spacing_max")

        names = []
        if existing is not None:
            names.append(f"Existing Pole：{existing.name()}")
        if new is not None:
            names.append(f"New Pole：{new.name()}")
        self.pole_label.setText("\n".join(names) if names else "未绑定")
        self.line_label.setText(edge.name() if edge is not None else "未绑定")
        self.rule_label.setText(
            "杆间距最大值（项目配置）："
            + (f"{float(max_spacing):g} m" if max_spacing is not None else "未配置")
        )

        valid_spacing = False
        try:
            valid_spacing = float(max_spacing) > 0
        except (TypeError, ValueError):
            pass

        # A tool whose purpose is to add poles must have a project-configured
        # New Pole layer. Existing poles are optional inputs.
        self._ok_button.setEnabled(
            new is not None
            and new.type() == QgsMapLayerType.VectorLayer
            and QgsWkbTypes.geometryType(new.wkbType()) == QgsWkbTypes.PointGeometry
            and edge is not None
            and edge.type() == QgsMapLayerType.VectorLayer
            and QgsWkbTypes.geometryType(edge.wkbType()) == QgsWkbTypes.LineGeometry
            and valid_spacing
        )

    def _start(self):
        payload = context.require_project(self, "超距增点")
        if not payload:
            return
        existing = context.project_layer(payload, "Existing Pole")
        new = context.project_layer(payload, "New Pole")
        edge = context.project_layer(payload, "Pole Edge")
        try:
            max_spacing = float((payload.get("parameters", {}) or {})["pole_spacing_max"])
        except (KeyError, TypeError, ValueError):
            QtWidgets.QMessageBox.warning(self, "超距增点", "当前 ODN Project 未配置有效的杆间距最大值。")
            return

        if new is None or edge is None:
            QtWidgets.QMessageBox.warning(self, "超距增点", "当前 ODN Project 未绑定 New Pole / Pole Edge。")
            return
        if QgsWkbTypes.geometryType(new.wkbType()) != QgsWkbTypes.PointGeometry:
            QtWidgets.QMessageBox.warning(self, "超距增点", "项目配置中的 New Pole 不是点图层。")
            return
        if QgsWkbTypes.geometryType(edge.wkbType()) != QgsWkbTypes.LineGeometry:
            QtWidgets.QMessageBox.warning(self, "超距增点", "项目配置中的 Pole Edge 不是线图层。")
            return

        self.accept()
        OverlengthPoleProcessor(
            self.iface,
            payload,
            [("Existing Pole", existing), ("New Pole", new)],
            edge,
            max_spacing,
        ).run()


class OverlengthPoleProcessor:
    SEARCH_METERS = 3.0
    IMPROVEMENT_METERS = 5.0

    def __init__(self, iface, payload, pole_layers, line_layer, max_spacing):
        self.iface = iface
        self.project = QgsProject.instance()
        self.payload = payload or {}
        self.pole_layers = [(role, layer) for role, layer in pole_layers if layer is not None]
        self.new_pole_layer = next((layer for role, layer in self.pole_layers if role == "New Pole"), None)
        self.line_layer = line_layer
        self.max_spacing = float(max_spacing)
        self.da = QgsDistanceArea()
        self.da.setEllipsoid(self.project.ellipsoid())
        self._index = QgsSpatialIndex()
        self._idx_map = {}
        self._created_ids = []
        self._stats = {"duplicates": 0, "over": 0, "new": 0, "segments": 0}

    def _prepare_poles(self):
        dst = self.line_layer.crs()
        self._index = QgsSpatialIndex()
        self._idx_map = {}
        index_id = 1
        for role, layer in self.pole_layers:
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue
            transform = (
                QgsCoordinateTransform(layer.crs(), dst, self.project.transformContext())
                if layer.crs() != dst else None
            )
            for feature in layer.getFeatures():
                geometry = feature.geometry()
                if geometry.isEmpty():
                    continue
                try:
                    point = QgsPointXY(geometry.centroid().asPoint())
                    if transform:
                        point = QgsPointXY(transform.transform(point))
                    spatial_feature = QgsFeature()
                    spatial_feature.setId(index_id)
                    spatial_feature.setGeometry(QgsGeometry.fromPointXY(point))
                    self._index.addFeature(spatial_feature)
                    self._idx_map[index_id] = (layer.id(), int(feature.id()), point, role)
                    index_id += 1
                except Exception:
                    continue

    def _meters_per_unit(self, point):
        self.da.setSourceCrs(self.line_layer.crs(), self.project.transformContext())
        try:
            values = [
                self.da.measureLine(point, QgsPointXY(point.x() + 1, point.y())),
                self.da.measureLine(point, QgsPointXY(point.x(), point.y() + 1)),
            ]
            values = [value for value in values if value > 1e-12]
            return min(values) if values else 1.0
        except Exception:
            return 1.0

    def _query_poles(self, line_geom):
        point = QgsPointXY(line_geom.centroid().asPoint())
        radius = self.SEARCH_METERS / self._meters_per_unit(point)
        bbox = line_geom.boundingBox()
        bbox.grow(radius)
        result = []
        for index_id in self._index.intersects(bbox):
            data = self._idx_map.get(index_id)
            if not data:
                continue
            try:
                if line_geom.closestSegmentWithContext(data[2])[0] <= radius * radius:
                    result.append((index_id, data, data[2]))
            except Exception:
                pass
        return result

    @staticmethod
    def _parts(geometry):
        return geometry.asMultiPolyline() if geometry.isMultipart() else [geometry.asPolyline()]

    def _cum_lengths(self, points):
        self.da.setSourceCrs(self.line_layer.crs(), self.project.transformContext())
        cumulative = [0.0]
        total = 0.0
        for a, b in zip(points[:-1], points[1:]):
            total += self.da.measureLine(QgsPointXY(a), QgsPointXY(b))
            cumulative.append(total)
        return cumulative, total

    def _project_location(self, points, point):
        geometry = QgsGeometry.fromPolylineXY([QgsPointXY(x) for x in points])
        try:
            result = geometry.closestSegmentWithContext(QgsPointXY(point))
            projected = QgsPointXY(result[1])
            after_vertex = int(result[2])
        except Exception:
            return None
        if len(points) < 2:
            return None
        segment = max(0, min(len(points) - 2, after_vertex - 1))
        cumulative, total = self._cum_lengths(points)
        a, b = QgsPointXY(points[segment]), QgsPointXY(points[segment + 1])
        segment_length = self.da.measureLine(a, b)
        fraction = (
            max(0.0, min(1.0, self.da.measureLine(a, projected) / segment_length))
            if segment_length > 1e-12 else 0.0
        )
        return cumulative[segment] + segment_length * fraction, projected, total

    @staticmethod
    def _midpoint(cuts):
        return (min(cuts) + max(cuts)) / 2.0 if cuts else 0.0

    def _choose_existing(self, points, candidates):
        locations = []
        _, total = self._cum_lengths(points)
        for _, data, point in candidates:
            location = self._project_location(points, point)
            if not location:
                continue
            distance = location[0]
            if distance <= 0.001 or total - distance <= 0.001:
                continue
            locations.append((distance, QgsPointXY(point)))
        if not locations:
            return []

        selected = []
        cuts = [0.0, total]
        remaining = locations[:]
        while remaining:
            ordered = sorted(cuts)
            current_max = max(b - a for a, b in zip(ordered[:-1], ordered[1:]))
            best = None
            best_gain = self.IMPROVEMENT_METERS
            for distance, point in remaining:
                if any(abs(distance - existing) < 0.01 for existing in cuts):
                    continue
                trial = sorted(cuts + [distance])
                new_max = max(b - a for a, b in zip(trial[:-1], trial[1:]))
                gain = current_max - new_max
                midpoint = self._midpoint(cuts)
                if (
                    gain >= best_gain
                    and (
                        best is None
                        or gain > best[0]
                        or (abs(gain - best[0]) < 1e-9 and abs(distance - midpoint) < abs(best[1] - midpoint))
                    )
                ):
                    best = (gain, distance, point)
            if best is None:
                break
            _, distance, point = best
            selected.append((distance, QgsPointXY(point)))
            cuts.append(distance)
            remaining = [item for item in remaining if abs(item[0] - distance) >= 0.01]
        return sorted(selected, key=lambda item: item[0])

    def _has_overlength(self, cut_specs, total):
        ordered = [(0.0,)] + [(distance,) for distance, _ in cut_specs] + [(total,)]
        return any(
            right[0] - left[0] > self.max_spacing + 1e-8
            for left, right in zip(ordered[:-1], ordered[1:])
        )

    def _point_at_distance(self, points, distance):
        cumulative, total = self._cum_lengths(points)
        if distance <= 0:
            return QgsPointXY(points[0])
        if distance >= total:
            return QgsPointXY(points[-1])
        for index in range(len(points) - 1):
            if cumulative[index] <= distance <= cumulative[index + 1]:
                span = cumulative[index + 1] - cumulative[index]
                fraction = (distance - cumulative[index]) / span if span > 1e-12 else 0.0
                a, b = QgsPointXY(points[index]), QgsPointXY(points[index + 1])
                return QgsPointXY(
                    a.x() + (b.x() - a.x()) * fraction,
                    a.y() + (b.y() - a.y()) * fraction,
                )
        return QgsPointXY(points[-1])

    def _make_new_cuts(self, cut_specs, total):
        boundaries = [(0.0, QgsPointXY(self._last_pts[0]))]
        boundaries.extend(sorted(cut_specs, key=lambda item: item[0]))
        boundaries.append((total, QgsPointXY(self._last_pts[-1])))
        result = []
        for left, right in zip(boundaries[:-1], boundaries[1:]):
            cursor_distance = left[0]
            right_distance = right[0]
            while right_distance - cursor_distance > self.max_spacing + 1e-8:
                step = self.max_spacing
                if cursor_distance + step >= right_distance - 1e-8:
                    break
                new_distance = cursor_distance + step
                new_point = self._point_at_distance(self._last_pts, new_distance)
                result.append((new_distance, new_point))
                self._add_new_pole(new_point)
                cursor_distance = new_distance
        return sorted(cut_specs + result, key=lambda item: item[0])

    def _subline(self, points, cut_specs):
        cumulative, total = self._cum_lengths(points)
        cuts = [(0.0, QgsPointXY(points[0]))]
        cuts.extend(
            (distance, QgsPointXY(point))
            for distance, point in sorted(cut_specs, key=lambda item: item[0])
        )
        cuts.append((total, QgsPointXY(points[-1])))
        output = []
        for (start_distance, start_point), (end_distance, end_point) in zip(cuts[:-1], cuts[1:]):
            if end_distance - start_distance <= 1e-8:
                continue
            coords = [start_point]
            for index in range(1, len(points) - 1):
                if start_distance < cumulative[index] < end_distance:
                    coords.append(QgsPointXY(points[index]))
            coords.append(end_point)
            if len(coords) >= 2:
                output.append(QgsGeometry.fromPolylineXY(coords))
        return output

    @staticmethod
    def _duplicate_key(points):
        values = [(round(point.x(), 9), round(point.y(), 9)) for point in points]
        reverse = list(reversed(values))
        return tuple(values if tuple(values) <= tuple(reverse) else reverse)

    def _remove_duplicates(self, features):
        seen = set()
        unique = []
        duplicate_ids = []
        for feature in features:
            geometry = feature.geometry()
            keys = []
            if geometry.isMultipart():
                for part in geometry.asMultiPolyline():
                    keys.append(self._duplicate_key([QgsPointXY(p) for p in part]))
            else:
                keys.append(self._duplicate_key([QgsPointXY(p) for p in geometry.asPolyline()]))
            key = tuple(keys)
            if key in seen:
                duplicate_ids.append(feature.id())
            else:
                seen.add(key)
                unique.append(feature)
        return unique, duplicate_ids

    def _name_field(self):
        configured = (
            self.payload.get("field_registry", {}).get("New Pole", {}) or {}
        ).get("名称")
        if configured and self.new_pole_layer and configured in self.new_pole_layer.fields().names():
            return configured
        return "Name" if self.new_pole_layer and "Name" in self.new_pole_layer.fields().names() else None

    def _add_new_pole(self, point):
        if self.new_pole_layer is None:
            raise RuntimeError("当前 ODN Project 未绑定 New Pole 图层。")
        feature = QgsFeature(self.new_pole_layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
        name_field = self._name_field()
        if name_field:
            feature[name_field] = ""
        if self.new_pole_layer.addFeature(feature):
            self._created_ids.append(feature.id())
            self._stats["new"] += 1

    def run(self):
        layer = self.line_layer
        if layer is None:
            self._msg("项目配置中的 Pole Edge 不存在。", 2)
            return
        self._prepare_poles()
        features = list(layer.getFeatures())
        unique, duplicate_ids = self._remove_duplicates(features)
        if duplicate_ids:
            layer.startEditing()
            for feature_id in duplicate_ids:
                layer.deleteFeature(feature_id)
            layer.commitChanges()
            self._stats["duplicates"] = len(duplicate_ids)

        replacements = []
        for feature in unique:
            geometry = feature.geometry()
            if geometry.isEmpty():
                continue
            parts = self._parts(geometry)
            changed = False
            new_geometries = []
            for points in parts:
                if len(points) < 2:
                    continue
                self._last_pts = [QgsPointXY(point) for point in points]
                _, total = self._cum_lengths(points)
                candidates = self._query_poles(geometry)
                existing = self._choose_existing(points, candidates)
                if not self._has_overlength(existing, total):
                    new_geometries.append(QgsGeometry.fromPolylineXY(self._last_pts))
                    continue

                self._stats["over"] += 1
                changed = True
                dynamic = self._make_new_cuts(existing, total)
                while self._has_overlength(dynamic, total):
                    before = len(dynamic)
                    dynamic = self._make_new_cuts(dynamic, total)
                    if len(dynamic) == before:
                        break
                new_geometries.extend(self._subline(points, dynamic))
                self._stats["segments"] += len(new_geometries)

            if changed:
                replacements.append((feature.id(), feature, new_geometries))

        # Roll back project edits if writing the configured New Pole layer fails.
        try:
            if self.new_pole_layer.editBuffer() is None:
                self.new_pole_layer.startEditing()
            layer.startEditing()
            for feature_id, feature, geometries in replacements:
                layer.deleteFeature(feature_id)
                for geometry in geometries:
                    new_feature = QgsFeature(layer.fields())
                    new_feature.setGeometry(geometry)
                    new_feature.setAttributes(feature.attributes())
                    layer.addFeature(new_feature)
            if not layer.commitChanges():
                raise RuntimeError("Pole Edge 修改无法提交。")
            if not self.new_pole_layer.commitChanges():
                raise RuntimeError("New Pole 修改无法提交。")
        except Exception:
            if self.new_pole_layer.isEditable():
                self.new_pole_layer.rollBack()
            if layer.isEditable():
                layer.rollBack()
            raise

        if self._created_ids:
            self.new_pole_layer.selectByIds(self._created_ids)
        self.iface.mapCanvas().refresh()
        self._msg(
            f"超距增点完成：检查超距线 {self._stats['over']} 条，"
            f"新增 New Pole {self._stats['new']} 个，"
            f"删除重复线 {self._stats['duplicates']} 条。"
        )

    def _msg(self, text, level=0):
        try:
            self.iface.messageBar().pushMessage("ODN Tools Pro", text, level=level, duration=5)
        except Exception:
            pass
