# -*- coding: utf-8 -*-
"""Standalone FAT-to-pole return action.

Rule:
- A FAT belongs to the nearest Existing Pole / New Pole only when the FAT is
  within the configured FAT-to-pole limit (default 3 m).
- FAT geometry is moved directly onto that pole.
- Completed Link topology, saved route geometry and Distribution Cable are NOT
  recalculated or changed here.
- The operation is wrapped in the QGIS layer edit-command stack so it can be
  undone as one action.
"""

from qgis.PyQt import QtWidgets
from qgis.core import (
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    Qgis,
)

from . import odn_project_context as context
from .plugin_undo import record_layer


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / FAT归杆", level)
    except Exception:
        pass


def _distance_m(da, a, b):
    try:
        return float(da.measureLine(QgsPointXY(a), QgsPointXY(b)))
    except Exception:
        try:
            return float(a.distance(b))
        except Exception:
            return float("inf")


def _pole_candidates_in_fat_crs(fat_layer, pole_layers):
    candidates = []
    fat_crs = fat_layer.crs()
    project = QgsProject.instance()
    transforms = {}
    for layer in pole_layers:
        if layer is None:
            continue
        transform = None
        if layer.crs() != fat_crs:
            key = (layer.crs().authid(), fat_crs.authid())
            transform = transforms.get(key)
            if transform is None:
                try:
                    transform = QgsCoordinateTransform(
                        layer.crs(), fat_crs, project.transformContext()
                    )
                except Exception as exc:
                    _log(
                        f"[pole-crs-fail] layer={layer.name()}; "
                        f"reason={type(exc).__name__}:{exc}",
                        Qgis.Warning,
                    )
                    continue
                transforms[key] = transform
        for feature in layer.getFeatures():
            try:
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    continue
                point = QgsPointXY(geom.centroid().asPoint())
                if transform is not None:
                    point = QgsPointXY(transform.transform(point))
                candidates.append(
                    {
                        "layer": layer.name(),
                        "fid": int(feature.id()),
                        "point": point,
                    }
                )
            except Exception as exc:
                _log(
                    f"[pole-read-fail] layer={layer.name()}; fid={feature.id()}; "
                    f"reason={type(exc).__name__}:{exc}",
                    Qgis.Warning,
                )
    return candidates


def _nearest_pole(fat_feature, candidates, da, max_distance):
    try:
        geom = fat_feature.geometry()
        if geom is None or geom.isEmpty():
            return None, None
        fat_point = QgsPointXY(geom.centroid().asPoint())
    except Exception:
        return None, None

    best = None
    for candidate in candidates:
        distance = _distance_m(da, fat_point, candidate["point"])
        if best is None or distance < best[0]:
            best = (distance, candidate)
    if best is None or best[0] > max_distance + 1e-9:
        return None, (best[0] if best is not None else None)
    return best[1], best[0]


def _find_row_layout(layout, target):
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is target:
            return layout
        child = item.layout()
        if child is not None:
            found = _find_row_layout(child, target)
            if found is not None:
                return found
    return None


def install_fat_return_button(dock):
    """Add FAT归杆 to the existing completed-summary row, once."""
    if getattr(dock, "_fat_return_button", None) is not None:
        return
    target = getattr(dock, "summary_fat_label", None)
    root = dock.widget() if hasattr(dock, "widget") else dock
    row_root = root.layout() if root is not None else None
    if target is None or row_root is None:
        _log("[ui] summary_fat_label not found; FAT归杆 button not installed", Qgis.Warning)
        return

    row = _find_row_layout(row_root, target)
    if row is None:
        _log("[ui] summary row not found; FAT归杆 button not installed", Qgis.Warning)
        return

    button = QtWidgets.QPushButton("FAT归杆")
    button.setMinimumHeight(22)
    button.setToolTip(
        "仅把距离所属杆不超过 3 m 的 FAT 移到杆上。\n"
        "不重建已完成 Link，不修改 Link 拓扑，也不修改 Distribution Cable。"
    )
    button.clicked.connect(lambda: fat_return_to_poles(dock))

    insert_at = row.count()
    for index in range(row.count()):
        if row.itemAt(index).spacerItem() is not None:
            insert_at = index
            break
    row.insertWidget(insert_at, button)
    dock._fat_return_button = button


def fat_return_to_poles(dock):
    controller = getattr(dock, "_controller", None)
    if controller is None:
        QtWidgets.QMessageBox.warning(dock, "FAT归杆", "当前链路设计控制器不可用。")
        return False
    if getattr(controller, "_draw_active", False):
        dock.info.setText("请先保存或退出当前规划，再执行 FAT归杆。")
        return False

    try:
        payload = controller._v9_payload()
    except Exception:
        payload = context.current_payload() or {}

    fat_layer = context.project_layer(payload, "FAT")
    existing_pole = context.project_layer(payload, "Existing Pole")
    new_pole = context.project_layer(payload, "New Pole")
    if fat_layer is None:
        QtWidgets.QMessageBox.warning(dock, "FAT归杆", "当前项目没有绑定 FAT 图层。")
        return False

    pole_layers = [layer for layer in (existing_pole, new_pole) if layer is not None]
    if not pole_layers:
        QtWidgets.QMessageBox.warning(
            dock,
            "FAT归杆",
            "当前项目没有绑定 Existing Pole / New Pole 图层。",
        )
        return False

    max_distance = 3.0
    try:
        max_distance = float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0))
    except (TypeError, ValueError):
        max_distance = 3.0
    max_distance = max(0.01, max_distance)

    candidates = _pole_candidates_in_fat_crs(fat_layer, pole_layers)
    if not candidates:
        QtWidgets.QMessageBox.warning(dock, "FAT归杆", "没有读取到有效的 POLE。")
        return False

    distance_area = QgsDistanceArea()
    distance_area.setSourceCrs(fat_layer.crs(), QgsProject.instance().transformContext())
    try:
        distance_area.setEllipsoid("WGS84")
    except Exception:
        pass

    all_features = list(fat_layer.getFeatures())
    if not all_features:
        dock.info.setText("当前 FAT 图层没有可处理要素。")
        return False

    if not fat_layer.isEditable() and not fat_layer.startEditing():
        QtWidgets.QMessageBox.warning(dock, "FAT归杆", "无法进入 FAT 编辑状态。")
        return False

    moved = 0
    skipped = 0
    details = []
    record_layer(fat_layer, "FAT归杆")
    try:
        fat_layer.beginEditCommand("FAT归杆")
        for feature in all_features:
            pole, distance = _nearest_pole(feature, candidates, distance_area, max_distance)
            if pole is None:
                skipped += 1
                if distance is not None:
                    details.append(f"FAT {feature.id()}: 最近 POLE {distance:.2f} m，超过 {max_distance:.2f} m")
                else:
                    details.append(f"FAT {feature.id()}: 无法确定最近 POLE")
                continue

            old_geom = feature.geometry()
            target = QgsPointXY(pole["point"])
            feature.setGeometry(__import__("qgis.core", fromlist=["QgsGeometry"]).QgsGeometry.fromPointXY(target))
            if not fat_layer.changeGeometry(feature.id(), feature.geometry()):
                raise RuntimeError(f"FAT {feature.id()} 几何写入失败")
            moved += 1
            _log(
                f"[moved] FAT={feature.id()}; pole={pole['layer']}:{pole['fid']}; "
                f"distance={distance:.3f}m"
            )
        fat_layer.endEditCommand()
    except Exception as exc:
        try:
            fat_layer.destroyEditCommand()
        except Exception:
            pass
        _log(f"[failed] {type(exc).__name__}:{exc}", Qgis.Critical)
        QtWidgets.QMessageBox.warning(
            dock,
            "FAT归杆",
            f"FAT归杆失败，本次 FAT 几何修改已回退：\n{exc}",
        )
        return False

    fat_layer.triggerRepaint()
    dock.info.setText(
        f"FAT归杆完成：移动 {moved} 个，超出 {max_distance:.2f} m 或无法匹配 {skipped} 个。"
    )
    if skipped:
        _log(f"[summary] moved={moved}; skipped={skipped}; max_distance={max_distance:.3f}m", Qgis.Warning)
    else:
        _log(f"[summary] moved={moved}; skipped=0; max_distance={max_distance:.3f}m")
    return True
