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
from qgis.core import QgsDistanceArea, QgsFeatureRequest, QgsGeometry, QgsPointXY, QgsUnitTypes

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
                self._repair_saved_distances()
                self._persist_state()
                return

        if isinstance(state, dict):
            designs = state.get("designs", [])
            draft = state.get("draft")
            self._designs = designs if isinstance(designs, list) else []
            if isinstance(draft, dict):
                self._restore_draft(draft)
        self._reconcile_written_state()
        self._repair_saved_distances()

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

    def _distance_meters(self, points, source_crs):
        if not source_crs or not source_crs.isValid() or len(points) < 2:
            return None
        geometry = QgsGeometry.fromPolylineXY(points)
        distance = QgsDistanceArea()
        distance.setSourceCrs(source_crs, context.QgsProject.instance().transformContext() if hasattr(context, "QgsProject") else None)
        try:
            ellipsoid = source_crs.ellipsoidAcronym()
        except Exception:
            ellipsoid = ""
        if not ellipsoid or str(ellipsoid).upper() in ("NONE", "NONE(0)"):
            ellipsoid = "WGS84"
        try:
            distance.setEllipsoid(str(ellipsoid))
        except Exception:
            distance.setEllipsoid("WGS84")
        measured = float(distance.measureLength(geometry) or 0.0)
        try:
            return float(distance.convertLengthMeasurement(measured, distance.lengthUnits(), QgsUnitTypes.DistanceMeters))
        except Exception:
            return measured

    def _repair_saved_distances(self):
        """Repair legacy saved 0/degree-valued distances from their stored route points."""
        if not self._designs:
            return False
        from qgis.core import QgsCoordinateReferenceSystem, QgsProject
        changed = False
        for design in self._designs:
            raw_crs = design.get("source_crs")
            if not raw_crs:
                continue
            source_crs = QgsCoordinateReferenceSystem(str(raw_crs))
            if not source_crs.isValid():
                continue
            total = 0.0
            valid_segments = 0
            for segment in design.get("segments", []):
                raw_points = segment.get("points", [])
                try:
                    points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw_points]
                except Exception:
                    continue
                measured = self._distance_meters_with_project(points, source_crs, QgsProject.instance())
                if measured is None:
                    continue
                if abs(float(segment.get("distance", 0.0) or 0.0) - measured) > 0.05:
                    segment["distance"] = round(measured, 3)
                    changed = True
                total += measured
                valid_segments += 1
            if valid_segments:
                old_total = float(design.get("length", 0.0) or 0.0)
                if abs(old_total - total) > 0.05:
                    design["length"] = round(total, 3)
                    changed = True
        if changed:
            self._persist_state()
        return changed

    @staticmethod
    def _distance_meters_with_project(points, source_crs, project):
        if not source_crs or not source_crs.isValid() or len(points) < 2:
            return None
        geometry = QgsGeometry.fromPolylineXY(points)
        distance = QgsDistanceArea()
        distance.setSourceCrs(source_crs, project.transformContext())
        try:
            ellipsoid = source_crs.ellipsoidAcronym()
        except Exception:
            ellipsoid = ""
        if not ellipsoid or str(ellipsoid).upper() in ("NONE", "NONE(0)"):
            ellipsoid = "WGS84"
        try:
            distance.setEllipsoid(str(ellipsoid))
        except Exception:
            distance.setEllipsoid("WGS84")
        measured = float(distance.measureLength(geometry) or 0.0)
        try:
            return float(distance.convertLengthMeasurement(measured, distance.lengthUnits(), QgsUnitTypes.DistanceMeters))
        except Exception:
            return measured

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
        self._repair_saved_distances()
        return super().open_completed_designs()
