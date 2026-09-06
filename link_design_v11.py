# -*- coding: utf-8 -*-
"""Link Design v11 visual-state fix.

Planning-state display:
- Unplanned FATs keep their normal layer style.
- FATs already assigned to a saved Link are shown gray so planned vs.
  unplanned FATs are immediately distinguishable.
- FATs in the current planning draft are highlighted yellow and are not gray.
- The FDT selected to start the current Link is highlighted yellow.
- Planning markers are independent from saved-Link selection bands.
"""

from qgis.PyQt.QtGui import QColor
from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject, QgsWkbTypes
from qgis.gui import QgsRubberBand

from . import link_design_v9 as _v9
from . import link_design_v10 as _v10


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
            # Gray ring clearly communicates "already planned" while keeping
            # the underlying layer style visible.
            self._add_marker(info, QColor(150, 150, 150, 225), 14, 3, self._planned_fat_bands)

        # Nothing yellow until a draft has actually started.
        if not seq:
            return

        # 2) Active FDT: yellow throughout the current planning session.
        fdt_id = getattr(controller, "_current_fdt_id", None)
        if fdt_id is not None:
            info = self.engine.points.get(("FDT", int(fdt_id)))
            self._add_marker(info, QColor(255, 215, 0, 235), 20, 4, self._planning_state_bands)

        # 3) FATs confirmed in the current draft: yellow, overriding gray.
        for fid in sorted(active_fat_ids):
            info = self.engine.points.get(("FAT", int(fid)))
            self._add_marker(info, QColor(255, 215, 0, 235), 16, 4, self._planning_state_bands)

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


# v9's controller creates the map tool through this module-global symbol.
# Substitute only the enhanced visual map tool while keeping v10 dock logic.
_v9.LinkDesignMapToolV9 = LinkDesignMapToolV11


class LinkDesignDock(_v10.LinkDesignDock):
    """v11 dock: inherits all v10 tree/selection/zoom fixes."""

    pass
