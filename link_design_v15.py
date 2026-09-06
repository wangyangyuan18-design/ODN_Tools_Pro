# -*- coding: utf-8 -*-
"""Link Design v15 UI/state refinements over v14.

This layer intentionally keeps the existing Link Design calculation engine
unchanged. It only adds:
- explicit exit-design behavior with save-before-exit confirmation;
- a compact checkbox controlling the gray "completed FAT" markers;
- clearing saved-link preview highlighting when clicking blank tree space.
"""

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QEvent, QSettings, Qt

from . import link_design_v14 as _v14
from . import link_design_v11 as _v11

SHOW_COMPLETED_FAT_KEY = "ODNToolsPro/LinkDesign/show_completed_fat_marks"


# -----------------------------------------------------------------------------
# Keep the existing v11 marker implementation, but make its gray completed-FAT
# layer user-controllable. Yellow active-planning markers remain unchanged.
# -----------------------------------------------------------------------------
_original_refresh_planning_state = _v11.LinkDesignMapToolV11.refresh_planning_state


def _v15_refresh_planning_state(self):
    _original_refresh_planning_state(self)
    if getattr(self.controller, "_show_completed_fat_marks", True):
        return
    try:
        self._remove_bands(self._planned_fat_bands)
    except Exception:
        pass


_v11.LinkDesignMapToolV11.refresh_planning_state = _v15_refresh_planning_state


class LinkDesignDock(_v14.LinkDesignDock):
    """Active Link Design dock with a deliberately minimal UI refinement."""

    def __init__(self, iface, parent=None):
        self._show_completed_fat_marks = bool(
            QSettings().value(SHOW_COMPLETED_FAT_KEY, True, type=bool)
        )
        super().__init__(iface, parent)
        self._install_ui_refinements()

    # ----- completed-FAT marker checkbox -------------------------------------
    def _install_ui_refinements(self):
        self._replace_summary_row()
        self.tree.viewport().installEventFilter(self)

        # The v14 overlay's Exit button originally calls controller.exit_design.
        # Replace only that connection so the dock can hide the overlay and ask
        # whether an unfinished Link should be saved first.
        try:
            self._overlay.exit.clicked.disconnect(self._controller.exit_design)
        except Exception:
            pass
        self._overlay.exit.clicked.connect(self._exit_design)

    def _replace_summary_row(self):
        root = self.widget()
        layout = root.layout() if root is not None else None
        if layout is None or not hasattr(self, "summary"):
            return

        old_summary = self.summary
        try:
            index = layout.indexOf(old_summary)
        except Exception:
            index = -1
        if index < 0:
            return

        layout.removeWidget(old_summary)
        old_summary.hide()

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self.show_fat_marks = QtWidgets.QCheckBox()
        self.show_fat_marks.setChecked(self._show_completed_fat_marks)
        self.show_fat_marks.setToolTip("显示/隐藏已完成 FAT 的灰色标记")
        self.show_fat_marks.setFixedWidth(18)
        self.show_fat_marks.setFixedHeight(18)

        self.summary_link_label = QtWidgets.QLabel()
        self.summary_fat_label = QtWidgets.QLabel()

        row.addWidget(self.summary_link_label)
        row.addSpacing(8)
        row.addWidget(self.show_fat_marks)
        row.addWidget(self.summary_fat_label)
        row.addStretch(1)
        layout.insertLayout(index, row)

        self.show_fat_marks.toggled.connect(self._toggle_completed_fat_marks)

    def _toggle_completed_fat_marks(self, checked):
        self._show_completed_fat_marks = bool(checked)
        QSettings().setValue(SHOW_COMPLETED_FAT_KEY, self._show_completed_fat_marks)
        try:
            QSettings().sync()
        except Exception:
            pass
        tool = getattr(self._controller, "_tool", None)
        if tool is not None:
            try:
                tool.refresh_planning_state()
            except Exception:
                pass
        self.iface.mapCanvas().refresh()

    # ----- blank-space click clears the saved-Link preview ------------------
    def eventFilter(self, obj, event):
        if obj is getattr(self.tree, "viewport", lambda: None)():
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                pos = event.position().toPoint()
                if self.tree.itemAt(pos) is None:
                    self.tree.clearSelection()
                    self.tree.setCurrentItem(None)
                    self._controller._clear_saved_bands()
                    self.info.setText("—")
                    return True
        return super().eventFilter(obj, event)

    # ----- exit design --------------------------------------------------------
    def _has_unfinished_design(self):
        c = self._controller
        return bool(
            getattr(c, "_draw_active", False)
            or getattr(c, "_sequence", None)
            or getattr(c, "_editing_index", None) is not None
        )

    def _exit_design(self):
        c = self._controller
        if self._has_unfinished_design():
            answer = QtWidgets.QMessageBox.question(
                self,
                "退出设计",
                "当前有正在规划或修改中的 Link。\n\n"
                "是否保存当前规划后退出？",
                QtWidgets.QMessageBox.Save
                | QtWidgets.QMessageBox.Discard
                | QtWidgets.QMessageBox.Cancel,
                QtWidgets.QMessageBox.Save,
            )
            if answer == QtWidgets.QMessageBox.Cancel:
                return
            if answer == QtWidgets.QMessageBox.Save:
                try:
                    result = c.save_current_link()
                except Exception as exc:
                    QtWidgets.QMessageBox.warning(
                        self,
                        "退出设计",
                        f"保存失败，未退出设计：\n{exc}",
                    )
                    return
                if not result:
                    QtWidgets.QMessageBox.warning(
                        self,
                        "退出设计",
                        "当前规划保存失败，未退出设计。",
                    )
                    return

        try:
            tool = getattr(c, "_tool", None)
            if tool is not None:
                try:
                    tool.clear_preview_only()
                except Exception:
                    pass
                try:
                    tool._clear_planning_state_bands()
                except Exception:
                    pass
            c._stop_tool()
        except Exception:
            pass

        c._draw_active = False
        c._sequence = []
        c._current_fdt = None
        c._current_fdt_id = None
        c._current_link = None
        c._hover_segment = None
        c._hover_distance = 0.0
        try:
            c._editing_index = None
            c._editing_written_index = None
        except Exception:
            pass
        try:
            c._persist_state()
            c._refresh_ui()
        except Exception:
            pass

        try:
            self._overlay.hide()
        except Exception:
            pass
        self.iface.mapCanvas().refresh()

    # ----- refresh summary without changing its compact layout ---------------
    def refresh_from_core(self):
        super().refresh_from_core()
        c = self._controller
        total_fats = 0
        try:
            total_fats = _v14._v9._v2._total_fats(c)
        except Exception:
            try:
                total_fats = _v14._v9._v2._total_fats(c)
            except Exception:
                pass

        used_fats = set()
        for design in getattr(c, "_designs", []) or []:
            for node in design.get("nodes", []) or []:
                try:
                    used_fats.add(int(node[0]))
                except Exception:
                    pass

        if hasattr(self, "summary_link_label"):
            self.summary_link_label.setText(f"已完成Link:{len(c._designs)}")
            self.summary_fat_label.setText(f"已完成FAT:{len(used_fats)}/{total_fats}")
        elif hasattr(self, "summary"):
            self.summary.setText(
                f"已完成Link:{len(c._designs)}    已完成FAT:{len(used_fats)}/{total_fats}"
            )

        # Make the overlay visible again whenever Link Design is actively used
        # after it was explicitly hidden by "退出设计".
        try:
            if getattr(c, "_draw_active", False) or getattr(c, "_sequence", None):
                self._overlay.show()
                self._overlay.raise_()
        except Exception:
            pass
