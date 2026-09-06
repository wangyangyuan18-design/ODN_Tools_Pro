# -*- coding: utf-8 -*-
"""Link Design v10 interaction fixes.

Keeps v9 planning logic intact and fixes:
- FDT expansion state survives refreshes.
- Link/FDT double-click zoom uses the stored route CRS even when no route
  engine is active yet.
- Tree selection is preserved when controller refreshes the dock.
- Saved-route highlighting is replaced rather than accumulated.
- Current planning route preview is kept separate from saved-route selection.
"""

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsRectangle

from . import link_design_v9 as _v9


class LinkDesignDock(_v9.LinkDesignDock):
    """v10 dock with stable tree state and deterministic map selection."""

    def __init__(self, iface, parent=None):
        self._syncing_tree = False
        super().__init__(iface, parent)

    @staticmethod
    def _item_key(item):
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        if not data:
            return None
        if data[0] == "link":
            return ("link", int(data[1]))
        return ("fdt", tuple(sorted(int(x) for x in data[1])))

    def _capture_tree_state(self):
        expanded = set()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item and item.isExpanded():
                expanded.add(self._item_key(item))
        selected = [self._item_key(item) for item in self.tree.selectedItems()]
        selected = [x for x in selected if x is not None]
        current = self._item_key(self.tree.currentItem())
        return expanded, selected, current

    def _restore_tree_state(self, state):
        expanded, selected, current = state
        self._syncing_tree = True
        self.tree.blockSignals(True)
        try:
            selected_set = set(selected)
            for i in range(self.tree.topLevelItemCount()):
                parent = self.tree.topLevelItem(i)
                key = self._item_key(parent)
                parent.setExpanded(key in expanded)
                if key in selected_set:
                    parent.setSelected(True)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    if self._item_key(child) in selected_set:
                        child.setSelected(True)
                    if self._item_key(child) == current:
                        self.tree.setCurrentItem(child)
                if key == current:
                    self.tree.setCurrentItem(parent)
        finally:
            self.tree.blockSignals(False)
            self._syncing_tree = False

    def _clear_saved_route_selection(self):
        # Only clear the saved-route selection layer. Do not touch the current
        # planning route preview maintained by LinkDesignMapToolV9.
        try:
            self._controller._clear_saved_bands()
        except Exception:
            pass

    def _tree_selection_changed(self):
        if self._syncing_tree:
            return
        indexes = self._tree_selected_links()
        entries = [(i, self._controller._designs[i]) for i in indexes]
        self._clear_saved_route_selection()
        if not entries:
            self.info.setText("—")
            return
        self._controller.show_saved_designs(entries)
        if len(entries) == 1:
            d = entries[0][1]
            self.info.setText(
                f"{d.get('fdt','')}/{d.get('link','')}：{len(d.get('nodes', []))} 个 FAT，"
                f"{float(d.get('length', 0.0) or 0.0):.1f}m，"
                f"{'修改中' if self._controller._editing_index == indexes[0] else '已完成'}。"
            )
        else:
            total = sum(float(d.get("length", 0.0) or 0.0) for _, d in entries)
            self.info.setText(f"已选 {len(entries)} 条 Link，总距离 {total:.1f}m。")

    def _tree_double_clicked(self, item, column):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        # Keep the selection stable before zooming.
        if data[0] == "link":
            index = int(data[1])
            if 0 <= index < len(self._controller._designs):
                self._zoom_design(self._controller._designs[index])
        elif data[0] == "fdt":
            indexes = [int(i) for i in data[1]]
            designs = [self._controller._designs[i] for i in indexes if 0 <= i < len(self._controller._designs)]
            self._zoom_designs(designs)

    def _zoom_designs(self, designs):
        rect = None
        src = None
        for design in designs:
            # Route points are persisted in the Pole Edge CRS used when the
            # Link was saved. Fall back to the currently prepared engine.
            if src is None:
                authid = design.get("source_crs")
                if authid:
                    try:
                        candidate = QgsCoordinateReferenceSystem(str(authid))
                        if candidate.isValid():
                            src = candidate
                    except Exception:
                        pass
                if src is None and self._controller._engine is not None:
                    src = self._controller._engine.edge_layer.crs()
            for segment in design.get("segments", []):
                for raw in segment.get("points", []):
                    try:
                        p = QgsPointXY(float(raw[0]), float(raw[1]))
                    except Exception:
                        continue
                    if rect is None:
                        rect = QgsRectangle(p.x(), p.y(), p.x(), p.y())
                    else:
                        rect.combineExtentWith(p.x(), p.y())
        if rect is None:
            return
        canvas = self.iface.mapCanvas()
        dst = canvas.mapSettings().destinationCrs()
        if src is not None and src.isValid() and src != dst:
            try:
                transform = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext())
                corners = [
                    transform.transform(QgsPointXY(rect.xMinimum(), rect.yMinimum())),
                    transform.transform(QgsPointXY(rect.xMinimum(), rect.yMaximum())),
                    transform.transform(QgsPointXY(rect.xMaximum(), rect.yMinimum())),
                    transform.transform(QgsPointXY(rect.xMaximum(), rect.yMaximum())),
                ]
                rect = QgsRectangle(corners[0], corners[0])
                for p in corners[1:]:
                    rect.combineExtentWith(p.x(), p.y())
            except Exception:
                return
        # Give very short/single-point links enough padding to make the zoom
        # visibly useful.
        if rect.width() <= 0 or rect.height() <= 0:
            pad = max(canvas.mapUnitsPerPixel() * 80.0, 2.0)
            rect = QgsRectangle(
                rect.xMinimum() - pad,
                rect.yMinimum() - pad,
                rect.xMaximum() + pad,
                rect.yMaximum() + pad,
            )
        else:
            rect.scale(1.20)
        canvas.setExtent(rect)
        canvas.refresh()

    def refresh_from_core(self):
        state = self._capture_tree_state() if hasattr(self, "tree") else (set(), [], None)
        super().refresh_from_core()
        if hasattr(self, "tree"):
            self._restore_tree_state(state)

    def _modify_selected(self):
        if self._controller._draw_active or self._controller._paused_by_source_change:
            self.info.setText("当前正在规划，请先保存或取消当前规划后再修改 Link。")
            return
        super()._modify_selected()

    def closeEvent(self, event):
        super().closeEvent(event)
