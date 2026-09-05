# -*- coding: utf-8 -*-
"""Project-driven UI and interaction policy for Scheme 3 Link Design."""

from collections import defaultdict
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsWkbTypes


def _payload(self):
    try:
        from . import odn_project_context as context
        return context.current_payload() or getattr(self, "_odn_project_payload", None) or {}
    except Exception:
        return getattr(self, "_odn_project_payload", None) or {}


def _safe_int(value, default, minimum=1, maximum=1000):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _prepare_layers(self):
    payload = _payload(self)
    try:
        from . import odn_project_context as context
        fdt = context.project_layer(payload, "FDT")
        fat = context.project_layer(payload, "FAT")
        existing = context.project_layer(payload, "Existing Pole")
        new = context.project_layer(payload, "New Pole")
        edge = context.project_layer(payload, "Pole Edge")
        return fdt, ([fat] if fat is not None else []), [p for p in (existing, new) if p is not None], edge
    except Exception:
        return self._layers()


def _build_ui(self):
    root = QtWidgets.QVBoxLayout(self)
    root.setContentsMargins(10, 8, 10, 8)

    title = QtWidgets.QLabel("链路设计")
    f = QFont(); f.setPointSize(13); f.setBold(True); title.setFont(f)
    root.addWidget(title)

    self.project_label = QtWidgets.QLabel("当前项目：读取中…")
    self.project_label.setStyleSheet("color:#555;")
    root.addWidget(self.project_label)

    design_box = QtWidgets.QGroupBox("链路设计")
    dl = QtWidgets.QVBoxLayout(design_box)
    self.start_btn = QtWidgets.QPushButton("＋开始画链路")
    dl.addWidget(self.start_btn)
    self.design_hint = QtWidgets.QLabel("点击开始后，在地图上先点击 FDT，再依次点击 FAT。")
    self.design_hint.setWordWrap(True); self.design_hint.setStyleSheet("color:#666;")
    dl.addWidget(self.design_hint)

    self.design_tree = QtWidgets.QTreeWidget()
    self.design_tree.setHeaderLabels(["FDT / Link", "FAT 数量", "累计长度"])
    self.design_tree.setMinimumHeight(170)
    dl.addWidget(self.design_tree)
    root.addWidget(design_box)

    current_box = QtWidgets.QGroupBox("当前设计")
    cl = QtWidgets.QVBoxLayout(current_box)
    self.current_label = QtWidgets.QLabel("等待开始")
    self.current_path_label = QtWidgets.QLabel("请点击“开始画链路”，然后在地图上点击 FDT。")
    self.current_path_label.setWordWrap(True)
    self.segment_label = QtWidgets.QLabel("当前段：—")
    self.route_label = QtWidgets.QLabel("Pole Edge：—")
    self.total_label = QtWidgets.QLabel("Link 累计：—")
    for w in (self.current_label, self.current_path_label, self.segment_label, self.route_label, self.total_label):
        cl.addWidget(w)

    row = QtWidgets.QHBoxLayout()
    self.undo_btn = QtWidgets.QPushButton("撤销上一点")
    self.finish_btn = QtWidgets.QPushButton("完成 Link")
    self.cancel_draw_btn = QtWidgets.QPushButton("停止画链路")
    self.undo_btn.setEnabled(False); self.finish_btn.setEnabled(False); self.cancel_draw_btn.setEnabled(False)
    row.addWidget(self.undo_btn); row.addWidget(self.finish_btn); row.addWidget(self.cancel_draw_btn)
    cl.addLayout(row)
    root.addWidget(current_box)

    self.status = QtWidgets.QLabel("状态：等待开始设计")
    self.status.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
    root.addWidget(self.status)

    buttons = QtWidgets.QHBoxLayout()
    for text, slot in [("保存设计", self.save_design), ("检查全部设计", self.check_all),
                       ("预览全部路由", self.preview_all), ("生成正式Cable", self.generate_cables), ("关闭", self.close)]:
        b = QtWidgets.QPushButton(text); buttons.addWidget(b); b.clicked.connect(slot)
    root.addLayout(buttons)

    self.start_btn.clicked.connect(self.start_link)
    self.undo_btn.clicked.connect(self.undo_last)
    self.finish_btn.clicked.connect(self.finish_link)
    self.cancel_draw_btn.clicked.connect(self.stop_drawing)

    # Compatibility attributes: Link Design no longer exposes project-rule selectors.
    self.fdt_combo = QtWidgets.QComboBox()
    self.link_combo = QtWidgets.QComboBox()
    self._refresh_project_ui()


def _refresh_project_ui(self):
    payload = _payload(self)
    project = payload.get("project", {}) or {}
    name = project.get("name") or project.get("project_name") or "当前 ODN Project"
    version = project.get("odn_version", "2.0")
    params = payload.get("parameters", {}) or {}
    self._project_max_links = _safe_int(params.get("fdt_max_links", 4), 4)
    self._project_max_fats = _safe_int(params.get("max_fats_per_link", 4), 4)
    self.project_label.setText(f"当前项目：{name}    |    ODN {version}")
    self.design_hint.setText(
        f"点击开始后，在地图上先点击 FDT，再依次点击 FAT。当前项目规则：每个 FDT 最多 {self._project_max_links} 个 Link；每个 Link 最多 {self._project_max_fats} 个 FAT。"
    )


def start_link(self):
    if getattr(self, "_draw_active", False):
        self.status.setText("状态：已经在画链路，请继续点击地图上的 FDT/FAT，或完成 Link。")
        return
    self._refresh_project_ui()
    fdt_layer, fats, poles, edge = _prepare_layers(self)
    missing = []
    if fdt_layer is None or QgsWkbTypes.geometryType(fdt_layer.wkbType()) != QgsWkbTypes.PointGeometry:
        missing.append("FDT")
    if not fats:
        missing.append("FAT")
    if edge is None or QgsWkbTypes.geometryType(edge.wkbType()) != QgsWkbTypes.LineGeometry:
        missing.append("Pole Edge")
    if missing:
        QtWidgets.QMessageBox.warning(self, "链路设计", "当前项目缺少链路设计所需绑定：\n\n" + "\n".join(missing) + "\n\n请先在【项目配置】中修正。")
        return

    # Import locally so this compatibility layer remains independent of the existing module name.
    from .scheme3_manual_link_planner import Scheme3Engine, Scheme3MapTool
    params = {
        "max_fats": self._project_max_fats,
        "max_seg": 455.0,
        "attach": float(params_value(self, "fat_pole_max_distance", 3.0)),
        "pole_tol": 0.25,
        "return_threshold": float(params_value(self, "bb_return_threshold", 100.0)),
        "allow_bb": True,
        "allow_sfc": True,
    }
    self._engine = Scheme3Engine(self.iface, fdt_layer, fats, poles, edge, params)
    self._tool = Scheme3MapTool(self.iface, self._engine, self)
    self.iface.mapCanvas().setMapTool(self._tool)
    self._draw_active = True
    self._awaiting_fdt = True
    self._current = []
    self._current_fdt = None
    self._current_link = None
    self.undo_btn.setEnabled(False); self.finish_btn.setEnabled(False); self.cancel_draw_btn.setEnabled(True)
    self.current_label.setText("等待选择 FDT")
    self.current_path_label.setText("请在地图上点击一个 FDT。")
    self.segment_label.setText("当前段：—"); self.route_label.setText("Pole Edge：—"); self.total_label.setText("Link 累计：—")
    self.status.setText("状态：正在等待 FDT…")
    self._refresh_tree()
    self._tool.start()


def params_value(self, key, default):
    p = (_payload(self).get("parameters", {}) or {}).get(key, default)
    try:
        return float(p)
    except (TypeError, ValueError):
        return default


def set_fdt(self, rid, label):
    if not getattr(self, "_draw_active", False) or not getattr(self, "_awaiting_fdt", False):
        return
    self._current_fdt = label
    self._current_fdt_id = rid
    used = {d.get("link") for d in getattr(self, "_designs", []) if d.get("fdt") == label}
    next_link = next((f"L{i}" for i in range(1, self._project_max_links + 1) if f"L{i}" not in used), None)
    if next_link is None:
        QtWidgets.QMessageBox.warning(self, "链路设计", f"{label} 已达到项目配置的最大 Link 数：{self._project_max_links}。")
        self._current_fdt = None; self._current_fdt_id = None
        return
    self._current_link = next_link
    self._awaiting_fdt = False
    self._update_current()
    self.undo_btn.setEnabled(True); self.finish_btn.setEnabled(False)
    self.status.setText(f"状态：已选择 {label} / {next_link}，现在请依次点击 FAT。")


def add_node(self, typ, rid, label):
    if not getattr(self, "_draw_active", False) or getattr(self, "_awaiting_fdt", False):
        return
    if typ != "FAT":
        return
    ids = [x[0] for x in self._current]
    if rid in ids:
        self.status.setText(f"状态：{label} 已在当前 Link 中，不能重复选择。")
        return
    if len(self._current) >= self._project_max_fats:
        self.status.setText(f"状态：当前 Link 已达到项目配置上限 {self._project_max_fats} 个 FAT。")
        return
    prev = self._current[-1][1] if self._current else self._current_fdt
    info = self._engine.segment_info(prev, label) if self._engine else None
    if info is None:
        self.status.setText(f"状态：{prev} → {label} 无法沿 Pole Edge 建立完整路径。")
        self._update_current(); return
    self._current.append((rid, label))
    self._update_current()
    if self._tool:
        self._tool.refresh_route_preview()
    self.finish_btn.setEnabled(True)
    self.status.setText(f"状态：已加入 {label}，当前段 {info['distance']:.1f} m，Link 累计 {self._link_total():.1f} m。")


def _link_infos(self):
    if not self._engine or not self._current_fdt:
        return []
    seq = [self._current_fdt] + [x[1] for x in self._current]
    infos = []
    for a, b in zip(seq[:-1], seq[1:]):
        info = self._engine.segment_info(a, b)
        if info is None:
            return []
        infos.append(info)
    return infos


def _link_total(self):
    return sum(i["distance"] for i in _link_infos(self))


def _update_current(self):
    path = [self._current_fdt] + [x[1] for x in self._current] if self._current_fdt else []
    self.current_label.setText(f"{self._current_fdt or '等待 FDT'} / {self._current_link or '—'}")
    self.current_path_label.setText("\n↓\n".join(path) if path else "请先点击 FDT")
    infos = _link_infos(self)
    if infos:
        last = infos[-1]
        total = sum(i["distance"] for i in infos)
        self.segment_label.setText(f"当前段：{last['from_label']} → {last['to_label']}    {last['distance']:.1f} m")
        self.route_label.setText(f"Pole Edge：{len(last['edge_sequence'])} 个边段    |    实际路径：{last['distance']:.1f} m")
        self.total_label.setText(f"Link 累计：{total:.1f} m")
    else:
        self.segment_label.setText("当前段：—"); self.route_label.setText("Pole Edge：—"); self.total_label.setText("Link 累计：—")
    self._refresh_tree()


def finish_link(self):
    if not getattr(self, "_draw_active", False) or getattr(self, "_awaiting_fdt", False):
        return
    if not self._current:
        self.status.setText("状态：当前还没有 FAT，不能完成 Link。")
        return
    infos = _link_infos(self)
    if not infos:
        QtWidgets.QMessageBox.warning(self, "链路设计", "当前 Link 存在无法沿 Pole Edge 建立的路径，不能完成。")
        return
    total = sum(i["distance"] for i in infos)
    done_fdt, done_link = self._current_fdt, self._current_link
    self._designs.append({
        "fdt": done_fdt,
        "link": done_link,
        "nodes": list(self._current),
        "length": round(total, 3),
        "segments": [
            {"from": i["from_label"], "to": i["to_label"], "distance": round(i["distance"], 3), "edge_count": len(i["edge_sequence"])}
            for i in infos
        ],
    })
    self._current = []
    used = {d.get("link") for d in self._designs if d.get("fdt") == done_fdt}
    next_link = next((f"L{i}" for i in range(1, self._project_max_links + 1) if f"L{i}" not in used), None)
    if next_link:
        self._current_link = next_link
        self._current_fdt = done_fdt
        self._awaiting_fdt = False
        self.undo_btn.setEnabled(True); self.finish_btn.setEnabled(False)
        self.status.setText(f"状态：已完成 {done_fdt}/{done_link}（{total:.1f} m），进入 {next_link}，继续点击 FAT。")
    else:
        self._current_fdt = None; self._current_link = None; self._awaiting_fdt = True
        self.undo_btn.setEnabled(False); self.finish_btn.setEnabled(False)
        self.status.setText(f"状态：已完成 {done_fdt}/{done_link}（{total:.1f} m），该 FDT 已达到 Link 上限。")
    if self._tool:
        self._tool.clear_current()
    self._update_current()


def stop_drawing(self):
    self._draw_active = False; self._awaiting_fdt = False; self._current = []
    self._current_fdt = None; self._current_link = None
    self.undo_btn.setEnabled(False); self.finish_btn.setEnabled(False); self.cancel_draw_btn.setEnabled(False)
    if self._tool:
        try: self._tool.clear_current()
        except Exception: pass
        self._tool = None
    self.current_label.setText("等待开始"); self.current_path_label.setText("请点击“开始画链路”。")
    self.segment_label.setText("当前段：—"); self.route_label.setText("Pole Edge：—"); self.total_label.setText("Link 累计：—")
    self.status.setText("状态：已停止画链路。")


def _refresh_tree(self):
    self.design_tree.clear()
    groups = defaultdict(list)
    for d in getattr(self, "_designs", []):
        groups[d.get("fdt", "")].append(d)
    for fdt, designs in groups.items():
        parent = QtWidgets.QTreeWidgetItem([fdt, "", ""]); self.design_tree.addTopLevelItem(parent)
        for d in designs:
            length = d.get("length")
            QtWidgets.QTreeWidgetItem(parent, [d.get("link", ""), str(len(d.get("nodes", []))), f"{float(length):.1f} m" if length is not None else "—"])
        parent.setExpanded(True)


def install_project_ui_policy(dialog_class):
    if getattr(dialog_class, "_project_ui_policy_installed", False):
        return
    dialog_class._build_ui = _build_ui
    dialog_class._refresh_project_ui = _refresh_project_ui
    dialog_class._prepare_layers = _prepare_layers
    dialog_class.start_link = start_link
    dialog_class.set_fdt = set_fdt
    dialog_class.add_node = add_node
    dialog_class._link_infos = _link_infos
    dialog_class._link_total = _link_total
    dialog_class._update_current = _update_current
    dialog_class.finish_link = finish_link
    dialog_class.stop_drawing = stop_drawing
    dialog_class._refresh_tree = _refresh_tree
    dialog_class._project_ui_policy_installed = True
