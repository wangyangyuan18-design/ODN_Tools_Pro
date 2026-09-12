# -*- coding: utf-8 -*-
"""Canonical Link Design entry point.

Runtime architecture:
    __init__.py -> link_design.py -> link_design_core.py

The versioned link_design_v9..v17 modules are compatibility facades only.
Offset planning and FAT landing are executed directly by cable_offset_core.
"""

import copy
import hashlib
import uuid

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    Qgis,
)

from . import link_design_core as _core
from . import cable_offset_core as _offset_core
from . import odn_project_context as context


_CoreController = _core._CoreController
LinkDesignMapToolV9 = _core.LinkDesignMapToolV9
PlanningOverlay = _core.PlanningOverlay

LINK_ID_FIELD = "_ODN_LINK_ID"
SEGMENT_ID_FIELD = "_ODN_SEGMENT"
LINK_FDT_FIELD = "_ODN_FDT"
LINK_NAME_FIELD = "_ODN_LINK"


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Link Design", level)
    except Exception:
        pass


def _stable_link_id(design):
    existing = design.get("_link_id")
    if existing:
        return str(existing)
    parts = []
    for item in design.get("sequence_ids") or design.get("nodes") or []:
        try:
            parts.append(f"{str(item[0])}:{int(item[1])}")
        except Exception:
            parts.append(str(item))
    basis = "|".join(parts)
    link_id = "LNK_" + (hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20].upper() if basis else uuid.uuid4().hex.upper())
    design["_link_id"] = link_id
    return link_id


def _prepare_design_identities(designs):
    for design in designs or []:
        _stable_link_id(design)


def _crs_from_authid(authid):
    if not authid:
        return None
    try:
        crs = QgsCoordinateReferenceSystem(str(authid))
        return crs if crs.isValid() else None
    except Exception:
        return None


def _segment_geometry_in_target(design, segment, target_crs):
    raw_points = segment.get("points", []) or []
    if len(raw_points) < 2:
        return None
    try:
        points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw_points]
    except Exception:
        return None
    source = _crs_from_authid(design.get("source_crs"))
    if source is not None and source != target_crs:
        try:
            transform = QgsCoordinateTransform(source, target_crs, QgsProject.instance().transformContext())
            points = [transform.transform(p) for p in points]
        except Exception:
            return None
    return QgsGeometry.fromPolylineXY(points)


def _ensure_sync_fields(layer):
    existing = {field.name() for field in layer.fields()}
    additions = []
    for name, typ in (
        (LINK_ID_FIELD, QVariant.String),
        (SEGMENT_ID_FIELD, QVariant.Int),
        (LINK_FDT_FIELD, QVariant.String),
        (LINK_NAME_FIELD, QVariant.String),
    ):
        if name not in existing:
            additions.append(QgsField(name, typ, len=128) if typ == QVariant.String else QgsField(name, typ))
    if not additions:
        return True
    if not layer.isEditable() and not layer.startEditing():
        return False
    if not layer.dataProvider().addAttributes(additions):
        return False
    layer.updateFields()
    return True


def _write_design_to_dc(controller, layer, design_index, design):
    _prepare_design_identities([design])
    link_id = _stable_link_id(design)
    if not _ensure_sync_fields(layer):
        raise RuntimeError("Distribution Cable 无法建立 Link/Segment 内部标识字段。")

    link_idx = layer.fields().indexOf(LINK_ID_FIELD)
    seg_idx = layer.fields().indexOf(SEGMENT_ID_FIELD)
    existing = {}
    for feature in layer.getFeatures():
        if str(feature.attribute(link_idx) or "") != link_id:
            continue
        try:
            seg_no = int(feature.attribute(seg_idx))
        except Exception:
            continue
        existing.setdefault(seg_no, []).append(feature.id())

    wanted = {}
    for seg_no, segment in enumerate(design.get("segments", []) or [], start=1):
        geometry = _segment_geometry_in_target(design, segment, layer.crs())
        if geometry is None or geometry.isEmpty():
            continue
        wanted[seg_no] = geometry

    for seg_no, fids in existing.items():
        if seg_no not in wanted:
            for fid in fids:
                layer.deleteFeature(fid)
            continue
        target = wanted[seg_no]
        kept = False
        for fid in fids:
            feature = layer.getFeature(fid)
            if not kept and feature.isValid() and feature.geometry().equals(target):
                feature.setAttribute(link_idx, link_id)
                feature.setAttribute(seg_idx, seg_no)
                feature.setAttribute(layer.fields().indexOf(LINK_FDT_FIELD), str(design.get("fdt", "")))
                feature.setAttribute(layer.fields().indexOf(LINK_NAME_FIELD), str(design.get("link", "")))
                layer.updateFeature(feature)
                kept = True
            else:
                layer.deleteFeature(fid)
        if not kept:
            feature = QgsFeature(layer.fields())
            feature.setGeometry(target)
            feature.setAttribute(link_idx, link_id)
            feature.setAttribute(seg_idx, seg_no)
            feature.setAttribute(layer.fields().indexOf(LINK_FDT_FIELD), str(design.get("fdt", "")))
            feature.setAttribute(layer.fields().indexOf(LINK_NAME_FIELD), str(design.get("link", "")))
            if not layer.addFeature(feature):
                raise RuntimeError(f"无法写入 Distribution Cable：{design.get('fdt','')}/{design.get('link','')}")

    for seg_no, geometry in wanted.items():
        rows = []
        for feature in layer.getFeatures():
            if str(feature.attribute(link_idx) or "") == link_id:
                try:
                    if int(feature.attribute(seg_idx)) == seg_no:
                        rows.append(feature)
                except Exception:
                    pass
        if rows:
            continue
        feature = QgsFeature(layer.fields())
        feature.setGeometry(geometry)
        feature.setAttribute(link_idx, link_id)
        feature.setAttribute(seg_idx, seg_no)
        feature.setAttribute(layer.fields().indexOf(LINK_FDT_FIELD), str(design.get("fdt", "")))
        feature.setAttribute(layer.fields().indexOf(LINK_NAME_FIELD), str(design.get("link", "")))
        if not layer.addFeature(feature):
            raise RuntimeError(f"无法写入 Distribution Cable：{design.get('fdt','')}/{design.get('link','')}")

    return link_id


def _restore_authoritative_routes(host, persist=True):
    controller = host._controller
    engine = controller._engine or controller._prepare_engine()
    if engine is None:
        raise RuntimeError("无法建立当前 Pole Edge 路由引擎。")
    controller._engine = engine
    payload = controller._v9_payload()
    edge_layer = context.project_layer(payload, "Pole Edge")
    if edge_layer is None:
        raise RuntimeError("当前项目没有绑定 Pole Edge 图层。")
    changed = False
    failed = 0

    for design in controller._designs or []:
        seq_ids = design.get("sequence_ids", []) or []
        segments = design.get("segments", []) or []
        if len(seq_ids) < 2 or len(segments) != len(seq_ids) - 1:
            continue
        rebuilt = []
        ok = True
        for old_segment, first, second in zip(segments, seq_ids[:-1], seq_ids[1:]):
            try:
                route = engine.route(str(first[0]), int(first[1]), str(second[0]), int(second[1]))
            except Exception:
                route = None
            if not route or not route.get("edge_sequence"):
                ok = False
                failed += 1
                break
            item = dict(old_segment)
            item["from"] = route.get("from_label", old_segment.get("from", first[1]))
            item["to"] = route.get("to_label", old_segment.get("to", second[1]))
            item["distance"] = round(float(route.get("distance", 0.0)), 3)
            item["pole_edge_distance"] = round(float(route.get("pole_edge_distance", route.get("distance", 0.0))), 3)
            item["edge_sequence"] = list(route.get("edge_sequence", []))
            item["edge_count"] = len(item["edge_sequence"])
            item["points"] = [[float(p.x()), float(p.y())] for p in route.get("points", [])]
            rebuilt.append(item)
        if not ok:
            continue
        new_length = round(sum(float(s.get("distance", 0.0) or 0.0) for s in rebuilt), 3)
        if rebuilt != segments or abs(float(design.get("length", 0.0) or 0.0) - new_length) > 0.001:
            changed = True
        design["segments"] = rebuilt
        design["length"] = new_length
        design["source_crs"] = edge_layer.crs().authid()
        design["needs_resync"] = True
        design["written"] = False

    if changed and persist:
        controller._persist_state()
    _log(f"[route-restore] failed={failed}; changed={int(changed)}")
    return not failed


def _start_design(self):
    host = getattr(self, "host", None)
    if host is not None:
        try:
            _restore_authoritative_routes(host, persist=True)
        except Exception as exc:
            self.status.setText(f"状态：无法恢复 Pole Edge 规划路线：{exc}")
            QtWidgets.QMessageBox.warning(host, "链路设计", str(exc))
            return False
    return _core._CoreController.start_design(self)


def _save_current_link(self):
    result = _core._CoreController.save_current_link(self)
    if not result:
        return result
    host = getattr(self, "host", None)
    if host is not None:
        _restore_authoritative_routes(host, persist=True)
    return True


_CoreController.start_design = _start_design
_CoreController.save_current_link = _save_current_link
_CoreController.write_planned_links = lambda self: _write_all(self)


def _write_all(controller):
    payload = controller._v9_payload()
    layer = context.project_layer(payload, "Distribution Cable")
    if layer is None:
        QtWidgets.QMessageBox.warning(controller, "写入图层", "当前项目没有绑定 Distribution Cable 图层。")
        return False
    if not controller._designs:
        return True
    if not layer.isEditable() and not layer.startEditing():
        QtWidgets.QMessageBox.warning(controller, "写入图层", "无法进入 Distribution Cable 编辑状态。")
        return False
    try:
        _prepare_design_identities(controller._designs)
        for index, design in enumerate(controller._designs):
            _write_design_to_dc(controller, layer, index, design)
            design["written"] = True
            design["needs_resync"] = False
        layer.triggerRepaint()
        controller._persist_state()
        controller._refresh_ui()
        return True
    except Exception as exc:
        QtWidgets.QMessageBox.warning(controller, "写入图层", f"同步 Distribution Cable 失败：\n{exc}")
        return False


class LinkDesignDock(_core.LinkDesignDock):
    """Canonical Link Design dock.

    UI stays deliberately compact; Offset is exposed as a dedicated button and
    executes cable_offset_core directly. The planning controller remains the
    only source of Link topology.
    """

    def __init__(self, iface, parent=None):
        super().__init__(iface, parent)
        self._offset_button = QtWidgets.QPushButton("偏移并写入图层", self)
        self._offset_button.clicked.connect(self._offset_and_write)
        try:
            self.widget().layout().addWidget(self._offset_button)
        except Exception:
            pass

    def _check_dc_consistency_before_design(self, layer=None):
        return True

    def showEvent(self, event):
        try:
            _restore_authoritative_routes(self, persist=True)
        except Exception as exc:
            _log(f"[route-restore-show-fail] {type(exc).__name__}: {exc}", Qgis.Critical)
        super().showEvent(event)

    def _offset_and_write(self):
        controller = self._controller
        if controller._draw_active:
            self.info.setText("请先保存或退出当前规划，再执行偏移并写入图层。")
            return
        if not controller._designs:
            self.info.setText("当前没有已完成的 Link 可偏移写入。")
            return

        spacing_value, control_distance_value = _offset_core.get_settings()
        spacing, control_distance = spacing_value, control_distance_value
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("偏移并写入图层")
        form = QtWidgets.QFormLayout(dialog)
        spacing_box = QtWidgets.QDoubleSpinBox(dialog)
        spacing_box.setRange(0.01, 20.0)
        spacing_box.setDecimals(2)
        spacing_box.setValue(spacing)
        spacing_box.setSuffix(" m")
        control_box = QtWidgets.QDoubleSpinBox(dialog)
        control_box.setRange(0.01, 5.0)
        control_box.setDecimals(2)
        control_box.setValue(control_distance)
        control_box.setSuffix(" m")
        form.addRow("偏移间距", spacing_box)
        form.addRow("拐角控制距离", control_box)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        spacing = float(spacing_box.value())
        control_distance = float(control_box.value())
        _offset_core.save_settings(spacing, control_distance)

        payload = controller._v9_payload()
        dc_layer = context.project_layer(payload, "Distribution Cable")
        edge_layer = context.project_layer(payload, "Pole Edge")
        fat_layer = context.project_layer(payload, "FAT")
        if dc_layer is None or edge_layer is None or fat_layer is None:
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", "当前项目缺少 Distribution Cable、Pole Edge 或 FAT 图层。")
            return
        if not dc_layer.isEditable() and not dc_layer.startEditing():
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", "无法进入 Distribution Cable 编辑状态。")
            return
        if not fat_layer.isEditable() and not fat_layer.startEditing():
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", "无法进入 FAT 编辑状态。")
            return

        try:
            _restore_authoritative_routes(self, persist=True)
            planned = copy.deepcopy(controller._designs)
            summary = _offset_core.apply_offset_layout(
                planned,
                dc_layer,
                edge_layer,
                spacing=spacing,
                control_distance_m=control_distance,
                fat_layer=fat_layer,
                fat_max_distance_m=float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0) or 3.0),
            )
            _prepare_design_identities(planned)
            for index, design in enumerate(planned):
                _write_design_to_dc(controller, dc_layer, index, design)
            fat_written = _offset_core.commit_fat_landing_points(fat_layer, summary)

            controller._designs = planned
            for design in controller._designs:
                design["written"] = True
                design["needs_resync"] = False
            dc_layer.triggerRepaint()
            fat_layer.triggerRepaint()
            controller._persist_state()
            self.refresh_from_core()
            self.info.setText(
                f"已偏移并写入 {len(controller._designs)} 条 Link；"
                f"Cable 偏移 {summary.get('changed_designs', 0)} 条；"
                f"FAT 落点更新 {fat_written} 个。"
            )
        except Exception as exc:
            _log(f"[offset-write-fail] {type(exc).__name__}: {exc}", Qgis.Critical)
            QtWidgets.QMessageBox.warning(self, "偏移并写入图层", f"偏移写入失败：\n{exc}")


LinkDesignCore = _CoreController
LinkDesignMapTool = LinkDesignMapToolV9

# Compatibility symbols used by older in-plugin imports.
LinkDesignMapToolV11 = LinkDesignMapToolV9
LinkDesignMapToolV15 = LinkDesignMapToolV9
