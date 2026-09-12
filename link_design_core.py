# -*- coding: utf-8 -*-
"""Authoritative Link Design core.

Runtime architecture:
    __init__.py -> link_design.py -> link_design_core.py
                       -> odn_project_routing.py
                       -> cable_offset_core.py

This module intentionally contains the complete Link Design business layer:
state persistence, draft handling, FDT/FAT selection, route generation,
validation, save/edit/delete CRUD, Distribution Cable reconciliation and the
compact QGIS UI. No versioned Link Design module is used at runtime.
"""

import copy
import json
import os
from math import sqrt

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import QEvent, QSettings, Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsWkbTypes,
    Qgis,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from . import odn_project_context as context
from .odn_project_routing import OdnProjectRouteEngine
from . import cable_offset_core as offset_core

SETTINGS_PREFIX = "ODNToolsPro/LinkDesign/state/odn/"
LINK_ID_FIELD = "_ODN_LINK_ID"
SEGMENT_ID_FIELD = "_ODN_SEGMENT"
LINK_FDT_FIELD = "_ODN_FDT"
LINK_NAME_FIELD = "_ODN_LINK"


def _log(message, level=Qgis.Info):
    try:
        QgsMessageLog.logMessage(str(message), "ODN_Tools_Pro / Link Design", level)
    except Exception:
        pass


def fresh_payload(controller=None):
    path = context.current_path()
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if payload.get("format") == "ODN Project":
                context.set_current(path, payload=payload)
                return payload
        except (OSError, ValueError, TypeError):
            pass
    if controller is not None:
        value = getattr(controller, "_odn_project_payload", None)
        if isinstance(value, dict):
            return value
    return context.current_payload() or {}


def _param(controller, key, default=None):
    return (fresh_payload(controller).get("parameters", {}) or {}).get(key, default)


def max_links(controller):
    try:
        value = int(_param(controller, "fdt_max_links", 8))
    except (TypeError, ValueError):
        value = 8
    return max(1, value)


def max_fats(controller):
    try:
        value = int(_param(controller, "max_fats_per_link", 4))
    except (TypeError, ValueError):
        value = 4
    return max(1, value)


def total_fats(controller):
    layer = context.project_layer(fresh_payload(controller), "FAT")
    return int(layer.featureCount()) if layer is not None else 0


def assigned_fat_ids(controller, exclude_index=None, include_draft=False):
    result = set()
    for index, design in enumerate(getattr(controller, "_designs", []) or []):
        if exclude_index is not None and index == exclude_index:
            continue
        for item in design.get("nodes", []) or []:
            try:
                result.add(int(item[0]))
            except (TypeError, ValueError, IndexError):
                continue
    if include_draft:
        for item in getattr(controller, "_sequence", []) or []:
            if item[0] == "FAT":
                result.add(int(item[1]))
    return result


def planned_fat_count(controller):
    return len(assigned_fat_ids(controller, include_draft=True))


def next_link(controller, fdt_label, exclude_index=None):
    used = {
        str(d.get("link"))
        for index, d in enumerate(controller._designs)
        if index != exclude_index and d.get("fdt") == fdt_label
    }
    for number in range(1, max_links(controller) + 1):
        candidate = f"L{number}"
        if candidate not in used:
            return candidate
    return None


def _state_key():
    path = context.current_path()
    if not path:
        path = "__NO_ACTIVE_ODN_PROJECT__"
    return SETTINGS_PREFIX + os.path.normcase(os.path.abspath(path)).replace("\\", "/")


def _crs_from_authid(authid):
    if not authid:
        return None
    crs = QgsCoordinateReferenceSystem(str(authid))
    return crs if crs.isValid() else None


def _geometry_from_segment(design, segment, target_crs):
    raw = segment.get("points", []) or []
    if len(raw) < 2:
        return None
    try:
        points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw]
    except (TypeError, ValueError, IndexError):
        return None
    source = _crs_from_authid(design.get("source_crs"))
    if source is not None and source != target_crs:
        transform = QgsCoordinateTransform(source, target_crs, QgsProject.instance().transformContext())
        try:
            points = [transform.transform(point) for point in points]
        except Exception:
            return None
    return QgsGeometry.fromPolylineXY(points)


def _ensure_link_fields(layer):
    existing = {field.name() for field in layer.fields()}
    additions = []
    for name, qtype in (
        (LINK_ID_FIELD, 10),
        (SEGMENT_ID_FIELD, 4),
        (LINK_FDT_FIELD, 10),
        (LINK_NAME_FIELD, 10),
    ):
        if name not in existing:
            field = QgsField(name, qtype)
            if qtype == 10:
                field.setLength(128)
            additions.append(field)
    if not additions:
        return True
    if not layer.isEditable() and not layer.startEditing():
        return False
    if not layer.dataProvider().addAttributes(additions):
        return False
    layer.updateFields()
    return True


def _stable_link_id(design):
    existing = str(design.get("_link_id") or "").strip()
    if existing:
        return existing
    parts = []
    for item in design.get("sequence_ids") or design.get("nodes") or []:
        try:
            parts.append(f"{item[0]}:{int(item[1])}")
        except Exception:
            parts.append(str(item))
    base = "|".join(parts) or os.urandom(8).hex()
    import hashlib
    design["_link_id"] = "LNK_" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:20].upper()
    return design["_link_id"]


def _feature_ids_for_link(layer, link_id):
    idx = layer.fields().indexOf(LINK_ID_FIELD)
    if idx < 0:
        return []
    result = []
    for feature in layer.getFeatures():
        if str(feature.attribute(idx) or "") == str(link_id):
            result.append(int(feature.id()))
    return result


class LinkDesignController:
    """Single business controller for Link Design."""

    def __init__(self, iface, host=None):
        self.iface = iface
        self.host = host
        self._designs = []
        self._sequence = []
        self._direction = "FDT_TO_FAT"
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        self._editing_index = None
        self._draw_active = False
        self._engine = None
        self._tool = None
        self._saved_bands = []
        self._hover_segment = None
        self._hover_distance = 0.0
        self._paused_by_source_change = False
        self._source_monitor_connections = []
        self._odn_project_payload = None
        self._load_state()

    # ---------- persistence / draft ----------
    def _draft_payload(self):
        if not self._sequence:
            return None
        return {
            "sequence": [list(item) for item in self._sequence],
            "direction": self._direction,
            "current_fdt": self._current_fdt,
            "current_fdt_id": self._current_fdt_id,
            "current_link": self._current_link,
            "editing_index": self._editing_index,
        }

    def _persist_state(self):
        state = {"designs": self._designs, "draft": self._draft_payload()}
        try:
            settings = QSettings()
            settings.setValue(_state_key(), json.dumps(state, ensure_ascii=False, separators=(",", ":")))
            settings.sync()
        except Exception as exc:
            _log(f"[persist-fail] {type(exc).__name__}: {exc}", Qgis.Warning)

    def _load_state(self):
        self._designs = []
        self._sequence = []
        try:
            raw = QSettings().value(_state_key(), "")
            state = json.loads(str(raw)) if raw else None
        except Exception:
            state = None
        if not isinstance(state, dict):
            return
        designs = state.get("designs", [])
        self._designs = designs if isinstance(designs, list) else []
        draft = state.get("draft")
        if isinstance(draft, dict):
            self._restore_draft(draft)

    def _restore_draft(self, draft):
        try:
            self._sequence = [
                (str(item[0]), int(item[1]), str(item[2]))
                for item in draft.get("sequence", []) if len(item) >= 3
            ]
        except Exception:
            self._sequence = []
        self._direction = str(draft.get("direction") or "FDT_TO_FAT")
        self._current_fdt = draft.get("current_fdt")
        try:
            self._current_fdt_id = int(draft.get("current_fdt_id")) if draft.get("current_fdt_id") is not None else None
        except (TypeError, ValueError):
            self._current_fdt_id = None
        self._current_link = draft.get("current_link")
        try:
            self._editing_index = int(draft.get("editing_index")) if draft.get("editing_index") is not None else None
        except (TypeError, ValueError):
            self._editing_index = None

    def _clear_draft(self):
        self._sequence = []
        self._direction = "FDT_TO_FAT"
        self._current_fdt = None
        self._current_fdt_id = None
        self._current_link = None
        self._editing_index = None

    # ---------- project / route ----------
    def _prepare_engine(self):
        payload = fresh_payload(self)
        fdt = context.project_layer(payload, "FDT")
        fat = context.project_layer(payload, "FAT")
        edge = context.project_layer(payload, "Pole Edge")
        missing = [role for role, layer in (("FDT", fdt), ("FAT", fat), ("Pole Edge", edge)) if layer is None]
        if missing:
            self._warn("链路设计", "当前项目缺少必要图层绑定：\n\n" + "\n".join(missing))
            return None
        for role, layer, geom in (("FDT", fdt, QgsWkbTypes.PointGeometry), ("FAT", fat, QgsWkbTypes.PointGeometry), ("Pole Edge", edge, QgsWkbTypes.LineGeometry)):
            if QgsWkbTypes.geometryType(layer.wkbType()) != geom:
                self._warn("链路设计", f"项目配置中的 {role} 几何类型不正确。")
                return None
        attach = max(0.01, float(_param(self, "fat_pole_max_distance", 3.0) or 3.0))
        try:
            engine = OdnProjectRouteEngine(self.iface, payload, attach)
        except Exception as exc:
            self._warn("链路设计", f"无法建立 Pole Edge 路由引擎：\n{exc}")
            return None
        if not engine.ready():
            self._warn("链路设计", "当前 FDT / FAT / Pole Edge 无法建立有效路由网络。")
            return None
        return engine

    def _coincident_route(self, first, second):
        """Return a valid zero-length segment when FDT and FAT share a point."""
        if {str(first[0]).upper(), str(second[0]).upper()} != {"FDT", "FAT"}:
            return None
        try:
            _, a = self._engine.point_by_id(first[0], int(first[1]))
            _, b = self._engine.point_by_id(second[0], int(second[1]))
            if a is None or b is None:
                return None
            pa = self._engine._point_in_edge_crs(a)
            pb = self._engine._point_in_edge_crs(b)
            if self._engine._point_distance(pa, pb) > 1e-6:
                return None
            return {
                "from_label": first[2], "to_label": second[2],
                "distance": 0.0, "pole_edge_distance": 0.0,
                "edge_sequence": [], "points": [QgsPointXY(pa)],
                "zero_length": True,
            }
        except Exception:
            return None

    def _route(self, first, second):
        if self._engine is None:
            self._engine = self._prepare_engine()
        if self._engine is None:
            return None
        coincident = self._coincident_route(first, second)
        if coincident is not None:
            return coincident
        try:
            return self._engine.route(first[0], int(first[1]), second[0], int(second[1]))
        except Exception as exc:
            _log(f"[route-fail] {first} -> {second}: {type(exc).__name__}: {exc}", Qgis.Warning)
            return None

    def _restore_authoritative_routes(self):
        self._engine = self._engine or self._prepare_engine()
        if self._engine is None:
            return False
        changed = False
        failures = 0
        edge_authid = self._engine.edge_layer.crs().authid()
        for design in self._designs:
            sequence = design.get("sequence_ids", []) or []
            if len(sequence) < 2:
                continue
            rebuilt = []
            ok = True
            for index, (first, second) in enumerate(zip(sequence[:-1], sequence[1:])):
                labels = design.get("sequence", []) or []
                first = (str(first[0]), int(first[1]), labels[index] if index < len(labels) else str(first[1]))
                second = (str(second[0]), int(second[1]), labels[index + 1] if index + 1 < len(labels) else str(second[1]))
                route = self._route(first, second)
                if route is None:
                    ok = False
                    failures += 1
                    break
                segment = {
                    "from": route.get("from_label", first[2]),
                    "to": route.get("to_label", second[2]),
                    "distance": round(float(route.get("distance", 0.0) or 0.0), 3),
                    "pole_edge_distance": round(float(route.get("pole_edge_distance", 0.0) or 0.0), 3),
                    "edge_sequence": list(route.get("edge_sequence", []) or []),
                    "edge_count": len(route.get("edge_sequence", []) or []),
                    "points": [[float(p.x()), float(p.y())] for p in route.get("points", [])],
                    "zero_length": bool(route.get("zero_length", False)),
                }
                rebuilt.append(segment)
            if not ok:
                continue
            new_length = round(sum(float(s.get("distance", 0.0) or 0.0) for s in rebuilt), 3)
            if rebuilt != design.get("segments", []) or abs(float(design.get("length", 0.0) or 0.0) - new_length) > 0.001:
                changed = True
            design["segments"] = rebuilt
            design["length"] = new_length
            design["source_crs"] = edge_authid
            if design.get("written"):
                design["external_change"] = None
        if changed:
            self._persist_state()
        _log(f"[route-restore] failed={failures}; changed={int(changed)}")
        return failures == 0

    # ---------- source monitoring ----------
    def _disconnect_source_monitors(self):
        for signal, slot in self._source_monitor_connections:
            try:
                signal.disconnect(slot)
            except Exception:
                pass
        self._source_monitor_connections = []

    def _install_source_monitors(self):
        self._disconnect_source_monitors()
        payload = fresh_payload(self)
        for role in ("FDT", "FAT", "Pole Edge"):
            layer = context.project_layer(payload, role)
            if layer is None:
                continue
            for name in ("featureAdded", "featureDeleted", "geometryChanged", "attributeValueChanged", "committedFeaturesAdded", "committedFeaturesRemoved", "committedGeometriesChanges", "committedAttributeValuesChanges"):
                signal = getattr(layer, name, None)
                if signal is None:
                    continue
                try:
                    signal.connect(self._source_changed)
                    self._source_monitor_connections.append((signal, self._source_changed))
                except Exception:
                    pass

    def _source_changed(self, *args):
        if not self._draw_active:
            return
        self._paused_by_source_change = True
        self._stop_tool()
        self._set_status("已暂停：检测到 FDT/FAT/Pole Edge 数据变化。完成地图编辑后点击“继续规划”。")

    # ---------- planning ----------
    def start_design(self):
        if self._draw_active:
            self.pause_planning()
            return True
        self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        if not self._sequence:
            self._direction = "FDT_TO_FAT"
            self._current_fdt = None
            self._current_fdt_id = None
            self._current_link = None
            self._editing_index = None
            self._set_status("规划中：请选择 FDT。")
        else:
            self._set_status("已恢复草稿：继续选择 FAT。")
        return self._activate_map_tool()

    def pause_planning(self):
        self._paused_by_source_change = False
        self._stop_tool()
        self._persist_state()

    def resume_planning(self):
        self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self._set_status("规划中：请选择下一个 FAT。")
        return self._activate_map_tool()

    def _activate_map_tool(self):
        self._tool = LinkDesignMapToolV9(self.iface, self._engine, self)
        self.iface.mapCanvas().setMapTool(self._tool)
        self._draw_active = True
        self._install_source_monitors()
        self._tool.start()
        self._refresh_ui()
        return True

    def _start_from_hit(self, info):
        if info.get("typ") != "FDT":
            self._set_status("链路必须从 FDT 开始。")
            return False
        if self._sequence:
            self._set_status("当前 Link 已开始，只能选择 FAT。")
            return False
        link = self._current_link or next_link(self, info["label"], exclude_index=self._editing_index)
        if not link:
            self._set_status(f"{info['label']} 已达到最大 Link 数：{max_links(self)}。")
            return False
        self._current_fdt = info["label"]
        self._current_fdt_id = int(info["feature_id"])
        self._current_link = link
        self._sequence = [("FDT", int(info["feature_id"]), str(info["label"]))]
        self._persist_state()
        self._refresh_ui()
        return True

    def add_fat(self, info):
        if not self._draw_active or info.get("typ") != "FAT" or not self._sequence:
            return False
        fid = int(info["feature_id"])
        if any(item[0] == "FAT" and int(item[1]) == fid for item in self._sequence):
            self._set_status(f"{info['label']} 已经在当前 Link 中。")
            return False
        count = sum(1 for item in self._sequence if item[0] == "FAT")
        if count >= max_fats(self):
            self._set_status(f"当前 Link 已达到 FAT 上限：{max_fats(self)}。")
            return False
        route = self._route(self._sequence[-1], ("FAT", fid, str(info["label"])))
        if route is None:
            self._set_status(f"{self._sequence[-1][2]} → {info['label']} 无法沿 Pole Edge 建立完整路径。")
            return False
        self._sequence.append(("FAT", fid, str(info["label"])))
        self._persist_state()
        self._refresh_ui()
        if self._tool:
            self._tool.refresh_route_preview()
        return True

    def _validate_design(self, design):
        errors = []
        fats = list(design.get("nodes", []) or [])
        if not design.get("fdt") or not design.get("link"):
            errors.append("缺少 FDT 或 Link 编号。")
        if not fats:
            errors.append("Link 至少需要 1 个 FAT。")
        if len(fats) > max_fats(self):
            errors.append(f"FAT 数量 {len(fats)} 超过上限 {max_fats(self)}。")
        try:
            link_no = int(str(design.get("link", "L0")).upper().replace("L", ""))
            if link_no < 1 or link_no > max_links(self):
                errors.append(f"Link 编号超出项目范围 L1-L{max_links(self)}。")
        except ValueError:
            errors.append("Link 编号格式无效。")
        if len(design.get("sequence_ids", []) or []) < 2:
            errors.append("缺少完整 Link 拓扑。")
        if len(design.get("segments", []) or []) != len(design.get("sequence_ids", []) or []) - 1:
            errors.append("路线段数量与 Link 拓扑不一致。")
        return errors

    def _make_design(self):
        if not self._sequence:
            self._set_status("当前没有可保存的规划。")
            return None
        fats = [item for item in self._sequence if item[0] == "FAT"]
        if not fats:
            self._set_status("当前 Link 至少需要 1 个 FAT。")
            return None
        fdt = next((item for item in self._sequence if item[0] == "FDT"), None)
        if fdt is None:
            self._set_status("当前规划缺少 FDT。")
            return None
        link = self._current_link or next_link(self, self._current_fdt or fdt[2], exclude_index=self._editing_index)
        if link is None:
            self._set_status("当前 FDT 没有可用 Link 编号。")
            return None
        assigned = assigned_fat_ids(self, exclude_index=self._editing_index)
        duplicates = [item[2] for item in fats if int(item[1]) in assigned]
        if duplicates:
            self._warn("无法保存规划", "以下 FAT 已经属于其他 Link：\n\n" + "、".join(duplicates))
            return None
        segments = []
        for first, second in zip(self._sequence[:-1], self._sequence[1:]):
            route = self._route(first, second)
            if route is None:
                self._set_status(f"{first[2]} → {second[2]} 无法建立完整路线。")
                return None
            segments.append({
                "from": route.get("from_label", first[2]),
                "to": route.get("to_label", second[2]),
                "distance": round(float(route.get("distance", 0.0) or 0.0), 3),
                "pole_edge_distance": round(float(route.get("pole_edge_distance", 0.0) or 0.0), 3),
                "edge_sequence": list(route.get("edge_sequence", []) or []),
                "edge_count": len(route.get("edge_sequence", []) or []),
                "points": [[float(p.x()), float(p.y())] for p in route.get("points", [])],
                "zero_length": bool(route.get("zero_length", False)),
            })
        design = {
            "fdt": self._current_fdt or fdt[2],
            "fdt_id": int(self._current_fdt_id if self._current_fdt_id is not None else fdt[1]),
            "link": str(link),
            "nodes": [(int(item[1]), item[2]) for item in fats],
            "sequence": [item[2] for item in self._sequence],
            "sequence_ids": [(item[0], int(item[1])) for item in self._sequence],
            "direction": "FDT_TO_FAT",
            "written": False,
            "written_fids": [],
            "source_crs": self._engine.edge_layer.crs().authid(),
            "length": round(sum(float(s["distance"]) for s in segments), 3),
            "segments": segments,
        }
        errors = self._validate_design(design)
        if errors:
            self._warn("无法保存规划", "\n".join(errors))
            return None
        return design

    # ---------- CRUD ----------
    def save_current_link(self):
        design = self._make_design()
        if design is None:
            return False
        index = self._editing_index
        if index is not None and 0 <= index < len(self._designs):
            if self._designs[index].get("written"):
                if not self._replace_written_link(index, design):
                    return False
            else:
                self._designs[index] = design
        else:
            same = next((i for i, d in enumerate(self._designs) if d.get("fdt") == design["fdt"] and d.get("link") == design["link"]), None)
            if same is not None:
                if self._designs[same].get("written"):
                    self._warn("已写入 Link", "该 Link 已经写入图层，请从已完成设计中修改。")
                    return False
                self._designs[same] = design
            else:
                self._designs.append(design)
        text = f"已保存 {design['fdt']}/{design['link']}"
        self._clear_draft()
        self._persist_state()
        self._clear_saved_bands()
        self._refresh_ui()
        self._set_status(text)
        return True

    def load_design_for_edit(self, index):
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(self._designs):
            return False
        design = self._designs[index]
        sequence = design.get("sequence_ids", []) or []
        labels = design.get("sequence", []) or []
        if not sequence or len(sequence) != len(labels):
            self._warn("修改 Link", "该 Link 缺少有效规划拓扑。")
            return False
        if design.get("written"):
            if not self._reconcile_written_state(index):
                self._warn("修改 Link", "该 Link 已写入图层，且无法安全确认当前 Distribution Cable 归属。")
                return False
        self._sequence = [(str(item[0]), int(item[1]), str(labels[pos])) for pos, item in enumerate(sequence)]
        self._direction = "FDT_TO_FAT"
        self._current_fdt = str(design.get("fdt", self._sequence[0][2]))
        self._current_fdt_id = int(design.get("fdt_id", self._sequence[0][1]))
        self._current_link = str(design.get("link", ""))
        self._editing_index = index
        self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self._set_status(f"正在修改 {self._current_fdt}/{self._current_link}。")
        self._activate_map_tool()
        self._persist_state()
        return True

    def delete_link(self, index):
        try:
            index = int(index)
        except (TypeError, ValueError):
            return False
        if index < 0 or index >= len(self._designs):
            return False
        design = self._designs[index]
        title = f"{design.get('fdt', '')}/{design.get('link', '')}"
        if design.get("written"):
            if not self._reconcile_written_state(index):
                self._warn("删除 Link", "无法安全确认该 Link 对应的 Distribution Cable 要素。")
                return False
            payload = fresh_payload(self)
            layer = context.project_layer(payload, "Distribution Cable")
            if layer is None:
                return False
            fids = design.get("written_fids") or []
            if not fids:
                self._warn("删除 Link", "已写入 Link 没有有效的 Distribution Cable 要素 ID。")
                return False
            if not layer.isEditable() and not layer.startEditing():
                self._warn("删除 Link", "无法进入 Distribution Cable 编辑状态。")
                return False
            answer = QtWidgets.QMessageBox.question(self.host or self.iface.mainWindow(), "删除 Link", f"确定删除 {title} 吗？\n\n这会同时删除对应 Distribution Cable。", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                return False
            for fid in fids:
                if not layer.deleteFeature(int(fid)):
                    self._warn("删除 Link", f"删除 Distribution Cable 要素 {fid} 失败。")
                    return False
            layer.triggerRepaint()
        else:
            answer = QtWidgets.QMessageBox.question(self.host or self.iface.mainWindow(), "删除 Link", f"确定删除 {title} 吗？", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                return False
        del self._designs[index]
        if self._editing_index == index:
            self._clear_draft()
        elif self._editing_index is not None and self._editing_index > index:
            self._editing_index -= 1
        self._persist_state()
        self._refresh_ui()
        return True

    def _build_layer_features(self, layer, design):
        features = []
        for segment_no, segment in enumerate(design.get("segments", []) or [], start=1):
            if segment.get("zero_length"):
                # Logical zero-length segments are not materialized as cable features.
                continue
            geometry = _geometry_from_segment(design, segment, layer.crs())
            if geometry is None or geometry.isEmpty() or geometry.length() <= 0:
                raise RuntimeError(f"{design.get('fdt')}/{design.get('link')} 存在无效线路段。")
            feature = QgsFeature(layer.fields())
            feature.setGeometry(geometry)
            _stable_link_id(design)
            for field, value in (
                (LINK_ID_FIELD, design["_link_id"]),
                (SEGMENT_ID_FIELD, segment_no),
                (LINK_FDT_FIELD, design.get("fdt", "")),
                (LINK_NAME_FIELD, design.get("link", "")),
            ):
                idx = layer.fields().indexOf(field)
                if idx >= 0:
                    feature.setAttribute(idx, value)
            features.append(feature)
        return features

    def _write_design_features(self, layer, design):
        features = self._build_layer_features(layer, design)
        added = []
        for feature in features:
            if not layer.addFeature(feature):
                for fid in added:
                    layer.deleteFeature(fid)
                raise RuntimeError(f"无法写入 Distribution Cable：{design.get('fdt')}/{design.get('link')}")
            added.append(int(feature.id()))
        return added

    def _replace_written_link(self, index, design):
        layer = context.project_layer(fresh_payload(self), "Distribution Cable")
        if layer is None:
            return False
        if not _ensure_link_fields(layer):
            return False
        old = self._designs[index]
        old_fids = list(old.get("written_fids") or [])
        if not old_fids:
            return False
        if not layer.isEditable() and not layer.startEditing():
            return False
        try:
            new_fids = self._write_design_features(layer, design)
            for fid in old_fids:
                if not layer.deleteFeature(int(fid)):
                    raise RuntimeError(f"无法删除旧 Distribution Cable 要素 {fid}")
        except Exception as exc:
            for fid in new_fids if 'new_fids' in locals() else []:
                layer.deleteFeature(int(fid))
            self._warn("修改 Link", f"更新失败，已回滚新线路：\n{exc}")
            return False
        design["written"] = True
        design["written_fids"] = new_fids
        self._designs[index] = design
        layer.triggerRepaint()
        return True

    def _reconcile_written_state(self, index=None):
        indices = range(len(self._designs)) if index is None else [index]
        layer = context.project_layer(fresh_payload(self), "Distribution Cable")
        if layer is None:
            return False
        if not _ensure_link_fields(layer):
            return False
        changed = False
        for i in indices:
            if i < 0 or i >= len(self._designs):
                continue
            design = self._designs[i]
            if not design.get("written"):
                continue
            _stable_link_id(design)
            fids = list(design.get("written_fids") or [])
            if not fids:
                fids = _feature_ids_for_link(layer, design["_link_id"])
            expected = self._build_layer_features(layer, design)
            if len(fids) != len(expected):
                design["written"] = False
                design["written_fids"] = []
                design["external_change"] = "Distribution Cable 已被手动修改，Link 重新标记为已规划。"
                changed = True
                continue
            valid = True
            for fid, expected_feature in zip(fids, expected):
                current = layer.getFeature(int(fid))
                if not current.isValid() or not current.geometry().equals(expected_feature.geometry()):
                    valid = False
                    break
            if not valid:
                design["written"] = False
                design["written_fids"] = []
                design["external_change"] = "Distribution Cable 已被手动修改，Link 重新标记为已规划。"
                changed = True
            else:
                design["written_fids"] = [int(fid) for fid in fids]
        if changed:
            self._persist_state()
        return True

    def write_planned_links(self):
        layer = context.project_layer(fresh_payload(self), "Distribution Cable")
        if layer is None:
            self._warn("写入图层", "当前项目没有绑定 Distribution Cable 图层。")
            return False
        if not _ensure_link_fields(layer):
            self._warn("写入图层", "无法建立 Distribution Cable Link 标识字段。")
            return False
        pending = [(i, d) for i, d in enumerate(self._designs) if not d.get("written")]
        if not pending:
            return True
        if not layer.isEditable() and not layer.startEditing():
            return False
        added_map = {}
        try:
            for index, design in pending:
                added_map[index] = self._write_design_features(layer, design)
            for index, fids in added_map.items():
                self._designs[index]["written"] = True
                self._designs[index]["written_fids"] = [int(fid) for fid in fids]
                self._designs[index]["external_change"] = None
            layer.triggerRepaint()
            self._persist_state()
        except Exception as exc:
            for fids in added_map.values():
                for fid in fids:
                    layer.deleteFeature(int(fid))
            self._warn("写入图层", f"写入失败，已回滚本次写入：\n{exc}")
            return False
        self._refresh_ui()
        return True

    # ---------- offset ----------
    def offset_and_write(self, spacing=None, control_distance=None):
        if self._draw_active:
            self._set_status("请先保存或退出当前规划，再执行偏移。")
            return False
        if not self._designs:
            self._set_status("当前没有已保存的 Link。")
            return False
        if spacing is None or control_distance is None:
            spacing, control_distance = offset_core.get_settings()
        payload = fresh_payload(self)
        dc = context.project_layer(payload, "Distribution Cable")
        edge = context.project_layer(payload, "Pole Edge")
        fat = context.project_layer(payload, "FAT")
        if dc is None or edge is None or fat is None:
            self._warn("偏移并写入图层", "当前项目缺少 Distribution Cable、Pole Edge 或 FAT 图层。")
            return False
        if not dc.isEditable() and not dc.startEditing():
            return False
        if not fat.isEditable() and not fat.startEditing():
            return False
        try:
            self._restore_authoritative_routes()
            planned = copy.deepcopy(self._designs)
            for d in planned:
                d["_link_id"] = self._design_link_id(d)
            summary = offset_core.apply_offset_layout(
                planned,
                dc,
                edge,
                spacing=float(spacing),
                control_distance_m=float(control_distance),
                fat_layer=fat,
                fat_max_distance_m=float((payload.get("parameters") or {}).get("fat_pole_max_distance", 3.0) or 3.0),
            )
            self._designs = planned
            for design in self._designs:
                design["written"] = False
                design["written_fids"] = []
                design["needs_resync"] = True
            if not self.write_planned_links():
                return False
            fat.triggerRepaint()
            self._refresh_ui()
            self._set_status(f"已偏移并写入 {len(self._designs)} 条 Link。")
            return True
        except Exception as exc:
            _log(f"[offset-write-fail] {type(exc).__name__}: {exc}", Qgis.Critical)
            self._warn("偏移并写入图层", f"偏移写入失败：\n{exc}")
            return False

    @staticmethod
    def _design_link_id(design):
        return _stable_link_id(design)

    # ---------- UI helpers ----------
    def _set_status(self, text):
        try:
            self.status.setText("状态：" + str(text))
        except Exception:
            pass
        self._refresh_ui()

    def _warn(self, title, text):
        try:
            QtWidgets.QMessageBox.warning(self.host or self.iface.mainWindow(), title, text)
        except Exception:
            pass

    def _clear_saved_bands(self):
        canvas = self.iface.mapCanvas()
        for band in self._saved_bands:
            try:
                canvas.scene().removeItem(band)
            except Exception:
                pass
        self._saved_bands = []

    def show_saved_designs(self, entries):
        self._clear_saved_bands()
        self._engine = self._engine or self._prepare_engine()
        if self._engine is None:
            return
        canvas = self.iface.mapCanvas()
        target_crs = canvas.mapSettings().destinationCrs()
        source_crs = self._engine.edge_layer.crs()
        for _, design in entries:
            for segment in design.get("segments", []) or []:
                raw = segment.get("points", []) or []
                if len(raw) < 2:
                    continue
                points = [QgsPointXY(float(p[0]), float(p[1])) for p in raw]
                if source_crs != target_crs:
                    try:
                        transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance().transformContext())
                        points = [transform.transform(point) for point in points]
                    except Exception:
                        continue
                band = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
                band.setWidth(5)
                band.setColor(QtWidgets.QApplication.palette().highlight().color())
                band.setToGeometry(QgsGeometry.fromPolylineXY(points), target_crs)
                self._saved_bands.append(band)

    def _stop_tool(self):
        self._draw_active = False
        self._disconnect_source_monitors()
        if self._tool:
            try:
                self._tool.clear_preview_only()
            except Exception:
                pass
            try:
                self.iface.mapCanvas().unsetMapTool(self._tool)
            except Exception:
                pass
            self._tool = None
        try:
            self.start_btn.setEnabled(True)
        except Exception:
            pass

    def exit_design(self):
        self._persist_state()
        self._stop_tool()
        self._clear_saved_bands()
        self._clear_draft()
        self._refresh_ui()
        try:
            self.host.hide()
        except Exception:
            pass

    def undo_last(self):
        if not self._sequence:
            return
        self._sequence.pop()
        if not self._sequence:
            self._current_fdt = None
            self._current_fdt_id = None
            self._current_link = None
        self._persist_state()
        self._refresh_ui()
        if self._tool:
            self._tool.refresh_route_preview()

    def prospective_route(self, info):
        if not self._sequence:
            return None
        target = (info.get("typ"), int(info.get("feature_id")), str(info.get("label")))
        if target[0] == "FAT" and sum(1 for item in self._sequence if item[0] == "FAT") >= max_fats(self):
            return None
        return self._route(self._sequence[-1], target)

    def _refresh_ui(self):
        try:
            self.link_count_label.setText(f"已完成 Link：{len(self._designs)}")
            self.planned_fat_label.setText(f"已完成 FAT：{planned_fat_count(self)}/{total_fats(self)}")
            if self._sequence:
                self.fdt_label.setText(f"当前：{self._current_fdt or '—'} / {self._current_link or '—'}")
                self.current_path_label.setText(" → ".join(item[2] for item in self._sequence))
                routes = [self._route(a, b) for a, b in zip(self._sequence[:-1], self._sequence[1:])]
                routes = [route for route in routes if route is not None]
                self.distance_label.setText("、".join(f"{float(route.get('distance', 0.0)):.1f}m" for route in routes) or "—")
                if routes:
                    route = routes[-1]
                    self.segment_label.setText(f"{route.get('from_label', '')} → {route.get('to_label', '')}  {float(route.get('distance', 0.0)):.1f}m")
                    self.route_label.setText(f"Pole Edge：{float(route.get('pole_edge_distance', 0.0)):.1f}m / {len(route.get('edge_sequence', []))}段")
            else:
                self.fdt_label.setText("当前：—")
                self.current_path_label.setText("—")
                self.distance_label.setText("—")
                self.segment_label.setText("—")
                self.route_label.setText("—")
            self.save_btn.setEnabled(bool(self._sequence))
            if self._draw_active:
                self.start_btn.setText("暂停规划")
            elif self._sequence:
                self.start_btn.setText("继续规划")
            else:
                self.start_btn.setText("开始规划")
        except Exception:
            pass
        try:
            self.host.refresh_from_core()
        except Exception:
            pass

    def close(self):
        self._stop_tool()
        self._persist_state()
        self._clear_saved_bands()


class LinkDesignMapToolV9(QgsMapTool):
    """Compact FDT/FAT selector with cached spatial index and preview route."""

    def __init__(self, iface, engine, controller):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.engine = engine
        self.controller = controller
        self.canvas = iface.mapCanvas()
        self.setCursor(Qt.CrossCursor)
        self._index = QgsSpatialIndex()
        self._info_by_key = {}
        self._candidate_infos = []
        self._candidate_index = 0
        self._candidate_band = None
        self._route_band = None
        self._build_index()

    def _build_index(self):
        target_crs = self.canvas.mapSettings().destinationCrs()
        number = 1
        for key, info in self.engine.points.items():
            point = QgsPointXY(info["point"])
            source_crs = info["layer"].crs()
            if source_crs != target_crs:
                try:
                    point = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance().transformContext()).transform(point)
                except Exception:
                    continue
            feature = QgsFeature()
            feature.setId(number)
            feature.setGeometry(QgsGeometry.fromPointXY(point))
            self._index.addFeature(feature)
            self._info_by_key[number] = info
            number += 1

    def _candidates(self, pos):
        point = self.toMapCoordinates(pos)
        tolerance = self.canvas.mapUnitsPerPixel() * 26.0
        candidates = []
        for fid in self._index.nearestNeighbor(point, 16):
            info = self._info_by_key.get(int(fid))
            if info is None:
                continue
            p = QgsPointXY(info["point"])
            source_crs = info["layer"].crs()
            if source_crs != self.canvas.mapSettings().destinationCrs():
                try:
                    p = QgsCoordinateTransform(source_crs, self.canvas.mapSettings().destinationCrs(), QgsProject.instance().transformContext()).transform(p)
                except Exception:
                    continue
            distance = sqrt((p.x() - point.x()) ** 2 + (p.y() - point.y()) ** 2)
            if distance <= tolerance:
                candidates.append((distance, info))
        preferred = "FDT" if not self.controller._sequence else "FAT"
        candidates.sort(key=lambda item: (0 if item[1].get("typ") == preferred else 1, item[0]))
        return [info for _, info in candidates]

    def _selected(self):
        if not self._candidate_infos:
            return None
        return self._candidate_infos[self._candidate_index]

    def _draw_candidate(self):
        if self._candidate_band is not None:
            try:
                self.canvas.scene().removeItem(self._candidate_band)
            except Exception:
                pass
            self._candidate_band = None
        info = self._selected()
        if info is None:
            return
        point = QgsPointXY(info["point"])
        source = info["layer"].crs()
        target = self.canvas.mapSettings().destinationCrs()
        if source != target:
            try:
                point = QgsCoordinateTransform(source, target, QgsProject.instance().transformContext()).transform(point)
            except Exception:
                return
        band = QgsRubberBand(self.canvas, QgsWkbTypes.PointGeometry)
        band.setColor(QColor(30, 144, 255))
        band.setWidth(2)
        band.setIcon(QgsRubberBand.ICON_CIRCLE)
        band.setIconSize(14)
        band.setToGeometry(QgsGeometry.fromPointXY(point), target)
        self._candidate_band = band

    def _preview_route(self):
        self._controller_refresh_route_preview()
        info = self._selected()
        if info is None:
            return None
        return self.controller.prospective_route(info)

    def _controller_refresh_route_preview(self):
        self.controller._hover_segment = None
        self.controller._hover_distance = 0.0
        info = self._selected()
        if info is None or not self.controller._sequence:
            self.controller._refresh_ui()
            return
        route = self.controller.prospective_route(info)
        if route is None:
            self.controller._refresh_ui()
            return
        prev = self.controller._sequence[-1]
        self.controller._hover_segment = (prev[2], info["label"])
        self.controller._hover_distance = float(route.get("distance", 0.0) or 0.0)
        self.controller._refresh_ui()

    def start(self):
        self.refresh_route_preview()

    def canvasMoveEvent(self, event):
        if not self.controller._draw_active:
            return
        self._candidate_infos = self._candidates(event.pos())
        self._candidate_index = 0
        self._draw_candidate()
        self._controller_refresh_route_preview()

    def canvasReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or not self.controller._draw_active:
            return
        if not self._candidate_infos:
            self._candidate_infos = self._candidates(event.pos())
            self._candidate_index = 0
        info = self._selected()
        if info is None:
            return
        if not self.controller._sequence:
            self.controller._start_from_hit(info)
        elif info.get("typ") == "FAT":
            self.controller.add_fat(info)
        self._candidate_infos = []
        self._candidate_index = 0
        self._draw_candidate()
        self.refresh_route_preview()
        self.controller._refresh_ui()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Tab, Qt.Key_Backtab) and self._candidate_infos:
            self._candidate_index = (self._candidate_index + (1 if event.key() == Qt.Key_Tab else -1)) % len(self._candidate_infos)
            self._draw_candidate()
            self._controller_refresh_route_preview()
            event.accept()
            return
        if event.key() == Qt.Key_Backspace:
            self.controller.undo_last(); event.accept(); return
        if event.key() == Qt.Key_Escape:
            self.controller.pause_planning(); event.accept(); return
        super().keyPressEvent(event)

    def refresh_route_preview(self):
        if self._route_band is not None:
            try:
                self.canvas.scene().removeItem(self._route_band)
            except Exception:
                pass
            self._route_band = None
        sequence = self.controller._sequence
        if len(sequence) < 2:
            return
        points = []
        target_crs = self.canvas.mapSettings().destinationCrs()
        source_crs = self.engine.edge_layer.crs()
        for first, second in zip(sequence[:-1], sequence[1:]):
            route = self.controller._route(first, second)
            if route is None:
                continue
            route_points = list(route.get("points", []) or [])
            if source_crs != target_crs:
                try:
                    transform = QgsCoordinateTransform(source_crs, target_crs, QgsProject.instance().transformContext())
                    route_points = [transform.transform(point) for point in route_points]
                except Exception:
                    continue
            if len(route_points) >= 2:
                points.extend(route_points if not points else route_points[1:])
        if len(points) >= 2:
            band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
            band.setWidth(4)
            band.setColor(self.canvas.palette().highlight().color())
            band.setToGeometry(QgsGeometry.fromPolylineXY(points), target_crs)
            self._route_band = band

    def clear_preview_only(self):
        for name in ("_candidate_band", "_route_band"):
            band = getattr(self, name, None)
            if band is not None:
                try:
                    self.canvas.scene().removeItem(band)
                except Exception:
                    pass
                setattr(self, name, None)


class PlanningOverlay(QtWidgets.QFrame):
    """Small always-visible planning strip above the map."""

    def __init__(self, canvas, controller):
        super().__init__(canvas)
        self.canvas = canvas
        self.controller = controller
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setStyleSheet("QFrame{background:rgba(255,255,255,245);border:1px solid #b8b8b8;border-radius:4px;} QPushButton{min-height:24px;}")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._build()
        canvas.installEventFilter(self)
        self.raise_()
        self.show()

    def _build(self):
        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        self.current = QtWidgets.QLabel("当前路径：—")
        self.plan = QtWidgets.QLabel("规划中：—")
        self.link_path = QtWidgets.QLabel("LINK：—")
        self.distance = QtWidgets.QLabel("距离：—")
        self.start = QtWidgets.QPushButton("开始规划")
        self.save = QtWidgets.QPushButton("保存规划")
        self.exit = QtWidgets.QPushButton("退出设计")
        layout.addWidget(self.current, 0, 0, 1, 3)
        layout.addWidget(self.plan, 1, 0)
        layout.addWidget(self.link_path, 2, 0, 1, 3)
        layout.addWidget(self.distance, 3, 0)
        row = QtWidgets.QHBoxLayout(); row.addStretch(); row.addWidget(self.start); row.addWidget(self.save); row.addWidget(self.exit)
        layout.addLayout(row, 4, 0, 1, 3)
        self.start.clicked.connect(self._toggle)
        self.save.clicked.connect(self._save)
        self.exit.clicked.connect(self.controller.exit_design)
        self.resize(700, 120)
        self._reposition()

    def _toggle(self):
        if self.controller._draw_active:
            self.controller.pause_planning()
        elif self.controller._sequence:
            self.controller.resume_planning()
        else:
            self.controller.start_design()
        self.refresh()

    def _save(self):
        self.controller.save_current_link()
        self.refresh()

    def eventFilter(self, obj, event):
        if obj is self.canvas and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    def _reposition(self):
        width = min(760, max(520, self.canvas.width() - 40))
        self.setFixedWidth(width)
        self.move(max(0, (self.canvas.width() - width) // 2), 8)

    def refresh(self):
        c = self.controller
        if c._sequence:
            labels = [item[2] for item in c._sequence]
            self.plan.setText(f"规划中：{c._current_fdt or '—'} / {c._current_link or '—'}")
            self.link_path.setText("LINK：" + " → ".join(labels))
            self.distance.setText("距离：" + "、".join(f"{float(c._route(a,b).get('distance',0.0)):.1f}m" for a,b in zip(c._sequence[:-1],c._sequence[1:]) if c._route(a,b)))
        else:
            self.plan.setText("规划中：—")
            self.link_path.setText("LINK：—")
            self.distance.setText("距离：—")
        if c._hover_segment:
            self.current.setText(f"当前路径：{c._hover_segment[0]} → {c._hover_segment[1]}    实时距离：{c._hover_distance:.1f}m")
        elif c._sequence and len(c._sequence) >= 2:
            self.current.setText(f"当前路径：{c._sequence[-2][2]} → {c._sequence[-1][2]}")
        else:
            self.current.setText("当前路径：—")
        self.start.setText("暂停规划" if c._draw_active else ("继续规划" if c._sequence else "开始规划"))
        self._reposition()


class LinkDesignDock(QtWidgets.QDockWidget):
    """Compact Link Design UI. All business logic lives in LinkDesignController."""

    def __init__(self, iface, parent=None):
        super().__init__("链路设计", parent or iface.mainWindow())
        self.iface = iface
        self.setObjectName("ODNToolsPro_LinkDesignDock")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable | QtWidgets.QDockWidget.DockWidgetClosable)
        self.resize(430, 640)
        self.setMinimumWidth(380)
        self._controller = LinkDesignController(iface, self)
        self._build_ui()
        self._overlay = PlanningOverlay(iface.mapCanvas(), self._controller)
        self._controller.host = self
        self.refresh_from_core()

    def _build_ui(self):
        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)
        title = QtWidgets.QLabel("已完成设计")
        font = title.font(); font.setBold(True); title.setFont(font)
        layout.addWidget(title)
        self.summary = QtWidgets.QLabel("已完成 Link：0")
        layout.addWidget(self.summary)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["FDT / Link", "FAT数", "距离", "状态"])
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self._tree_selection_changed)
        self.tree.itemDoubleClicked.connect(self._tree_double_clicked)
        layout.addWidget(self.tree, 1)
        self.info = QtWidgets.QLabel("—")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color:#666;")
        layout.addWidget(self.info)
        row1 = QtWidgets.QHBoxLayout()
        self.modify_btn = QtWidgets.QPushButton("修改选中 Link")
        self.delete_btn = QtWidgets.QPushButton("删除选中 Link")
        row1.addWidget(self.modify_btn); row1.addWidget(self.delete_btn)
        layout.addLayout(row1)
        row2 = QtWidgets.QHBoxLayout()
        self.write_btn = QtWidgets.QPushButton("确定并写入图层")
        self.offset_btn = QtWidgets.QPushButton("偏移并写入图层")
        row2.addWidget(self.write_btn); row2.addWidget(self.offset_btn)
        layout.addLayout(row2)
        self.modify_btn.clicked.connect(self._modify_selected)
        self.delete_btn.clicked.connect(self._delete_selected)
        self.write_btn.clicked.connect(self._write_all)
        self.offset_btn.clicked.connect(self._offset_all)
        self.setWidget(root)

        # controller UI compatibility proxies
        self._controller.link_count_label = QtWidgets.QLabel(self)
        self._controller.planned_fat_label = QtWidgets.QLabel(self)
        self._controller.fdt_label = QtWidgets.QLabel(self)
        self._controller.current_path_label = QtWidgets.QLabel(self)
        self._controller.distance_label = QtWidgets.QLabel(self)
        self._controller.segment_label = QtWidgets.QLabel(self)
        self._controller.route_label = QtWidgets.QLabel(self)
        self._controller.start_btn = QtWidgets.QPushButton(self)
        self._controller.save_btn = QtWidgets.QPushButton(self)
        self._controller.status = QtWidgets.QLabel(self)

    def _selected_indexes(self):
        result = []
        for item in self.tree.selectedItems():
            data = item.data(0, Qt.UserRole)
            if data and data[0] == "link":
                result.append(int(data[1]))
            elif data and data[0] == "fdt":
                result.extend(int(i) for i in data[1])
        return sorted(set(i for i in result if 0 <= i < len(self._controller._designs)))

    def _tree_selection_changed(self):
        indices = self._selected_indexes()
        if not indices:
            self.info.setText("—")
            self._controller._clear_saved_bands()
            return
        entries = [(i, self._controller._designs[i]) for i in indices]
        self._controller.show_saved_designs(entries)
        self.info.setText("\n".join(f"{d.get('fdt','')}/{d.get('link','')}：{len(d.get('nodes',[]))} FAT，{float(d.get('length',0.0)):.1f}m" for _, d in entries))

    def _tree_double_clicked(self, item, column):
        data = item.data(0, Qt.UserRole)
        if data and data[0] == "link":
            self._controller.show_saved_designs([(int(data[1]), self._controller._designs[int(data[1])])])

    def _modify_selected(self):
        indices = self._selected_indexes()
        if len(indices) != 1:
            self.info.setText("请选择一个 Link。")
            return
        self._controller.load_design_for_edit(indices[0])
        self.refresh_from_core()

    def _delete_selected(self):
        for index in sorted(self._selected_indexes(), reverse=True):
            if not self._controller.delete_link(index):
                break
        self.refresh_from_core()

    def _write_all(self):
        self._controller.write_planned_links()
        self.refresh_from_core()

    def _offset_all(self):
        spacing, control = offset_core.get_settings()
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("偏移并写入图层")
        form = QtWidgets.QFormLayout(dialog)
        spacing_box = QtWidgets.QDoubleSpinBox(dialog); spacing_box.setRange(0.01,20.0); spacing_box.setDecimals(2); spacing_box.setValue(spacing); spacing_box.setSuffix(" m")
        control_box = QtWidgets.QDoubleSpinBox(dialog); control_box.setRange(0.01,5.0); control_box.setDecimals(2); control_box.setValue(control); control_box.setSuffix(" m")
        form.addRow("偏移间距：", spacing_box); form.addRow("拐角控制距离：", control_box)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            return
        offset_core.save_settings(spacing_box.value(), control_box.value())
        self._controller.offset_and_write(spacing_box.value(), control_box.value())
        self.refresh_from_core()

    def refresh_from_core(self):
        c = self._controller
        total = total_fats(c)
        used = assigned_fat_ids(c)
        self.summary.setText(f"已完成 Link：{len(c._designs)}    已完成 FAT：{len(used)}/{total}")
        self.tree.blockSignals(True)
        self.tree.clear()
        grouped = {}
        for i, design in enumerate(c._designs):
            grouped.setdefault(str(design.get("fdt", "未知 FDT")), []).append((i, design))
        for fdt in sorted(grouped):
            entries = grouped[fdt]
            parent = QtWidgets.QTreeWidgetItem([fdt, str(sum(len(d.get("nodes",[])) for _,d in entries)), f"{sum(float(d.get('length',0.0)) for _,d in entries):.1f}m", "已完成"])
            parent.setData(0, Qt.UserRole, ("fdt", [i for i,_ in entries]))
            self.tree.addTopLevelItem(parent)
            for i, design in sorted(entries, key=lambda x: str(x[1].get("link", ""))):
                status = "已写入" if design.get("written") else "已规划"
                child = QtWidgets.QTreeWidgetItem([str(design.get("link", "L?")), str(len(design.get("nodes",[]))), f"{float(design.get('length',0.0)):.1f}m", status])
                child.setData(0, Qt.UserRole, ("link", i))
                parent.addChild(child)
        self.tree.blockSignals(False)
        self._overlay.refresh()

    def closeEvent(self, event):
        try:
            self._overlay.close()
            self._controller.close()
        except Exception:
            pass
        event.accept()


# Compatibility aliases for other current modules; these do not represent
# additional runtime implementations.
_CoreController = LinkDesignController
LinkDesignCore = LinkDesignController
LinkDesignMapTool = LinkDesignMapToolV9
LinkDesignMapToolV11 = LinkDesignMapToolV9
LinkDesignMapToolV15 = LinkDesignMapToolV9
