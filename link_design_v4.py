# -*- coding: utf-8 -*-
"""Compatibility layer for project-scoped Link Design management.

Migrates the previous QGIS-project-scoped Link Design state into the active
ODN Project scope when no project-scoped state exists. Written-link status is
reconciled against the current Distribution Cable layer so manual edits made
in QGIS do not leave stale "已写入" records. For old written Links without
stored DC feature ids, recover ids only when every stored segment has exactly
one matching Distribution Cable geometry; ambiguous matches are rejected
rather than guessed.
"""

import json

from qgis.PyQt.QtCore import QSettings
from qgis.core import QgsFeatureRequest, QgsGeometry

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
                # Persist the migrated state immediately under this ODN Project.
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

    def _fresh_project_payload(self):
        # Reuse v2's authoritative reload so Project Configuration remains the
        # only source for layer bindings and operational parameters.
        from .link_design_v2 import _fresh_payload
        return _fresh_payload(self)

    def _reconcile_written_state(self):
        """Make Link status reflect the actual Distribution Cable layer.

        The QGIS layer is authoritative for whether a previously-written Link
        still exists. When all recorded DC features are gone, the Link becomes
        "已规划" again and can be written again. When only part of a Link was
        manually deleted, its stored ownership is cleared and the Link becomes
        "已规划" rather than guessing at which remaining features are safe to
        keep or delete.
        """
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
                # Legacy records are handled lazily by _recover_written_fids.
                continue
            existing = []
            for fid in fids:
                try:
                    feature = layer.getFeature(int(fid))
                except Exception:
                    feature = None
                if feature is not None and feature.isValid():
                    existing.append(int(fid))
            if len(existing) == len(fids):
                continue

            # The user changed the DC layer outside Link Design. Do not keep a
            # stale "已写入" flag. If every feature is gone, this cleanly makes
            # the Link writable again. If only some are gone, we deliberately
            # do not guess which remaining geometry should be removed.
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
        return super().open_completed_designs()


class CompletedDesignDialog(_ProjectScopedLinkDesignDialog.__mro__[1]):
    """Unused compatibility declaration; actual dialog is injected below."""
    pass
