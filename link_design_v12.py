# -*- coding: utf-8 -*-
"""Link Design v12: Link/Distribution Cable synchronization.

Data model:
- Saved Link Design is the complete topology/planning set.
- Distribution Cable is the materialized geometry subset of that set.
- Saved design records carry stable internal Link/Segment identities; display
  names (FDT/FAT names) are never used as primary identity keys.
- DC may contain only a subset of saved designs, but may never contain
  unidentified/orphan planned segments while Link Design is active.
"""

import copy

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QSettings
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QVariant,
)

from . import link_design_v9 as _v9
from . import link_design_v11 as _v11


_original_save_current_link = _v9._CoreController.save_current_link
_original_write_planned_links = _v9._CoreController.write_planned_links

# Internal fields on Distribution Cable. They are deliberately independent
# from FDT/FAT display names so renaming equipment never breaks associations.
LINK_ID_FIELD = "_ODN_LINK_ID"
SEGMENT_ID_FIELD = "_ODN_SEGMENT"
LINK_FDT_FIELD = "_ODN_FDT"
LINK_NAME_FIELD = "_ODN_LINK"


def _ensure_sync_fields(layer):
    """Ensure internal synchronization fields exist without disturbing user fields."""
    additions = []
    existing = {f.name() for f in layer.fields()}
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
        return bool(layer.dataProvider().addAttributes(additions)) and bool(layer.updateFields())
    except Exception:
        return False


def _stable_link_id(design, index=None):
    """Return a persistent internal identity, never dependent on display names.

    Older designs may not yet have _link_id. In that case derive a deterministic
    fallback from the saved index and sequence-node IDs and immediately persist
    it into the in-memory design. Display names are intentionally excluded.
    """
    existing = design.get("_link_id")
    if existing:
        return str(existing)
    nodes = design.get("sequence_ids") or design.get("nodes") or []
    parts = []
    for item in nodes:
        try:
            parts.append(f"{item[0]}:{int(item[1])}")
        except Exception:
            parts.append(str(item))
    basis = "|".join(parts)
    # Keep legacy designs stable within this project without using FDT/FAT names.
    if basis:
        link_id = f"LEGACY_{abs(hash(basis)) & 0xFFFFFFFF:08X}"
    else:
        link_id = f"LEGACY_INDEX_{int(index or 0)}"
    design["_link_id"] = link_id
    return link_id


def _segment_identity(design_index, segment_index):
    return f"{_stable_link_id(design_index[1], design_index[0])}/S{int(segment_index) + 1}"


def _segment_geometry_in_target(design, segment, target_crs):
    raw_points = segment.get("points", [])
    if len(raw_points) < 2:
        return None
    points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw_points]
    source_crs = _crs_from_authid(design.get("source_crs"))
    if source_crs is not None and source_crs != target_crs:
        transform = QgsCoordinateTransform(
            source_crs,
            target_crs,
            QgsProject.instance().transformContext(),
        )
        points = [transform.transform(p) for p in points]
    return QgsGeometry.fromPolylineXY(points)


def _crs_from_authid(authid):
    if not authid:
        return None
    try:
        crs = QgsCoordinateReferenceSystem(str(authid))
        return crs if crs.isValid() else None
    except Exception:
        return None


def _add_sync_attrs(feature, link_id, segment_index, design):
    """Write stable identity attributes to a DC feature."""
    fields = feature.fields()
    for name, value in (
        (LINK_ID_FIELD, link_id),
        (SEGMENT_ID_FIELD, int(segment_index) + 1),
        # These are informational only, never identity keys.
        (LINK_FDT_FIELD, str(design.get("fdt", ""))),
        (LINK_NAME_FIELD, str(design.get("link", ""))),
    ):
        idx = fields.indexOf(name)
        if idx >= 0:
            feature.setAttribute(idx, value)


def _dc_records(layer):
    """Return {(link_id, segment_no): [(fid, feature), ...]} for synchronized DC."""
    result = {}
    if layer is None:
        return result
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


def _v12_add_fat(self, info):
    """Allow FAT moves from another Link, including a Link already written."""
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
    """Save current Link while preserving a stable internal Link identity."""
    pending = dict(getattr(self, "_pending_reassignments", {}) or {})
    backup = copy.deepcopy(self._designs)

    try:
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

        # Ensure the newly saved design also has a stable internal identity.
        try:
            if self._editing_index is not None and 0 <= int(self._editing_index) < len(self._designs):
                _stable_link_id(self._designs[int(self._editing_index)], int(self._editing_index))
        except Exception:
            pass
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


def _prepare_design_identities(designs):
    changed = False
    for index, design in enumerate(designs or []):
        before = design.get("_link_id")
        _stable_link_id(design, index)
        changed = changed or before != design.get("_link_id")
    return changed


def _sync_dc_from_designs(self, layer):
    """Materialize the current saved designs into DC using stable identities."""
    if not _ensure_sync_fields(layer):
        raise RuntimeError("Distribution Cable 无法建立 Link 同步字段。")

    designs = list(getattr(self, "_designs", []) or [])
    _prepare_design_identities(designs)

    records = _dc_records(layer)
    deleted = 0
    added = 0
    updated = 0

    for design_index, design in enumerate(designs):
        link_id = _stable_link_id(design, design_index)
        segments = design.get("segments", []) or []
        wanted = {}
        for seg_index, segment in enumerate(segments):
            geom = _segment_geometry_in_target(design, segment, layer.crs())
            if geom is None or geom.isEmpty():
                raise RuntimeError(
                    f"{design.get('fdt','')}/{design.get('link','')} Segment {seg_index + 1} 几何无效。"
                )
            wanted[(link_id, seg_index + 1)] = (seg_index, geom)

        # Delete duplicate/stale segments for this Link; manual unrelated DC
        # features are untouched because they have different/empty identities.
        existing_keys = [key for key in records if key[0] == link_id]
        for key in existing_keys:
            items = records.get(key, [])
            wanted_item = wanted.get(key)
            keep = None
            if wanted_item is not None:
                target_geom = wanted_item[1]
                for fid, feature in items:
                    if keep is None and _same_geometry(feature.geometry(), target_geom):
                        keep = fid
                    else:
                        if layer.deleteFeature(fid):
                            deleted += 1
            else:
                for fid, _feature in items:
                    if layer.deleteFeature(fid):
                        deleted += 1

            if keep is not None:
                # Refresh informational names without using them for identity.
                feature = layer.getFeature(keep)
                _add_sync_attrs(feature, link_id, int(key[1]) - 1, design)
                if layer.changeAttributeValues({keep: {
                    layer.fields().indexOf(LINK_FDT_FIELD): feature.attribute(LINK_FDT_FIELD),
                    layer.fields().indexOf(LINK_NAME_FIELD): feature.attribute(LINK_NAME_FIELD),
                }}):
                    updated += 1
                continue

        # Add missing segments from the current Link design.
        for key, (seg_index, geom) in wanted.items():
            items = _dc_records(layer).get(key, [])
            if items:
                # Existing geometry may have been changed manually; the Link
                # design is authoritative when this explicit write is requested.
                matching = None
                for fid, feature in items:
                    if _same_geometry(feature.geometry(), geom):
                        matching = fid
                        break
                if matching is not None:
                    continue
                for fid, _feature in items:
                    if layer.deleteFeature(fid):
                        deleted += 1

            feature = QgsFeature(layer.fields())
            feature.setGeometry(geom)
            _add_sync_attrs(feature, link_id, seg_index, design)
            if not layer.addFeature(feature):
                raise RuntimeError(
                    f"无法写入 Distribution Cable：{design.get('fdt','')}/{design.get('link','')} Segment {seg_index + 1}"
                )
            added += 1

        design["written"] = True
        design["needs_resync"] = False
        design.pop("_resync_source", None)

    layer.triggerRepaint()
    self._persist_state()
    return {"deleted": deleted, "added": added, "updated": updated}


def _collect_orphan_dc_records(layer, designs):
    known = {_stable_link_id(d, i) for i, d in enumerate(designs or [])}
    records = _dc_records(layer)
    orphan = []
    for key, items in records.items():
        if key[0] not in known:
            orphan.extend(items)
    return orphan


def _show_startup_sync_dialog(self, layer):
    """Enforce saved-design >= DC before continuing into Link Design."""
    designs = getattr(self, "_designs", []) or []
    _prepare_design_identities(designs)
    if not _ensure_sync_fields(layer):
        # Existing DC without internal identity cannot safely be interpreted
        # automatically. Do not silently delete user data.
        QtWidgets.QMessageBox.warning(
            self,
            "Link 数据检查",
            "Distribution Cable 缺少内部 Link 标识字段，无法安全判断线路属于哪个 Link。\n\n"
            "请先执行一次“确定并写入图层”建立关联。",
        )
        return False

    records = _dc_records(layer)
    known = {_stable_link_id(d, i) for i, d in enumerate(designs)}
    dc_keys = set(records)
    orphan = [key for key in dc_keys if key[0] not in known]

    if not orphan:
        self._persist_state()
        return True

    details = []
    for link_id, seg in orphan:
        details.append(f"{link_id} / S{seg}")
    text = (
        "Link 数据不一致\n\n"
        "Distribution Cable 中存在未出现在已完成设计的数据：\n"
        + "、".join(details[:30])
        + ("……" if len(details) > 30 else "")
        + "\n\n"
        "请选择：\n"
        "[同步到已完成设计] 会保留这些 DC 线路并建立拓扑记录。\n"
        "[从 Distribution Cable 删除] 会删除这些无法归属的 DC 线路。"
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

    # This migration path is deliberately conservative. A DC orphan can only
    # be synced into a new saved Link if the project already has enough context
    # to reconstruct it; otherwise deletion is the only unambiguous operation.
    if answer == QtWidgets.QMessageBox.No:
        if not layer.isEditable() and not layer.startEditing():
            QtWidgets.QMessageBox.warning(self, "Link 数据检查", "无法进入 Distribution Cable 编辑状态。")
            return False
        for key in orphan:
            for fid, _feature in records.get(key, []):
                layer.deleteFeature(fid)
        layer.triggerRepaint()
        return True

    QtWidgets.QMessageBox.warning(
        self,
        "Link 数据检查",
        "检测到 DC 中存在无法与现有已完成 Link 建立稳定身份的线路。\n\n"
        "为避免误把线路归入错误的 FDT/Link，本次没有自动猜测归属。"
        "请先从已有 Link 重新写入，建立内部关联后再继续。",
    )
    return False


def _v12_write_planned_links(self):
    """Synchronize the complete saved-design set into DC by Link/Segment ID."""
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
        summary = _sync_dc_from_designs(self, layer)
    except Exception as exc:
        QtWidgets.QMessageBox.warning(self, "写入图层", f"同步 Distribution Cable 失败：\n{exc}")
        return False

    self._refresh_ui()
    QtWidgets.QMessageBox.information(
        self,
        "写入图层",
        "已完成设计与 Distribution Cable 同步完成。\n\n"
        f"新增：{summary['added']}\n"
        f"替换/删除旧线路：{summary['deleted']}\n"
        f"属性更新：{summary['updated']}\n\n"
        "未被 Link 识别的手工线路不会因为本次写入而被删除。",
    )
    return True


_v9._CoreController.add_fat = _v12_add_fat
_v9._CoreController.save_current_link = _v12_save_current_link
_v9._CoreController.write_planned_links = _v12_write_planned_links
_v9.LinkDesignMapToolV9 = _v11.LinkDesignMapToolV11


class LinkDesignDock(_v11.LinkDesignDock):
    """v12 dock: Link/Distribution Cable synchronization model."""

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
