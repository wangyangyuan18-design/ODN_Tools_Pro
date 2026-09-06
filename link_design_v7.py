# -*- coding: utf-8 -*-
"""Link Design UI v7.

Keeps v4 as the authoritative planning/state layer while refining the UI and
canvas interaction:
- current FAT path and full LINK path are shown separately
- distances are refreshed after clicks, not calculated during mouse hover
- FDT/FAT objects at the same pole can be cycled with Tab before clicking
- completed FATs are visually separated from the current Link FATs
- duplicate cable geometry is rejected before writing
"""

from math import sqrt

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from .link_design_v4 import LinkDesignDialog as _V4LinkDesignDialog
from .link_design_v6 import CompletedDesignDialogV6
from . import odn_project_context as context
from .link_design_v2 import _fresh_payload


def _all_completed_fat_ids(dialog):
    result = set()
    for design in dialog._designs:
        for item in design.get("nodes", []):
            try:
                result.add(int(item[0]))
            except (TypeError, ValueError, IndexError):
                continue
    return result


def _current_fdt_designs(dialog):
    fdt = str(getattr(dialog, "_current_fdt", "") or "")
    if not fdt:
        return []
    return [(i, d) for i, d in enumerate(dialog._designs) if str(d.get("fdt", "")) == fdt]


def _current_link_fat_ids(dialog):
    result = set()
    for item in getattr(dialog, "_sequence", []):
        if item and item[0] == "FAT":
            try:
                result.add(int(item[1]))
            except (TypeError, ValueError, IndexError):
                pass
    return result


class LinkDesignDialog(_V4LinkDesignDialog):
    """v7 UI wrapper; v4 remains authoritative for data and CRUD."""

    def __init__(self, iface, parent=None):
        self._fat_completed_bands = []
        self._fat_current_bands = []
        super().__init__(iface, parent)
        self._refresh_fat_overlays()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(5)

        title = QtWidgets.QLabel("链路设计")
        font = title.font()
        font.setBold(True)
        font.setPointSize(13)
        title.setFont(font)
        root.addWidget(title)

        self.plan_label = QtWidgets.QLabel("规划中")
        self.plan_label.setStyleSheet("font-weight:600;")
        root.addWidget(self.plan_label)

        self.current_path_label = QtWidgets.QLabel("当前路径：—")
        self.current_path_label.setWordWrap(True)
        root.addWidget(self.current_path_label)

        self.link_path_label = QtWidgets.QLabel("LINK路径：—")
        self.link_path_label.setWordWrap(True)
        root.addWidget(self.link_path_label)

        self.distance_label = QtWidgets.QLabel("各段距离：—    总距离：—")
        self.distance_label.setWordWrap(True)
        root.addWidget(self.distance_label)

        self.fdt_label = QtWidgets.QLabel("规划中 FDT：— | Link：—")
        root.addWidget(self.fdt_label)
        self.link_count_label = QtWidgets.QLabel("当前 FDT 已规划链路：0")
        self.planned_fat_label = QtWidgets.QLabel("当前 FDT 已规划 FAT：0")
        root.addWidget(self.link_count_label)
        root.addWidget(self.planned_fat_label)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setFrameShadow(QtWidgets.QFrame.Sunken)
        root.addWidget(line)

        row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("开始规划")
        self.save_btn = QtWidgets.QPushButton("保存规划")
        self.done_btn = QtWidgets.QPushButton("已完成设计")
        self.exit_btn = QtWidgets.QPushButton("退出设计")
        for button in (self.start_btn, self.save_btn, self.done_btn, self.exit_btn):
            button.setMinimumHeight(28)
            row.addWidget(button)
        root.addLayout(row)

        self.status = QtWidgets.QLabel("状态：等待开始规划")
        self.status.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.start_btn.clicked.connect(self.start_design)
        self.save_btn.clicked.connect(self.save_current_link)
        self.done_btn.clicked.connect(self.open_completed_designs)
        self.exit_btn.clicked.connect(self.exit_design)
        self.save_btn.setEnabled(False)
        self.resize(540, 335)
        self.setMinimumWidth(500)

    def _prepare_engine(self):
        return super()._prepare_engine()

    def _activate_map_tool(self):
        if self._engine is None:
            self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self._clear_fat_overlays()
        self._tool = LinkDesignMapToolV7(self.iface, self._engine, self)
        self.iface.mapCanvas().setMapTool(self._tool)
        self._draw_active = True
        self.start_btn.setEnabled(False)
        self._tool.start()
        self._refresh_fat_overlays()
        return True

    def _clear_fat_overlays(self):
        canvas = self.iface.mapCanvas()
        for band in self._fat_completed_bands + self._fat_current_bands:
            try:
                canvas.scene().removeItem(band)
            except Exception:
                pass
        self._fat_completed_bands = []
        self._fat_current_bands = []

    def _fat_point_canvas(self, info):
        point = QgsPointXY(info["point"])
        src = info["layer"].crs()
        dst = self.iface.mapCanvas().mapSettings().destinationCrs()
        if src != dst:
            point = QgsCoordinateTransform(
                src, dst, QgsProject.instance().transformContext()
            ).transform(point)
        return point

    def _draw_fat_point(self, info, color, size):
        band = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.PointGeometry)
        band.setColor(color)
        band.setWidth(2)
        band.setIcon(QgsRubberBand.ICON_CIRCLE)
        band.setIconSize(size)
        band.setToGeometry(
            QgsGeometry.fromPointXY(self._fat_point_canvas(info)),
            self.iface.mapCanvas().mapSettings().destinationCrs(),
        )
        return band

    def _refresh_fat_overlays(self):
        if not hasattr(self, "_fat_completed_bands"):
            return
        self._clear_fat_overlays()
        if self._engine is None:
            return
        completed = _all_completed_fat_ids(self)
        current = _current_link_fat_ids(self)
        for _, info in self._engine.points_of_type("FAT"):
            fid = int(info["feature_id"])
            if fid in current:
                self._fat_current_bands.append(
                    self._draw_fat_point(info, QColor(255, 196, 0), 13)
                )
            elif fid in completed:
                self._fat_completed_bands.append(
                    self._draw_fat_point(info, QColor(150, 150, 150), 10)
                )

    def start_design(self):
        result = super().start_design()
        self._refresh_fat_overlays()
        return result

    def _start_from_hit(self, info):
        result = super()._start_from_hit(info)
        self._refresh_fat_overlays()
        return result

    def add_fat(self, info):
        result = super().add_fat(info)
        self._refresh_fat_overlays()
        return result

    def add_fdt(self, info):
        result = super().add_fdt(info)
        self._refresh_fat_overlays()
        return result

    def undo_last(self):
        result = super().undo_last()
        self._refresh_fat_overlays()
        return result

    def load_design_for_edit(self, index):
        result = super().load_design_for_edit(index)
        self._refresh_fat_overlays()
        return result

    def _stop_tool(self):
        super()._stop_tool()
        self._refresh_fat_overlays()

    def save_current_link(self):
        result = super().save_current_link()
        self._refresh_fat_overlays()
        return result

    def write_planned_links(self):
        """Prevent a second identical cable from being written accidentally."""
        layer = context.project_layer(_fresh_payload(self), "Distribution Cable")
        if layer is None:
            return super().write_planned_links()
        pending = [(i, d) for i, d in enumerate(self._designs) if not d.get("written")]
        for index, design in pending:
            try:
                expected = self._build_layer_features(layer, design)
            except Exception:
                continue
            for new_feature in expected:
                bbox = new_feature.geometry().boundingBox()
                request = QgsFeatureRequest().setFilterRect(bbox)
                for existing in layer.getFeatures(request):
                    try:
                        if existing.geometry().equals(new_feature.geometry()):
                            QtWidgets.QMessageBox.warning(
                                self,
                                "重复写入",
                                f"{design.get('fdt', '')}/{design.get('link', '')} 的线路已经存在于 Distribution Cable 图层，未重复写入。",
                            )
                            return False
                    except Exception:
                        continue
        return super().write_planned_links()

    def _refresh_ui(self):
        self.plan_label.setText(
            "规划中" + (" · 修改" if self._editing_index is not None else "")
        )
        self.fdt_label.setText(
            f"规划中 FDT：{self._current_fdt or '—'} | Link：{self._current_link or '—'}"
        )

        designs = _current_fdt_designs(self)
        fdt_fats = set()
        for _, design in designs:
            for item in design.get("nodes", []):
                try:
                    fdt_fats.add(int(item[0]))
                except (TypeError, ValueError, IndexError):
                    pass
        self.link_count_label.setText(f"当前 FDT 已规划链路：{len(designs)}")
        self.planned_fat_label.setText(f"当前 FDT 已规划 FAT：{len(fdt_fats)}")

        if not self._sequence:
            self.current_path_label.setText("当前路径：—")
            self.link_path_label.setText("LINK路径：—")
            self.distance_label.setText("各段距离：—    总距离：—")
            self.save_btn.setEnabled(False)
            return

        fat_labels = [item[2] for item in self._sequence if item[0] == "FAT"]
        all_labels = [item[2] for item in self._sequence]
        self.current_path_label.setText(
            "当前路径：" + (" → ".join(fat_labels) if fat_labels else "—")
        )
        self.link_path_label.setText(
            "LINK路径：" + (" → ".join(all_labels) if all_labels else "—")
        )

        routes = []
        if self._engine:
            for first, second in zip(self._sequence[:-1], self._sequence[1:]):
                route = self._engine.route(
                    first[0], first[1], second[0], second[1]
                )
                if route:
                    routes.append(route)
        distances = "、".join(f"{r['distance']:.1f}m" for r in routes) if routes else "—"
        total = sum(float(r["distance"]) for r in routes)
        total_text = f"{total:.1f}m" if routes else "—"
        self.distance_label.setText(f"各段距离：{distances}    总距离：{total_text}")
        self.save_btn.setEnabled(
            bool(self._current_fdt and any(x[0] == "FAT" for x in self._sequence))
        )

    def open_completed_designs(self):
        self._reconcile_written_state()
        self._repair_saved_distances()
        dialog = CompletedDesignDialogV6(self)
        dialog.exec_()
        self._refresh_ui()
        self._refresh_fat_overlays()

    def closeEvent(self, event):
        self._clear_fat_overlays()
        super().closeEvent(event)


class LinkDesignMapToolV7(QgsMapTool):
    """Canvas selector with fast hover and Tab cycling for same-pole objects."""

    def __init__(self, iface, engine, dialog):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.engine = engine
        self.dialog = dialog
        self.canvas = iface.mapCanvas()
        self.setCursor(Qt.CrossCursor)
        self._candidate_infos = []
        self._candidate_index = 0
        self._candidate_band = None
        self._route_band = None
        self._canvas_points = []
        self._build_canvas_index()

    def _build_canvas_index(self):
        dst = self.canvas.mapSettings().destinationCrs()
        transform_cache = {}
        for _, info in self.engine.points.items():
            point = QgsPointXY(info["point"])
            src = info["layer"].crs()
            key = (src.authid(), dst.authid())
            transform = transform_cache.get(key)
            if src != dst:
                if transform is None:
                    transform = QgsCoordinateTransform(
                        src, dst, QgsProject.instance().transformContext()
                    )
                    transform_cache[key] = transform
                try:
                    point = transform.transform(point)
                except Exception:
                    continue
            self._canvas_points.append((point, info))

    def start(self):
        self.refresh_route_preview()
        self._clear_candidate()

    def _hover_candidates(self, pos):
        point = self.toMapCoordinates(pos)
        tol = self.canvas.mapUnitsPerPixel() * 26.0
        result = []
        for canvas_point, info in self._canvas_points:
            dist = sqrt(
                (canvas_point.x() - point.x()) ** 2
                + (canvas_point.y() - point.y()) ** 2
            )
            if dist <= tol:
                result.append((dist, info))
        result.sort(key=lambda x: x[0])
        return result

    def _preferred_order(self, candidates):
        if not candidates:
            return []
        direction = getattr(self.dialog, "_direction", None)
        sequence = getattr(self.dialog, "_sequence", [])
        if not sequence:
            preferred = "FDT"
        elif direction == "FAT_TO_FDT":
            preferred = "FAT"
        else:
            preferred = "FAT"
        return sorted(
            candidates,
            key=lambda item: (0 if item[1]["typ"] == preferred else 1, item[0])
        )

    def _set_candidates(self, candidates):
        ordered = self._preferred_order(candidates)
        self._candidate_infos = [info for _, info in ordered]
        self._candidate_index = min(self._candidate_index, max(0, len(self._candidate_infos) - 1))
        self._draw_candidate()

    def _draw_candidate(self):
        self._clear_candidate_band()
        if not self._candidate_infos:
            return
        info = self._candidate_infos[self._candidate_index]
        src = info["layer"].crs()
        dst = self.canvas.mapSettings().destinationCrs()
        point = QgsPointXY(info["point"])
        if src != dst:
            try:
                point = QgsCoordinateTransform(
                    src, dst, QgsProject.instance().transformContext()
                ).transform(point)
            except Exception:
                return
        band = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        band.setColor(QColor(30, 144, 255))
        band.setWidth(3)
        band.setIcon(QgsRubberBand.ICON_CIRCLE)
        band.setIconSize(16 if len(self._candidate_infos) > 1 else 13)
        band.setToGeometry(QgsGeometry.fromPointXY(point), dst)
        self._candidate_band = band

        if len(self._candidate_infos) == 1:
            self.dialog.status.setText(
                f"状态：悬停 {info['typ']} {info['label']}，点击确认。"
            )
        else:
            parts = []
            for i, item in enumerate(self._candidate_infos):
                mark = "●" if i == self._candidate_index else "○"
                parts.append(f"{mark}{item['typ']} {item['label']}")
            self.dialog.status.setText(
                "状态：" + "  |  ".join(parts) + "；按 Tab 切换，点击确认。"
            )

    def _clear_candidate_band(self):
        if self._candidate_band is not None:
            try:
                self.canvas.scene().removeItem(self._candidate_band)
            except Exception:
                pass
            self._candidate_band = None

    def _clear_candidate(self):
        self._candidate_infos = []
        self._candidate_index = 0
        self._clear_candidate_band()

    def canvasMoveEvent(self, event):
        if not self.dialog._draw_active:
            self._clear_candidate()
            return
        candidates = self._hover_candidates(event.pos())
        if not candidates:
            self._clear_candidate()
            return
        old = self._candidate_infos[self._candidate_index] if self._candidate_infos else None
        ordered = self._preferred_order(candidates)
        infos = [info for _, info in ordered]
        self._candidate_infos = infos
        if old is not None:
            for i, info in enumerate(infos):
                if (
                    info["typ"] == old.get("typ")
                    and info["feature_id"] == old.get("feature_id")
                ):
                    self._candidate_index = i
                    break
            else:
                self._candidate_index = 0
        else:
            self._candidate_index = 0
        self._draw_candidate()

    def _selected_info(self):
        if not self._candidate_infos:
            return None
        return self._candidate_infos[self._candidate_index]

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.dialog._draw_active:
            return
        if not self._candidate_infos:
            self._set_candidates(self._hover_candidates(event.pos()))
        info = self._selected_info()
        if info is None:
            return

        if info["typ"] == "FAT":
            fid = int(info["feature_id"])
            completed = _all_completed_fat_ids(self.dialog)
            current = _current_link_fat_ids(self.dialog)
            if fid in completed and fid not in current:
                self.dialog.status.setText(
                    f"状态：{info['label']} 已完成设计，不能重复规划。"
                )
                return

        self._clear_candidate()
        if not self.dialog._sequence:
            self.dialog._start_from_hit(info)
        elif info["typ"] == "FAT":
            self.dialog.add_fat(info)
        elif info["typ"] == "FDT":
            self.dialog.add_fdt(info)
        self.refresh_route_preview()
        self.dialog._refresh_ui()
        self.dialog._refresh_fat_overlays()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Tab and self._candidate_infos:
            self._candidate_index = (self._candidate_index + 1) % len(self._candidate_infos)
            self._draw_candidate()
            event.accept()
            return
        if event.key() == Qt.Key_Backtab and self._candidate_infos:
            self._candidate_index = (self._candidate_index - 1) % len(self._candidate_infos)
            self._draw_candidate()
            event.accept()
            return
        if event.key() == Qt.Key_Backspace:
            self.dialog.undo_last()
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.dialog._stop_tool()
            event.accept()
            return
        super().keyPressEvent(event)

    def _to_canvas_points(self, route):
        points = route["points"]
        src = self.engine.edge_layer.crs()
        dst = self.canvas.mapSettings().destinationCrs()
        if src == dst:
            return points
        transform = QgsCoordinateTransform(
            src, dst, QgsProject.instance().transformContext()
        )
        return [transform.transform(p) for p in points]

    def refresh_route_preview(self):
        if self._route_band is not None:
            try:
                self.canvas.scene().removeItem(self._route_band)
            except Exception:
                pass
            self._route_band = None
        if len(self.dialog._sequence) < 2:
            return
        points = []
        for first, second in zip(
            self.dialog._sequence[:-1], self.dialog._sequence[1:]
        ):
            route = self.engine.route(
                first[0], first[1], second[0], second[1]
            )
            if route:
                route_points = self._to_canvas_points(route)
                points.extend(route_points if not points else route_points[1:])
        if len(points) >= 2:
            band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            band.setWidth(4)
            band.setColor(self.canvas.palette().highlight().color())
            band.setToGeometry(
                QgsGeometry.fromPolylineXY(points),
                self.canvas.mapSettings().destinationCrs(),
            )
            self._route_band = band

    def clear_preview_only(self):
        self._clear_candidate()
        if self._route_band is not None:
            try:
                self.canvas.scene().removeItem(self._route_band)
            except Exception:
                pass
            self._route_band = None
