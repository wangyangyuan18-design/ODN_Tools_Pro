# -*- coding: utf-8 -*-
"""Link Design change detection and targeted repair.

Change detection compares the last confirmed Link Design snapshot with the
current FDT/FAT/Pole Edge source layers. It never re-optimizes FAT ordering.
Only the affected segments of the affected Link are rebuilt.
"""

import copy
import json

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QSettings, Qt
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
)

from . import odn_project_context as context
from . import link_design_v9 as _v9

SNAPSHOT_KEY_PREFIX = "ODNToolsPro/LinkDesign/change_snapshot/"
POINT_TOLERANCE_M = 0.05
GEOMETRY_TOLERANCE_M = 0.20


def _project_key():
    path = QgsProject.instance().fileName() or "__UNSAVED_PROJECT__"
    return SNAPSHOT_KEY_PREFIX + str(path).replace("\\", "/")


def _save_snapshot(snapshot):
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    settings = QSettings()
    settings.setValue(_project_key(), raw)
    settings.sync()


def _load_snapshot():
    try:
        raw = QSettings().value(_project_key(), "")
        if raw:
            value = json.loads(str(raw))
            return value if isinstance(value, dict) else None
    except Exception:
        pass
    return None


def _feature_point(layer, fid):
    if layer is None or fid is None:
        return None
    try:
        feature = layer.getFeature(int(fid))
    except Exception:
        return None
    if not feature.isValid() or feature.geometry().isEmpty():
        return None
    try:
        point = feature.geometry().asPoint()
        return QgsPointXY(point)
    except Exception:
        return None


def _point_distance_m(point_a, point_b, crs):
    if point_a is None or point_b is None:
        return None
    try:
        da = QgsDistanceArea()
        da.setSourceCrs(crs, QgsProject.instance().transformContext())
        return float(da.measureLine(point_a, point_b))
    except Exception:
        try:
            return point_a.distance(point_b)
        except Exception:
            return None


def _line_geometry(points):
    try:
        pts = [QgsPointXY(float(p[0]), float(p[1])) for p in points]
    except Exception:
        return None
    return QgsGeometry.fromPolylineXY(pts) if len(pts) >= 2 else None


def _geometry_distance_m(points_a, points_b, crs):
    ga = _line_geometry(points_a)
    gb = _line_geometry(points_b)
    if ga is None or gb is None:
        return None
    try:
        d = ga.hausdorffDistance(gb)
        if d is None:
            return None
        # Stored route points are in the Pole Edge CRS. For normal projected
        # project CRSs this is already meters; for geographic CRSs fall back
        # to endpoint/chord distance through QgsDistanceArea.
        if crs.isGeographic():
            da = QgsDistanceArea()
            da.setSourceCrs(crs, QgsProject.instance().transformContext())
            pa = ga.asPolyline()
            pb = gb.asPolyline()
            if pa and pb:
                return max(
                    da.measureLine(pa[0], pb[0]),
                    da.measureLine(pa[-1], pb[-1]),
                )
        return float(d)
    except Exception:
        return None


def _route_signature(segment):
    return {
        "from": str(segment.get("from", "")),
        "to": str(segment.get("to", "")),
        "points": copy.deepcopy(segment.get("points", []) or []),
    }


def _design_key(design):
    return f"{design.get('fdt', '')}/{design.get('link', '')}"


def build_snapshot(controller):
    """Capture source-node positions and confirmed route geometry."""
    payload = _v9._fresh_payload(controller)
    layers = {
        role: context.project_layer(payload, role)
        for role in ("FDT", "FAT", "Pole Edge")
    }
    snapshot = {"version": 1, "designs": {}}
    for design in getattr(controller, "_designs", []) or []:
        if not design.get("segments"):
            continue
        entry = {
            "sequence_ids": copy.deepcopy(design.get("sequence_ids", [])),
            "sequence": copy.deepcopy(design.get("sequence", [])),
            "source_crs": design.get("source_crs", ""),
            "segments": [_route_signature(s) for s in design.get("segments", [])],
            "node_positions": {},
        }
        for item in design.get("sequence_ids", []) or []:
            if len(item) < 2:
                continue
            typ, fid = str(item[0]), int(item[1])
            layer = layers.get(typ)
            point = _feature_point(layer, fid)
            if point is not None:
                entry["node_positions"][f"{typ}:{fid}"] = [float(point.x()), float(point.y())]
        snapshot["designs"][_design_key(design)] = entry
    return snapshot


def ensure_snapshot(controller):
    snapshot = _load_snapshot()
    if isinstance(snapshot, dict) and isinstance(snapshot.get("designs"), dict):
        return snapshot
    snapshot = build_snapshot(controller)
    _save_snapshot(snapshot)
    return snapshot


def save_snapshot(controller):
    snapshot = build_snapshot(controller)
    _save_snapshot(snapshot)
    return snapshot


def _current_engine(controller):
    engine = getattr(controller, "_engine", None)
    if engine is None:
        try:
            engine = controller._prepare_engine()
        except Exception:
            engine = None
    return engine


def _resolve_current_sequence(design, snapshot_entry):
    seq = design.get("sequence_ids") or snapshot_entry.get("sequence_ids") or []
    result = []
    for item in seq:
        if len(item) >= 2:
            result.append((str(item[0]), int(item[1])))
    return result


def _current_route(controller, engine, first, second):
    try:
        return engine.route(first[0], first[1], second[0], second[1])
    except Exception:
        return None


def _route_points(route):
    return [[float(p.x()), float(p.y())] for p in route.get("points", [])]


def _route_segment_from_result(route):
    return {
        "from": route.get("from_label", ""),
        "to": route.get("to_label", ""),
        "distance": round(float(route.get("distance", 0.0)), 3),
        "pole_edge_distance": round(float(route.get("pole_edge_distance", 0.0)), 3),
        "edge_count": len(route.get("edge_sequence", [])),
        "points": _route_points(route),
    }


def detect_changes(controller):
    """Return change records. This function does not mutate Link designs."""
    snapshot = ensure_snapshot(controller)
    payload = _v9._fresh_payload(controller)
    fdt_layer = context.project_layer(payload, "FDT")
    fat_layer = context.project_layer(payload, "FAT")
    edge_layer = context.project_layer(payload, "Pole Edge")
    engine = _current_engine(controller)
    if engine is None:
        return {"snapshot": snapshot, "changes": [], "error": "当前无法建立 Pole Edge 路由引擎。"}

    changes = []
    seen = set()

    for design in getattr(controller, "_designs", []) or []:
        key = _design_key(design)
        base = snapshot.get("designs", {}).get(key)
        if not base:
            # Newly saved Link with no historical snapshot is not a change.
            continue
        seq = _resolve_current_sequence(design, base)
        if not seq:
            continue

        base_pos = base.get("node_positions", {}) or {}
        node_changes = {}
        for typ, fid in seq:
            layer = fdt_layer if typ == "FDT" else fat_layer if typ == "FAT" else None
            old_raw = base_pos.get(f"{typ}:{fid}")
            current = _feature_point(layer, fid)
            if current is None:
                if typ == "FAT" and old_raw is not None:
                    node_changes[(typ, fid)] = {"kind": "deleted", "old": old_raw}
                elif typ == "FDT" and old_raw is not None:
                    node_changes[(typ, fid)] = {"kind": "deleted", "old": old_raw}
                continue
            if old_raw is None:
                continue
            old_point = QgsPointXY(float(old_raw[0]), float(old_raw[1]))
            crs = layer.crs()
            moved = _point_distance_m(old_point, current, crs)
            if moved is not None and moved > POINT_TOLERANCE_M:
                node_changes[(typ, fid)] = {
                    "kind": "moved",
                    "old": old_raw,
                    "new": [float(current.x()), float(current.y())],
                    "distance_m": moved,
                }

        # Detect missing FATs and position changes first; those are the root
        # cause when a route also changes as a consequence.
        for (typ, fid), change in node_changes.items():
            if typ == "FAT" and change["kind"] == "moved":
                idx = next((i for i, x in enumerate(seq) if x == (typ, fid)), None)
                if idx is None:
                    continue
                affected = []
                if idx > 0:
                    affected.append(idx - 1)
                if idx < len(seq) - 1:
                    affected.append(idx)
                changes.append({
                    "key": key,
                    "type": "FAT 移动",
                    "detail": f"{design.get('fdt','')}/{design.get('link','')}  {next((x[2] for x in design.get('nodes', []) if int(x[0]) == fid), f'FAT{fid}')} 移动",
                    "design_index": getattr(controller, "_designs", []).index(design),
                    "node_index": idx,
                    "affected_segments": affected,
                    "repairable": True,
                    "node_distance_m": float(change["distance_m"]),
                    "old_position": change["old"],
                    "new_position": change["new"],
                })
            elif typ == "FAT" and change["kind"] == "deleted":
                idx = next((i for i, x in enumerate(seq) if x == (typ, fid)), None)
                if idx is None:
                    continue
                label = next((str(x[1]) for x in design.get("nodes", []) if int(x[0]) == fid), str(fid))
                repairable = len([x for x in seq if x[0] == "FAT"]) > 1
                changes.append({
                    "key": key,
                    "type": "FAT 已删除",
                    "detail": f"{key}  {label} 已删除",
                    "design_index": getattr(controller, "_designs", []).index(design),
                    "node_index": idx,
                    "affected_segments": [max(0, idx - 1)],
                    "repairable": repairable,
                    "deleted_fat_id": fid,
                    "deleted_fat_label": label,
                    "reason": "删除后仍有其他 FAT，可自动连接前后节点。" if repairable else "该 Link 已没有其他 FAT，无法自动形成有效 Link。",
                })
            elif typ == "FDT" and change["kind"] == "moved":
                changes.append({
                    "key": key,
                    "type": "FDT 移动",
                    "detail": f"{key}  FDT 位置发生变化",
                    "design_index": getattr(controller, "_designs", []).index(design),
                    "affected_segments": [0],
                    "repairable": True,
                    "node_distance_m": float(change["distance_m"]),
                    "old_position": change["old"],
                    "new_position": change["new"],
                })

        affected_by_node = {i for change in changes if change["key"] == key for i in change.get("affected_segments", [])}

        # For unchanged nodes, route geometry differences identify Pole Edge
        # changes. Do not report these when already explained by a moved node.
        base_segments = base.get("segments", []) or []
        route_count = min(len(seq) - 1, len(base_segments))
        for seg_idx in range(max(0, route_count)):
            first, second = seq[seg_idx], seq[seg_idx + 1]
            route = _current_route(controller, engine, first, second)
            if route is None:
                changes.append({
                    "key": key,
                    "type": "Pole Edge 变化",
                    "detail": f"{key}  第 {seg_idx + 1} 段 Pole Edge 路径无法按当前网络复现",
                    "design_index": getattr(controller, "_designs", []).index(design),
                    "affected_segments": [seg_idx],
                    "repairable": False,
                    "reason": "当前 Pole Edge 无法建立该段完整路径。",
                })
                continue
            old_points = base_segments[seg_idx].get("points", []) if seg_idx < len(base_segments) else []
            distance = _geometry_distance_m(old_points, _route_points(route), edge_layer.crs())
            if distance is not None and distance > GEOMETRY_TOLERANCE_M and seg_idx not in affected_by_node:
                changes.append({
                    "key": key,
                    "type": "Pole Edge 变化",
                    "detail": f"{key}  第 {seg_idx + 1} 段 Pole Edge 发生变化",
                    "design_index": getattr(controller, "_designs", []).index(design),
                    "affected_segments": [seg_idx],
                    "repairable": True,
                    "route_change_distance_m": float(distance),
                })

    # De-duplicate by Link + type + affected segment(s), preserving discovery order.
    unique = []
    for change in changes:
        sig = (
            change.get("key"),
            change.get("type"),
            tuple(change.get("affected_segments", [])),
            change.get("deleted_fat_id"),
        )
        if sig in seen:
            continue
        seen.add(sig)
        unique.append(change)
    return {"snapshot": snapshot, "changes": unique, "error": None}


def _build_updated_design(controller, design, changes):
    """Repair affected routes while preserving the exact original sequence."""
    engine = _current_engine(controller)
    if engine is None:
        raise RuntimeError("当前无法建立 Pole Edge 路由引擎。")

    updated = copy.deepcopy(design)
    sequence = []
    for item in updated.get("sequence_ids", []) or []:
        if len(item) >= 2:
            sequence.append((str(item[0]), int(item[1])))

    deleted_ids = {int(c["deleted_fat_id"]) for c in changes if c.get("type") == "FAT 已删除" and c.get("deleted_fat_id") is not None}
    if deleted_ids:
        sequence = [x for x in sequence if not (x[0] == "FAT" and x[1] in deleted_ids)]

    if len([x for x in sequence if x[0] == "FAT"]) == 0:
        raise RuntimeError("删除 FAT 后 Link 没有剩余 FAT，无法自动修复。")

    # Capture the old physical route so the existing writer can replace it.
    source_copy = {
        "fdt": design.get("fdt", ""),
        "link": design.get("link", ""),
        "source_crs": design.get("source_crs", ""),
        "segments": copy.deepcopy(design.get("segments", []) or []),
    }

    routes = []
    for first, second in zip(sequence[:-1], sequence[1:]):
        route = engine.route(first[0], first[1], second[0], second[1])
        if route is None:
            raise RuntimeError(f"{first[0]}-{first[1]} → {second[0]}-{second[1]} 无法沿当前 Pole Edge 建立路径。")
        routes.append(route)

    updated["sequence_ids"] = [[x[0], int(x[1])] for x in sequence]
    old_labels = updated.get("sequence", []) or []
    label_by_id = {}
    for i, item in enumerate(updated.get("sequence_ids", []) or []):
        if i < len(old_labels) and len(item) >= 2:
            label_by_id[(str(item[0]), int(item[1]))] = str(old_labels[i])
    updated["sequence"] = [label_by_id.get(x, str(x[1])) for x in sequence]
    updated["nodes"] = [
        [x[1], label_by_id.get(x, str(x[1]))]
        for x in sequence if x[0] == "FAT"
    ]
    updated["segments"] = [_route_segment_from_result(route) for route in routes]
    updated["length"] = round(sum(float(r.get("distance", 0.0)) for r in routes), 3)
    updated["written"] = False
    updated["needs_resync"] = bool(design.get("written") or design.get("needs_resync")) or bool(source_copy["segments"])
    updated["_resync_source"] = source_copy
    return updated


def apply_changes(controller, detection_result):
    """Apply all repairable detected changes. Returns repaired Link count."""
    changes = detection_result.get("changes", []) or []
    grouped = {}
    for change in changes:
        if change.get("repairable"):
            grouped.setdefault(change["design_index"], []).append(change)
    if not grouped:
        return 0

    backup = copy.deepcopy(controller._designs)
    repaired = 0
    try:
        for index, link_changes in grouped.items():
            design = controller._designs[index]
            updated = _build_updated_design(controller, design, link_changes)
            controller._designs[index] = updated
            repaired += 1
        controller._persist_state()
        save_snapshot(controller)
        controller._refresh_ui()
        return repaired
    except Exception:
        controller._designs = backup
        controller._persist_state()
        controller._refresh_ui()
        raise


class ChangeDetectionDialog(QtWidgets.QDialog):
    """Compact change report with one-click confirmation of all repairs."""

    def __init__(self, controller, detection_result, parent=None):
        super().__init__(parent or controller)
        self.controller = controller
        self.result = detection_result
        self.setWindowTitle("变更检测")
        self.resize(620, 470)
        self.setModal(True)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(7)

        summary = QtWidgets.QLabel()
        total = len(self.result.get("changes", []) or [])
        summary.setText(f"发现 {total} 条变化")
        font = summary.font()
        font.setBold(True)
        font.setPointSize(12)
        summary.setFont(font)
        root.addWidget(summary)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Link", "变化"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        root.addWidget(self.tree, 1)

        self.detail = QtWidgets.QLabel("请选择一条变化查看详情。")
        self.detail.setWordWrap(True)
        self.detail.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        self.detail.setMinimumHeight(95)
        root.addWidget(self.detail)

        if self.result.get("error"):
            self.detail.setText(str(self.result["error"]))

        for index, change in enumerate(self.result.get("changes", []) or []):
            item = QtWidgets.QTreeWidgetItem([change.get("key", ""), change.get("type", "")])
            item.setData(0, Qt.UserRole, index)
            if not change.get("repairable"):
                item.setDisabled(False)
                item.setText(1, f"{change.get('type','')}（无法自动修复）")
            self.tree.addTopLevelItem(item)

        self.tree.currentItemChanged.connect(self._show_detail)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

        button_row = QtWidgets.QHBoxLayout()
        self.confirm_btn = QtWidgets.QPushButton("确认修改全部")
        self.cancel_btn = QtWidgets.QPushButton("取消")
        self.confirm_btn.setMinimumHeight(30)
        self.cancel_btn.setMinimumHeight(30)
        button_row.addStretch(1)
        button_row.addWidget(self.confirm_btn)
        button_row.addWidget(self.cancel_btn)
        root.addLayout(button_row)

        self.confirm_btn.clicked.connect(self._confirm_all)
        self.cancel_btn.clicked.connect(self.reject)
        if not any(c.get("repairable") for c in self.result.get("changes", []) or []):
            self.confirm_btn.setEnabled(False)

    def _show_detail(self, current, previous=None):
        if current is None:
            return
        try:
            index = int(current.data(0, Qt.UserRole))
            change = self.result["changes"][index]
        except Exception:
            return
        lines = [
            change.get("key", ""),
            f"变化：{change.get('type','')}",
        ]
        if change.get("node_distance_m") is not None:
            lines.append(f"位置变化：{float(change['node_distance_m']):.1f} m")
        if change.get("route_change_distance_m") is not None:
            lines.append(f"路线几何变化：{float(change['route_change_distance_m']):.1f} m")
        if change.get("reason"):
            lines.append(str(change["reason"]))
        if change.get("type") in ("FAT 移动", "FDT 移动"):
            # Recalculate current segment distances for the visible detail only.
            try:
                design = self.controller._designs[int(change["design_index"])]
                segs = design.get("segments", []) or []
                affected = change.get("affected_segments", []) or []
                for seg_idx in affected:
                    if 0 <= seg_idx < len(segs):
                        old_distance = float(segs[seg_idx].get("distance", 0.0) or 0.0)
                        lines.append(f"原规划段距离：{old_distance:.1f} m")
            except Exception:
                pass
        lines.append("保持原有 FAT 顺序，不进行自动拓扑优化。")
        if not change.get("repairable"):
            lines.append("需要进入“修改选中 Link”人工处理。")
        self.detail.setText("\n".join(lines))

    def _confirm_all(self):
        try:
            repaired = apply_changes(self.controller, self.result)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "变更检测", f"确认修改失败，未提交 Link 设计变化：\n{exc}")
            return
        unrepairable = sum(1 for c in self.result.get("changes", []) or [] if not c.get("repairable"))
        message = f"已自动修复 {repaired} 条受影响 Link。\n\nFAT 顺序保持不变。"
        if repaired:
            message += "\n\n这些 Link 已标记为待写入，稍后执行“确定并写入图层”即可同步 Distribution Cable。"
        if unrepairable:
            message += f"\n\n另有 {unrepairable} 条变化无法自动修复，请通过“修改选中 Link”人工处理。"
        QtWidgets.QMessageBox.information(self, "变更检测", message)
        self.accept()
