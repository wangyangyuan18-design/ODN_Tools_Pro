# -*- coding: utf-8 -*-
"""Link Design change detection.

The detector deliberately uses a simple, deterministic model:
1. Keep the last confirmed Link Design as the baseline.
2. Re-run EVERY saved Link against the CURRENT FDT/FAT/Pole Edge data.
3. Compare the new result with the baseline.
4. Show only Links whose result changed.
5. On confirmation, replace only those changed Links with the freshly rebuilt
   result. FAT order is never optimized automatically.
"""

import copy
import json

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QSettings, Qt
from qgis.core import QgsDistanceArea, QgsGeometry, QgsPointXY, QgsProject

from . import link_design_v9 as _v9
from . import odn_project_context as context

SNAPSHOT_KEY_PREFIX = "ODNToolsPro/LinkDesign/change_snapshot_v2/"
POINT_TOLERANCE_M = 0.05
GEOMETRY_TOLERANCE_M = 0.20


def _project_key():
    path = QgsProject.instance().fileName() or "__UNSAVED_PROJECT__"
    return SNAPSHOT_KEY_PREFIX + str(path).replace("\\", "/")


def _save_snapshot(snapshot):
    QSettings().setValue(_project_key(), json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    QSettings().sync()


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
        return QgsPointXY(feature.geometry().asPoint())
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
            return float(point_a.distance(point_b))
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
        if crs.isGeographic():
            da = QgsDistanceArea()
            da.setSourceCrs(crs, QgsProject.instance().transformContext())
            pa = ga.asPolyline()
            pb = gb.asPolyline()
            if pa and pb:
                return float(max(da.measureLine(pa[0], pb[0]), da.measureLine(pa[-1], pb[-1])))
        return float(ga.hausdorffDistance(gb))
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
    payload = _v9._fresh_payload(controller)
    layers = {role: context.project_layer(payload, role) for role in ("FDT", "FAT", "Pole Edge")}
    snapshot = {"version": 2, "designs": {}}
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
            point = _feature_point(layers.get(typ), fid)
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
    try:
        controller._engine = None
        return controller._prepare_engine()
    except Exception:
        return None


def _sequence(design, fallback=None):
    seq = design.get("sequence_ids") or fallback or []
    return [(str(x[0]), int(x[1])) for x in seq if len(x) >= 2]


def _node_label(design, typ, fid):
    for item in design.get("nodes", []) or []:
        try:
            if int(item[0]) == int(fid):
                return str(item[1]) if len(item) > 1 else str(fid)
        except Exception:
            continue
    for idx, item in enumerate(design.get("sequence_ids", []) or []):
        try:
            if str(item[0]) == typ and int(item[1]) == int(fid):
                labels = design.get("sequence", []) or []
                if idx < len(labels):
                    return str(labels[idx])
        except Exception:
            pass
    return f"{typ}{fid}"


def _route_points(route):
    return [[float(p.x()), float(p.y())] for p in route.get("points", [])]


def _route_segment(route):
    return {
        "from": route.get("from_label", ""),
        "to": route.get("to_label", ""),
        "distance": round(float(route.get("distance", 0.0)), 3),
        "pole_edge_distance": round(float(route.get("pole_edge_distance", 0.0)), 3),
        "edge_count": len(route.get("edge_sequence", [])),
        "points": _route_points(route),
    }


def _deleted_fats(seq, fat_layer):
    result = []
    for typ, fid in seq:
        if typ != "FAT":
            continue
        if _feature_point(fat_layer, fid) is None:
            result.append(fid)
    return result


def _rebuild_design_preview(engine, candidate_seq):
    routes = []
    for first, second in zip(candidate_seq[:-1], candidate_seq[1:]):
        route = engine.route(first[0], first[1], second[0], second[1])
        if route is None:
            return None, f"{first[0]}{first[1]} → {second[0]}{second[1]} 无法沿当前 Pole Edge 建立路径。"
        routes.append(route)
    return [_route_segment(r) for r in routes], None


def detect_changes(controller):
    """Re-run every Link on current data and return only changed Links."""
    baseline = ensure_snapshot(controller)
    engine = _current_engine(controller)
    if engine is None:
        return {"snapshot": baseline, "changes": [], "error": "当前无法建立最新 Pole Edge 路由引擎。"}

    payload = _v9._fresh_payload(controller)
    fdt_layer = context.project_layer(payload, "FDT")
    fat_layer = context.project_layer(payload, "FAT")
    edge_layer = context.project_layer(payload, "Pole Edge")
    if fdt_layer is None or fat_layer is None or edge_layer is None:
        return {"snapshot": baseline, "changes": [], "error": "缺少 FDT、FAT 或 Pole Edge 图层。"}

    changes = []
    for index, design in enumerate(getattr(controller, "_designs", []) or []):
        key = _design_key(design)
        base = baseline.get("designs", {}).get(key)
        if not base or not design.get("segments"):
            continue

        original_seq = _sequence(design, base.get("sequence_ids"))
        if not original_seq:
            continue

        deleted_fids = _deleted_fats(original_seq, fat_layer)
        candidate_seq = [x for x in original_seq if not (x[0] == "FAT" and x[1] in deleted_fids)]
        reasons = []

        old_pos = base.get("node_positions", {}) or {}
        for typ, fid in original_seq:
            if typ not in ("FDT", "FAT"):
                continue
            old_raw = old_pos.get(f"{typ}:{fid}")
            layer = fdt_layer if typ == "FDT" else fat_layer
            current = _feature_point(layer, fid)
            if current is None:
                if typ == "FAT" and fid in deleted_fids:
                    reasons.append(f"{_node_label(design, typ, fid)} 已删除")
                elif typ == "FDT":
                    reasons.append("FDT 已删除")
                continue
            if old_raw is not None:
                moved = _point_distance_m(
                    QgsPointXY(float(old_raw[0]), float(old_raw[1])),
                    current,
                    layer.crs(),
                )
                if moved is not None and moved > POINT_TOLERANCE_M:
                    reasons.append(f"{_node_label(design, typ, fid)} 移动")

        if len([x for x in candidate_seq if x[0] == "FAT"]) == 0:
            changes.append({
                "key": key,
                "design_index": index,
                "repairable": False,
                "type": "Link 变化",
                "detail": f"{key}：删除 FAT 后没有剩余 FAT。",
                "reasons": reasons or ["FAT 删除"],
            })
            continue

        preview_segments, route_error = _rebuild_design_preview(engine, candidate_seq)
        if preview_segments is None:
            changes.append({
                "key": key,
                "design_index": index,
                "repairable": False,
                "type": "Link 变化",
                "detail": f"{key}：最新数据无法完整重算。",
                "reasons": reasons + ["当前 Pole Edge 无法建立完整路径"],
                "error": route_error,
            })
            continue

        old_segments = base.get("segments", []) or []
        baseline_seq = _sequence(base)
        sequence_changed = candidate_seq != baseline_seq
        geometry_changed = len(preview_segments) != len(old_segments)
        if not geometry_changed:
            for old_seg, new_seg in zip(old_segments, preview_segments):
                d = _geometry_distance_m(old_seg.get("points", []), new_seg.get("points", []), edge_layer.crs())
                if d is not None and d > GEOMETRY_TOLERANCE_M:
                    geometry_changed = True
                    break
                if old_seg.get("from") != new_seg.get("from") or old_seg.get("to") != new_seg.get("to"):
                    geometry_changed = True
                    break

        if not reasons and geometry_changed:
            reasons.append("Pole Edge / 路径变化")
        elif reasons and geometry_changed:
            reasons.append("最新路径已重新计算")

        if sequence_changed or geometry_changed:
            changes.append({
                "key": key,
                "design_index": index,
                "repairable": True,
                "type": "Link 变化",
                "detail": f"{key}  {'、'.join(dict.fromkeys(reasons)) if reasons else '最新重算结果发生变化'}",
                "reasons": list(dict.fromkeys(reasons)) or ["最新重算结果发生变化"],
            })

    return {"snapshot": baseline, "changes": changes, "error": None}


def _build_updated_design(controller, design):
    """Build the entire Link again from current source layers; keep FAT order."""
    engine = _current_engine(controller)
    if engine is None:
        raise RuntimeError("当前无法建立最新 Pole Edge 路由引擎。")

    updated = copy.deepcopy(design)
    original_seq = _sequence(design)
    payload = _v9._fresh_payload(controller)
    fat_layer = context.project_layer(payload, "FAT")
    if fat_layer is None:
        raise RuntimeError("缺少 FAT 图层。")

    deleted = set(_deleted_fats(original_seq, fat_layer))
    sequence = [x for x in original_seq if not (x[0] == "FAT" and x[1] in deleted)]
    if not [x for x in sequence if x[0] == "FAT"]:
        raise RuntimeError("删除 FAT 后 Link 没有剩余 FAT，无法自动修复。")

    routes = []
    for first, second in zip(sequence[:-1], sequence[1:]):
        route = engine.route(first[0], first[1], second[0], second[1])
        if route is None:
            raise RuntimeError(f"{first[0]}{first[1]} → {second[0]}{second[1]} 无法沿当前 Pole Edge 建立路径。")
        routes.append(route)

    labels_by_id = {}
    old_sequence = _sequence(design)
    old_labels = design.get("sequence", []) or []
    for idx, item in enumerate(old_sequence):
        if idx < len(old_labels):
            labels_by_id[item] = str(old_labels[idx])

    updated["sequence_ids"] = [[typ, fid] for typ, fid in sequence]
    updated["sequence"] = [labels_by_id.get(x, _node_label(design, x[0], x[1])) for x in sequence]
    updated["nodes"] = [[fid, labels_by_id.get(("FAT", fid), _node_label(design, "FAT", fid))] for typ, fid in sequence if typ == "FAT"]
    updated["segments"] = [_route_segment(r) for r in routes]
    updated["length"] = round(sum(float(r.get("distance", 0.0)) for r in routes), 3)
    updated["written"] = False
    updated["needs_resync"] = True
    updated["_resync_source"] = {
        "fdt": design.get("fdt", ""),
        "link": design.get("link", ""),
        "source_crs": design.get("source_crs", ""),
        "segments": copy.deepcopy(design.get("segments", []) or []),
    }
    return updated


def apply_changes(controller, detection_result):
    """Replace only changed Links with their full latest-data rebuild."""
    changes = detection_result.get("changes", []) or []
    indexes = sorted({int(c["design_index"]) for c in changes if c.get("repairable")}, reverse=True)
    if not indexes:
        return 0

    backup = copy.deepcopy(controller._designs)
    try:
        for index in indexes:
            controller._designs[index] = _build_updated_design(controller, controller._designs[index])
        controller._persist_state()
        save_snapshot(controller)
        controller._refresh_ui()
        return len(indexes)
    except Exception:
        controller._designs = backup
        controller._persist_state()
        controller._refresh_ui()
        raise


class ChangeDetectionDialog(QtWidgets.QDialog):
    """Simple report: list changed Links and one confirm-all action."""

    def __init__(self, controller, detection_result, parent=None):
        super().__init__(parent or controller)
        self.controller = controller
        self.result = detection_result
        self.setWindowTitle("变更检测")
        self.resize(560, 400)
        self.setModal(True)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(7)

        total = len(self.result.get("changes", []) or [])
        summary = QtWidgets.QLabel(f"发现 {total} 条 Link 变化")
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

        self.detail = QtWidgets.QLabel("点击 Link 查看原因。")
        self.detail.setWordWrap(True)
        self.detail.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        self.detail.setMinimumHeight(65)
        root.addWidget(self.detail)

        for index, change in enumerate(self.result.get("changes", []) or []):
            reasons = change.get("reasons", []) or [change.get("type", "Link 变化")]
            item = QtWidgets.QTreeWidgetItem([change.get("key", ""), "、".join(reasons)])
            item.setData(0, Qt.UserRole, index)
            self.tree.addTopLevelItem(item)

        self.tree.currentItemChanged.connect(self._show_detail)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

        row = QtWidgets.QHBoxLayout()
        self.confirm_btn = QtWidgets.QPushButton("确认修改")
        self.cancel_btn = QtWidgets.QPushButton("取消")
        row.addStretch(1)
        row.addWidget(self.confirm_btn)
        row.addWidget(self.cancel_btn)
        root.addLayout(row)

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
            "原因：" + "、".join(change.get("reasons", []) or [change.get("type", "Link 变化")]),
            "处理：使用当前 FDT / FAT / Pole Edge 完整重算该 Link。",
            "原 FAT 顺序保持不变。",
        ]
        if change.get("error"):
            lines.insert(2, str(change["error"]))
        if not change.get("repairable"):
            lines.append("该 Link 无法自动修复，请进入 Link Design 人工处理。")
        self.detail.setText("\n".join(lines))

    def _confirm_all(self):
        try:
            repaired = apply_changes(self.controller, self.result)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "变更检测", f"确认修改失败，未提交任何 Link 变化：\n{exc}")
            return
        blocked = sum(1 for c in self.result.get("changes", []) or [] if not c.get("repairable"))
        message = f"已重新计算并更新 {repaired} 条 Link。\n\nFAT 顺序保持不变。"
        if repaired:
            message += "\n\n请执行“确定并写入图层”同步 Distribution Cable。"
        if blocked:
            message += f"\n\n另有 {blocked} 条 Link 无法自动修复。"
        QtWidgets.QMessageBox.information(self, "变更检测", message)
        self.accept()
