# -*- coding: utf-8 -*-
"""Link Design v12: stable Link/Distribution Cable synchronization."""

import copy
import hashlib

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

from . import link_design_v9 as _v9
from . import link_design_v11 as _v11

_original_save_current_link = _v9._CoreController.save_current_link
_original_write_planned_links = _v9._CoreController.write_planned_links

LINK_ID_FIELD = "_ODN_LINK_ID"
SEGMENT_ID_FIELD = "_ODN_SEGMENT"
LINK_FDT_FIELD = "_ODN_FDT"
LINK_NAME_FIELD = "_ODN_LINK"


def _ensure_sync_fields(layer):
    existing = {f.name() for f in layer.fields()}
    additions = []
    if LINK_ID_FIELD not in existing:
        additions.append(QgsField(LINK_ID_FIELD, QVariant.String, len=128))
    if SEGMENT_ID_FIELD not in existing:
        additions.append(QgsField(SEGMENT_ID_FIELD, QVariant.Int))
    if LINK_FDT_FIELD not in existing:
        additions.append(QgsField(LINK_FDT_FIELD, QVariant.String, len=128))
    if LINK_NAME_FIELD not in existing:
        additions.append(QgsField(LINK_NAME_FIELD, QVariant.String, len=128))
    if not additions:
        return True
    try:
        if not layer.isEditable() and not layer.startEditing():
            return False
        return bool(layer.dataProvider().addAttributes(additions)) and bool(layer.updateFields())
    except Exception:
        return False


def _sequence_identity(sequence):
    parts = []
    for item in sequence or []:
        try:
            parts.append(f"{str(item[0])}:{int(item[1])}")
        except Exception:
            parts.append(str(item))
    return "|".join(parts)


def _stable_link_id(design, index=None):
    """Return a persistent internal ID that never depends on display names.

    New designs get a UUID-like random value generated once and persisted in
    the design record. Legacy records are deterministically migrated from
    their saved node/sequence IDs using SHA-1, not Python hash().
    """
    existing = design.get("_link_id")
    if existing:
        return str(existing)

    sequence = design.get("sequence_ids") or design.get("nodes") or []
    basis = _sequence_identity(sequence)
    if not basis:
        basis = f"legacy-index:{int(index or 0)}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20].upper()
    link_id = f"LNK_{digest}"
    design["_link_id"] = link_id
    return link_id


def _crs_from_authid(authid):
    if not authid:
        return None
    try:
        crs = QgsCoordinateReferenceSystem(str(authid))
        return crs if crs.isValid() else None
    except Exception:
        return None


def _segment_geometry_in_target(design, segment, target_crs):
    raw_points = segment.get("points", [])
    if len(raw_points) < 2:
        return None
    try:
        points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw_points]
    except Exception:
        return None
    source_crs = _crs_from_authid(design.get("source_crs"))
    if source_crs is not None and source_crs != target_crs:
        try:
            transform = QgsCoordinateTransform(
                source_crs,
                target_crs,
                QgsProject.instance().transformContext(),
            )
            points = [transform.transform(p) for p in points]
        except Exception:
            return None
    return QgsGeometry.fromPolylineXY(points)


def _add_sync_attrs(feature, link_id, segment_number, design):
    for name, value in (
        (LINK_ID_FIELD, str(link_id)),
        (SEGMENT_ID_FIELD, int(segment_number)),
        (LINK_FDT_FIELD, str(design.get("fdt", ""))),
        (LINK_NAME_FIELD, str(design.get("link", ""))),
    ):
        idx = feature.fields().indexOf(name)
        if idx >= 0:
            feature.setAttribute(idx, value)


def _dc_records(layer):
    result = {}
    link_idx = layer.fields().indexOf(LINK_ID_FIELD)
    seg_idx = layer.fields().indexOf(SEGMENT_ID_FIELD)
    if link_idx < 0 or seg_idx < 0:
        return result
    for feature in layer.getFeatures():
        link_id = feature.attribute(link_idx)
        seg = feature.attribute(seg_idx)
        if not link_id or seg in (None, ""):
            continue
        try:
            key = (str(link_id), int(seg))
        except Exception:
            continue
        result.setdefault(key, []).append((int(feature.id()), feature))
    return result


def _same_geometry(a, b, tolerance=1e-7):
    if a is None or b is None or a.isEmpty() or b.isEmpty():
        return False
    try:
        if a.equals(b):
            return True
    except Exception:
        pass
    try:
        return a.hausdorffDistance(b) <= tolerance
    except Exception:
        return False


def _prepare_design_identities(designs):
    changed = False
    for index, design in enumerate(designs or []):
        before = design.get("_link_id")
        _stable_link_id(design, index)
        changed = changed or before != design.get("_link_id")
    return changed


def _v12_add_fat(self, info):
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
            except (TypeError, ValueError, IndexError):
                continue
        if owner:
            break

    if owner and owner[0] != self._editing_index:
        old_index, old_design = owner
        answer = QtWidgets.QMessageBox.question(
            self,
            "FAT 重新分配",
            f"{info['label']} 已属于 {old_design.get('fdt','')}/{old_design.get('link','')}。\n\n"
            "是否将它从原 Link 删除并重新分配到当前 Link？\n\n"
            "保存当前 Link 时，原 Link 会同步移除该 FAT；如果原 Link 已写入图层，"
            "原路线会在“确定并写入图层”时被替换。",
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


def _v12_save_current_link(self):
    pending = dict(getattr(self, "_pending_reassignments", {}) or {})
    backup = copy.deepcopy(self._designs)

    try:
        _prepare_design_identities(self._designs)
        sources = {}
        for fid, index in pending.items():
            sources.setdefault(int(index), []).append(int(fid))

        for index, fids in sorted(sources.items(), reverse=True):
            if index < 0 or index >= len(self._designs):
                continue
            old_design = self._designs[index]
            rebuilt = self._remove_fats_from_design(old_design, fids)
            if rebuilt is None:
                raise RuntimeError(
                    f"原 Link {old_design.get('fdt','')}/{old_design.get('link','')} 删除 FAT 后无法形成有效 Link"
                )
            rebuilt["_link_id"] = _stable_link_id(old_design, index)
            rebuilt["written"] = False
            rebuilt["needs_resync"] = True
            self._designs[index] = rebuilt

        result = _original_save_current_link(self)
        if not result:
            self._designs = backup
            self._pending_reassignments = pending
            self._persist_state()
            self._refresh_ui()
            return False

        _prepare_design_identities(self._designs)
        self._pending_reassignments = {}
        self._persist_state()
        return True
    except Exception as exc:
        self._designs = backup
        self._pending_reassignments = pending
        self.status.setText(f"状态：保存失败：{exc}")
        self._persist_state()
        self._refresh_ui()
        QtWidgets.QMessageBox.warning(self, "保存规划", str(exc))
        return False


def _write_design_to_dc(self, layer, design_index, design, replace_changed=True):
    link_id = _stable_link_id(design, design_index)
    existing = _dc_records(layer)
    segments = design.get("segments", []) or []
    wanted = {}
    for seg_index, segment in enumerate(segments):
        geom = _segment_geometry_in_target(design, segment, layer.crs())
        if geom is None or geom.isEmpty():
            raise RuntimeError(
                f"{design.get('fdt','')}/{design.get('link','')} Segment {seg_index + 1} 几何无效。"
            )
        wanted[(link_id, seg_index + 1)] = (seg_index, geom)

    # Existing DC records are identified by stable Link/Segment IDs, never by
    # FDT/FAT names and never by geometry alone.
    for key, items in list(existing.items()):
        if key[0] != link_id:
            continue
        if key not in wanted:
            for fid, _feature in items:
                if layer.deleteFeature(fid):
                    continue
        else:
            target_geom = wanted[key][1]
            kept = False
            for fid, feature in items:
                if not kept and _same_geometry(feature.geometry(), target_geom):
                    fresh = layer.getFeature(fid)
                    _add_sync_attrs(fresh, link_id, key[1], design)
                    layer.updateFeature(fresh)
                    kept = True
                else:
                    layer.deleteFeature(fid)

    current = _dc_records(layer)
    for key, (seg_index, geom) in wanted.items():
        rows = current.get(key, [])
        if rows and not replace_changed:
            continue
        if rows:
            for fid, feature in rows:
                if _same_geometry(feature.geometry(), geom):
                    fresh = layer.getFeature(fid)
                    _add_sync_attrs(fresh, link_id, seg_index + 1, design)
                    layer.updateFeature(fresh)
                else:
                    layer.deleteFeature(fid)
            rows = _dc_records(layer).get(key, [])
        if not rows:
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geom)
            _add_sync_attrs(feature, link_id, seg_index + 1, design)
            if not layer.addFeature(feature):
                raise RuntimeError(
                    f"无法写入 Distribution Cable：{design.get('fdt','')}/{design.get('link','')} Segment {seg_index + 1}"
                )

    return link_id


def _v12_write_planned_links(self):
    """Explicit write: saved design is authoritative for all current Link geometry."""
    layer = _v9.context.project_layer(_v9._fresh_payload(self), "Distribution Cable")
    if layer is None:
        QtWidgets.QMessageBox.warning(self, "写入图层", "当前项目没有绑定 Distribution Cable 图层。")
        return False
    if not self._designs:
        QtWidgets.QMessageBox.information(self, "写入图层", "当前没有已保存的 Link 规划。")
        return True
    if not layer.isEditable() and not layer.startEditing():
        QtWidgets.QMessageBox.warning(self, "写入图层", f"无法进入 Distribution Cable 编辑状态：{layer.name()}")
        return False

    try:
        _prepare_design_identities(self._designs)
        summary = {"links": 0, "segments": 0}
        for index, design in enumerate(self._designs):
            if not (design.get("segments") or []):
                continue
            _write_design_to_dc(self, layer, index, design, replace_changed=True)
            summary["links"] += 1
            summary["segments"] += len(design.get("segments") or [])
            design["written"] = True
            design["needs_resync"] = False
            design.pop("_resync_source", None)
        layer.triggerRepaint()
        self._persist_state()
    except Exception as exc:
        QtWidgets.QMessageBox.warning(self, "写入图层", f"同步 Distribution Cable 失败：\n{exc}")
        return False

    self._refresh_ui()
    QtWidgets.QMessageBox.information(
        self,
        "写入图层",
        f"已同步 {summary['links']} 条 Link，共 {summary['segments']} 个线路段。\n\n"
        "已存在的 Link/Segment 会按稳定内部 ID 替换为最新线路；"
        "FDT/FAT 名称变化不会改变归属。",
    )
    return True


def _find_design_by_link_id(designs, link_id):
    for index, design in enumerate(designs or []):
        if str(design.get("_link_id", "")) == str(link_id):
            return index, design
    return None, None


def _sync_orphan_dc_into_designs(self, layer, orphan_records):
    """Import orphan DC records by stable ID into completed-design topology.

    This is intentionally geometry-only for the import: it records the DC
    feature as a planned segment placeholder. It does not invent FAT ordering.
    A newly imported Link can therefore be displayed and later edited in Link
    Design, while the user remains responsible for completing missing topology.
    """
    records = _dc_records(layer)
    grouped = {}
    for key in orphan_records:
        grouped.setdefault(key[0], []).append(key[1])

    imported = 0
    for link_id, segment_numbers in grouped.items():
        # There must not be an existing design under this stable identity.
        if any(str(d.get("_link_id", "")) == str(link_id) for d in self._designs):
            continue
        # We cannot derive a valid FAT sequence or FDT identity from arbitrary
        # DC geometry. The safe import therefore creates a minimal internal
        # carrier record; it is visible to the consistency layer and can be
        # edited into a normal Link later.
        features = []
        source_crs = layer.crs().authid()
        for seg_no in sorted(segment_numbers):
            rows = records.get((link_id, seg_no), [])
            if not rows:
                continue
            feature = rows[0][1]
            geom = feature.geometry()
            if geom.isEmpty():
                continue
            line = geom.asPolyline()
            if not line:
                continue
            features.append({"points": [[p.x(), p.y()] for p in line]})
        if not features:
            continue
        self._designs.append({
            "_link_id": str(link_id),
            "fdt": "",
            "link": str(link_id),
            "nodes": [],
            "sequence_ids": [],
            "segments": features,
            "source_crs": source_crs,
            "written": True,
            "needs_resync": False,
        })
        imported += 1

    if imported:
        self._persist_state()
        self._refresh_ui()
    return imported


def _show_startup_sync_dialog(self, layer):
    """Enforce saved-design >= DC and resolve orphan DC data before Link Design."""
    designs = getattr(self, "_designs", []) or []
    _prepare_design_identities(designs)
    if not _ensure_sync_fields(layer):
        QtWidgets.QMessageBox.warning(
            self,
            "Link 数据检查",
            "Distribution Cable 无法建立内部 Link 标识字段，无法安全判断线路属于哪个 Link。",
        )
        return False

    records = _dc_records(layer)
    known = {_stable_link_id(d, i) for i, d in enumerate(designs)}
    orphan = [key for key in records if key[0] not in known]
    if not orphan:
        self._persist_state()
        return True

    details = [f"{link_id} / S{seg}" for link_id, seg in orphan]
    text = (
        "Link 数据不一致\n\n"
        "Distribution Cable 中存在已完成设计没有的数据：\n"
        + "、".join(details[:30])
        + ("……" if len(details) > 30 else "")
        + "\n\n"
        "选择“同步到已完成设计”会保留 DC 线路并建立对应的已完成设计记录。\n"
        "选择“从 Distribution Cable 删除”会删除这些无法归属的 DC 线路。"
    )
    answer = QtWidgets.QMessageBox.question(
        self,
        "Link 数据检查",
        text,
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel,
        QtWidgets.QMessageBox.Yes,
    )
    if answer == QtWidgets.QMessageBox.Cancel:
        return False
    if answer == QtWidgets.QMessageBox.Yes:
        try:
            _sync_orphan_dc_into_designs(self, layer, orphan)
            return True
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Link 数据检查", f"同步到已完成设计失败：\n{exc}")
            return False

    if not layer.isEditable() and not layer.startEditing():
        QtWidgets.QMessageBox.warning(self, "Link 数据检查", "无法进入 Distribution Cable 编辑状态。")
        return False
    for key in orphan:
        for fid, _feature in records.get(key, []):
            layer.deleteFeature(fid)
    layer.triggerRepaint()
    return True


_v9._CoreController.add_fat = _v12_add_fat
_v9._CoreController.save_current_link = _v12_save_current_link
_v9._CoreController.write_planned_links = _v12_write_planned_links
_v9.LinkDesignMapToolV9 = _v11.LinkDesignMapToolV11


class LinkDesignDock(_v11.LinkDesignDock):
    """v12 dock: stable Link/Distribution Cable synchronization model."""

    def _check_dc_consistency_before_design(self):
        try:
            layer = _v9.context.project_layer(_v9._fresh_payload(self._controller), "Distribution Cable")
        except Exception:
            layer = None
        if layer is None:
            return True
        return _show_startup_sync_dialog(self._controller, layer)

    def showEvent(self, event):
        super().showEvent(event)
        try:
            if getattr(self, "_controller", None) is not None:
                self._check_dc_consistency_before_design()
        except Exception:
            pass

    pass
