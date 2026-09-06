# -*- coding: utf-8 -*-
"""Link Design v11 visual-state fix.

Keeps the v10 dock behavior and makes planning-state highlighting explicit:
- Existing/planned FAT features keep their normal layer style; they are never
  grayed just because they already belong to a saved Link.
- The FDT chosen to start the current Link is highlighted yellow.
- FATs already confirmed in the current planning sequence are highlighted
  yellow as well.
- These planning markers are independent from saved-Link selection bands.
"""

from qgis.PyQt.QtGui import QColor
from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject, QgsWkbTypes
from qgis.gui import QgsRubberBand

from . import link_design_v9 as _v9
from . import link_design_v10 as _v10


class LinkDesignMapToolV11(_v9.LinkDesignMapToolV9):
    """v9 map tool plus persistent yellow FDT/FAT planning markers."""

    def __init__(self, iface, engine, controller):
        super().__init__(iface, engine, controller)
        self._planning_state_bands = []

    def _clear_planning_state_bands(self):
        for band in list(self._planning_state_bands):
            try:
                self.canvas.scene().removeItem(band)
            except Exception:
                pass
        self._planning_state_bands = []

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

    def _add_planning_marker(self, info, size, width=3):
        point = self._point_in_canvas(info)
        if point is None:
            return
        band = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        band.setColor(QColor(255, 215, 0, 235))
        band.setWidth(width)
        band.setIcon(QgsRubberBand.ICON_CIRCLE)
        band.setIconSize(size)
        band.setToGeometry(
            QgsGeometry.fromPointXY(point), self.canvas.mapSettings().destinationCrs()
        )
        self._planning_state_bands.append(band)

    def refresh_planning_state(self):
        self._clear_planning_state_bands()
        controller = self.controller
        seq = list(getattr(controller, "_sequence", []) or [])
        if not seq:
            return

        # Highlight the active FDT first. This remains visible throughout the
        # current Link planning session, including while hovering FATs.
        fdt_id = getattr(controller, "_current_fdt_id", None)
        if fdt_id is not None:
            info = self.engine.points.get(("FDT", int(fdt_id)))
            self._add_planning_marker(info, 20, 3)

        # Current Link FATs are the active planning state. FATs belonging to
        # other saved Links are deliberately untouched: their original layer
        # style remains visible instead of being grayed out.
        for typ, fid, _label in seq:
            if str(typ) != "FAT":
                continue
            info = self.engine.points.get(("FAT", int(fid)))
            self._add_planning_marker(info, 16, 3)

    def start(self):
        super().start()
        self.refresh_planning_state()

    def refresh_route_preview(self):
        super().refresh_route_preview()
        self.refresh_planning_state()

    def clear_preview_only(self):
        # Pause/source-edit clears route preview but keeps the yellow planning
        # state visible so the draft is still obvious before Continue Planning.
        super().clear_preview_only()


# v9's controller creates its map tool through this module-global symbol. Keep
# the stable controller/dock logic while substituting only the enhanced tool.
_v9.LinkDesignMapToolV9 = LinkDesignMapToolV11


class LinkDesignDock(_v10.LinkDesignDock):
    """v11 dock: inherits all v10 tree/selection/zoom fixes."""

    pass
