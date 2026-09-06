# -*- coding: utf-8 -*-
"""Link Design v12: allow FAT reallocation from written Links.

Keeps v11 visual-state behavior. A FAT that already belongs to another Link
may be moved even when the source Link has already been written. The source
Link is rebuilt without the moved FAT and marked for Distribution Cable
resynchronization; the current Link receives the FAT.
"""

import copy

from qgis.PyQt import QtWidgets

from . import link_design_v9 as _v9
from . import link_design_v11 as _v11


_original_save_current_link = _v9._CoreController.save_current_link


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
            "其旧路线会标记为需要重新同步。",
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


def _mark_source_for_resync(self, index, design):
    stale = getattr(self, "_stale_written_designs", None)
    if stale is None:
        stale = {}
        self._stale_written_designs = stale
    if index not in stale:
        stale[index] = copy.deepcopy(design)


def _v12_save_current_link(self):
    """Save current Link and commit source FAT moves, including written sources."""
    pending = dict(getattr(self, "_pending_reassignments", {}) or {})
    backup = copy.deepcopy(self._designs)
    backup_stale = copy.deepcopy(getattr(self, "_stale_written_designs", {}) or {})

    try:
        sources = {}
        for fid, index in pending.items():
            sources.setdefault(int(index), []).append(int(fid))

        for index, fids in sorted(sources.items(), reverse=True):
            if index < 0 or index >= len(self._designs):
                continue
            old_design = self._designs[index]
            if old_design.get("written"):
                _mark_source_for_resync(self, index, old_design)
            rebuilt = self._remove_fats_from_design(old_design, fids)
            if rebuilt is None:
                raise RuntimeError(
                    f"原 Link {old_design.get('fdt','')}/{old_design.get('link','')} 删除 FAT 后无法形成有效 Link"
                )
            rebuilt["written"] = False
            rebuilt["needs_resync"] = True
            self._designs[index] = rebuilt

        result = _original_save_current_link(self)
        if not result:
            self._designs = backup
            self._stale_written_designs = backup_stale
            self._pending_reassignments = pending
            self._persist_state()
            self._refresh_ui()
            return False

        self._pending_reassignments = {}
        self._persist_state()
        return True
    except Exception as exc:
        self._designs = backup
        self._stale_written_designs = backup_stale
        self._pending_reassignments = pending
        self.status.setText(f"状态：保存失败：{exc}")
        self._persist_state()
        self._refresh_ui()
        QtWidgets.QMessageBox.warning(self, "保存规划", str(exc))
        return False


_v9._CoreController.add_fat = _v12_add_fat
_v9._CoreController.save_current_link = _v12_save_current_link
_v9.LinkDesignMapToolV9 = _v11.LinkDesignMapToolV11


class LinkDesignDock(_v11.LinkDesignDock):
    """v12 dock: v11 UI/highlighting plus written-Link FAT reallocation."""

    pass
