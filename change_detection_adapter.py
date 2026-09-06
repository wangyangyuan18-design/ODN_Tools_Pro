# -*- coding: utf-8 -*-
"""Compatibility wrapper for reliable first-time Link Design change detection."""

import copy

from qgis.core import QgsPointXY, QgsProject

from . import change_detection as _cd


def _snapshot_from_saved_designs(controller):
    """Build a baseline from persisted Link route geometry, not current layers.

    This is used only when no change-detection snapshot exists yet. Existing
    saved Link routes represent the last confirmed design and therefore must be
    treated as the historical baseline; otherwise the first detection would
    snapshot already-modified FDT/FAT/Pole Edge data and report no change.
    """
    snapshot = {"version": 1, "designs": {}}
    for design in getattr(controller, "_designs", []) or []:
        if not design.get("segments"):
            continue
        entry = {
            "sequence_ids": copy.deepcopy(design.get("sequence_ids", [])),
            "sequence": copy.deepcopy(design.get("sequence", [])),
            "source_crs": design.get("source_crs", ""),
            "segments": [_cd._route_signature(s) for s in design.get("segments", [])],
            "node_positions": {},
        }

        # Recover node positions from persisted route endpoints. The route
        # engine stores each segment from ODN node A to node B, so the first
        # point of segment i and the last point of segment i-1 are the saved
        # positions of node i. This is enough to detect normal FDT/FAT moves.
        segments = entry["segments"]
        seq = []
        for item in entry["sequence_ids"] or []:
            if len(item) >= 2:
                seq.append((str(item[0]), int(item[1])))

        for idx, (typ, fid) in enumerate(seq):
            point = None
            if idx == 0 and segments:
                pts = segments[0].get("points", []) or []
                if pts:
                    point = pts[0]
            elif idx > 0 and idx - 1 < len(segments):
                pts = segments[idx - 1].get("points", []) or []
                if pts:
                    point = pts[-1]
            if point is not None and len(point) >= 2:
                entry["node_positions"][f"{typ}:{fid}"] = [
                    float(point[0]), float(point[1])
                ]

        snapshot["designs"][_cd._design_key(design)] = entry
    return snapshot


def detect_changes(controller):
    """Run detection against a reliable historical baseline and fresh graph."""
    existing = _cd._load_snapshot()
    if not (isinstance(existing, dict) and isinstance(existing.get("designs"), dict)):
        baseline = _snapshot_from_saved_designs(controller)
        if baseline.get("designs"):
            _cd._save_snapshot(baseline)

    # Force the actual detector to rebuild the routing graph from the current
    # source layers rather than reusing the interactive cached engine.
    controller._engine = None
    return _cd.detect_changes(controller)


ChangeDetectionDialog = _cd.ChangeDetectionDialog
save_snapshot = _cd.save_snapshot
build_snapshot = _cd.build_snapshot
