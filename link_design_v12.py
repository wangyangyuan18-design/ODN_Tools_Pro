# -*- coding: utf-8 -*-
"""Link Design v12: written-Link FAT reallocation and cable resync."""

import copy

from qgis.PyQt import QtWidgets
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

from . import link_design_v9 as _v9
from . import link_design_v11 as _v11


_original_save_current_link = _v9._CoreController.save_current_link
_original_write_planned_links = _v9._CoreController.write_planned_links


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


def _remember_resync_source(self, index, design):
    """Persist the previous written route so it can be replaced safely later."""
    rebuilt = copy.deepcopy(design)
    return {
        "fdt": rebuilt.get("fdt", ""),
        "link": rebuilt.get("link", ""),
        "source_crs": rebuilt.get("source_crs", ""),
        "segments": copy.deepcopy(rebuilt.get("segments", [])),
    }


def _v12_save_current_link(self):
    """Save current Link while allowing FAT moves from already-written sources."""
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
            if old_design.get("written"):
                rebuilt["_resync_source"] = _remember_resync_source(self, index, old_design)
            rebuilt["written"] = False
            rebuilt["needs_resync"] = bool(old_design.get("written") or old_design.get("needs_resync"))
            self._designs[index] = rebuilt

        result = _original_save_current_link(self)
        if not result:
            self._designs = backup
            self._pending_reassignments = pending
            self._persist_state()
            self._refresh_ui()
            return False

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
    points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw_points]
    source_crs = _crs_from_authid(design.get("source_crs"))
    if source_crs is not None and source_crs != target_crs:
        transform = QgsCoordinateTransform(
            source_crs, target_crs, QgsProject.instance().transformContext()
        )
        points = [transform.transform(p) for p in points]
    return QgsGeometry.fromPolylineXY(points)


def _geometry_matches(a, b):
    try:
        if a.equals(b):
            return True
    except Exception:
        pass
    try:
        # The stored route is the exact source of subsequent rewrites. The
        # fallback is intentionally very small and is only for provider-level
        # coordinate serialization differences.
        d = a.hausdorffDistance(b)
        return d <= 1e-7
    except Exception:
        return False


def _find_matching_feature_ids(layer, design, segments, claimed):
    ids = []
    for segment in segments or []:
        expected = _segment_geometry_in_target(design, segment, layer.crs())
        if expected is None:
            continue
        for feature in layer.getFeatures():
            fid = int(feature.id())
            if fid in claimed or fid in ids:
                continue
            geom = feature.geometry()
            if not geom or geom.isEmpty():
                continue
            if _geometry_matches(geom, expected):
                ids.append(fid)
                break
    return ids


def _v12_write_planned_links(self):
    """Synchronize all saved designs without duplicating or losing manual lines."""
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

    claimed = set()
    delete_ids = set()
    add_plans = []
    reused_count = 0

    for index, design in enumerate(self._designs):
        current_segments = design.get("segments", []) or []
        if not current_segments:
            continue

        source_for_replacement = design.get("_resync_source")
        if source_for_replacement:
            source_segments = source_for_replacement.get("segments", []) or []
            source_design = {
                "source_crs": source_for_replacement.get("source_crs", ""),
            }
            source_matches = _find_matching_feature_ids(
                layer, source_design, source_segments, claimed
            )
            delete_ids.update(source_matches)
            claimed.update(source_matches)
            add_plans.append((index, current_segments))
            continue

        existing_matches = _find_matching_feature_ids(
            layer, design, current_segments, claimed
        )
        if len(existing_matches) == len(current_segments):
            claimed.update(existing_matches)
            reused_count += 1
        else:
            # A saved Link that is not yet written, or whose stored cable is
            # incomplete, should be completed from the current design. Do not
            # delete unrelated/manual Distribution Cable features.
            claimed.update(existing_matches)
            add_plans.append((index, current_segments))

    # Remove stale cable first. Additions are rolled back together with these
    # removals if any later write fails.
    deleted_features = []
    added_fids = []
    try:
        for fid in sorted(delete_ids):
            feature = layer.getFeature(fid)
            if feature.isValid():
                deleted_features.append(QgsFeature(feature))
                if not layer.deleteFeature(fid):
                    raise RuntimeError(f"无法删除旧 Distribution Cable Feature：{fid}")

        # Deduplicate new additions against any feature that remains in the
        # layer. This prevents repeated clicks from creating duplicate lines.
        for index, segments in add_plans:
            design = self._designs[index]
            for segment in segments:
                geom = _segment_geometry_in_target(design, segment, layer.crs())
                if geom is None or geom.isEmpty():
                    raise RuntimeError(
                        f"{design.get('fdt','')}/{design.get('link','')} 存在无效线路段，未完成写入。"
                    )
                duplicate = False
                for feature in layer.getFeatures():
                    fid = int(feature.id())
                    if fid in claimed or fid in added_fids:
                        continue
                    if _geometry_matches(feature.geometry(), geom):
                        duplicate = True
                        claimed.add(fid)
                        break
                if duplicate:
                    continue
                feature = QgsFeature(layer.fields())
                feature.setGeometry(geom)
                if not layer.addFeature(feature):
                    raise RuntimeError(
                        f"无法写入 Distribution Cable：{design.get('fdt','')}/{design.get('link','')}"
                    )
                added_fids.append(int(feature.id()))

        for index, design in enumerate(self._designs):
            if index < len(self._designs):
                design["written"] = True
                design.pop("_resync_source", None)
                design["needs_resync"] = False
        layer.triggerRepaint()
        self._persist_state()
    except Exception as exc:
        for fid in added_fids:
            try:
                layer.deleteFeature(fid)
            except Exception:
                pass
        for feature in deleted_features:
            try:
                layer.addFeature(feature)
            except Exception:
                pass
        layer.triggerRepaint()
        QtWidgets.QMessageBox.warning(
            self,
            "写入图层",
            f"同步 Distribution Cable 失败，已回滚本次修改：\n{exc}",
        )
        return False

    pending_count = sum(1 for d in self._designs if d.get("written"))
    self._refresh_ui()
    QtWidgets.QMessageBox.information(
        self,
        "写入图层",
        f"已同步 {pending_count} 条 Link。\n\n"
        f"复用已有线路：{reused_count} 条 Link\n"
        f"新增/替换线路：{len(add_plans)} 条 Link\n"
        "未被任何规划 Link 对应的历史手工线路保持不变。",
    )
    return True


_v9._CoreController.add_fat = _v12_add_fat
_v9._CoreController.save_current_link = _v12_save_current_link
_v9._CoreController.write_planned_links = _v12_write_planned_links
_v9.LinkDesignMapToolV9 = _v11.LinkDesignMapToolV11


class LinkDesignDock(_v11.LinkDesignDock):
    """v12 dock: v11 UI plus safe FAT reallocation and cable synchronization."""

    pass
