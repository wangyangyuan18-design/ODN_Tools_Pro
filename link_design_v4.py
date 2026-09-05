# -*- coding: utf-8 -*-
"""Compatibility layer for project-scoped Link Design management.

Migrates the previous QGIS-project-scoped Link Design state into the active
ODN Project scope. The active ODN Project remains the single source of truth
for Link Design configuration and layer bindings. Written-link status is
reconciled against the current Distribution Cable layer so manual edits made
in QGIS do not leave stale "已写入" records.
"""

import json

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsFeatureRequest

from . import odn_project_context as context
from .link_design_v2 import _project_state_key as _legacy_project_state_key
from .link_design_v3 import LinkDesignDialog as _ProjectScopedLinkDesignDialog


class LinkDesignDialog(_ProjectScopedLinkDesignDialog):
    def _load_saved_state(self):
        # Prefer the ODN-Project-scoped store. Only migrate legacy state when
        # this ODN Project has never had a project-scoped Link Design store.
        self._designs = []
        self._sequence = []
        self._editing_index = None
        self._editing_written_index = None
        settings = QSettings()
        state = None
        try:
            raw = settings.value(self._project_state_key(), "")
            if raw:
                state = json.loads(str(raw))
        except Exception:
            state = None

        if not isinstance(state, dict):
            try:
                raw = settings.value(_legacy_project_state_key(), "")
                if raw:
                    state = json.loads(str(raw))
            except Exception:
                state = None
            if isinstance(state, dict):
                designs = state.get("designs", [])
                draft = state.get("draft")
                self._designs = designs if isinstance(designs, list) else []
                if isinstance(draft, dict):
                    self._restore_draft(draft)
                self._reconcile_written_state()
                self._persist_state()
                return

        if isinstance(state, dict):
            designs = state.get("designs", [])
            draft = state.get("draft")
            self._designs = designs if isinstance(designs, list) else []
            if isinstance(draft, dict):
                self._restore_draft(draft)
        self._reconcile_written_state()

    def _project_state_key(self):
        # Link Design data belongs to the active ODN Project, not the QGIS
        # .qgz file that happens to be open.
        path = context.current_path()
        if not path:
            return "ODNToolsPro/LinkDesign/state/odn/__NO_ACTIVE_ODN_PROJECT__"
        import os
        normalized = os.path.normcase(os.path.abspath(path)).replace("\\", "/")
        return f"ODNToolsPro/LinkDesign/state/odn/{normalized}"

    def _fresh_project_payload(self):
        # Reuse v2's authoritative reload so Project Configuration remains the
        # only source for layer bindings and operational parameters.
        from .link_design_v2 import _fresh_payload
        return _fresh_payload(self)

    def _reconcile_written_state(self):
        """Synchronize stored Link status with the actual Distribution Cable layer."""
        if not self._designs:
            return False
        layer = context.project_layer(self._fresh_project_payload(), "Distribution Cable")
        if layer is None:
            return False

        changed = False
        for design in self._designs:
            if not design.get("written"):
                continue
            fids = design.get("written_fids") or []
            if not fids:
                # Legacy written records are handled lazily by geometry recovery.
                continue
            all_present = True
            for fid in fids:
                try:
                    feature = layer.getFeature(int(fid))
                except Exception:
                    feature = None
                if feature is None or not feature.isValid():
                    all_present = False
                    break
            if all_present:
                continue

            # The user edited/deleted the DC feature(s) directly in QGIS.
            # Do not keep a stale "已写入" state. The Link returns to the
            # editable "已规划" state so it can be safely re-written.
            design["written"] = False
            design["written_fids"] = []
            design["external_change"] = "Distribution Cable 图层已被手动修改，Link 已重新标记为已规划。"
            changed = True

        if changed:
            self._persist_state()
        return changed

    def _recover_written_fids(self, index):
        if index < 0 or index >= len(self._designs):
            return None
        design = self._designs[index]
        existing = design.get("written_fids") or []
        if existing:
            return [int(fid) for fid in existing]
        if not design.get("written"):
            return []

        layer = context.project_layer(self._fresh_project_payload(), "Distribution Cable")
        if layer is None:
            return None
        try:
            expected_features = self._build_layer_features(layer, design)
        except Exception:
            return None

        matched = []
        used = set()
        for expected in expected_features:
            bbox = expected.geometry().boundingBox()
            candidates = []
            request = QgsFeatureRequest().setFilterRect(bbox)
            for feature in layer.getFeatures(request):
                if feature.id() in used:
                    continue
                try:
                    if feature.geometry().equals(expected.geometry()):
                        candidates.append(int(feature.id()))
                except Exception:
                    continue
            if len(candidates) != 1:
                return None
            matched.append(candidates[0])
            used.add(candidates[0])

        design["written_fids"] = matched
        self._persist_state()
        return matched

    def load_design_for_edit(self, index):
        self._reconcile_written_state()
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if 0 <= index < len(self._designs) and self._designs[index].get("written"):
            if self._recover_written_fids(index) is None:
                from qgis.PyQt.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "修改 Link",
                    "无法唯一确定该 Link 对应的 Distribution Cable 要素，因此不会猜测或误删其他线路。",
                )
                return False
        return super().load_design_for_edit(index)

    def _replace_written_link(self, index, design):
        self._recover_written_fids(index)
        return super()._replace_written_link(index, design)

    def delete_link(self, index):
        self._reconcile_written_state()
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if 0 <= index < len(self._designs) and self._designs[index].get("written"):
            if self._recover_written_fids(index) is None:
                from qgis.PyQt.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "删除 Link",
                    "无法唯一确定该 Link 对应的 Distribution Cable 要素，因此不会猜测或误删其他线路。",
                )
                return False
        return super().delete_link(index)

    def open_completed_designs(self):
        # Always reconcile against the live Distribution Cable layer immediately
        # before showing the completed-design browser.
        self._reconcile_written_state()
        return super().open_completed_designs()
