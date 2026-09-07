# -*- coding: utf-8 -*-
"""Runtime compatibility patch for Distribution Cable offset writes.

Some historical Link records can contain output points expressed in the
metric work CRS even when the design metadata says they are in Pole Edge CRS.
The offset engine already validates both interpretations; this patch lets the
final DC writer use the same evidence and records the exact interpretation
used. It never accepts fewer than two points and never suppresses a failed
coordinate transform.
"""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    Qgis,
)

from . import link_design_v12 as _v12

_INSTALLED = False
_ORIGINAL = _v12._segment_geometry_in_target


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / DC Write", level)
    except Exception:
        pass


def _crs(authid):
    if not authid:
        return None
    try:
        value = QgsCoordinateReferenceSystem(str(authid))
        return value if value.isValid() else None
    except Exception:
        return None


def _build(points, source_crs, target_crs):
    if len(points) < 2 or source_crs is None or target_crs is None:
        return None
    try:
        out = [QgsPointXY(float(p[0]), float(p[1])) for p in points]
    except Exception:
        return None
    if source_crs != target_crs:
        try:
            transform = QgsCoordinateTransform(
                source_crs, target_crs, QgsProject.instance().transformContext()
            )
            out = [QgsPointXY(transform.transform(p)) for p in out]
        except Exception:
            return None
    try:
        geom = QgsGeometry.fromPolylineXY(out)
    except Exception:
        return None
    if geom is None or geom.isEmpty():
        return None
    return geom


def _patched_segment_geometry_in_target(design, segment, target_crs):
    points = segment.get("points", []) or []
    if len(points) < 2:
        _log(
            f"[geometry-invalid] points={len(points)}; "
            f"source={design.get('source_crs','')}; target={target_crs.authid()}",
            Qgis.Warning,
        )
        return None

    declared = _crs(design.get("source_crs"))
    layout = design.get("layout") or {}
    work = _crs(layout.get("work_crs"))

    # Normal path: saved Link points are authoritative Pole Edge coordinates.
    candidates = []
    if declared is not None:
        candidates.append(("declared", declared))
    if work is not None and (declared is None or work != declared):
        candidates.append(("work_crs", work))

    for label, source in candidates:
        geom = _build(points, source, target_crs)
        if geom is not None:
            _log(
                f"[geometry-write-crs] source={label}; source_crs={source.authid()}; "
                f"target_crs={target_crs.authid()}; points={len(points)}"
            )
            return geom

    _log(
        f"[geometry-invalid] all CRS interpretations failed; "
        f"declared={design.get('source_crs','')}; "
        f"work={layout.get('work_crs','')}; target={target_crs.authid()}; "
        f"points={len(points)}; "
        f"first={points[0]}; last={points[-1]}",
        Qgis.Critical,
    )
    return None


def install_offset_write_patch():
    global _INSTALLED
    if _INSTALLED:
        return
    _v12._segment_geometry_in_target = _patched_segment_geometry_in_target
    _INSTALLED = True
