# -*- coding: utf-8 -*-
"""Project-driven overlength pole insertion tool."""

from qgis.PyQt import QtWidgets
from qgis.core import (
    QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsGeometry,
    QgsMapLayerType, QgsPointXY, QgsProject, QgsSpatialIndex, QgsWkbTypes,
)

from . import odn_project_context as context


class OverlengthPoleDialog(QtWidgets.QDialog):
    """Add New Pole features so every Pole Edge span meets project rules."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("超距增点")
        self.setMinimumWidth(520)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setSpacing(9)

        title = QtWidgets.QLabel("超距增点")
        font = title.font()
        font.setBold(True)
        font.setPointSize(12)
        title.setFont(font)
        lay.addWidget(title)

        lay.addWidget(QtWidgets.QLabel("杆子图层（项目配置）"))
        self.pole_label = QtWidgets.QLabel("—")
        self.pole_label.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        lay.addWidget(self.pole_label)

        lay.addWidget(QtWidgets.QLabel("连线图层（项目配置）"))
        self.line_label = QtWidgets.QLabel("—")
        self.line_label.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        lay.addWidget(self.line_label)

        lay.addWidget(QtWidgets.QLabel("最大允许距离（米）"))
        self.rule_label = QtWidgets.QLabel("—")
        self.rule_label.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        self.rule_label.setStyleSheet("padding:6px;")
        lay.addWidget(self.rule_label)

        tip = QtWidgets.QLabel(
            "规则来自当前 ODN Project。程序按 Pole Edge 两端杆类型选择对应上限；"
            "超距时先计算最少需要的新建 New Pole 数量，再在允许范围内均衡分配各段距离。"
            "新增杆位全部写入项目配置的 New Pole 图层，本功能不提供距离参数修改。"
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
        self._distance_rules = None
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
        rules = {
            "existing_existing": params.get("existing_existing_max_distance"),
            "existing_new": params.get("existing_new_max_distance"),
            "new_new": params.get("new_new_max_distance"),
        }
        self._distance_rules = rules

        names = []
        if existing is not None:
            names.append(f"Existing Pole：{existing.name()}")
        if new is not None:
            names.append(f"New Pole：{new.name()}")
        self.pole_label.setText("\n".join(names) if names else "未绑定")
        self.line_label.setText(edge.name() if edge is not None else "未绑定")

        def fmt(value):
            try:
                return f"{float(value):g} m"
            except (TypeError, ValueError):
                return "未配置"

        self.rule_label.setText(
            "Existing Pole - Existing Pole：" + fmt(rules["existing_existing"]) + "\n"
            "Existing Pole - New Pole：" + fmt(rules["existing_new"]) + "\n"
            "New Pole - New Pole：" + fmt(rules["new_new"])
        )

        valid_rules = True
        for value in rules.values():
            try:
                if float(value) <= 0:
                    valid_rules = False
            except (TypeError, ValueError):
                valid_rules = False

        self._ok_button.setEnabled(
            new is not None
            and new.type() == QgsMapLayerType.VectorLayer
            and QgsWkbTypes.geometryType(new.wkbType()) == QgsWkbTypes.PointGeometry
            and edge is not None
            and edge.type() == QgsMapLayerType.VectorLayer
            and QgsWkbTypes.geometryType(edge.wkbType()) == QgsWkbTypes.LineGeometry
            and valid_rules
        )

    def _start(self):
        payload = context.require_project(self, "超距增点")
        if not payload:
            return
        existing = context.project_layer(payload, "Existing Pole")
        new = context.project_layer(payload, "New Pole")
        edge = context.project_layer(payload, "Pole Edge")
        params = payload.get("parameters", {}) or {}
        try:
            distance_rules = {
                "existing_existing": float(params["existing_existing_max_distance"]),
                "existing_new": float(params["existing_new_max_distance"]),
                "new_new": float(params["new_new_max_distance"]),
            }
            if any(value <= 0 for value in distance_rules.values()):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            QtWidgets.QMessageBox.warning(self, "超距增点", "当前 ODN Project 未配置完整且有效的最大允许距离规则。")
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
        OverlengthPoleProcessor(self.iface, payload, [("Existing Pole", existing), ("New Pole", new)], edge, distance_rules).run()


class OverlengthPoleProcessor:
    SEARCH_METERS = 3.0

    def __init__(self, iface, payload, pole_layers, line_layer, distance_rules):
        self.iface = iface
        self.project = QgsProject.instance()
        self.payload = payload or {}
        self.pole_layers = [(role, layer) for role, layer in pole_layers if layer is not None]
        self.new_pole_layer = next((layer for role, layer in self.pole_layers if role == "New Pole"), None)
        self.line_layer = line_layer
        self.distance_rules = {key: float(value) for key, value in distance_rules.items()}
        self.da = QgsDistanceArea()
        self.da.setEllipsoid(self.project.ellipsoid())
        self._index = QgsSpatialIndex()
        self._idx_map = {}
        self._next_index_id = 1
        self._created_ids = []
        self._stats = {"duplicates": 0, "over": 0, "new": 0, "segments": 0}

    def _prepare_poles(self):
        dst = self.line_layer.crs()
        self._index = QgsSpatialIndex()
        self._idx_map = {}
        self._next_index_id = 1
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
                    self._add_index_point(point, role, layer.id(), int(feature.id()))
                except Exception:
                    continue

    def _add_index_point(self, point, role, layer_id=None, feature_id=None):
        index_id = self._next_index_id
        self._next_index_id += 1
        spatial_feature = QgsFeature()
        spatial_feature.setId(index_id)
        spatial_feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
        self._index.addFeature(spatial_feature)
        self._idx_map[index_id] = (layer_id, feature_id, QgsPointXY(point), role)

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

    def _endpoint_candidate(self, endpoint, candidates):
        best = None
        best_distance = float("inf")
        target = QgsGeometry.fromPointXY(QgsPointXY(endpoint))
        for item in candidates:
            _, data, point = item
            try:
                distance = target.distance(QgsGeometry.fromPointXY(QgsPointXY(point)))
            except Exception:
                continue
            if distance < best_distance:
                best_distance = distance
                best = item
        return best

    def _endpoint_role(self, endpoint, candidates):
        item = self._endpoint_candidate(endpoint, candidates)
        return item[1][3] if item else "Existing Pole"

    def _pair_limit(self, left_role, right_role):
        if left_role == "New Pole" and right_role == "New Pole":
            return self.distance_rules["new_new"]
        if left_role == "New Pole" or right_role == "New Pole":
            return self.distance_rules["existing_new"]
        return self.distance_rules["existing_existing"]

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
        fraction = max(0.0, min(1.0, self.da.measureLine(a, projected) / segment_length)) if segment_length > 1e-12 else 0.0
        return cumulative[segment] + segment_length * fraction, projected, total

    def _collect_poles_on_part(self, points):
        part_geom = QgsGeometry.fromPolylineXY([QgsPointXY(point) for point in points])
        candidates = self._query_poles(part_geom)
        located = []
        for _, data, point in candidates:
            location = self._project_location(points, point)
            if not location:
                continue
            located.append((location[0], QgsPointXY(point), data[3]))
        located.sort(key=lambda item: item[0])
        unique = []
        for item in located:
            if unique and abs(item[0] - unique[-1][0]) < 0.01:
                if unique[-1][2] != "New Pole" and item[2] == "New Pole":
                    unique[-1] = item
            else:
                unique.append(item)
        return unique, candidates

    @staticmethod
    def _balanced_lengths(total, caps):
        if not caps:
            return []
        remaining = float(total)
        active = list(range(len(caps)))
        lengths = [0.0] * len(caps)
        eps = 1e-9
        while active:
            average = remaining / len(active)
            limited = [index for index in active if caps[index] < average - eps]
            if not limited:
                for index in active:
                    lengths[index] = average
                break
            for index in limited:
                lengths[index] = caps[index]
                remaining -= caps[index]
                active.remove(index)
        return lengths

    def _plan_gap(self, total, left_role, right_role):
        direct_cap = self._pair_limit(left_role, right_role)
        if total <= direct_cap + 1e-8:
            return []

        segment_count = 2
        while True:
            caps = [self._pair_limit(left_role, "New Pole")]
            if segment_count > 2:
                caps.extend([self.distance_rules["new_new"] for _ in range(segment_count - 2)])
            caps.append(self._pair_limit("New Pole", right_role))
            if total <= sum(caps) + 1e-8:
                break
            segment_count += 1
            if segment_count > 10000:
                raise RuntimeError("超距增点无法在当前项目规则下形成可行分段。")

        lengths = self._balanced_lengths(total, caps)
        return [(length, index < segment_count - 1) for index, length in enumerate(lengths)]

    def _add_new_pole(self, point):
        if self.new_pole_layer is None:
            raise RuntimeError("当前 ODN Project 未绑定 New Pole 图层。")
        feature = QgsFeature(self.new_pole_layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
        name_field = (self.payload.get("field_registry", {}).get("New Pole", {}) or {}).get("名称")
        if name_field and name_field in self.new_pole_layer.fields().names():
            feature[name_field] = ""
        if not self.new_pole_layer.addFeature(feature):
            raise RuntimeError("新增 New Pole 要素失败。")
        self._created_ids.append(feature.id())
        self._stats["new"] += 1
        point = QgsPointXY(point)
        self._add_index_point(point, "New Pole", self.new_pole_layer.id(), int(feature.id()))

    def _split_gap(self, points, start_distance, end_distance, left_role, right_role):
        total = end_distance - start_distance
        plan = self._plan_gap(total, left_role, right_role)
        if not plan:
            return [], []

        new_cuts = []
        cursor = start_distance
        for length, needs_new_pole in plan:
            cursor += length
            if needs_new_pole:
                point = self._point_at_distance(points, cursor)
                self._add_new_pole(point)
                new_cuts.append((cursor, point, "New Pole"))
        return new_cuts, plan

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
                return QgsPointXY(a.x() + (b.x() - a.x()) * fraction, a.y() + (b.y() - a.y()) * fraction)
        return QgsPointXY(points[-1])

    def _subline(self, points, cut_specs):
        cumulative, total = self._cum_lengths(points)
        cuts = [(0.0, QgsPointXY(points[0]))]
        cuts.extend((distance, QgsPointXY(point)) for distance, point, *_ in sorted(cut_specs, key=lambda item: item[0]))
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

    def run(self):
        layer = self.line_layer
        if layer is None:
            self._msg("项目配置中的 Pole Edge 不存在。", 2)
            return
        new_started = False
        edge_started = False
        try:
            self._prepare_poles()
            if self.new_pole_layer.editBuffer() is None:
                if not self.new_pole_layer.startEditing():
                    raise RuntimeError("New Pole 图层无法进入编辑状态。")
                new_started = True
            if layer.editBuffer() is None:
                if not layer.startEditing():
                    raise RuntimeError("Pole Edge 图层无法进入编辑状态。")
                edge_started = True

            features = list(layer.getFeatures())
            unique, duplicate_ids = self._remove_duplicates(features)
            for feature_id in duplicate_ids:
                layer.deleteFeature(feature_id)
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
                    points = [QgsPointXY(point) for point in points]
                    located, _ = self._collect_poles_on_part(points)
                    if len(located) < 2:
                        new_geometries.append(QgsGeometry.fromPolylineXY(points))
                        continue

                    all_cuts = []
                    for left, right in zip(located[:-1], located[1:]):
                        left_distance, left_point, left_role = left
                        right_distance, right_point, right_role = right
                        gap = right_distance - left_distance
                        self.max_gap_for_debug = self._pair_limit(left_role, right_role)
                        if gap <= self._pair_limit(left_role, right_role) + 1e-8:
                            continue
                        self._stats["over"] += 1
                        changed = True
                        new_cuts, _ = self._split_gap(points, left_distance, right_distance, left_role, right_role)
                        all_cuts.extend(new_cuts)

                    if changed:
                        combined = [
                            (distance, point, role) for distance, point, role in located
                        ] + all_cuts
                        combined.sort(key=lambda item: item[0])
                        new_geometries.extend(self._subline(points, combined))
                        self._stats["segments"] += len(new_geometries)
                    else:
                        new_geometries.append(QgsGeometry.fromPolylineXY(points))

                if changed:
                    replacements.append((feature.id(), feature, new_geometries))

            for feature_id, feature, geometries in replacements:
                layer.deleteFeature(feature_id)
                for geometry in geometries:
                    new_feature = QgsFeature(layer.fields())
                    new_feature.setGeometry(geometry)
                    new_feature.setAttributes(feature.attributes())
                    layer.addFeature(new_feature)

            if edge_started and not layer.commitChanges():
                raise RuntimeError("Pole Edge 修改无法提交。")
            if new_started and not self.new_pole_layer.commitChanges():
                raise RuntimeError("New Pole 修改无法提交。")
        except Exception:
            if new_started and self.new_pole_layer.isEditable():
                self.new_pole_layer.rollBack()
            if edge_started and layer.isEditable():
                layer.rollBack()
            raise

        if self._created_ids:
            self.new_pole_layer.selectByIds(self._created_ids)
        self.iface.mapCanvas().refresh()
        self._msg(
            f"超距增点完成：检查超距线 {self._stats['over']} 段，"
            f"新增 New Pole {self._stats['new']} 个，"
            f"删除重复线 {self._stats['duplicates']} 条。"
        )

    def _msg(self, text, level=0):
        try:
            self.iface.messageBar().pushMessage("ODN Tools Pro", text, level=level, duration=5)
        except Exception:
            pass
