# -*- coding: utf-8 -*-
"""Link Design UI v8.

Separates hover preview from confirmed LINK information.
Hovering a candidate updates the dialog only with the current candidate
path/distance. Clicking commits the node and refreshes the full LINK details.
Tab cycles candidates at the same/near location.
"""

from contextlib import contextmanager
from math import sqrt

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt

from . import link_design_v2 as _v2
from . import link_design_v7 as _v7
from .link_design_v7 import _all_completed_fat_ids, _current_link_fat_ids


@contextmanager
def _unlimited_fat_limit():
    """Temporarily make FAT-per-Link capacity equal to the project's FAT count.

    This keeps the legacy v2/v4 validation path intact while removing the
    obsolete fixed per-Link FAT restriction.
    """
    old = _v2._max_fats

    def project_fat_capacity(dialog):
        try:
            total = int(_v2._total_fats(dialog))
            return max(1, total)
        except Exception:
            return 999999999

    _v2._max_fats = project_fat_capacity
    try:
        yield
    finally:
        _v2._max_fats = old


@contextmanager
def _eight_link_minimum():
    """Keep the existing project setting authoritative, but never fall back to 4."""
    old = _v2._max_links

    def project_link_capacity(dialog):
        try:
            configured = int(old(dialog))
        except Exception:
            configured = 0
        return max(8, configured)

    _v2._max_links = project_link_capacity
    try:
        yield
    finally:
        _v2._max_links = old


class LinkDesignDialog(_v7.LinkDesignDialog):
    """v8 dialog with hover-only live distance and separated confirmed LINK data."""

    def __init__(self, iface, parent=None):
        self._confirmed_segment = None
        self._hover_segment = None
        super().__init__(iface, parent)

    def _build_ui(self):
        super()._build_ui()
        root = self.layout()
        self.realtime_distance_label = QtWidgets.QLabel("实时距离：—")
        self.realtime_distance_label.setWordWrap(True)
        try:
            root.insertWidget(3, self.realtime_distance_label)
        except Exception:
            root.addWidget(self.realtime_distance_label)

    def _activate_map_tool(self):
        if self._engine is None:
            with _unlimited_fat_limit(), _eight_link_minimum():
                self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self._clear_fat_overlays()
        self._tool = LinkDesignMapToolV8(self.iface, self._engine, self)
        self.iface.mapCanvas().setMapTool(self._tool)
        self._draw_active = True
        self.start_btn.setEnabled(False)
        self._tool.start()
        self._refresh_fat_overlays()
        return True

    def start_design(self):
        with _unlimited_fat_limit(), _eight_link_minimum():
            result = super().start_design()
        return result

    def _start_from_hit(self, info):
        with _unlimited_fat_limit(), _eight_link_minimum():
            result = super()._start_from_hit(info)
        if result:
            self._confirmed_segment = None
            self._hover_segment = None
        self._refresh_fat_overlays()
        return result

    def add_fat(self, info):
        previous = self._sequence[-1] if self._sequence else None
        with _unlimited_fat_limit(), _eight_link_minimum():
            result = super().add_fat(info)
        if result and previous is not None:
            self._confirmed_segment = (previous[2], info.get("label", ""))
            self._hover_segment = None
        self._refresh_fat_overlays()
        return result

    def add_fdt(self, info):
        previous = self._sequence[-1] if self._sequence else None
        with _unlimited_fat_limit(), _eight_link_minimum():
            result = super().add_fdt(info)
        if result and previous is not None:
            self._confirmed_segment = (previous[2], info.get("label", ""))
            self._hover_segment = None
        self._refresh_fat_overlays()
        return result

    def _make_design(self):
        with _unlimited_fat_limit(), _eight_link_minimum():
            return super()._make_design()

    def save_current_link(self):
        with _unlimited_fat_limit(), _eight_link_minimum():
            result = super().save_current_link()
        self._hover_segment = None
        self._confirmed_segment = None
        self._refresh_fat_overlays()
        return result

    def _refresh_ui(self):
        super()._refresh_ui()
        self.plan_label.setText(
            "规划中（实时距离）" + (" · 修改" if self._editing_index is not None else "")
        )
        if not self._sequence:
            self.current_path_label.setText("当前路径：—")
            self.link_path_label.setText("LINK路径：—")
            self.distance_label.setText("各段距离：—    总距离：—")
            self.realtime_distance_label.setText("实时距离：—")
            return

        if self._confirmed_segment:
            a, b = self._confirmed_segment
            self.current_path_label.setText(f"当前路径：{a} → {b}")
        else:
            last_two = self._sequence[-2:] if len(self._sequence) >= 2 else []
            if len(last_two) == 2:
                self.current_path_label.setText(
                    f"当前路径：{last_two[0][2]} → {last_two[1][2]}"
                )
            else:
                self.current_path_label.setText("当前路径：—")
        self.realtime_distance_label.setText("实时距离：—")

    def _set_hover_preview(self, info, route):
        previous = self._sequence[-1] if self._sequence else None
        if previous is None or route is None:
            self._hover_segment = None
            self.realtime_distance_label.setText("实时距离：—")
            return
        self._hover_segment = (previous[2], info.get("label", ""))
        self.current_path_label.setText(
            f"当前路径：{previous[2]} → {info.get('label', '')}"
        )
        self.realtime_distance_label.setText(
            f"实时距离：{float(route.get('distance', 0.0)):.1f}m"
        )

    def _clear_hover_preview(self):
        self._hover_segment = None
        if self._confirmed_segment:
            a, b = self._confirmed_segment
            self.current_path_label.setText(f"当前路径：{a} → {b}")
        elif len(self._sequence) >= 2:
            a, b = self._sequence[-2][2], self._sequence[-1][2]
            self.current_path_label.setText(f"当前路径：{a} → {b}")
        else:
            self.current_path_label.setText("当前路径：—")
        self.realtime_distance_label.setText("实时距离：—")


class LinkDesignMapToolV8(_v7.LinkDesignMapToolV7):
    """Fast hover candidate selection with cached single-segment distance."""

    def __init__(self, iface, engine, dialog):
        self._route_cache = {}
        self._last_preview_key = None
        super().__init__(iface, engine, dialog)

    def _candidate_key(self, info):
        return (str(info.get("typ", "")), int(info.get("feature_id", -1)))

    def _preview_route(self, info):
        sequence = self.dialog._sequence
        if not sequence or self.engine is None:
            return None
        previous = sequence[-1]
        if info.get("typ") == "FDT" and self.dialog._direction != "FAT_TO_FDT":
            return None
        if any(item[0] == info.get("typ") and item[1] == info.get("feature_id") for item in sequence):
            return None
        if info.get("typ") == "FAT":
            fid = int(info.get("feature_id", -1))
            if fid in _all_completed_fat_ids(self.dialog) and fid not in _current_link_fat_ids(self.dialog):
                return None
        key = (
            previous[0],
            int(previous[1]),
            str(info.get("typ", "")),
            int(info.get("feature_id", -1)),
        )
        if key not in self._route_cache:
            try:
                self._route_cache[key] = self.engine.route(
                    previous[0], previous[1], info["typ"], info["feature_id"]
                )
            except Exception:
                self._route_cache[key] = None
        return self._route_cache[key]

    def _update_live_preview(self):
        info = self._selected_info()
        if info is None:
            self.dialog._clear_hover_preview()
            self._last_preview_key = None
            return
        key = self._candidate_key(info)
        if key == self._last_preview_key:
            return
        self._last_preview_key = key
        route = self._preview_route(info)
        if route is None:
            self.dialog._clear_hover_preview()
            return
        self.dialog._set_hover_preview(info, route)

    def canvasMoveEvent(self, event):
        if not self.dialog._draw_active:
            self._clear_candidate()
            self.dialog._clear_hover_preview()
            return
        candidates = self._hover_candidates(event.pos())
        if not candidates:
            self._clear_candidate()
            self.dialog._clear_hover_preview()
            self._last_preview_key = None
            return
        old = self._selected_info()
        ordered = self._preferred_order(candidates)
        infos = [info for _, info in ordered]
        self._candidate_infos = infos
        if old is not None:
            old_key = self._candidate_key(old)
            self._candidate_index = next(
                (i for i, info in enumerate(infos) if self._candidate_key(info) == old_key),
                0,
            )
        else:
            self._candidate_index = 0
        self._draw_candidate()
        self._update_live_preview()

    def _draw_candidate(self):
        super()._draw_candidate()
        self._update_live_preview()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Tab, Qt.Key_Backtab) and self._candidate_infos:
            delta = 1 if event.key() == Qt.Key_Tab else -1
            self._candidate_index = (self._candidate_index + delta) % len(self._candidate_infos)
            self._draw_candidate()
            self._update_live_preview()
            event.accept()
            return
        super().keyPressEvent(event)

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.dialog._draw_active:
            return
        if not self._candidate_infos:
            self._set_candidates(self._hover_candidates(event.pos()))
        info = self._selected_info()
        if info is None:
            return
        previous = self.dialog._sequence[-1] if self.dialog._sequence else None
        super().canvasReleaseEvent(event)
        if previous is not None and self.dialog._sequence:
            try:
                new_last = self.dialog._sequence[-1]
                if new_last[2] == info.get("label"):
                    self.dialog._confirmed_segment = (previous[2], new_last[2])
            except Exception:
                pass
        self._last_preview_key = None
        self.dialog.realtime_distance_label.setText("实时距离：—")
        self.dialog._refresh_ui()

    def clear_preview_only(self):
        super().clear_preview_only()
        try:
            self.dialog._clear_hover_preview()
        except Exception:
            pass
