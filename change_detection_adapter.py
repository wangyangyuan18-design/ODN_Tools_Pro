# -*- coding: utf-8 -*-
"""Reliable Link Design change detection.

Design rule:
1. Never repair individual route segments.
2. Re-run every saved Link against the current FDT/FAT/Pole Edge data.
3. Compare the newly calculated complete Link with the last confirmed snapshot.
4. Only Links whose complete result changed are proposed for confirmation.
5. Keep the saved FAT order; a missing FAT is removed from that saved sequence.
"""

import copy
import json

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QSettings, Qt
from qgis.core import QgsDistanceArea, QgsGeometry, QgsPointXY, QgsProject

from . import change_detection as _cd
from . import link_design_v9 as _v9
from . import odn_project_context as context

SNAPSHOT_KEY_PREFIX = "ODNToolsPro/LinkDesign/change_snapshot_v2/"
POINT_TOLERANCE_M = 0.05
GEOMETRY_TOLERANCE_M = 0.20


def _project_key():
    path = QgsProject.instance().fileName() or "__UNSAVED_PROJECT__"
    return SNAPSHOT_KEY_PREFIX + str(path).replace("\\", "/")


def _load_snapshot():
    try:
        raw = QSettings().value(_project_key(), "")
        if raw:
            value = json.loads(str(raw))
            return value if isinstance(value, dict) else None
    except Exception:
        pass
    return None


def _save_snapshot(snapshot):
    QSettings().setValue(_project_key(), json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    QSettings().sync()


def _feature_point(layer, fid):
    if layer is None:
        return None
    try:
        f = layer.getFeature(int(fid))
        if not f.isValid() or f.geometry().isEmpty():
            return None
        return QgsPointXY(f.geometry().asPoint())
    except Exception:
        return None


def _distance_m(a, b, crs):
    if a is None or b is None:
        return None
    try:
        da = QgsDistanceArea()
        da.setSourceCrs(crs, QgsProject.instance().transformContext())
        return float(da.measureLine(a, b))
    except Exception:
        try:
            return float(a.distance(b))
        except Exception:
            return None


def _snapshot_from_saved_designs(controller):
    """Use persisted Link routes as the historical baseline on first use."""
    snapshot = {"version": 2, "designs": {}}
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
        seq = [(str(x[0]), int(x[1])) for x in design.get("sequence_ids", []) or [] if len(x) >= 2]
        segments = entry["segments"]
        for i, (typ, fid) in enumerate(seq):
            pts = []
            if i == 0 and segments:
                pts = segments[0].get("points", []) or []
                pt = pts[0] if pts else None
            elif i > 0 and i - 1 < len(segments):
                pts = segments[i - 1].get("points", []) or []
                pt = pts[-1] if pts else None
            else:
                pt = None
            if pt is not None and len(pt) >= 2:
                entry["node_positions"][f"{typ}:{fid}"] = [float(pt[0]), float(pt[1])]
        snapshot["designs"][f"{design.get('fdt','')}/{design.get('link','')}"] = entry
    return snapshot


def _ensure_baseline(controller):
    snapshot = _load_snapshot()
    if isinstance(snapshot, dict) and snapshot.get("version") == 2 and isinstance(snapshot.get("designs"), dict):
        return snapshot
    snapshot = _snapshot_from_saved_designs(controller)
    if snapshot.get("designs"):
        _save_snapshot(snapshot)
    return snapshot


def save_snapshot(controller):
    snapshot = {"version": 2, "designs": {}}
    for design in getattr(controller, "_designs", []) or []:
        if not design.get("segments"):
            continue
        snapshot["designs"][f"{design.get('fdt','')}/{design.get('link','')}"] = {
            "sequence_ids": copy.deepcopy(design.get("sequence_ids", [])),
            "sequence": copy.deepcopy(design.get("sequence", [])),
            "source_crs": design.get("source_crs", ""),
            "segments": [_cd._route_signature(s) for s in design.get("segments", [])],
        }
    _save_snapshot(snapshot)
    return snapshot


def build_snapshot(controller):
    return save_snapshot(controller)


def _route_to_segment(route):
    return {
        "from": str(route.get("from_label", "")),
        "to": str(route.get("to_label", "")),
        "distance": round(float(route.get("distance", 0.0)), 3),
        "pole_edge_distance": round(float(route.get("pole_edge_distance", 0.0)), 3),
        "edge_count": len(route.get("edge_sequence", [])),
        "points": [[float(p.x()), float(p.y())] for p in route.get("points", [])],
    }


def _geom_diff(a_points, b_points, crs):
    ga = _cd._line_geometry(a_points)
    gb = _cd._line_geometry(b_points)
    if ga is None or gb is None:
        return True
    try:
        if crs.isGeographic():
            da = QgsDistanceArea()
            da.setSourceCrs(crs, QgsProject.instance().transformContext())
            pa, pb = ga.asPolyline(), gb.asPolyline()
            if pa and pb:
                return max(da.measureLine(pa[0], pb[0]), da.measureLine(pa[-1], pb[-1])) > GEOMETRY_TOLERANCE_M
        return float(ga.hausdorffDistance(gb)) > GEOMETRY_TOLERANCE_M
    except Exception:
        return True


def _labels_for_sequence(design, sequence):
    labels = {}
    old_ids = design.get("sequence_ids", []) or []
    old_labels = design.get("sequence", []) or []
    for i, item in enumerate(old_ids):
        if len(item) >= 2 and i < len(old_labels):
            labels[(str(item[0]), int(item[1]))] = str(old_labels[i])
    return labels


def _full_rerun(controller, design, fdt_layer, fat_layer, engine):
    seq = [(str(x[0]), int(x[1])) for x in design.get("sequence_ids", []) or [] if len(x) >= 2]
    if not seq:
        return None, "没有保存的 Link 顺序。", None

    # Preserve topology/order. A deleted FAT is the only automatic sequence edit.
    cleaned = []
    deleted_fats = []
    for typ, fid in seq:
        layer = fdt_layer if typ == "FDT" else fat_layer if typ == "FAT" else None
        point = _feature_point(layer, fid)
        if point is None:
            if typ == "FAT":
                deleted_fats.append(fid)
                continue
            return None, f"{typ}{fid} 已不存在，无法重新计算该 Link。", {"unrepairable": True}
        cleaned.append((typ, fid))

    if not any(typ == "FAT" for typ, _ in cleaned):
        return None, "删除 FAT 后该 Link 已没有剩余 FAT。", {"unrepairable": True}

    routes = []
    for first, second in zip(cleaned[:-1], cleaned[1:]):
        try:
            route = engine.route(first[0], first[1], second[0], second[1])
        except Exception:
            route = None
        if route is None:
            return None, f"{first[0]}{first[1]} → {second[0]}{second[1]} 无法沿当前 Pole Edge 建立路径。", {"unrepairable": True}
        routes.append(route)

    labels = _labels_for_sequence(design, seq)
    proposed = copy.deepcopy(design)
    proposed["sequence_ids"] = [[typ, int(fid)] for typ, fid in cleaned]
    proposed["sequence"] = [labels.get((typ, fid), str(fid)) for typ, fid in cleaned]
    proposed["nodes"] = [[fid, labels.get((typ, fid), str(fid))] for typ, fid in cleaned if typ == "FAT"]
    proposed["segments"] = [_route_to_segment(r) for r in routes]
    proposed["length"] = round(sum(float(r.get("distance", 0.0)) for r in routes), 3)
    proposed["written"] = False
    proposed["needs_resync"] = True
    proposed["_resync_source"] = {
        "fdt": design.get("fdt", ""),
        "link": design.get("link", ""),
        "source_crs": design.get("source_crs", ""),
        "segments": copy.deepcopy(design.get("segments", []) or []),
    }
    return proposed, None, {"deleted_fats": deleted_fats}


def detect_changes(controller):
    """Full-rerun comparison. No Link mutation occurs during detection."""
    baseline = _ensure_baseline(controller)
    payload = _v9._fresh_payload(controller)
    fdt_layer = context.project_layer(payload, "FDT")
    fat_layer = context.project_layer(payload, "FAT")
    controller._engine = None
    engine = controller._prepare_engine()
    if engine is None:
        return {"snapshot": baseline, "changes": [], "error": "当前无法建立 Pole Edge 路由引擎。"}

    changes = []
    for index, design in enumerate(getattr(controller, "_designs", []) or []):
        if not design.get("segments"):
            continue
        key = f"{design.get('fdt','')}/{design.get('link','')}"
        base = baseline.get("designs", {}).get(key)
        if not base:
            continue
        proposed, error, meta = _full_rerun(controller, design, fdt_layer, fat_layer, engine)
        if proposed is None:
            changes.append({
                "key": key,
                "design_index": index,
                "type": "无法重新计算",
                "detail": f"{key}  {error}",
                "repairable": False,
                "reason": error,
            })
            continue

        old_seq = [(str(x[0]), int(x[1])) for x in base.get("sequence_ids", []) or [] if len(x) >= 2]
        new_seq = [(str(x[0]), int(x[1])) for x in proposed.get("sequence_ids", []) or [] if len(x) >= 2]
        changed = old_seq != new_seq
        reasons = []
        if meta.get("deleted_fats"):
            changed = True
            reasons.append("FAT 删除")
        if not changed:
            old_segments = base.get("segments", []) or []
            new_segments = proposed.get("segments", []) or []
            if len(old_segments) != len(new_segments):
                changed = True
                reasons.append("路线段数变化")
            else:
                crs = engine.edge_layer.crs()
                for old_seg, new_seg in zip(old_segments, new_segments):
                    if abs(float(old_seg.get("distance", 0.0)) - float(new_seg.get("distance", 0.0))) > 0.01:
                        changed = True
                        reasons.append("线路距离变化")
                        break
                    if _geom_diff(old_seg.get("points", []) or [], new_seg.get("points", []) or [], crs):
                        changed = True
                        reasons.append("Pole Edge 路径变化")
                        break
        if changed:
            if not reasons:
                reasons.append("FDT/FAT/Pole Edge 更新后路线发生变化")
            changes.append({
                "key": key,
                "design_index": index,
                "type": "线路发生变化",
                "detail": f"{key}  {'、'.join(reasons)}",
                "repairable": True,
                "proposed": proposed,
                "reasons": reasons,
            })

    return {"snapshot": baseline, "changes": changes, "error": None}


def apply_changes(controller, detection_result):
    """Commit the already calculated proposals; do not calculate again."""
    changes = [c for c in detection_result.get("changes", []) or [] if c.get("repairable") and c.get("proposed")]
    if not changes:
        return 0
    backup = copy.deepcopy(controller._designs)
    try:
        for change in changes:
            idx = int(change["design_index"])
            controller._designs[idx] = copy.deepcopy(change["proposed"])
        controller._persist_state()
        save_snapshot(controller)
        controller._refresh_ui()
        return len(changes)
    except Exception:
        controller._designs = backup
        controller._persist_state()
        controller._refresh_ui()
        raise


class ChangeDetectionDialog(QtWidgets.QDialog):
    def __init__(self, controller, detection_result, parent=None):
        super().__init__(parent or controller)
        self.controller = controller
        self.result = detection_result
        self.setWindowTitle("变更检测")
        self.resize(600, 430)
        root = QtWidgets.QVBoxLayout(self)
        summary = QtWidgets.QLabel(f"发现 {len(self.result.get('changes', []) or [])} 条变化")
        f = summary.font(); f.setBold(True); f.setPointSize(12); summary.setFont(f)
        root.addWidget(summary)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Link", "变化"])
        self.tree.setRootIsDecorated(False)
        root.addWidget(self.tree, 1)
        self.detail = QtWidgets.QLabel("请选择一条变化查看详情。")
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(80)
        root.addWidget(self.detail)
        for i, change in enumerate(self.result.get("changes", []) or []):
            item = QtWidgets.QTreeWidgetItem([change.get("key", ""), change.get("type", "")])
            item.setData(0, Qt.UserRole, i)
            self.tree.addTopLevelItem(item)
        self.tree.currentItemChanged.connect(self._show_detail)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.confirm = QtWidgets.QPushButton("确认修改全部")
        cancel = QtWidgets.QPushButton("取消")
        row.addWidget(self.confirm); row.addWidget(cancel); root.addLayout(row)
        self.confirm.clicked.connect(self._confirm)
        cancel.clicked.connect(self.reject)
        if not any(c.get("repairable") and c.get("proposed") for c in self.result.get("changes", []) or []):
            self.confirm.setEnabled(False)

    def _show_detail(self, current, previous=None):
        if current is None:
            return
        try:
            c = self.result["changes"][int(current.data(0, Qt.UserRole))]
        except Exception:
            return
        lines = [c.get("key", ""), f"变化：{c.get('type','')}"]
        if c.get("reasons"):
            lines.append("原因：" + "、".join(c["reasons"]))
        if c.get("reason"):
            lines.append(str(c["reason"]))
        if c.get("repairable"):
            lines.append("处理：按最新 FDT / FAT / Pole Edge 完整重新计算该 Link。")
            lines.append("原有 FAT 顺序保持不变。")
        self.detail.setText("\n".join(lines))

    def _confirm(self):
        try:
            count = apply_changes(self.controller, self.result)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "变更检测", f"确认修改失败：\n{exc}")
            return
        QtWidgets.QMessageBox.information(self, "变更检测", f"已更新 {count} 条 Link。\n\nFAT 顺序保持不变。")
        self.accept()
