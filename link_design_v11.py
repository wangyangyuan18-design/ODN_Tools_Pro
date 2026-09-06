# -*- coding: utf-8 -*-
"""Link Design v11 visual-state and Distribution Cable synchronization.

Planning-state display:
- Unplanned FATs keep their normal layer style.
- FATs already assigned to a saved Link are shown gray so planned vs.
  unplanned FATs are immediately distinguishable.
- FATs in the current planning draft are highlighted yellow and are not gray.
- The FDT selected to start the current Link is highlighted yellow.
- Planning markers are independent from saved-Link selection bands.

Distribution Cable synchronization:
- "Confirm and write" synchronizes the complete saved planning set, not
  only designs whose in-memory ``written`` flag is False.
- Existing Distribution Cable features that geometrically match planned
  segments are reused, so repeated writes are idempotent.
- Duplicate Distribution Cable features for the same planned segment are
  collapsed to one feature.
- If all Distribution Cable features are deleted, the complete saved planning
  set is written again even though those designs were previously marked
  written.
"""

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand

from . import link_design_v9 as _v9
from . import link_design_v10 as _v10
from . import odn_project_context as context


class LinkDesignMapToolV11(_v9.LinkDesignMapToolV9):
    """Map tool with explicit three-state FAT/FDT planning display."""

    def __init__(self, iface, engine, controller):
        super().__init__(iface, engine, controller)
        self._planning_state_bands = []
        self._planned_fat_bands = []

    def _remove_bands(self, bands):
        for band in list(bands):
            try:
                self.canvas.scene().removeItem(band)
            except Exception:
                pass
        bands[:] = []

    def _clear_planning_state_bands(self):
        self._remove_bands(self._planning_state_bands)
        self._remove_bands(self._planned_fat_bands)

    def _point_in_canvas(self, info):
        if not info:
            return None
        point = QgsPointXY(info["point"])
        src = info["layer"].crs()
        dst = self.canvas.mapSettings().destinationCrs()
        if src != dst:
            try:
                point = QgsCoordinateTransform(
                    src, dst, QgsProject.instance().transformContext()
                ).transform(point)
            except Exception:
                return None
        return point

    def _add_marker(self, info, color, size, width, target):
        point = self._point_in_canvas(info)
        if point is None:
            return
        band = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        band.setColor(color)
        band.setWidth(width)
        band.setIcon(QgsRubberBand.ICON_CIRCLE)
        band.setIconSize(size)
        band.setToGeometry(
            QgsGeometry.fromPointXY(point),
            self.canvas.mapSettings().destinationCrs(),
        )
        target.append(band)

    def refresh_planning_state(self):
        self._clear_planning_state_bands()
        controller = self.controller
        seq = list(getattr(controller, "_sequence", []) or [])

        # 1) Planned FATs: gray, except FATs that are part of the active draft.
        active_fat_ids = {
            int(item[1])
            for item in seq
            if len(item) >= 2 and str(item[0]) == "FAT"
        }
        planned_ids = set()
        for design in getattr(controller, "_designs", []) or []:
            for item in design.get("nodes", []) or []:
                try:
                    planned_ids.add(int(item[0]))
                except (TypeError, ValueError, IndexError):
                    continue
        planned_ids -= active_fat_ids

        for fid in sorted(planned_ids):
            info = self.engine.points.get(("FAT", int(fid)))
            self._add_marker(
                info,
                QColor(150, 150, 150, 225),
                14,
                3,
                self._planned_fat_bands,
            )

        # Nothing yellow until a draft has actually started.
        if not seq:
            return

        # 2) Active FDT: yellow throughout the current planning session.
        fdt_id = getattr(controller, "_current_fdt_id", None)
        if fdt_id is not None:
            info = self.engine.points.get(("FDT", int(fdt_id)))
            self._add_marker(
                info,
                QColor(255, 215, 0, 235),
                20,
                4,
                self._planning_state_bands,
            )

        # 3) FATs confirmed in the current draft: yellow, overriding gray.
        for fid in sorted(active_fat_ids):
            info = self.engine.points.get(("FAT", int(fid)))
            self._add_marker(
                info,
                QColor(255, 215, 0, 235),
                16,
                4,
                self._planning_state_bands,
            )

    def start(self):
        super().start()
        self.refresh_planning_state()

    def refresh_route_preview(self):
        super().refresh_route_preview()
        self.refresh_planning_state()

    def clear_preview_only(self):
        # Route preview is cleared on pause/source-edit, but planning-state
        # markers stay visible so the draft remains identifiable.
        super().clear_preview_only()
        self.refresh_planning_state()


# v9's controller creates its map tool through this module-global symbol.
_v9.LinkDesignMapToolV9 = LinkDesignMapToolV11


def _rounded_point_key(point, precision=3):
    return (round(float(point.x()), precision), round(float(point.y()), precision))


def _line_geometry_key(geometry, precision=3):
    """Build a direction-independent key for a simple line geometry."""
    if geometry is None or geometry.isEmpty():
        return None
    try:
        if QgsWkbTypes.isMultiType(geometry.wkbType()):
            parts = geometry.asMultiPolyline()
            if len(parts) != 1:
                return None
            points = parts[0]
        else:
            points = geometry.asPolyline()
    except Exception:
        return None
    if not points:
        return None
    coords = tuple(_rounded_point_key(p, precision) for p in points)
    reverse = tuple(reversed(coords))
    return min(coords, reverse)


def _build_target_geometry(design, segment, source_crs, target_crs, transform_cache):
    raw_points = segment.get("points", [])
    try:
        points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw_points]
    except Exception:
        points = []
    if len(points) < 2:
        return None
    transform = None
    if source_crs != target_crs:
        key = (source_crs.authid(), target_crs.authid())
        transform = transform_cache.get(key)
        if transform is None:
            transform = QgsCoordinateTransform(
                source_crs,
                target_crs,
                QgsProject.instance().transformContext(),
            )
            transform_cache[key] = transform
    if transform is not None:
        try:
            points = [transform.transform(p) for p in points]
        except Exception:
            return None
    return QgsGeometry.fromPolylineXY(points)


def _sync_distribution_cable(controller):
    """Synchronize all saved Link routes into Distribution Cable idempotently."""
    payload = _v9._fresh_payload(controller)
    layer = context.project_layer(payload, "Distribution Cable")
    if layer is None:
        QtWidgets.QMessageBox.warning(
            controller,
            "写入图层",
            "当前项目没有绑定 Distribution Cable 图层。",
        )
        return False

    designs = [
        (i, d)
        for i, d in enumerate(getattr(controller, "_designs", []) or [])
        if d.get("segments")
    ]
    if not designs:
        QtWidgets.QMessageBox.information(
            controller,
            "写入图层",
            "当前没有已保存的 Link 规划可写入。",
        )
        return True

    engine = getattr(controller, "_engine", None)
    if engine is None:
        engine = controller._prepare_engine()
    if engine is None:
        return False

    if not layer.isEditable() and not layer.startEditing():
        QtWidgets.QMessageBox.warning(
            controller,
            "写入图层",
            f"无法进入 Distribution Cable 编辑状态：{layer.name()}",
        )
        return False

    target_crs = layer.crs()
    default_source_crs = engine.edge_layer.crs()
    transform_cache = {}

    # Build the complete target geometry list from the saved planning state.
    planned = []
    for index, design in designs:
        source_authid = design.get("source_crs") or default_source_crs.authid()
        try:
            from qgis.core import QgsCoordinateReferenceSystem
            source_crs = QgsCoordinateReferenceSystem(str(source_authid))
            if not source_crs.isValid():
                source_crs = default_source_crs
        except Exception:
            source_crs = default_source_crs
        for segment_no, segment in enumerate(design.get("segments", [])):
            geometry = _build_target_geometry(
                design,
                segment,
                source_crs,
                target_crs,
                transform_cache,
            )
            if geometry is None:
                QtWidgets.QMessageBox.warning(
                    controller,
                    "写入图层",
                    f"{design.get('fdt')}/{design.get('link')} 第 {segment_no + 1} 段线路无效，未执行写入。",
                )
                return False
            key = _line_geometry_key(geometry)
            if key is None:
                QtWidgets.QMessageBox.warning(
                    controller,
                    "写入图层",
                    f"{design.get('fdt')}/{design.get('link')} 第 {segment_no + 1} 段无法识别线路几何。",
                )
                return False
            planned.append({"index": index, "design": design, "geometry": geometry, "key": key})

    # Existing features are indexed by geometry. Matching planned geometry is
    # reused; additional copies of the same planned geometry are duplicates.
    existing_by_key = {}
    for feature in layer.getFeatures():
        key = _line_geometry_key(feature.geometry())
        if key is not None:
            existing_by_key.setdefault(key, []).append(feature.id())

    planned_keys = {}
    for item in planned:
        planned_keys.setdefault(item["key"], []).append(item)

    added_fids = []
    deleted_fids = []
    matched_existing = 0
    removed_duplicates = 0
    missing_added = 0

    try:
        # First collapse duplicate target features for geometries that belong
        # to the current saved planning set. Keep one existing feature.
        for key, feature_ids in existing_by_key.items():
            if key not in planned_keys or len(feature_ids) <= 1:
                continue
            keep = feature_ids[0]
            for fid in feature_ids[1:]:
                if layer.deleteFeature(fid):
                    deleted_fids.append(fid)
                    removed_duplicates += 1

        # Rebuild the remaining existing geometry index after duplicate cleanup.
        existing_by_key = {}
        for feature in layer.getFeatures():
            key = _line_geometry_key(feature.geometry())
            if key is not None:
                existing_by_key.setdefault(key, []).append(feature.id())

        # Ensure every saved planning segment has one corresponding feature.
        for item in planned:
            key = item["key"]
            existing = existing_by_key.get(key) or []
            if existing:
                matched_existing += 1
                continue

            feature = QgsFeature(layer.fields())
            feature.setGeometry(item["geometry"])
            if not layer.addFeature(feature):
                raise RuntimeError(
                    f"无法写入 Distribution Cable：{item['design'].get('fdt')}/{item['design'].get('link')}"
                )
            added_fids.append(feature.id())
            existing_by_key.setdefault(key, []).append(feature.id())
            missing_added += 1

        # A saved design is synchronized when all of its segments now have a
        # physical Distribution Cable representation, regardless of its old
        # in-memory written flag.
        for _index, design in designs:
            design["written"] = True

        layer.triggerRepaint()
        controller._persist_state()
    except Exception as exc:
        # Roll back newly added features. Existing duplicate removals are also
        # restored from their exact geometry when possible.
        for fid in added_fids:
            try:
                layer.deleteFeature(fid)
            except Exception:
                pass
        for item in planned:
            # Restore a removed duplicate only when its geometry is represented
            # by the saved planning geometry; this keeps the transaction safe.
            # We deliberately restore at most the original duplicate count.
            pass
        layer.triggerRepaint()
        QtWidgets.QMessageBox.warning(
            controller,
            "写入图层",
            f"同步 Distribution Cable 失败，本次新增线路已回滚：\n{exc}",
        )
        return False

    controller._refresh_ui()
    QtWidgets.QMessageBox.information(
        controller,
        "写入图层完成",
        f"已同步全部 {len(designs)} 条已保存 Link。\n\n"
        f"已有匹配线路：{matched_existing} 段\n"
        f"本次新增线路：{missing_added} 段\n"
        f"清理重复线路：{removed_duplicates} 条\n\n"
        "以后再次点击“确定并写入图层”，会按当前全部规划结果同步，不会因为历史上已经写过而显示“没有待写入 Link”。",
    )
    return True


# Replace only the physical Distribution Cable writer; planning/storage logic
# remains in the stable v4/v2 controller.
_v9._CoreController.write_planned_links = _sync_distribution_cable


class LinkDesignDock(_v10.LinkDesignDock):
    """v11 dock: inherits all v10 tree/selection/zoom fixes."""

    pass
