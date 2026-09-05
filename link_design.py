# -*- coding: utf-8 -*-
"""Interactive ODN Link Design.

All operational layers and design limits come from the active ODN Project.
This module contains only the user interaction; physical routing is provided
by :mod:`odn_project_routing`.
"""
from math import sqrt
import json

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsProject,
    QgsRectangle, QgsSpatialIndex, QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from . import odn_project_context as context
from .odn_project_routing import OdnProjectRouteEngine

SETTINGS_KEY = "ODNToolsPro/LinkDesign"


def _payload(dialog):
    return context.current_payload() or getattr(dialog, "_odn_project_payload", None) or {}


def _project_layers(payload):
    return {
        role: context.project_layer(payload, role)
        for role in ("FDT", "FAT", "Existing Pole", "New Pole", "Pole Edge")
    }


def _required_int(dialog, key):
    value = (_payload(dialog).get("parameters", {}) or {}).get(key)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def _max_links(dialog):
    return _required_int(dialog, "fdt_max_links")


def _max_fats(dialog):
    return _required_int(dialog, "max_fats_per_link")


def _assigned_fat_ids(dialog):
    result = set()
    for design in dialog._designs:
        for item in design.get("nodes", []):
            try:
                result.add(int(item[0]))
            except (TypeError, ValueError, IndexError):
                continue
    return result


def _planned_fats(dialog):
    ids = _assigned_fat_ids(dialog)
    for item in dialog._sequence:
        if item[0] == "FAT":
            try:
                ids.add(int(item[1]))
            except (TypeError, ValueError, IndexError):
                pass
    return len(ids)


def _total_fats(dialog):
    layer = context.project_layer(_payload(dialog), "FAT")
    return int(layer.featureCount()) if layer is not None else 0


def _next_link(dialog, fdt_label):
    limit = _max_links(dialog)
    if limit is None:
        return None
    used = {d.get("link") for d in dialog._designs if d.get("fdt") == fdt_label}
    for index in range(1, limit + 1):
        candidate = f"L{index}"
        if candidate not in used:
            return candidate
    return None


def _points_are_expected(layer, geometry_type):
    return (
        layer is not None
        and layer.type() == layer.VectorLayer
        and QgsWkbTypes.geometryType(layer.wkbType()) == geometry_type
    )


class LinkDesignDialog(QtWidgets.QDialog):
    """Compact Link Design UI driven entirely by the active ODN Project."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("链路设计")
        self.resize(440, 300)
        self.setModal(False)
        self._designs = []
        self._sequence = []
        self._direction = None
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        self._draw_active = False
        self._engine = None
        self._tool = None
        self._build_ui()
        self._load_saved_designs()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(5)

        title = QtWidgets.QLabel("链路设计")
        font = title.font(); font.setBold(True); font.setPointSize(13); title.setFont(font)
        root.addWidget(title)

        self.plan_label = QtWidgets.QLabel("① 规划中（显示实时距离）：等待开始")
        self.fdt_label = QtWidgets.QLabel("② 规划中 FDT：—")
        self.link_count_label = QtWidgets.QLabel("已规划链路：0")
        self.planned_fat_label = QtWidgets.QLabel("已规划 FAT：0")
        self.total_fat_label = QtWidgets.QLabel("③ 总 FAT：0，已设计 FAT：0")
        for widget in (self.plan_label, self.fdt_label, self.link_count_label,
                       self.planned_fat_label, self.total_fat_label):
            root.addWidget(widget)

        line = QtWidgets.QFrame(); line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken); root.addWidget(line)

        self.current_path_label = QtWidgets.QLabel(
            "请点击“开始规划”，然后在地图上选择 FDT 或 FAT。"
        )
        self.current_path_label.setWordWrap(True)
        self.segment_label = QtWidgets.QLabel("当前段：—")
        self.route_label = QtWidgets.QLabel("Pole Edge：—")
        self.route_label.setWordWrap(True)
        root.addWidget(self.current_path_label)
        root.addWidget(self.segment_label)
        root.addWidget(self.route_label)

        row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("开始规划")
        self.done_btn = QtWidgets.QPushButton("已完成设计")
        self.exit_btn = QtWidgets.QPushButton("退出设计")
        row.addWidget(self.start_btn); row.addWidget(self.done_btn); row.addWidget(self.exit_btn)
        root.addLayout(row)

        self.status = QtWidgets.QLabel("状态：等待开始规划")
        self.status.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        root.addWidget(self.status)

        self.start_btn.clicked.connect(self.start_design)
        self.done_btn.clicked.connect(self.finish_design)
        self.exit_btn.clicked.connect(self.exit_design)
        self.done_btn.setEnabled(False)
        self._refresh_ui()

    def _load_saved_designs(self):
        ok, value = QgsProject.instance().readEntry(SETTINGS_KEY, "designs", "")
        if ok and value:
            try:
                self._designs = json.loads(value)
            except Exception:
                self._designs = []
        self._refresh_ui()

    def _persist_designs(self):
        QgsProject.instance().writeEntry(
            SETTINGS_KEY, "designs", json.dumps(self._designs, ensure_ascii=False)
        )

    def _prepare_engine(self):
        payload = _payload(self)
        layers = _project_layers(payload)
        missing = [role for role in ("FDT", "FAT", "Pole Edge") if layers[role] is None]
        if missing:
            QtWidgets.QMessageBox.warning(
                self, "链路设计",
                "当前项目缺少必要图层绑定：\n\n"
                + "\n".join(f"• {role}" for role in missing)
                + "\n\n请先在【项目配置】中修正。"
            )
            return None

        if QgsWkbTypes.geometryType(layers["FDT"].wkbType()) != QgsWkbTypes.PointGeometry:
            QtWidgets.QMessageBox.warning(self, "链路设计", "项目配置中的 FDT 不是点图层。")
            return None
        if QgsWkbTypes.geometryType(layers["FAT"].wkbType()) != QgsWkbTypes.PointGeometry:
            QtWidgets.QMessageBox.warning(self, "链路设计", "项目配置中的 FAT 不是点图层。")
            return None
        if QgsWkbTypes.geometryType(layers["Pole Edge"].wkbType()) != QgsWkbTypes.LineGeometry:
            QtWidgets.QMessageBox.warning(self, "链路设计", "项目配置中的 Pole Edge 不是线图层。")
            return None

        if _max_links(self) is None or _max_fats(self) is None:
            QtWidgets.QMessageBox.warning(
                self, "链路设计",
                "当前 ODN Project 缺少有效的“FDT 最大 Link 数”或“每条 Link 最大 FAT”参数。\n\n"
                "请先在【项目配置 → 设计设置】中完成配置。"
            )
            return None

        params = payload.get("parameters", {}) or {}
        try:
            attach = float(params["fat_pole_max_distance"])
        except (KeyError, TypeError, ValueError):
            QtWidgets.QMessageBox.warning(
                self, "链路设计",
                "当前 ODN Project 缺少有效的 FAT 挂杆最大距离参数，请先在【项目配置】中配置。"
            )
            return None

        engine = OdnProjectRouteEngine(self.iface, payload, attach)
        if not engine.ready():
            QtWidgets.QMessageBox.warning(self, "链路设计", "当前项目的 FDT、FAT 或 Pole Edge 无法使用。")
            return None
        return engine

    def start_design(self):
        if self._draw_active:
            self.status.setText("状态：正在规划中，请继续点击地图上的 FDT/FAT。")
            return
        self._engine = self._prepare_engine()
        if self._engine is None:
            return
        self._sequence = []
        self._direction = None
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        self._tool = LinkDesignMapTool(self.iface, self._engine, self)
        self.iface.mapCanvas().setMapTool(self._tool)
        self._draw_active = True
        self.start_btn.setEnabled(False)
        self.done_btn.setEnabled(True)
        self.status.setText("状态：规划中——请选择 FDT 或 FAT 作为起点。")
        self._refresh_ui()
        self._tool.start()

    def _start_from_hit(self, info):
        if info["typ"] == "FDT":
            link = _next_link(self, info["label"])
            if link is None:
                self.status.setText(
                    f"状态：{info['label']} 已达到项目配置的最大 Link 数：{_max_links(self) or '未配置'}。"
                )
                return False
            self._direction = "FDT_TO_FAT"
            self._current_fdt = info["label"]
            self._current_fdt_id = info["feature_id"]
            self._current_link = link
            self._sequence = [("FDT", info["feature_id"], info["label"])]
            self.status.setText(f"状态：{info['label']}/{link}，请继续点击 FAT。")
            return True

        if info["typ"] == "FAT":
            if info["feature_id"] in _assigned_fat_ids(self):
                self.status.setText(f"状态：{info['label']} 已分配到其他 Link，不能重复使用。")
                return False
            self._direction = "FAT_TO_FDT"
            self._sequence = [("FAT", info["feature_id"], info["label"])]
            self.status.setText(
                "状态：已从 FAT 开始，请继续点击 FAT，最后点击 FDT 完成反向链路。"
            )
            return True
        return False

    def add_fat(self, info):
        if not self._draw_active or info["typ"] != "FAT":
            return
        if self._direction is None:
            self._start_from_hit(info)
            self._refresh_ui()
            return
        if any(item[0] == "FAT" and item[1] == info["feature_id"] for item in self._sequence):
            self.status.setText(f"状态：{info['label']} 已在当前链路中。")
            return
        if info["feature_id"] in _assigned_fat_ids(self):
            self.status.setText(f"状态：{info['label']} 已分配到其他 Link，不能重复使用。")
            return
        max_fats = _max_fats(self)
        current_fats = sum(1 for item in self._sequence if item[0] == "FAT")
        if max_fats is None:
            self.status.setText("状态：当前项目未配置“每条 Link 最大 FAT”。")
            return
        if current_fats >= max_fats:
            self.status.setText(
                f"状态：当前 Link 已达到项目配置的 FAT 上限：{max_fats}。"
            )
            return
        previous = self._sequence[-1]
        route = self._engine.route(previous[0], previous[1], "FAT", info["feature_id"])
        if route is None:
            self.status.setText(
                f"状态：{previous[2]} → {info['label']} 无法沿 Pole Edge 建立完整路径。"
            )
            return
        self._sequence.append(("FAT", info["feature_id"], info["label"]))
        self._refresh_ui()
        self._tool.refresh_route_preview()

    def add_fdt(self, info):
        if not self._draw_active or info["typ"] != "FDT":
            return
        if self._direction != "FAT_TO_FDT" or not self._sequence:
            return
        link = _next_link(self, info["label"])
        if link is None:
            self.status.setText(
                f"状态：{info['label']} 已达到项目配置的最大 Link 数：{_max_links(self) or '未配置'}。"
            )
            return
        previous = self._sequence[-1]
        route = self._engine.route(previous[0], previous[1], "FDT", info["feature_id"])
        if route is None:
            self.status.setText(
                f"状态：{previous[2]} → {info['label']} 无法沿 Pole Edge 建立完整路径。"
            )
            return
        self._current_fdt = info["label"]
        self._current_fdt_id = info["feature_id"]
        self._current_link = link
        self._sequence.append(("FDT", info["feature_id"], info["label"]))
        self.finish_current_link()

    def finish_current_link(self):
        if not self._current_fdt:
            self.status.setText("状态：当前链路尚未闭合到 FDT。")
            return False
        fats = [item for item in self._sequence if item[0] == "FAT"]
        if not fats:
            self.status.setText("状态：当前链路至少需要 1 个 FAT。")
            return False

        max_fats = _max_fats(self)
        if max_fats is None:
            self.status.setText("状态：当前项目未配置“每条 Link 最大 FAT”。")
            return False
        if len(fats) > max_fats:
            self.status.setText(f"状态：当前 Link FAT 数 {len(fats)} 超过项目配置上限 {max_fats}。")
            return False

        routes = []
        for first, second in zip(self._sequence[:-1], self._sequence[1:]):
            route = self._engine.route(first[0], first[1], second[0], second[1])
            if route is None:
                self.status.setText(
                    f"状态：{first[2]} → {second[2]} 无法沿 Pole Edge 建立完整路径。"
                )
                return False
            routes.append(route)

        total = sum(item["distance"] for item in routes)
        design = {
            "fdt": self._current_fdt,
            "link": self._current_link,
            "nodes": [(item[1], item[2]) for item in fats],
            "length": round(total, 3),
            "sequence": [item[2] for item in self._sequence],
            "sequence_ids": [(item[0], item[1]) for item in self._sequence],
            "direction": self._direction,
            "segments": [
                {"from": r["from_label"], "to": r["to_label"],
                 "distance": round(r["distance"], 3),
                 "edge_count": len(r["edge_sequence"])}
                for r in routes
            ],
        }
        if not any(d.get("fdt") == design["fdt"] and d.get("link") == design["link"]
                   for d in self._designs):
            self._designs.append(design)
        self._persist_designs()

        finished_fdt, finished_link = self._current_fdt, self._current_link
        self._sequence = []
        self._direction = None
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        if self._tool:
            self._tool.clear_preview_only()
        self.status.setText(
            f"状态：已完成 {finished_fdt}/{finished_link}（{total:.1f} m），可以继续规划下一条链路。"
        )
        self._refresh_ui()
        return True

    def finish_design(self):
        if self._sequence and not self.finish_current_link():
            return
        self._persist_designs()
        self._stop_tool()
        self.status.setText(f"状态：设计已完成并保存，共 {len(self._designs)} 条链路。")
        self._refresh_ui()

    def exit_design(self):
        self._stop_tool()
        self.close()

    def _stop_tool(self):
        self._draw_active = False
        self._sequence = []
        self._direction = None
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        self.start_btn.setEnabled(True)
        self.done_btn.setEnabled(False)
        if self._tool:
            try:
                self._tool.clear_preview_only()
            except Exception:
                pass
            try:
                self.iface.mapCanvas().unsetMapTool(self._tool)
            except Exception:
                pass
            self._tool = None

    def undo_last(self):
        if not self._sequence:
            return
        removed = self._sequence.pop()
        if removed[0] == "FAT":
            self._refresh_ui()
        if not self._sequence:
            self._direction = None
            self._current_fdt = None
            self._current_fdt_id = None
            self._current_link = None
        elif self._direction == "FAT_TO_FDT" and self._sequence[-1][0] == "FAT":
            self._current_fdt = None
            self._current_fdt_id = None
            self._current_link = None
        self._refresh_ui()
        if self._tool:
            self._tool.refresh_route_preview()

    def prospective_route(self, info):
        if not self._sequence or not self._engine:
            return None
        if info["typ"] == "FDT" and self._direction != "FAT_TO_FDT":
            return None
        if info["typ"] == "FAT":
            if info["feature_id"] in _assigned_fat_ids(self):
                return None
            max_fats = _max_fats(self)
            current_fats = sum(1 for item in self._sequence if item[0] == "FAT")
            if max_fats is None or current_fats >= max_fats:
                return None
        previous = self._sequence[-1]
        return self._engine.route(previous[0], previous[1], info["typ"], info["feature_id"])

    def _refresh_ui(self, hover_info=None):
        self.link_count_label.setText(f"已规划链路：{len(self._designs)}")
        self.planned_fat_label.setText(f"已规划 FAT：{_planned_fats(self)}")
        self.total_fat_label.setText(
            f"③ 总 FAT：{_total_fats(self)}，已设计 FAT：{_planned_fats(self)}"
        )

        if not self._sequence:
            self.plan_label.setText("① 规划中（显示实时距离）：等待开始")
            self.fdt_label.setText("② 规划中 FDT：—")
            self.current_path_label.setText("请点击“开始规划”，然后在地图上选择 FDT 或 FAT。")
            self.segment_label.setText("当前段：—")
            self.route_label.setText("Pole Edge：—")
            return

        labels = [item[2] for item in self._sequence]
        committed = []
        for first, second in zip(self._sequence[:-1], self._sequence[1:]):
            route = self._engine.route(first[0], first[1], second[0], second[1])
            if route:
                committed.append(route)
        total = sum(route["distance"] for route in committed)
        if hover_info:
            total += hover_info["distance"]

        self.plan_label.setText(f"① 规划中（显示实时距离）：{total:.1f} m")
        self.fdt_label.setText(
            f"② 规划中 FDT：{self._current_fdt or '等待 FDT'}"
            f"  |  Link：{self._current_link or '待分配'}"
        )
        self.current_path_label.setText("当前路径：" + " → ".join(labels))
        last = hover_info or (committed[-1] if committed else None)
        if last:
            self.segment_label.setText(
                f"当前段：{last['from_label']} → {last['to_label']}    {last['distance']:.1f} m"
            )
            self.route_label.setText(
                f"Pole Edge 实际路径：{last['pole_edge_distance']:.1f} m"
                f"  |  边段：{len(last['edge_sequence'])}"
                f"  |  Link 累计：{total:.1f} m"
            )
        else:
            self.segment_label.setText("当前段：—")
            self.route_label.setText("Pole Edge：—")


class LinkDesignMapTool(QgsMapTool):
    """Canvas interaction for project-configured FDT/FAT Link Design."""

    def __init__(self, iface, engine, dialog):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.engine = engine
        self.dialog = dialog
        self.canvas = iface.mapCanvas()
        self.setCursor(Qt.CrossCursor)
        self._features = []
        self._index = QgsSpatialIndex()
        self._hover_band = None
        for key, info in engine.points.items():
            feature = QgsFeature(); feature.setId(key[1])
            feature.setGeometry(QgsGeometry.fromPointXY(info["point"]))
            self._index.addFeature(feature)
            self._features.append((key, info))

    def start(self):
        self.refresh_route_preview()

    def _nearest(self, pos):
        point = self.toMapCoordinates(pos)
        tolerance = self.canvas.mapUnitsPerPixel() * 24
        rect = QgsRectangle(point.x() - tolerance, point.y() - tolerance,
                            point.x() + tolerance, point.y() + tolerance)
        candidate_ids = self._index.intersects(rect)
        best = None
        for key, info in self._features:
            if key[1] not in candidate_ids:
                continue
            p = info["point"]
            distance = sqrt((p.x() - point.x()) ** 2 + (p.y() - point.y()) ** 2)
            if best is None or distance < best[0]:
                best = (distance, key, info)
        return best

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.dialog._draw_active:
            return
        hit = self._nearest(event.pos())
        if not hit:
            return
        _, _, info = hit
        if not self.dialog._sequence:
            self.dialog._start_from_hit(info)
        elif info["typ"] == "FAT":
            self.dialog.add_fat(info)
        elif info["typ"] == "FDT":
            self.dialog.add_fdt(info)
        self.refresh_route_preview()

    def canvasMoveEvent(self, event):
        if not self.dialog._draw_active or not self.dialog._sequence:
            self._clear_hover()
            return
        hit = self._nearest(event.pos())
        if not hit:
            self._clear_hover()
            return
        _, _, info = hit
        route = self.dialog.prospective_route(info)
        self.dialog._refresh_ui(route)
        self._draw_hover(route)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Backspace:
            self.dialog.undo_last(); return
        if event.key() == Qt.Key_Escape:
            self.dialog._stop_tool(); return
        super().keyPressEvent(event)

    def _to_canvas_points(self, route):
        points = route["points"]
        src = self.engine.edge_layer.crs()
        dst = self.canvas.mapSettings().destinationCrs()
        if src == dst:
            return points
        transform = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
        return [transform.transform(p) for p in points]

    def refresh_route_preview(self):
        self._clear_hover()
        if len(self.dialog._sequence) < 2:
            return
        points = []
        for first, second in zip(self.dialog._sequence[:-1], self.dialog._sequence[1:]):
            route = self.engine.route(first[0], first[1], second[0], second[1])
            if route:
                route_points = self._to_canvas_points(route)
                points.extend(route_points if not points else route_points[1:])
        if len(points) >= 2:
            band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            band.setWidth(4)
            band.setColor(QtWidgets.QApplication.palette().highlight().color())
            band.setToGeometry(QgsGeometry.fromPolylineXY(points), self.canvas.mapSettings().destinationCrs())
            self._hover_band = band

    def _draw_hover(self, route):
        self._clear_hover()
        if not route:
            return
        points = self._to_canvas_points(route)
        if len(points) < 2:
            return
        band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        band.setWidth(3)
        band.setColor(QtWidgets.QApplication.palette().highlight().color())
        band.setToGeometry(QgsGeometry.fromPolylineXY(points), self.canvas.mapSettings().destinationCrs())
        self._hover_band = band

    def _clear_hover(self):
        if self._hover_band is not None:
            try:
                self.canvas.scene().removeItem(self._hover_band)
            except Exception:
                pass
            self._hover_band = None

    def clear_preview_only(self):
        self._clear_hover()
