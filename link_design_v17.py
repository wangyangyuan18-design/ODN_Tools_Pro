# -*- coding: utf-8 -*-
"""Link Design v17: authoritative routes plus coordinated cable/FAT offset output.

Design rules:
- Completed Link Design routes are rebuilt from the same Pole Edge route engine
  used by the known-good Link Design baseline.
- Offset calculations operate on a deep copy only.
- The saved controller designs retain the un-offset Pole Edge route.
- Distribution Cable receives the optional offset geometry.
- FAT output points are recalculated from the owning Link after offset: a
  straight-through FAT lands on the Link's parallel offset; a large-corner
  FAT lands on the generated corner/control-distance bend. Cable endpoints are
  made coincident with the moved FAT.
- "FAT归杆" is an explicit manual action. It moves FAT points to their
  nearest configured pole within the project FAT-to-pole limit, keeps every
  Link's topology unchanged, and reroutes only the completed Link segments
  whose endpoints contain moved FATs. The updated routes are synchronized to
  Distribution Cable in the same manual action.
"""

import copy

from qgis.PyQt import QtWidgets
from qgis.core import (
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    Qgis,
)

from . import link_design_v16 as _v16
from . import link_design_v9 as _v9
from . import link_design_v12 as _v12
from . import odn_project_context as context
from . import cable_offset_layout_v6 as _offset
from .change_detection_adapter import save_snapshot


_ORIGINAL_SAVE_CURRENT_LINK = _v9._CoreController.save_current_link
_ORIGINAL_START_DESIGN = _v9._CoreController.start_design


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Link Design", level)
    except Exception:
        pass


def _restore_authoritative_routes(host, persist=True):
    """Rebuild completed Link geometry exactly from FDT/FAT sequence via Pole Edge."""
    controller = host._controller
    engine = controller._engine or controller._prepare_engine()
    if engine is None:
        raise RuntimeError("无法建立当前 Pole Edge 路由引擎，不能恢复已完成设计。")
    controller._engine = engine
    try:
        payload = controller._v9_payload()
    except Exception:
        payload = context.current_payload() or {}
    edge_layer = context.project_layer(payload, "Pole Edge")
    if edge_layer is None:
        raise RuntimeError("当前项目没有绑定 Pole Edge 图层。")
    edge_authid = edge_layer.crs().authid()

    restored_links = 0
    restored_segments = 0
    failed_links = 0
    changed = False
    for design_index, design in enumerate(controller._designs or []):
        seq_ids = design.get("sequence_ids", []) or []
        segments = design.get("segments", []) or []
        if len(seq_ids) < 2 or len(segments) != len(seq_ids) - 1:
            continue
        rebuilt_segments = []
        link_failed = False
        for segment_index, (old_segment, first, second) in enumerate(
            zip(segments, seq_ids[:-1], seq_ids[1:])
        ):
            try:
                route = engine.route(str(first[0]), int(first[1]), str(second[0]), int(second[1]))
            except Exception as exc:
                _log(
                    f"[route-restore-fail] design={design_index}; segment={segment_index}; "
                    f"{first[1]}->{second[1]}; reason={type(exc).__name__}:{exc}",
                    Qgis.Warning,
                )
                link_failed = True
                break
            if not route or not route.get("edge_sequence"):
                _log(
                    f"[route-restore-fail] design={design_index}; segment={segment_index}; "
                    f"{first[1]}->{second[1]}; reason=无法沿 Pole Edge 建立完整路径",
                    Qgis.Warning,
                )
                link_failed = True
                break
            rebuilt = dict(old_segment)
            rebuilt["from"] = route.get("from_label", old_segment.get("from", first[1]))
            rebuilt["to"] = route.get("to_label", old_segment.get("to", second[1]))
            rebuilt["distance"] = round(float(route["distance"]), 3)
            rebuilt["pole_edge_distance"] = round(float(route.get("pole_edge_distance", route["distance"])), 3)
            rebuilt["edge_sequence"] = list(route["edge_sequence"])
            rebuilt["edge_count"] = len(route["edge_sequence"])
            rebuilt["points"] = [[float(p.x()), float(p.y())] for p in route["points"]]
            rebuilt_segments.append(rebuilt)
        if link_failed:
            failed_links += 1
            continue
        new_length = round(sum(float(s.get("distance", 0.0) or 0.0) for s in rebuilt_segments), 3)
        old_length = float(design.get("length", 0.0) or 0.0)
        old_segments = design.get("segments", []) or []
        if rebuilt_segments != old_segments or abs(new_length - old_length) > 0.001:
            changed = True
        design["segments"] = rebuilt_segments
        design["length"] = new_length
        design["source_crs"] = edge_authid
        restored_links += 1
        restored_segments += len(rebuilt_segments)
        design["needs_resync"] = True
        design["written"] = False

    if changed and persist:
        controller._persist_state()
    _log(
        f"[route-restore] links={restored_links}; segments={restored_segments}; "
        f"failed_links={failed_links}; changed={int(changed)}; crs={edge_authid}"
    )
    return restored_links, restored_segments, failed_links, changed


def _v17_start_design(self):
    host = getattr(self, "host", None)
    if host is not None:
        try:
            _restore_authoritative_routes(host, persist=True)
        except Exception as exc:
            self.status.setText(f"状态：无法恢复 Pole Edge 规划路线：{exc}")
            QtWidgets.QMessageBox.warning(
                host, "链路设计",
                f"当前已完成 Link 路线无法按 Pole Edge 恢复，已阻止继续规划：\n{exc}",
            )
            return False
    return _ORIGINAL_START_DESIGN(self)


_v9._CoreController.start_design = _v17_start_design


def _v17_save_current_link(self):
    result = _ORIGINAL_SAVE_CURRENT_LINK(self)
    if not result:
        return result
    host = getattr(self, "host", None)
    if host is not None:
        try:
            _restore_authoritative_routes(host, persist=True)
        except Exception as exc:
            _log(f"[route-restore-save-fail] {type(exc).__name__}:{exc}", Qgis.Critical)
            return False
    return True


_v9._CoreController.save_current_link = _v17_save_current_link


def _distance_m(distance_area, a, b):
    try:
        return float(distance_area.measureLine(QgsPointXY(a), QgsPointXY(b)))
    except Exception:
        return float("inf")


def _pole_candidates_in_fat_crs(fat_layer, pole_layers):
    """Return all pole points in FAT CRS, carrying source feature identity."""
    candidates = []
    fat_crs = fat_layer.crs()
    transform_cache = {}
    context_obj = QgsProject.instance().transformContext()
    for pole_layer in pole_layers:
        if pole_layer is None:
            continue
        source_crs = pole_layer.crs()
        transform = None
        if source_crs != fat_crs:
            key = (source_crs.authid(), fat_crs.authid())
            transform = transform_cache.get(key)
            if transform is None:
                try:
                    transform = QgsCoordinateTransform(source_crs, fat_crs, context_obj)
                except Exception as exc:
                    _log(
                        f"[fat-return-pole-crs-fail] layer={pole_layer.name()}; "
                        f"reason={type(exc).__name__}:{exc}",
                        Qgis.Warning,
                    )
                    continue
                transform_cache[key] = transform
        for feature in pole_layer.getFeatures():
            try:
                geometry = feature.geometry()
                if geometry is None or geometry.isEmpty():
                    continue
                point = geometry.centroid().asPoint()
                if transform is not None:
                    point = transform.transform(point)
                candidates.append(
                    {
                        "layer": pole_layer.name(),
                        "feature_id": int(feature.id()),
                        "point": QgsPointXY(point),
                    }
                )
            except Exception as exc:
                _log(
                    f"[fat-return-pole-read-fail] layer={pole_layer.name()}; "
                    f"fid={feature.id()}; reason={type(exc).__name__}:{exc}",
                    Qgis.Warning,
                )
    return candidates


def _nearest_pole_for_fat(fat_feature, fat_layer, candidates, distance_area, max_distance):
    try:
        geometry = fat_feature.geometry()
        if geometry is None or geometry.isEmpty():
            return None, None
        fat_point = QgsPointXY(geometry.centroid().asPoint())
    except Exception:
        return None, None
    best = None
    for candidate in candidates:
        distance = _distance_m(distance_area, fat_point, candidate["point"])
        if best is None or distance < best[0]:
            best = (distance, candidate)
    if best is None:
        return None, None
    if best[0] > max_distance + 1e-9:
        return None, best[0]
    return best[1], best[0]


def _fat_ids_in_designs(designs):
    result = set()
    for design in designs or []:
        for item in design.get("sequence_ids", []) or []:
            try:
                if len(item) >= 2 and str(item[0]).upper() == "FAT":
                    result.add(int(item[1]))
            except (TypeError, ValueError, IndexError):
                continue
    return result


def _rebuild_designs_for_moved_fats(controller, moved_fids):
    """Re-route only completed Link segments incident to moved FATs.

    Sequence/topology is deliberately untouched. The routing source is always
    the current Pole Edge/FDT/FAT state after the manual FAT move.
    """
    moved_fids = {int(fid) for fid in moved_fids}
    engine = controller._prepare_engine()
    if engine is None:
        raise RuntimeError("FAT 归杆后无法建立当前 Pole Edge 路由引擎。")
    controller._engine = engine
    payload = controller._v9_payload()
    edge_layer = context.project_layer(payload, "Pole Edge")
    if edge_layer is None:
        raise RuntimeError("当前项目没有绑定 Pole Edge 图层。")
    edge_authid = edge_layer.crs().authid()

    affected_links = 0
    rerouted_segments = 0
    failed_links = []
    for design_index, design in enumerate(controller._designs or []):
        seq_ids = design.get("sequence_ids", []) or []
        segments = design.get("segments", []) or []
        if len(seq_ids) < 2 or len(segments) != len(seq_ids) - 1:
            continue
        contains_moved_fat = any(
            len(item) >= 2 and str(item[0]).upper() == "FAT" and int(item[1]) in moved_fids
            for item in seq_ids
            if len(item) >= 2
        )
        if not contains_moved_fat:
            continue

        rebuilt_segments = list(segments)
        link_failed = False
        changed_segment_count = 0
        for segment_index, (old_segment, first, second) in enumerate(
            zip(segments, seq_ids[:-1], seq_ids[1:])
        ):
            first_moved = str(first[0]).upper() == "FAT" and int(first[1]) in moved_fids
            second_moved = str(second[0]).upper() == "FAT" and int(second[1]) in moved_fids
            if not (first_moved or second_moved):
                continue
            try:
                route = engine.route(str(first[0]), int(first[1]), str(second[0]), int(second[1]))
            except Exception as exc:
                _log(
                    f"[fat-return-route-fail] design={design_index}; segment={segment_index}; "
                    f"{first[1]}->{second[1]}; reason={type(exc).__name__}:{exc}",
                    Qgis.Warning,
                )
                link_failed = True
                break
            if not route or not route.get("edge_sequence"):
                _log(
                    f"[fat-return-route-fail] design={design_index}; segment={segment_index}; "
                    f"{first[1]}->{second[1]}; reason=无法沿最新 Pole Edge 建立完整路径",
                    Qgis.Warning,
                )
                link_failed = True
                break
            rebuilt = dict(old_segment)
            rebuilt["from"] = route.get("from_label", old_segment.get("from", first[1]))
            rebuilt["to"] = route.get("to_label", old_segment.get("to", second[1]))
            rebuilt["distance"] = round(float(route["distance"]), 3)
            rebuilt["pole_edge_distance"] = round(float(route.get("pole_edge_distance", route["distance"])), 3)
            rebuilt["edge_sequence"] = list(route["edge_sequence"])
            rebuilt["edge_count"] = len(route["edge_sequence"])
            rebuilt["points"] = [[float(p.x()), float(p.y())] for p in route["points"]]
            rebuilt_segments[segment_index] = rebuilt
            changed_segment_count += 1

        if link_failed:
            failed_links.append(design_index)
            continue
        if changed_segment_count:
            design["segments"] = rebuilt_segments
            design["length"] = round(
                sum(float(s.get("distance", 0.0) or 0.0) for s in rebuilt_segments),
                3,
            )
            design["source_crs"] = edge_authid
            design["written"] = False
            design["needs_resync"] = True
            affected_links += 1
            rerouted_segments += changed_segment_count
            _log(
                f"[fat-return-link] design={design_index}; rerouted_segments={changed_segment_count}; "
                f"length={design['length']:.3f}m"
            )

    if failed_links:
        raise RuntimeError(
            "部分已完成 Link 无法按最新 Pole Edge 重建路径："
            + ", ".join(str(i + 1) for i in failed_links[:20])
            + ("……" if len(failed_links) > 20 else "")
        )
    return affected_links, rerouted_segments


def _install_fat_return_button(dock):
    """Insert FAT归杆 into the existing summary row, with no extra row."""
    if getattr(dock, "_fat_return_button", None) is not None:
        return
    target = getattr(dock, "summary_fat_label", None)
    root = dock.widget() if hasattr(dock, "widget") else None
    root = root or dock
    root_layout = root.layout() if root is not None else None
    if target is None or root_layout is None:
        return

    def find_row_layout(layout):
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            if widget is target:
                return layout
            child = item.layout()
            if child is not None:
                found = find_row_layout(child)
                if found is not None:
                    return found
        return None

    row = find_row_layout(root_layout)
    if row is None:
        return

    button = QtWidgets.QPushButton("FAT归杆")
    button.setMinimumHeight(22)
    button.setToolTip(
        "手动将 FAT 移回所属 POLE。\n"
        "按当前 FAT 与 Existing Pole/New Pole 的实际距离确定所属杆；\n"
        "不改变已完成 Link 的拓扑，只更新包含这些 FAT 的实际规划路径，并同步 Distribution Cable。"
    )
    button.clicked.connect(dock._fat_return_to_poles)
    stretch_index = row.count()
    for index in range(row.count()):
        item = row.itemAt(index)
        if item.spacerItem() is not None:
            stretch_index = index
            break
    row.insertWidget(stretch_index, button)
    dock._fat_return_button = button


class LinkDesignDock(_v16.LinkDesignDock):
    """Active dock with authoritative routing and isolated output offset."""

    def __init__(self, iface, parent=None):
        self._fat_return_button = None
        super().__init__(iface, parent)
        _install_fat_return_button(self)

    def _check_dc_consistency_before_design(self, layer=None):
        return True

    def showEvent(self, event):
        try:
            _restore_authoritative_routes(self, persist=True)
        except Exception as exc:
            _log(f"[route-restore-show-fail] {type(exc).__name__}:{exc}", Qgis.Critical)
        super().showEvent(event)

    def _fat_return_to_poles(self):
        controller = self._controller
        if getattr(controller, "_draw_active", False):
            self.info.setText("请先保存或退出当前规划，再执行 FAT 归杆。")
            return
        payload = controller._v9_payload()
        fat_layer = context.project_layer(payload, "FAT")
        existing_pole_layer = context.project_layer(payload, "Existing Pole")
        new_pole_layer = context.project_layer(payload, "New Pole")
        edge_layer = context.project_layer(payload, "Pole Edge")
        dc_layer = context.project_layer(payload, "Distribution Cable")
        if fat_layer is None:
            QtWidgets.QMessageBox.warning(self, "FAT归杆", "当前项目没有绑定 FAT 图层。")
            return
        pole_layers = [layer for layer in (existing_pole_layer, new_pole_layer) if layer is not None]
        if not pole_layers:
            QtWidgets.QMessageBox.warning(
                self,
                "FAT归杆",
                "当前项目没有绑定 Existing Pole / New Pole 图层，无法确定 FAT 所属杆。",
            )
            return
        if edge_layer is None:
            QtWidgets.QMessageBox.warning(self, "FAT归杆", "当前项目没有绑定 Pole Edge 图层。")
            return

        fat_limit = 3.0
        try:
            fat_limit = float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0))
        except (TypeError, ValueError):
            fat_limit = 3.0
        fat_limit = max(0.01, fat_limit)

        all_fats = list(fat_layer.getFeatures())
        if not all_fats:
            self.info.setText("当前 FAT 图层没有要处理的 FAT。")
            return

        candidates = _pole_candidates_in_fat_crs(fat_layer, pole_layers)
        if not candidates:
            QtWidgets.QMessageBox.warning(self, "FAT归杆", "没有读取到有效的 Existing Pole / New Pole 点。")
            return

        distance_area = QgsDistanceArea()
        distance_area.setSourceCrs(fat_layer.crs(), QgsProject.instance().transformContext())
        try:
            distance_area.setEllipsoid("WGS84")
        except Exception:
            pass

        backup_designs = copy.deepcopy(controller._designs)
        moved = []
        already_on_pole = 0
        skipped = 0
        skip_details = []
        planned_updates = []
        try:
            for fat_feature in all_fats:
                fid = int(fat_feature.id())
                pole, distance = _nearest_pole_for_fat(
                    fat_feature,
                    fat_layer,
                    candidates,
                    distance_area,
                    fat_limit,
                )
                if pole is None:
                    skipped += 1
                    reason = (
                        f"距离最近 POLE {distance:.3f}m > {fat_limit:.3f}m"
                        if distance is not None
                        else "无法确定最近 POLE"
                    )
                    skip_details.append((fid, reason))
                    _log(f"[fat-return-skip] fat={fid}; reason={reason}", Qgis.Warning)
                    continue
                current_point = QgsPointXY(fat_feature.geometry().centroid().asPoint())
                target_point = QgsPointXY(pole["point"])
                move_distance = _distance_m(distance_area, current_point, target_point)
                if move_distance <= 1e-7:
                    already_on_pole += 1
                    continue
                planned_updates.append((fat_feature, target_point, pole, distance))
                moved.append(fid)

            fat_command_started = False
            dc_command_started = False
            if planned_updates:
                if not fat_layer.isEditable() and not fat_layer.startEditing():
                    raise RuntimeError("无法进入 FAT 编辑状态。")
                fat_layer.beginEditCommand("FAT归杆")
                fat_command_started = True
                for feature, target_point, _pole, _distance in planned_updates:
                    feature.setGeometry(QgsGeometry.fromPointXY(target_point))
                    if not fat_layer.updateFeature(feature):
                        raise RuntimeError(f"无法更新 FAT feature {feature.id()} 的归杆位置。")

                # The route engine caches point attachment. Rebuild it after FAT
                # geometry changes so routing uses the new physical positions.
                controller._engine = None
                affected_links, rerouted_segments = _rebuild_designs_for_moved_fats(controller, moved)

                if dc_layer is not None and affected_links:
                    if not dc_layer.isEditable() and not dc_layer.startEditing():
                        raise RuntimeError("无法进入 Distribution Cable 编辑状态，不能同步归杆后的完成规划路径。")
                    dc_layer.beginEditCommand("FAT归杆同步完成规划路径")
                    dc_command_started = True
                    for design_index, design in enumerate(controller._designs):
                        if not design.get("needs_resync"):
                            continue
                        _v12._write_design_to_dc(controller, dc_layer, design_index, design)

                if dc_command_started:
                    dc_layer.endEditCommand()
                    dc_command_started = False
                if fat_command_started:
                    fat_layer.endEditCommand()
                    fat_command_started = False
            else:
                affected_links = 0
                rerouted_segments = 0

            if moved:
                controller._persist_state()
                save_snapshot(controller)

            if skipped:
                self.info.setText(
                    f"FAT归杆完成：移动 {len(moved)} 个；已在杆上 {already_on_pole} 个；"
                    f"跳过 {skipped} 个；受影响 Link {affected_links} 条。"
                )
            else:
                self.info.setText(
                    f"FAT归杆完成：移动 {len(moved)} 个；已在杆上 {already_on_pole} 个；"
                    f"受影响 Link {affected_links} 条，更新路径 {rerouted_segments} 段。"
                )
            fat_layer.triggerRepaint()
            if dc_layer is not None:
                dc_layer.triggerRepaint()
            self.refresh_from_core()

            details = (
                f"FAT 总数：{len(all_fats)}\n"
                f"移动到所属 POLE：{len(moved)}\n"
                f"原本已在 POLE：{already_on_pole}\n"
                f"跳过：{skipped}\n"
                f"受影响已完成 Link：{affected_links}\n"
                f"更新规划路径：{rerouted_segments} 段\n\n"
                "Link 的 FDT→FAT/FAT→FAT 拓扑与 FAT 顺序保持不变。\n"
                "同杆 FDT/FAT 按现有规则仍按 2m 计入逻辑路径。"
            )
            if skipped:
                details += "\n\n被跳过的 FAT：" + "、".join(str(fid) for fid, _ in skip_details[:30])
                if len(skip_details) > 30:
                    details += f"……共 {len(skip_details)} 个"
                details += f"\n详细原因已写入 QGIS 日志。"
            QtWidgets.QMessageBox.information(self, "FAT归杆", details)

        except Exception as exc:
            if dc_command_started:
                try:
                    dc_layer.destroyEditCommand()
                except Exception:
                    pass
            if fat_command_started:
                try:
                    fat_layer.destroyEditCommand()
                except Exception:
                    pass
            try:
                controller._designs = backup_designs
                controller._engine = None
                controller._persist_state()
            except Exception:
                pass
            try:
                # Restore only the FAT geometries we changed in this operation.
                for feature, _target_point, _pole, _distance in planned_updates:
                    original = fat_layer.getFeature(int(feature.id()))
                    if original.isValid():
                        original.setGeometry(feature.geometry())
            except Exception:
                pass
            _log(f"[fat-return-fail] {type(exc).__name__}:{exc}", Qgis.Critical)
            QtWidgets.QMessageBox.warning(
                self,
                "FAT归杆",
                f"FAT归杆失败，本次已恢复已完成规划数据：\n{exc}",
            )

    def _offset_and_write(self):
        controller = self._controller
        if controller._draw_active:
            self.info.setText("请先保存或退出当前规划，再执行偏移并写入图层。")
            return
        if not controller._designs:
            self.info.setText("当前没有已完成的 Link 可偏移写入。")
            return

        spacing_value, control_distance_value = _offset.get_settings()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("偏移并写入图层")
        dialog.setModal(True)
        form = QtWidgets.QFormLayout(dialog)
        form.setContentsMargins(12, 12, 12, 12)
        form.setSpacing(8)

        spacing = QtWidgets.QDoubleSpinBox(dialog)
        spacing.setRange(0.01, 20.0)
        spacing.setDecimals(2)
        spacing.setSingleStep(0.10)
        spacing.setValue(spacing_value)
        spacing.setSuffix(" m")
        form.addRow("偏移间距", spacing)

        control_distance = QtWidgets.QDoubleSpinBox(dialog)
        control_distance.setRange(0.01, 5.0)
        control_distance.setDecimals(2)
        control_distance.setSingleStep(0.05)
        control_distance.setValue(control_distance_value)
        control_distance.setSuffix(" m")
        control_distance.setToolTip(
            "拐角控制距离：Pole Edge 顶点到控制线 A 的距离。\n"
            "偏移拐点只能落在该控制线附近；实际角度由偏移量与该距离自然计算。"
        )
        form.addRow("拐角控制距离", control_distance)

        hint = QtWidgets.QLabel(
            "角度不再作为参数。\n"
            "控制距离 0.30 m 时：偏移 0.50 m ≈ 59.0°，"
            "偏移 1.00 m ≈ 73.3°，偏移 1.50 m ≈ 78.7°。\n"
            "FAT 落点：直行跟随 Link 偏移线；大拐角落在实际偏移拐点。"
        )
        hint.setWordWrap(True)
        form.addRow("规则", hint)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        form.addRow(buttons)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return

        spacing_value = float(spacing.value())
        control_distance_value = float(control_distance.value())
        _offset.save_settings(spacing_value, control_distance_value)

        payload = controller._v9_payload()
        dc_layer = context.project_layer(payload, "Distribution Cable")
        edge_layer = context.project_layer(payload, "Pole Edge")
        fat_layer = context.project_layer(payload, "FAT")
        if dc_layer is None or edge_layer is None or fat_layer is None:
            QtWidgets.QMessageBox.warning(
                self, "偏移并写入图层",
                "当前项目缺少 Distribution Cable、Pole Edge 或 FAT 图层。",
            )
            return
        if not dc_layer.isEditable() and not dc_layer.startEditing():
            QtWidgets.QMessageBox.warning(
                self, "偏移并写入图层", "无法进入 Distribution Cable 编辑状态。"
            )
            return
        if not fat_layer.isEditable() and not fat_layer.startEditing():
            QtWidgets.QMessageBox.warning(
                self, "偏移并写入图层", "无法进入 FAT 编辑状态，因此没有执行本次偏移写入。"
            )
            return

        try:
            _restore_authoritative_routes(self, persist=True)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self, "偏移并写入图层",
                f"已完成设计无法恢复为 Pole Edge 原始规划路线，因此停止写入：\n{exc}",
            )
            return

        planned_designs = copy.deepcopy(controller._designs)
        try:
            _v12._prepare_design_identities(planned_designs)
            fat_limit = 3.0
            try:
                fat_limit = float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0))
            except (TypeError, ValueError):
                fat_limit = 3.0
            summary = _offset.apply_explicit_layout_to_designs(
                planned_designs,
                dc_layer,
                edge_layer,
                spacing=spacing_value,
                control_distance_m=control_distance_value,
                fat_layer=fat_layer,
                fat_max_distance_m=fat_limit,
            )

            # FAT geometry is not committed until all copied Cable geometries
            # have successfully passed validation and been written.
            for index, design in enumerate(planned_designs):
                if not (design.get("segments") or []):
                    continue
                _v12._write_design_to_dc(controller, dc_layer, index, design)

            fat_written = _offset.commit_fat_landing_points(fat_layer, summary)

            for design in controller._designs:
                design["written"] = True
                design["needs_resync"] = False
                design.pop("_resync_source", None)

            dc_layer.triggerRepaint()
            fat_layer.triggerRepaint()
            controller._persist_state()
            save_snapshot(controller)
            self.refresh_from_core()
            self.info.setText(
                f"已偏移并写入全部 Link：{len(controller._designs)} 条；"
                f"拐角控制距离 {control_distance_value:.2f} m，"
                f"偏移间距 {spacing_value:.2f} m；"
                f"Cable 实际偏移 {summary.get('changed_designs', 0)} 条 Link；"
                f"FAT 落点更新 {fat_written} 个。"
            )
            if not summary.get("changed_designs") and not fat_written:
                QtWidgets.QMessageBox.information(
                    self,
                    "偏移并写入图层",
                    "已完成写入，但本次没有产生需要偏移的重叠 Pole Edge 段。\n\n"
                    "非重叠线路保持原规划位置，FAT 也保持原落点。",
                )
            elif summary.get("fat_skipped"):
                QtWidgets.QMessageBox.information(
                    self,
                    "偏移并写入图层",
                    f"Cable 偏移和 FAT 落点更新已完成。\n\n"
                    f"FAT 总数：{summary.get('fat_total', 0)}\n"
                    f"已更新：{fat_written}\n"
                    f"直行：{summary.get('fat_straight', 0)}\n"
                    f"大拐角：{summary.get('fat_corner', 0)}\n"
                    f"跳过：{summary.get('fat_skipped', 0)}\n\n"
                    "被跳过的 FAT 不会被强制移动，并已在日志中记录原因。",
                )
        except Exception as exc:
            _log(f"[offset-write-fail] {type(exc).__name__}: {exc}", Qgis.Critical)
            QtWidgets.QMessageBox.warning(
                self,
                "偏移并写入图层",
                f"偏移写入失败，本次没有把偏移结果写回已完成设计：\n{exc}",
            )
