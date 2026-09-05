# -*- coding: utf-8 -*-
"""Final project-driven Link Design UI and interaction policy."""

from collections import defaultdict
import json

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont
from qgis.core import QgsGeometry, QgsProject, QgsWkbTypes
from qgis.gui import QgsRubberBand

from . import odn_project_context as context

SETTINGS_KEY = "ODNToolsPro/Scheme3"


def _payload(self):
    return context.current_payload() or getattr(self, "_odn_project_payload", None) or {}


def _prepare_layers(self):
    payload = _payload(self)
    fdt = context.project_layer(payload, "FDT")
    fat = context.project_layer(payload, "FAT")
    existing = context.project_layer(payload, "Existing Pole")
    new = context.project_layer(payload, "New Pole")
    edge = context.project_layer(payload, "Pole Edge")
    return fdt, ([fat] if fat is not None else []), [x for x in (existing, new) if x is not None], edge


def _max_links(self):
    try: return max(1, int((_payload(self).get("parameters", {}) or {}).get("fdt_max_links", 4)))
    except Exception: return 4


def _next_link(self, fdt_label):
    used = {d.get("link") for d in self._designs if d.get("fdt") == fdt_label}
    for i in range(1, _max_links(self) + 1):
        x = f"L{i}"
        if x not in used: return x
    return None


def _planned_fats(self):
    labels = set()
    for d in self._designs: labels.update(x[1] for x in d.get("nodes", []))
    for item in getattr(self, "_sequence", []):
        if item[0] == "FAT": labels.add(item[2])
    return len(labels)


def _total_fats(self):
    try:
        layer = context.project_layer(_payload(self), "FAT")
        return layer.featureCount() if layer is not None else 0
    except Exception: return 0


def _build_ui(self):
    root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(10, 8, 10, 8); root.setSpacing(5)
    title = QtWidgets.QLabel("链路设计"); f = QFont(); f.setPointSize(13); f.setBold(True); title.setFont(f); root.addWidget(title)

    self.plan_label = QtWidgets.QLabel("① 规划中（显示实时距离）：等待开始")
    self.fdt_label = QtWidgets.QLabel("② 规划中 FDT：—")
    self.link_count_label = QtWidgets.QLabel("已规划链路：0")
    self.planned_fat_label = QtWidgets.QLabel("已规划 FAT：0")
    self.total_fat_label = QtWidgets.QLabel("③ 总 FAT：0，已设计 FAT：0")
    for w in (self.plan_label, self.fdt_label, self.link_count_label, self.planned_fat_label, self.total_fat_label): root.addWidget(w)
    line = QtWidgets.QFrame(); line.setFrameShape(QtWidgets.QFrame.HLine); line.setFrameShadow(QtWidgets.QFrame.Sunken); root.addWidget(line)
    self.current_path_label = QtWidgets.QLabel("请点击“开始规划”，然后在地图上选择 FDT 或 FAT。"); self.current_path_label.setWordWrap(True)
    self.segment_label = QtWidgets.QLabel("当前段：—")
    self.route_label = QtWidgets.QLabel("Pole Edge：—"); self.route_label.setWordWrap(True)
    root.addWidget(self.current_path_label); root.addWidget(self.segment_label); root.addWidget(self.route_label)

    row = QtWidgets.QHBoxLayout()
    self.start_btn = QtWidgets.QPushButton("开始规划")
    self.done_btn = QtWidgets.QPushButton("已完成设计")
    self.exit_btn = QtWidgets.QPushButton("退出设计")
    row.addWidget(self.start_btn); row.addWidget(self.done_btn); row.addWidget(self.exit_btn); root.addLayout(row)
    self.status = QtWidgets.QLabel("状态：等待开始规划"); self.status.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken); root.addWidget(self.status)

    # Hidden compatibility controls: users never select FDT/Link manually.
    self.fdt_combo = QtWidgets.QComboBox(); self.link_combo = QtWidgets.QComboBox(); self.fdt_combo.hide(); self.link_combo.hide()
    self.design_tree = QtWidgets.QTreeWidget(); self.design_tree.hide()
    self._draw_active = False; self._sequence = []; self._direction = None; self._current_fdt = None; self._current_fdt_id = None; self._current_link = None
    self._engine = None; self._tool = None
    self.start_btn.clicked.connect(self.start_link); self.done_btn.clicked.connect(self.finish_design); self.exit_btn.clicked.connect(self.exit_design)
    self._refresh_project_ui()


def _refresh_project_ui(self):
    payload = _payload(self); project = payload.get("project", {}) or {}
    name = project.get("name") or "当前 ODN Project"; version = project.get("odn_version", "2.0")
    self.setWindowTitle("链路设计")
    self.link_count_label.setText(f"已规划链路：{len(self._designs)}")
    self.planned_fat_label.setText(f"已规划 FAT：{_planned_fats(self)}")
    self.total_fat_label.setText(f"③ 总 FAT：{_total_fats(self)}，已设计 FAT：{_planned_fats(self)}")
    if not self._draw_active: self.status.setText(f"当前项目：{name}  |  ODN {version}")


def _load_saved_designs(self):
    ok, value = QgsProject.instance().readEntry(SETTINGS_KEY, "designs", "")
    if ok and value:
        try: self._designs = json.loads(value)
        except Exception: self._designs = []
    else: self._designs = []
    self._refresh_project_ui()


def _load_layer_candidates(self):
    return None


def _layers(self):
    return _prepare_layers(self)


def _selection_changed(self):
    return None


def start_link(self):
    if self._draw_active:
        self.status.setText("状态：正在规划中，请继续点击地图上的 FDT/FAT。")
        return
    fdt_layer, fats, poles, edge = _prepare_layers(self)
    if fdt_layer is None or not fats or edge is None:
        QtWidgets.QMessageBox.warning(self, "链路设计", "当前项目缺少链路设计所需图层：FDT、FAT、Pole Edge。\n\n请先在【项目配置】中修正图层绑定。")
        return
    from .scheme3_manual_link_planner import Scheme3Engine, Scheme3MapTool
    params = {
        "max_fats": 999999, "max_seg": 455.0,
        "attach": float((_payload(self).get("parameters", {}) or {}).get("fat_pole_max_distance", 3.0)),
        "pole_tol": 0.25,
        "return_threshold": float((_payload(self).get("parameters", {}) or {}).get("bb_return_threshold", 100.0)),
        "allow_bb": True, "allow_sfc": True,
    }
    self._engine = Scheme3Engine(self.iface, fdt_layer, fats, poles, edge, params)
    self._tool = Scheme3MapTool(self.iface, self._engine, self)
    self.iface.mapCanvas().setMapTool(self._tool)
    self._draw_active = True; self._sequence = []; self._direction = None
    self._current_fdt = None; self._current_fdt_id = None; self._current_link = None
    self.start_btn.setEnabled(False); self.done_btn.setEnabled(True); self.exit_btn.setEnabled(True)
    self.status.setText("状态：规划中——请选择 FDT 或 FAT 作为起点。")
    self._update_current(); self._tool.start()


def _start_from_hit(self, info, rid):
    if info["typ"] == "FDT":
        fdt = info["label"]; link = _next_link(self, fdt)
        if not link:
            QtWidgets.QMessageBox.warning(self, "链路设计", f"{fdt} 已达到项目配置的最大 Link 数：{_max_links(self)}。"); return False
        self._direction = "FDT_TO_FAT"; self._current_fdt = fdt; self._current_fdt_id = rid; self._current_link = link
        self._sequence = [("FDT", rid, fdt)]; self.status.setText(f"状态：{fdt}/{link}，请继续点击 FAT。"); return True
    if info["typ"] == "FAT":
        self._direction = "FAT_TO_FDT"; self._sequence = [("FAT", rid, info["label"])]
        self.status.setText("状态：已从 FAT 开始，请继续点击 FAT，最后点击 FDT 完成反向链路。"); return True
    return False


def set_fdt(self, rid, label):
    if not self._draw_active: return
    if self._direction == "FAT_TO_FDT":
        if len(self._sequence) < 1: return
        self._current_fdt = label; self._current_fdt_id = rid; self._current_link = _next_link(self, label)
        if not self._current_link:
            QtWidgets.QMessageBox.warning(self, "链路设计", f"{label} 已达到项目配置的最大 Link 数：{_max_links(self)}。"); return
        self._sequence.append(("FDT", rid, label)); self.finish_current_link(); return
    if self._direction is None:
        info = next((v for v in self._engine.points.values() if v["label"] == label and v["typ"] == "FDT"), None)
        if info: self._start_from_hit(info, rid)
    self._update_current()


def add_node(self, typ, rid, label):
    if not self._draw_active or typ != "FAT": return
    if self._direction is None:
        info = next((v for v in self._engine.points.values() if v["label"] == label and v["typ"] == "FAT"), None)
        if info: self._start_from_hit(info, rid)
        self._update_current(); return
    if any(x[0] == "FAT" and x[1] == rid for x in self._sequence):
        self.status.setText(f"状态：{label} 已在当前链路中。"); return
    prev = self._sequence[-1][2]
    if self._engine.segment_info(prev, label) is None:
        self.status.setText(f"状态：{prev} → {label} 无法沿 Pole Edge 建立路径。"); return
    self._sequence.append(("FAT", rid, label)); self._update_current(); self._tool.refresh_route_preview()


def _sequence_infos(self):
    if len(self._sequence) < 2 or not self._engine: return []
    labels = [x[2] for x in self._sequence]; out = []
    for a, b in zip(labels[:-1], labels[1:]):
        info = self._engine.segment_info(a, b)
        if not info: return []
        out.append(info)
    return out


def _update_current(self, hover_info=None):
    self._refresh_project_ui()
    if not self._sequence:
        self.plan_label.setText("① 规划中（显示实时距离）：等待选择起点"); self.fdt_label.setText("② 规划中 FDT：—")
        self.current_path_label.setText("请点击 FDT 或 FAT 开始规划。"); self.segment_label.setText("当前段：—"); self.route_label.setText("Pole Edge：—"); return
    labels = [x[2] for x in self._sequence]; infos = _sequence_infos(self); total = sum(i["distance"] for i in infos)
    if hover_info: total = sum(i["distance"] for i in infos) + hover_info["distance"]
    self.plan_label.setText(f"① 规划中（显示实时距离）：{total:.1f} m")
    self.fdt_label.setText(f"② 规划中 FDT：{self._current_fdt or '等待 FDT'}  |  Link：{self._current_link or '—'}")
    self.current_path_label.setText("当前路径：" + " → ".join(labels))
    last = hover_info or (infos[-1] if infos else None)
    if last:
        self.segment_label.setText(f"当前段：{last['from_label']} → {last['to_label']}    {last['distance']:.1f} m")
        self.route_label.setText(f"Pole Edge 实际路径：{last['distance']:.1f} m  |  边段：{len(last['edge_sequence'])}  |  Link 累计：{total:.1f} m")
    else:
        self.segment_label.setText("当前段：—"); self.route_label.setText("Pole Edge：—")


def finish_current_link(self):
    if not self._current_fdt:
        self.status.setText("状态：当前链路尚未闭合到 FDT。"); return False
    fats = [x for x in self._sequence if x[0] == "FAT"]
    if not fats: self.status.setText("状态：当前链路至少需要 1 个 FAT。"); return False
    if self._direction == "FAT_TO_FDT" and self._sequence[-1][0] != "FDT": return False
    infos = _sequence_infos(self)
    if not infos: self.status.setText("状态：当前链路存在无法沿 Pole Edge 建立的路径。"); return False
    total = sum(i["distance"] for i in infos)
    nodes = list(reversed([(x[1], x[2]) for x in fats])) if self._direction == "FAT_TO_FDT" else [(x[1], x[2]) for x in fats]
    design = {"fdt": self._current_fdt, "link": self._current_link, "nodes": nodes, "length": round(total, 3), "sequence": [x[2] for x in self._sequence], "direction": self._direction}
    if not any(d.get("fdt") == design["fdt"] and d.get("link") == design["link"] for d in self._designs): self._designs.append(design)
    self._persist_designs()
    done_link = design["link"]
    self._sequence = []; self._direction = None; self._current_link = None; self._current_fdt = None; self._current_fdt_id = None
    if self._tool: self._tool.clear_current()
    self.status.setText(f"状态：已完成 {design['fdt']}/{done_link}（{total:.1f} m），可以继续规划下一条链路。")
    self._update_current(); return True


def finish_link(self): return finish_current_link(self)

def _persist_designs(self):
    QgsProject.instance().writeEntry(SETTINGS_KEY, "designs", json.dumps(self._designs, ensure_ascii=False))


def finish_design(self):
    if self._sequence and not self.finish_current_link(): return
    self._persist_designs(); self._stop_tool(); self.status.setText(f"状态：设计已完成并保存，共 {len(self._designs)} 条链路。")


def exit_design(self):
    self._stop_tool(); self.close()


def _stop_tool(self):
    self._draw_active = False; self._sequence = []; self._direction = None; self._current_fdt = None; self._current_fdt_id = None; self._current_link = None
    self.start_btn.setEnabled(True); self.done_btn.setEnabled(False)
    if self._tool:
        try: self._tool.clear_current()
        except Exception: pass
        try: self.iface.mapCanvas().unsetMapTool(self._tool)
        except Exception: pass
        self._tool = None
    self._update_current()


def undo_last(self):
    if self._sequence:
        self._sequence.pop()
        if not self._sequence: self._direction = None; self._current_fdt = None; self._current_link = None
        elif self._direction == "FAT_TO_FDT": self._current_fdt = None; self._current_link = None
        self._update_current();
        if self._tool: self._tool.refresh_route_preview()


def check_all(self):
    QtWidgets.QMessageBox.information(self, "链路设计", f"当前已规划 {len(self._designs)} 条链路，已设计 FAT {_planned_fats(self)} 个。")


def preview_all(self):
    if not self._engine: return
    self._engine.clear_route_bands()
    for d in self._designs:
        seq = d.get("sequence") or ([d.get("fdt")] + [x[1] for x in d.get("nodes", [])])
        for a, b in zip(seq[:-1], seq[1:]):
            info = self._engine.segment_info(a, b)
            if info: self._engine.draw_route(info)


def generate_cables(self):
    QtWidgets.QMessageBox.information(self, "链路设计", "当前版本先保存 Link 设计状态；正式 Cable 写入按后续工程字段规则接入。")


def _map_release(self, event):
    if event.button() != Qt.LeftButton or not self.dialog._draw_active: return
    hit = self._nearest(event.pos())
    if not hit: return
    _, key, info = hit
    if not self.dialog._sequence: self.dialog._start_from_hit(info, key[1])
    elif info["typ"] == "FDT": self.dialog.set_fdt(key[1], info["label"])
    elif info["typ"] == "FAT": self.dialog.add_node("FAT", key[1], info["label"])
    self.refresh_route_preview()


def _map_move(self, event):
    if not self.dialog._draw_active or not self.dialog._sequence: return
    hit = self._nearest(event.pos())
    if not hit: return
    _, _, info = hit
    if info["typ"] not in ("FAT", "FDT"): return
    prev = self.dialog._sequence[-1][2]; route = self.engine.segment_info(prev, info["label"])
    if route: self.dialog._update_current(route)


def _map_preview(self):
    if self._hover_band:
        try: self.iface.mapCanvas().scene().removeItem(self._hover_band)
        except Exception: pass
        self._hover_band = None
    if len(self.dialog._sequence) < 2: return
    labels = [x[2] for x in self.dialog._sequence]; pts = []
    for a, b in zip(labels[:-1], labels[1:]):
        info = self.engine.segment_info(a, b)
        if info: pts.extend(info["points"] if not pts else info["points"][1:])
    if len(pts) >= 2:
        self._hover_band = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.LineGeometry); self._hover_band.setWidth(4); self._hover_band.setToGeometry(QgsGeometry.fromPolylineXY(pts), None)


def install_project_ui_policy(dialog_class):
    if getattr(dialog_class, "_odn_final_ui_installed", False): return
    dialog_class._build_ui = _build_ui
    dialog_class._load_layer_candidates = _load_layer_candidates
    dialog_class._load_saved_designs = _load_saved_designs
    dialog_class._layers = _layers
    dialog_class._selection_changed = _selection_changed
    dialog_class.start_link = start_link
    dialog_class.set_fdt = set_fdt
    dialog_class.add_node = add_node
    dialog_class._update_current = _update_current
    dialog_class._set_segment_info = lambda self, info, total_length=None: _update_current(self, info)
    dialog_class.finish_link = finish_link
    dialog_class.finish_current_link = finish_current_link
    dialog_class.finish_design = finish_design
    dialog_class.exit_design = exit_design
    dialog_class.undo_last = undo_last
    dialog_class.save_design = finish_design
    dialog_class.check_all = check_all
    dialog_class.preview_all = preview_all
    dialog_class.generate_cables = generate_cables
    dialog_class._refresh_tree = _refresh_tree
    dialog_class._persist_designs = _persist_designs
    dialog_class._params = lambda self: {"max_fats": 999999, "max_seg": 455.0, "attach": float((_payload(self).get("parameters", {}) or {}).get("fat_pole_max_distance", 3.0)), "pole_tol": 0.25, "return_threshold": 100.0, "allow_bb": True, "allow_sfc": True}
    dialog_class._odn_final_ui_installed = True

    from .scheme3_manual_link_planner import Scheme3MapTool
    Scheme3MapTool.canvasReleaseEvent = _map_release
    Scheme3MapTool.canvasMoveEvent = _map_move
    Scheme3MapTool.refresh_route_preview = _map_preview
