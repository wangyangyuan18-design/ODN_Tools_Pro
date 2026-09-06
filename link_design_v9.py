# -*- coding: utf-8 -*-
"""Link Design v9: compact dock workbench with fast interaction.

The existing v2/v3/v4 data and persistence layers remain authoritative.
This module adds the final compact UI, FDT->FAT-only interaction, fast route
services, pause/resume while editing source layers, multi-selection and FAT
reallocation staging.
"""

import copy
from math import sqrt

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QEvent
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from . import link_design_v2 as _v2
from .link_design_v4 import LinkDesignDialog as _BaseDialog
from .odn_project_routing import OdnProjectRouteEngine as _BaseRouteEngine
from . import odn_project_context as context


# -----------------------------------------------------------------------------
# Runtime service optimization. The original core is preserved; v9 swaps in
# a cached route engine so point attachment and repeated segment routes do not
# scan every Pole Edge graph node on each mouse move.
# -----------------------------------------------------------------------------
class _FastRouteEngine(_BaseRouteEngine):
    def __init__(self, iface, payload, attach_distance):
        super().__init__(iface, payload, attach_distance)
        self._attach_cache = {}
        self._route_cache = {}
        self._node_index = QgsSpatialIndex()
        self._node_by_fid = {}
        for number, (node, point) in enumerate(self.node_points.items(), start=1):
            feature = QgsFeature()
            feature.setId(number)
            feature.setGeometry(QgsGeometry.fromPointXY(point))
            self._node_index.addFeature(feature)
            self._node_by_fid[number] = node

    def attach(self, typ, feature_id):
        key = (str(typ), int(feature_id))
        if key in self._attach_cache:
            return self._attach_cache[key]
        _, info = self.point_by_id(typ, feature_id)
        if info is None or not self.node_points:
            self._attach_cache[key] = None
            return None
        point = self._point_in_edge_crs(info)
        candidates = self._node_index.nearestNeighbor(point, 16)
        best = None
        for fid in candidates:
            node = self._node_by_fid.get(int(fid))
            if node is None:
                continue
            node_point = self.node_points[node]
            distance = self._point_distance(point, node_point)
            if best is None or distance < best[0]:
                best = (distance, node)
        if best is None or best[0] > self.attach_distance:
            self._attach_cache[key] = None
            return None
        result = {
            "connector": best[0],
            "node": best[1],
            "point": point,
            "typ": typ,
            "feature_id": info["feature_id"],
            "label": info["label"],
        }
        self._attach_cache[key] = result
        return result

    def route(self, start_typ, start_id, end_typ, end_id):
        key = (str(start_typ), int(start_id), str(end_typ), int(end_id))
        if key in self._route_cache:
            return self._route_cache[key]
        result = super().route(start_typ, start_id, end_typ, end_id)
        if result is not None:
            # Engineering display rule agreed for co-located FDT/FAT.
            if result.get("distance", 0.0) <= 1e-6 and {str(start_typ), str(end_typ)} == {"FDT", "FAT"}:
                result = dict(result)
                result["distance"] = 2.0
        self._route_cache[key] = result
        return result


_v2.OdnProjectRouteEngine = _FastRouteEngine

# Link capacity rules: eight Link numbers are available at minimum; there is
# no fixed per-Link FAT limit anymore. The old parameter remains for backward
# compatibility but is not used as an engineering restriction.
_old_max_links = _v2._max_links
_old_max_fats = _v2._max_fats


def _v9_max_links(dialog):
    try:
        value = _old_max_links(dialog)
    except Exception:
        value = 0
    return max(8, int(value or 0))


def _v9_max_fats(dialog):
    try:
        total = int(_v2._total_fats(dialog))
    except Exception:
        total = 0
    return max(1, total)


_v2._max_links = _v9_max_links
_v2._max_fats = _v9_max_fats


class _CoreController(_BaseDialog):
    """Hidden controller reusing the stable v4/v3/v2 CRUD implementation."""

    def __init__(self, iface, host):
        self.host = host
        self._pending_reassignments = {}
        self._source_monitor_layers = []
        self._source_monitor_connections = []
        self._paused_by_source_change = False
        super().__init__(iface, iface.mainWindow())
        self.hide()

        # v2/v3 methods expect these widgets to exist. They are hidden proxies;
        # the visible controls live in the Dock and mirror this controller.
        self.plan_label = QtWidgets.QLabel(self)
        self.fdt_label = QtWidgets.QLabel(self)
        self.link_count_label = QtWidgets.QLabel(self)
        self.planned_fat_label = QtWidgets.QLabel(self)
        self.total_fat_label = QtWidgets.QLabel(self)
        self.current_path_label = QtWidgets.QLabel(self)
        self.distance_label = QtWidgets.QLabel(self)
        self.segment_label = QtWidgets.QLabel(self)
        self.route_label = QtWidgets.QLabel(self)
        self.status = QtWidgets.QLabel(self)
        self.start_btn = QtWidgets.QPushButton(self)
        self.save_btn = QtWidgets.QPushButton(self)
        self.done_btn = QtWidgets.QPushButton(self)
        self.exit_btn = QtWidgets.QPushButton(self)
        self._refresh_ui()

    def _build_ui(self):
        pass

    def _refresh_ui(self):
        try:
            self.host.refresh_from_core()
        except Exception:
            pass

    def _prepare_engine(self):
        return super()._prepare_engine()

    def _activate_map_tool(self):
        if self._engine is None:
            self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self._clear_saved_bands()
        self._tool = LinkDesignMapToolV9(self.iface, self._engine, self)
        self.iface.mapCanvas().setMapTool(self._tool)
        self._draw_active = True
        self._paused_by_source_change = False
        self._install_source_monitors()
        self._tool.start()
        return True

    def _install_source_monitors(self):
        self._disconnect_source_monitors()
        payload = self._v9_payload()
        for role in ("FDT", "FAT", "Pole Edge"):
            layer = context.project_layer(payload, role)
            if layer is None:
                continue
            self._source_monitor_layers.append(layer)
            for signal_name in (
                "editingStarted", "featureAdded", "featureDeleted",
                "geometryChanged", "attributeValueChanged",
                "committedFeaturesAdded", "committedFeaturesRemoved",
                "committedGeometriesChanges", "committedAttributeValuesChanges",
            ):
                signal = getattr(layer, signal_name, None)
                if signal is None:
                    continue
                try:
                    signal.connect(self._source_changed)
                    self._source_monitor_connections.append((signal, self._source_changed))
                except Exception:
                    pass

    def _disconnect_source_monitors(self):
        for signal, slot in getattr(self, "_source_monitor_connections", []):
            try:
                signal.disconnect(slot)
            except Exception:
                pass
        self._source_monitor_connections = []
        self._source_monitor_layers = []

    def _source_changed(self, *args):
        if not self._draw_active:
            return
        self._paused_by_source_change = True
        self._draw_active = False
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
        self.status.setText("状态：已暂停——检测到 FDT/FAT/Pole Edge 数据变化，请完成地图编辑后点击“继续规划”。")
        try:
            self.host.refresh_from_core()
        except Exception:
            pass

    def _v9_payload(self):
        try:
            return _v2._fresh_payload(self)
        except Exception:
            return context.current_payload() or {}

    def pause_planning(self):
        if not self._draw_active:
            return
        self._draw_active = False
        self.status.setText("状态：已暂停规划")
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
        try:
            self.host.refresh_from_core()
        except Exception:
            pass

    def resume_planning(self):
        # Rebuild from current FDT/FAT/Pole Edge geometry, so moved points and
        # edited Pole Edge are immediately reflected without losing the draft.
        self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self._paused_by_source_change = False
        self.status.setText("状态：继续规划")
        return self._activate_map_tool()

    def start_design(self):
        if self._draw_active:
            self.pause_planning()
            return True
        if self._sequence:
            return self.resume_planning()
        self._direction = None
        self._editing_index = None
        self._editing_written_index = None
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        self._pending_reassignments = {}
        self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self.status.setText("状态：规划中——请先选择 FDT")
        return self._activate_map_tool()

    def _start_from_hit(self, info):
        if info.get("typ") != "FDT":
            self.status.setText("状态：链路必须从 FDT 开始")
            return False
        if self._sequence:
            self.status.setText("状态：当前 Link 已开始，只能继续选择 FAT")
            return False
        link = _v2._next_link(self, info["label"], exclude_index=self._editing_index)
        if not link:
            self.status.setText(f"状态：{info['label']} 已达到最大 Link 数：{_v9_max_links(self)}")
            return False
        self._direction = "FDT_TO_FAT"
        self._current_fdt = info["label"]
        self._current_fdt_id = int(info["feature_id"])
        self._current_link = link
        self._sequence = [("FDT", int(info["feature_id"]), info["label"])]
        self._persist_state()
        self._refresh_ui()
        self.status.setText(f"状态：{self._current_fdt}/{self._current_link}，请选择 FAT")
        return True

    def add_fdt(self, info):
        self.status.setText("状态：链路设计只能从 FDT 开始，之后只能选择 FAT")
        return False

    def add_fat(self, info):
        if not self._draw_active or info.get("typ") != "FAT" or not self._sequence:
            return False
        fid = int(info["feature_id"])
        if any(item[0] == "FAT" and int(item[1]) == fid for item in self._sequence):
            self.status.setText(f"状态：{info['label']} 已经在当前 Link 中")
            return False

        owner = None
        for index, design in enumerate(self._designs):
            for item in design.get("nodes", []):
                try:
                    if int(item[0]) == fid:
                        owner = (index, design)
                        break
                except Exception:
                    continue
            if owner:
                break
        if owner and owner[0] != self._editing_index:
            old_index, old_design = owner
            if old_design.get("written"):
                QtWidgets.QMessageBox.warning(self, "FAT 重新分配", f"{info['label']} 已属于 {old_design.get('fdt','')}/{old_design.get('link','')} 且已写入图层。\n\n请先修改或删除原 Link，再重新分配。")
                return False
            answer = QtWidgets.QMessageBox.question(
                self, "FAT 重新分配",
                f"{info['label']} 已属于 {old_design.get('fdt','')}/{old_design.get('link','')}。\n\n是否将它重新分配到当前 Link？\n\n原 Link 只删除该 FAT，其他 FAT 顺序保持不变。",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return False
            self._pending_reassignments[fid] = old_index

        previous = self._sequence[-1]
        route = self._engine.route(previous[0], previous[1], "FAT", fid) if self._engine else None
        if route is None:
            self.status.setText(f"状态：{previous[2]} → {info['label']} 无法沿 Pole Edge 建立完整路径")
            return False
        self._sequence.append(("FAT", fid, info["label"]))
        self._persist_state()
        self._refresh_ui()
        if self._tool:
            self._tool.refresh_route_preview()
        return True

    def _remove_fats_from_design(self, design, remove_ids):
        remove_ids = {int(x) for x in remove_ids}
        seq_ids = design.get("sequence_ids", [])
        labels = design.get("sequence", [])
        seq = []
        for i, item in enumerate(seq_ids):
            if len(item) < 2:
                continue
            typ, fid = str(item[0]), int(item[1])
            if typ == "FAT" and fid in remove_ids:
                continue
            label = str(labels[i]) if i < len(labels) else str(fid)
            seq.append((typ, fid, label))
        if len([x for x in seq if x[0] == "FAT"]) == 0:
            return None
        routes = []
        for first, second in zip(seq[:-1], seq[1:]):
            route = self._engine.route(first[0], first[1], second[0], second[1])
            if route is None:
                return None
            routes.append(route)
        updated = copy.deepcopy(design)
        updated["sequence_ids"] = [(x[0], int(x[1])) for x in seq]
        updated["sequence"] = [x[2] for x in seq]
        updated["nodes"] = [(x[1], x[2]) for x in seq if x[0] == "FAT"]
        updated["direction"] = "FDT_TO_FAT"
        updated["length"] = round(sum(float(r["distance"]) for r in routes), 3)
        updated["segments"] = [
            {
                "from": r["from_label"], "to": r["to_label"],
                "distance": round(float(r["distance"]), 3),
                "pole_edge_distance": round(float(r["pole_edge_distance"]), 3),
                "edge_count": len(r["edge_sequence"]),
                "points": [[float(p.x()), float(p.y())] for p in r["points"]],
            }
            for r in routes
        ]
        return updated

    def save_current_link(self):
        # Allow staged FAT moves by temporarily removing the moved FATs from
        # their source plans; only commit the source updates after the current
        # Link has passed all normal validation.
        pending = dict(self._pending_reassignments or {})
        backup = copy.deepcopy(self._designs)
        sources = {}
        try:
            for fid, index in pending.items():
                sources.setdefault(int(index), []).append(int(fid))
            for index, fids in sorted(sources.items(), reverse=True):
                if index < 0 or index >= len(self._designs):
                    continue
                if self._designs[index].get("written"):
                    raise RuntimeError("已写入 Link 不能直接重新分配 FAT")
                rebuilt = self._remove_fats_from_design(self._designs[index], fids)
                if rebuilt is None:
                    raise RuntimeError("原 Link 删除 FAT 后无法形成有效 Link")
                self._designs[index] = rebuilt

            result = super().save_current_link()
            if not result:
                self._designs = backup
                self._pending_reassignments = pending
                self._persist_state()
                self._refresh_ui()
                return False
        except Exception as exc:
            self._designs = backup
            self._pending_reassignments = pending
            self.status.setText(f"状态：保存失败：{exc}")
            self._persist_state()
            self._refresh_ui()
            QtWidgets.QMessageBox.warning(self, "保存规划", str(exc))
            return False
        self._pending_reassignments = {}
        self._refresh_ui()
        return True

    def load_design_for_edit(self, index):
        result = super().load_design_for_edit(index)
        if result:
            self._pending_reassignments = {}
            self._refresh_ui()
        return result

    def delete_link(self, index):
        result = super().delete_link(index)
        if result:
            self._refresh_ui()
        return result

    def show_saved_designs(self, entries):
        super().show_saved_designs(entries)
        self._refresh_ui()

    def exit_design(self):
        self._persist_state()
        self._stop_tool()
        self._refresh_ui()

    def close_controller(self):
        self._disconnect_source_monitors()
        self._stop_tool()
        try:
            self.close()
        except Exception:
            pass


class PlanningOverlay(QtWidgets.QFrame):
    """Compact planning strip fixed at the top-center of the map canvas."""

    def __init__(self, canvas, controller):
        super().__init__(canvas)
        self.canvas = canvas
        self.controller = controller
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet("QFrame{background:rgba(255,255,255,245);border:1px solid #b8b8b8;border-radius:4px;} QLabel{padding:0 2px;} QPushButton{min-height:24px;padding:1px 12px;}")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._build()
        canvas.installEventFilter(self)
        self.raise_()
        self.show()

    def _build(self):
        root = QtWidgets.QGridLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setHorizontalSpacing(10)
        root.setVerticalSpacing(3)
        self.current = QtWidgets.QLabel("当前路径：—    实时距离：—")
        self.current.setWordWrap(True)
        self.plan = QtWidgets.QLabel("规划中 —    L—    FAT 数量 0")
        self.link_path = QtWidgets.QLabel("LINK路径：—")
        self.link_path.setWordWrap(True)
        self.distance = QtWidgets.QLabel("各段距离：—")
        self.total = QtWidgets.QLabel("总距离：—")
        self.start = QtWidgets.QPushButton("开始规划")
        self.save = QtWidgets.QPushButton("保存规划")
        self.exit = QtWidgets.QPushButton("退出设计")
        root.addWidget(self.current, 0, 0, 1, 3)
        root.addWidget(self.plan, 1, 0)
        root.addWidget(self.link_path, 2, 0, 1, 3)
        root.addWidget(self.distance, 3, 0, 1, 2)
        root.addWidget(self.total, 3, 2)
        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        row.addWidget(self.start)
        row.addWidget(self.save)
        row.addWidget(self.exit)
        root.addLayout(row, 4, 0, 1, 3)
        self.start.clicked.connect(self._toggle)
        self.save.clicked.connect(self._save)
        self.exit.clicked.connect(self.controller.exit_design)
        self.resize(720, 142)

    def _toggle(self):
        if self.controller._draw_active:
            self.controller.pause_planning()
        else:
            self.controller.start_design()
        self.controller.host.refresh_from_core()

    def _save(self):
        self.controller.save_current_link()
        self.controller.host.refresh_from_core()

    def eventFilter(self, obj, event):
        if obj is self.canvas and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    def _reposition(self):
        w = min(760, max(520, self.canvas.width() - 40))
        self.setFixedWidth(w)
        self.move(max(0, (self.canvas.width() - w) // 2), 8)

    def refresh(self):
        c = self.controller
        seq = list(c._sequence)
        if seq:
            all_labels = [x[2] for x in seq]
            fats = [x[2] for x in seq if x[0] == "FAT"]
            self.link_path.setText("LINK路径：" + " → ".join(all_labels))
            self.plan.setText(f"规划中 {c._current_fdt or '—'}    {c._current_link or 'L—'}    FAT 数量 {len(fats)}")
        else:
            self.link_path.setText("LINK路径：—")
            self.plan.setText("规划中 —    L—    FAT 数量 0")
        self.distance.setText("各段距离：—")
        self.total.setText("总距离：—")
        if c._draw_active and hasattr(c, "_tool") and c._tool:
            self.start.setText("暂停规划")
        elif seq:
            self.start.setText("继续规划")
        else:
            self.start.setText("开始规划")
        if hasattr(c, "_hover_segment") and c._hover_segment:
            a, b = c._hover_segment
            self.current.setText(f"当前路径：{a} → {b}    实时距离：{getattr(c, '_hover_distance', 0.0):.1f}m")
        elif seq and len(seq) >= 2:
            self.current.setText(f"当前路径：{seq[-2][2]} → {seq[-1][2]}    实时距离：—")
        else:
            self.current.setText("当前路径：—    实时距离：—")

        if seq and c._engine:
            routes = []
            for a, b in zip(seq[:-1], seq[1:]):
                route = c._engine.route(a[0], a[1], b[0], b[1])
                if route:
                    routes.append(route)
            if routes:
                self.distance.setText("各段距离：" + "、".join(f"{float(r['distance']):.1f}m" for r in routes))
                self.total.setText(f"总距离：{sum(float(r['distance']) for r in routes):.1f}m")
        if getattr(c, "_paused_by_source_change", False) and not c._draw_active:
            self.start.setText("继续规划")
        self._reposition()


class LinkDesignMapToolV9(QgsMapTool):
    """Fast spatial-index selector with cached one-segment hover preview."""

    def __init__(self, iface, engine, controller):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.engine = engine
        self.controller = controller
        self.canvas = iface.mapCanvas()
        self.setCursor(Qt.CrossCursor)
        self._candidate_infos = []
        self._candidate_index = 0
        self._candidate_band = None
        self._route_band = None
        self._route_cache = {}
        self._last_hover_key = None
        self._build_index()

    def _build_index(self):
        dst = self.canvas.mapSettings().destinationCrs()
        self._index = QgsSpatialIndex()
        self._info_by_fid = {}
        transform_cache = {}
        for number, (_, info) in enumerate(self.engine.points.items(), start=1):
            point = QgsPointXY(info["point"])
            src = info["layer"].crs()
            if src != dst:
                key = (src.authid(), dst.authid())
                transform = transform_cache.get(key)
                if transform is None:
                    transform = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
                    transform_cache[key] = transform
                try:
                    point = transform.transform(point)
                except Exception:
                    continue
            feature = QgsFeature()
            feature.setId(number)
            feature.setGeometry(QgsGeometry.fromPointXY(point))
            self._index.addFeature(feature)
            self._info_by_fid[number] = info

    def _candidates(self, pos):
        point = self.toMapCoordinates(pos)
        tolerance = self.canvas.mapUnitsPerPixel() * 26.0
        result = []
        for fid in self._index.nearestNeighbor(point, 12):
            info = self._info_by_fid.get(int(fid))
            if not info:
                continue
            src = info["layer"].crs()
            p = QgsPointXY(info["point"])
            if src != self.canvas.mapSettings().destinationCrs():
                try:
                    p = QgsCoordinateTransform(src, self.canvas.mapSettings().destinationCrs(), QgsProject.instance().transformContext()).transform(p)
                except Exception:
                    continue
            d = sqrt((p.x() - point.x()) ** 2 + (p.y() - point.y()) ** 2)
            if d <= tolerance:
                result.append((d, info))
        result.sort(key=lambda x: x[0])
        return result

    def _ordered(self, candidates):
        preferred = "FDT" if not self.controller._sequence else "FAT"
        return sorted(candidates, key=lambda x: (0 if x[1]["typ"] == preferred else 1, x[0]))

    def _selected(self):
        if not self._candidate_infos:
            return None
        return self._candidate_infos[self._candidate_index]

    def _draw_candidate(self):
        if self._candidate_band is not None:
            try: self.canvas.scene().removeItem(self._candidate_band)
            except Exception: pass
        self._candidate_band = None
        info = self._selected()
        if info is None:
            return
        point = QgsPointXY(info["point"])
        src = info["layer"].crs(); dst = self.canvas.mapSettings().destinationCrs()
        if src != dst:
            try: point = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext()).transform(point)
            except Exception: return
        band = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        band.setColor(QColor(30, 144, 255)); band.setWidth(2)
        band.setIcon(QgsRubberBand.ICON_CIRCLE); band.setIconSize(16 if len(self._candidate_infos) > 1 else 13)
        band.setToGeometry(QgsGeometry.fromPointXY(point), dst)
        self._candidate_band = band

    def _preview_route(self, info):
        seq = self.controller._sequence
        if not seq or info.get("typ") != "FAT":
            return None
        if any(x[0] == "FAT" and int(x[1]) == int(info["feature_id"]) for x in seq):
            return None
        key = (seq[-1][0], int(seq[-1][1]), "FAT", int(info["feature_id"]))
        if key not in self._route_cache:
            try:
                self._route_cache[key] = self.engine.route(*key)
            except Exception:
                self._route_cache[key] = None
        return self._route_cache[key]

    def _refresh_hover_ui(self):
        info = self._selected()
        if info is None:
            self.controller._hover_segment = None
            self.controller._hover_distance = 0.0
            self.controller._refresh_ui()
            return
        route = self._preview_route(info)
        if route is None:
            self.controller._hover_segment = None
            self.controller._hover_distance = 0.0
            self.controller._refresh_ui()
            return
        previous = self.controller._sequence[-1]
        self.controller._hover_segment = (previous[2], info["label"])
        self.controller._hover_distance = float(route.get("distance", 0.0))
        self.controller._refresh_ui()

    def start(self):
        self.refresh_route_preview()

    def canvasMoveEvent(self, event):
        if not self.controller._draw_active:
            return
        candidates = self._ordered(self._candidates(event.pos()))
        if not candidates:
            self._candidate_infos = []
            self._candidate_index = 0
            self._draw_candidate()
            self.controller._hover_segment = None
            self.controller._hover_distance = 0.0
            self.controller._refresh_ui()
            return
        old = self._selected()
        self._candidate_infos = [info for _, info in candidates]
        if old:
            old_key = (old.get("typ"), int(old.get("feature_id")))
            self._candidate_index = next((i for i, x in enumerate(self._candidate_infos) if (x.get("typ"), int(x.get("feature_id"))) == old_key), 0)
        else:
            self._candidate_index = 0
        self._draw_candidate()
        self._refresh_hover_ui()

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.controller._draw_active:
            return
        if not self._candidate_infos:
            self._candidate_infos = [info for _, info in self._ordered(self._candidates(event.pos()))]
            self._candidate_index = 0
        info = self._selected()
        if info is None:
            return
        if not self.controller._sequence:
            self.controller._start_from_hit(info)
        elif info.get("typ") == "FAT":
            self.controller.add_fat(info)
        self._candidate_infos = []
        self._candidate_index = 0
        self._last_hover_key = None
        self.controller._hover_segment = None
        self.controller._hover_distance = 0.0
        self._draw_candidate()
        self.refresh_route_preview()
        self.controller._refresh_ui()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Tab, Qt.Key_Backtab) and self._candidate_infos:
            delta = 1 if event.key() == Qt.Key_Tab else -1
            self._candidate_index = (self._candidate_index + delta) % len(self._candidate_infos)
            self._draw_candidate(); self._refresh_hover_ui(); event.accept(); return
        if event.key() == Qt.Key_Backspace:
            self.controller.undo_last(); event.accept(); return
        if event.key() == Qt.Key_Escape:
            self.controller.pause_planning(); event.accept(); return
        super().keyPressEvent(event)

    def refresh_route_preview(self):
        if self._route_band is not None:
            try: self.canvas.scene().removeItem(self._route_band)
            except Exception: pass
            self._route_band = None
        seq = self.controller._sequence
        if len(seq) < 2:
            return
        points = []
        for a, b in zip(seq[:-1], seq[1:]):
            route = self.engine.route(a[0], a[1], b[0], b[1])
            if not route:
                continue
            src = self.engine.edge_layer.crs(); dst = self.canvas.mapSettings().destinationCrs()
            route_points = route["points"]
            if src != dst:
                transform = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
                route_points = [transform.transform(p) for p in route_points]
            points.extend(route_points if not points else route_points[1:])
        if len(points) >= 2:
            band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            band.setWidth(4); band.setColor(self.canvas.palette().highlight().color())
            band.setToGeometry(QgsGeometry.fromPolylineXY(points), dst)
            self._route_band = band

    def clear_preview_only(self):
        for attr in ("_candidate_band", "_route_band"):
            band = getattr(self, attr, None)
            if band is not None:
                try: self.canvas.scene().removeItem(band)
                except Exception: pass
                setattr(self, attr, None)
        self.controller._hover_segment = None
        self.controller._hover_distance = 0.0


class LinkDesignDock(QtWidgets.QDockWidget):
    """Compact left-docked Link Design workbench."""

    def __init__(self, iface, parent=None):
        super().__init__("链路设计", parent or iface.mainWindow())
        self.iface = iface
        self.setObjectName("ODNToolsPro_LinkDesignDock")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable | QtWidgets.QDockWidget.DockWidgetClosable)
        self.resize(430, 640)
        self.setMinimumWidth(380)
        self._controller = _CoreController(iface, self)
        self._overlay = PlanningOverlay(iface.mapCanvas(), self._controller)
        self._build_ui()
        self.refresh_from_core()

    def _build_ui(self):
        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(4)

        title = QtWidgets.QLabel("已完成设计")
        font = title.font(); font.setBold(True); title.setFont(font)
        layout.addWidget(title)

        self.summary = QtWidgets.QLabel("已完成Link:0    已完成FAT:0/0")
        layout.addWidget(self.summary)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["FDT/Link", "FAT数", "总距离", "状态"])
        self.tree.setColumnWidth(0, 155)
        self.tree.setColumnWidth(1, 55)
        self.tree.setColumnWidth(2, 85)
        self.tree.setColumnWidth(3, 55)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._tree_selection_changed)
        self.tree.itemDoubleClicked.connect(self._tree_double_clicked)
        layout.addWidget(self.tree, 1)

        self.info = QtWidgets.QLabel("—")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color:#666;")
        layout.addWidget(self.info)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self.modify_btn = QtWidgets.QPushButton("修改选中Link")
        self.delete_btn = QtWidgets.QPushButton("删除选中Link")
        self.write_btn = QtWidgets.QPushButton("确定并写入图层")
        for b in (self.modify_btn, self.delete_btn, self.write_btn):
            b.setMinimumHeight(26); row.addWidget(b)
        layout.addLayout(row)
        self.modify_btn.clicked.connect(self._modify_selected)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.write_btn.clicked.connect(self._write_all)
        self.setWidget(root)

    def _tree_selected_links(self):
        selected = self.tree.selectedItems()
        indexes = []
        for item in selected:
            data = item.data(0, Qt.UserRole)
            if not data:
                continue
            if data[0] == "link":
                indexes.append(int(data[1]))
            else:
                for i in data[1]:
                    indexes.append(int(i))
        return sorted(set(i for i in indexes if 0 <= i < len(self._controller._designs)))

    def _tree_selection_changed(self):
        indexes = self._tree_selected_links()
        entries = [(i, self._controller._designs[i]) for i in indexes]
        if entries:
            self._controller.show_saved_designs(entries)
            if len(entries) == 1:
                d = entries[0][1]
                self.info.setText(f"{d.get('fdt','')}/{d.get('link','')}：{len(d.get('nodes',[]))} 个 FAT，{float(d.get('length',0.0) or 0.0):.1f}m，已完成。")
            else:
                total = sum(float(d.get("length", 0.0) or 0.0) for _, d in entries)
                self.info.setText(f"已选 {len(entries)} 条 Link，总距离 {total:.1f}m。")
        else:
            self._controller._clear_saved_bands()
            self.info.setText("—")

    def _tree_double_clicked(self, item, column):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if data[0] == "link":
            self._zoom_design(self._controller._designs[int(data[1])])
        else:
            entries = [self._controller._designs[i] for i in data[1] if 0 <= int(i) < len(self._controller._designs)]
            self._zoom_designs(entries)

    def _zoom_design(self, design):
        self._zoom_designs([design])

    def _zoom_designs(self, designs):
        rect = None
        src = None
        for design in designs:
            for segment in design.get("segments", []):
                pts = segment.get("points", [])
                if len(pts) < 1:
                    continue
                src = self._controller._engine.edge_layer.crs() if self._controller._engine else src
                for p in pts:
                    point = QgsPointXY(float(p[0]), float(p[1]))
                    if rect is None:
                        rect = QgsRectangle(point.x(), point.y(), point.x(), point.y())
                    else:
                        rect.combineExtentWith(point.x(), point.y())
        if rect is None:
            return
        dst = self.iface.mapCanvas().mapSettings().destinationCrs()
        if src != dst:
            try:
                transform = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
                a = transform.transform(QgsPointXY(rect.xMinimum(), rect.yMinimum()))
                b = transform.transform(QgsPointXY(rect.xMaximum(), rect.yMaximum()))
                rect = QgsRectangle(min(a.x(), b.x()), min(a.y(), b.y()), max(a.x(), b.x()), max(a.y(), b.y()))
            except Exception:
                pass
        rect.scale(1.15)
        self.iface.mapCanvas().setExtent(rect)
        self.iface.mapCanvas().refresh()

    def _modify_selected(self):
        if self._controller._draw_active:
            self.info.setText("当前正在规划，请先保存或退出当前规划后再修改 Link。")
            return
        indexes = self._tree_selected_links()
        if len(indexes) != 1:
            self.info.setText("请选择一个 Link 后再修改。")
            return
        if self._controller.load_design_for_edit(indexes[0]):
            self.refresh_from_core()

    def _delete_selected(self):
        if self._controller._draw_active:
            self.info.setText("当前正在规划，请先保存或退出当前规划后再删除 Link。")
            return
        indexes = self._tree_selected_links()
        if not indexes:
            self.info.setText("请选择需要删除的 Link。")
            return
        for index in sorted(indexes, reverse=True):
            if not self._controller.delete_link(index):
                break
        self.refresh_from_core()

    def _write_all(self):
        if self._controller._draw_active:
            self.info.setText("请先保存或退出当前规划，再写入图层。")
            return
        self._controller.write_planned_links()
        self.refresh_from_core()

    def refresh_from_core(self):
        c = self._controller
        total_fats = 0
        try:
            total_fats = _v2._total_fats(c)
        except Exception:
            pass
        used_fats = set()
        for d in c._designs:
            for n in d.get("nodes", []):
                try: used_fats.add(int(n[0]))
                except Exception: pass
        self.summary.setText(f"已完成Link:{len(c._designs)}    已完成FAT:{len(used_fats)}/{total_fats}")
        self.tree.blockSignals(True)
        self.tree.clear()
        grouped = {}
        for i, d in enumerate(c._designs):
            grouped.setdefault(str(d.get("fdt", "未知 FDT")), []).append((i, d))
        for fdt in sorted(grouped):
            entries = grouped[fdt]
            fats = set()
            total = 0.0
            for _, d in entries:
                total += float(d.get("length", 0.0) or 0.0)
                for n in d.get("nodes", []):
                    try: fats.add(int(n[0]))
                    except Exception: pass
            parent = QtWidgets.QTreeWidgetItem([f"{fdt}/{len(entries)}", str(len(fats)), f"{total:.1f}m", "已完成"])
            parent.setData(0, Qt.UserRole, ("fdt", [i for i, _ in entries]))
            parent.setExpanded(str(fdt) == str(c._current_fdt))
            self.tree.addTopLevelItem(parent)
            for i, d in sorted(entries, key=lambda x: str(x[1].get("link", ""))):
                status = "修改中" if c._editing_index == i else "已完成"
                child = QtWidgets.QTreeWidgetItem([str(d.get("link", "L?")), str(len(d.get("nodes", []))), f"{float(d.get('length',0.0) or 0.0):.1f}m", status])
                child.setData(0, Qt.UserRole, ("link", i))
                child.setToolTip(0, " → ".join(d.get("sequence", [])))
                parent.addChild(child)
        self.tree.blockSignals(False)
        if c._draw_active:
            self.info.setText(f"{c._current_fdt or '—'}/{c._current_link or 'L—'}：正在规划。")
        elif c._editing_index is not None and 0 <= c._editing_index < len(c._designs):
            d = c._designs[c._editing_index]
            self.info.setText(f"{d.get('fdt','')}/{d.get('link','')}：{len(d.get('nodes',[]))} 个 FAT，{float(d.get('length',0.0) or 0.0):.1f}m，修改中。")
        elif not self.info.text():
            self.info.setText("—")
        self._overlay.refresh()

    def closeEvent(self, event):
        try:
            self._overlay.close()
            self._controller.close_controller()
        except Exception:
            pass
        event.accept()
