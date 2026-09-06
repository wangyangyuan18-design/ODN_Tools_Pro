# -*- coding: utf-8 -*-
"""Link Design UI v6.

UI refinement for the production Link Design dialog. v4 remains the source
of truth for planning, persistence, reconciliation, editing and writing.
"""

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsCoordinateTransform, QgsGeometry, QgsPointXY, QgsProject, QgsWkbTypes
from qgis.gui import QgsRubberBand

from .link_design_v4 import LinkDesignDialog as _V4LinkDesignDialog
from .link_design_v2 import LinkDesignMapTool as _BaseMapTool
from .link_design_v2 import CompletedDesignDialog as _BaseCompletedDesignDialog


def _all_completed_fat_ids(dialog):
    result = set()
    for design in dialog._designs:
        for item in design.get("nodes", []):
            try:
                result.add(int(item[0]))
            except (TypeError, ValueError, IndexError):
                continue
    return result


def _current_fdt_designs(dialog):
    fdt = str(getattr(dialog, "_current_fdt", "") or "")
    if not fdt:
        return []
    return [(i, d) for i, d in enumerate(dialog._designs) if str(d.get("fdt", "")) == fdt]


def _current_link_fat_ids(dialog):
    result = set()
    for item in getattr(dialog, "_sequence", []):
        if item[0] == "FAT":
            try:
                result.add(int(item[1]))
            except (TypeError, ValueError, IndexError):
                pass
    return result


def _fat_count(design):
    return sum(1 for item in design.get("sequence_ids", []) if item and item[0] == "FAT")


class LinkDesignDialog(_V4LinkDesignDialog):
    """v6 UI with current-FDT statistics and project-wide FAT state display."""

    def __init__(self, iface, parent=None):
        self._fat_completed_bands = []
        self._fat_current_bands = []
        super().__init__(iface, parent)
        self._refresh_fat_overlays()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(5)

        title = QtWidgets.QLabel("链路设计")
        font = title.font(); font.setBold(True); font.setPointSize(13); title.setFont(font)
        root.addWidget(title)

        self.plan_label = QtWidgets.QLabel("规划中")
        self.plan_label.setStyleSheet("font-weight:600;")
        root.addWidget(self.plan_label)

        self.current_path_label = QtWidgets.QLabel("当前路径：—")
        self.current_path_label.setWordWrap(True)
        root.addWidget(self.current_path_label)

        self.distance_label = QtWidgets.QLabel("各段距离：—")
        self.distance_label.setWordWrap(True)
        root.addWidget(self.distance_label)

        self.total_distance_label = QtWidgets.QLabel("链路总距离：—")
        self.total_distance_label.setStyleSheet("font-weight:600;")
        root.addWidget(self.total_distance_label)

        self.fdt_label = QtWidgets.QLabel("规划中 FDT：— | Link：—")
        root.addWidget(self.fdt_label)
        self.link_count_label = QtWidgets.QLabel("当前 FDT 已规划链路：0")
        self.planned_fat_label = QtWidgets.QLabel("当前 FDT 已规划 FAT：0")
        root.addWidget(self.link_count_label)
        root.addWidget(self.planned_fat_label)

        line = QtWidgets.QFrame(); line.setFrameShape(QtWidgets.QFrame.HLine); line.setFrameShadow(QtWidgets.QFrame.Sunken)
        root.addWidget(line)

        row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("开始规划")
        self.save_btn = QtWidgets.QPushButton("保存规划")
        self.done_btn = QtWidgets.QPushButton("已完成设计")
        self.exit_btn = QtWidgets.QPushButton("退出设计")
        for button in (self.start_btn, self.save_btn, self.done_btn, self.exit_btn):
            button.setMinimumHeight(28); row.addWidget(button)
        root.addLayout(row)

        self.status = QtWidgets.QLabel("状态：等待开始规划")
        self.status.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self.start_btn.clicked.connect(self.start_design)
        self.save_btn.clicked.connect(self.save_current_link)
        self.done_btn.clicked.connect(self.open_completed_designs)
        self.exit_btn.clicked.connect(self.exit_design)
        self.save_btn.setEnabled(False)
        self.resize(520, 330); self.setMinimumWidth(480)

    def _activate_map_tool(self):
        if self._engine is None:
            self._engine = self._prepare_engine()
        if self._engine is None:
            return False
        self._clear_fat_overlays()
        self._tool = LinkDesignMapToolV6(self.iface, self._engine, self)
        self.iface.mapCanvas().setMapTool(self._tool)
        self._draw_active = True
        self.start_btn.setEnabled(False)
        self._tool.start()
        self._refresh_fat_overlays()
        return True

    def _clear_fat_overlays(self):
        canvas = self.iface.mapCanvas()
        for band in self._fat_completed_bands + self._fat_current_bands:
            try: canvas.scene().removeItem(band)
            except Exception: pass
        self._fat_completed_bands = []; self._fat_current_bands = []

    def _fat_point_canvas(self, info):
        point = QgsPointXY(info["point"])
        src = info["layer"].crs(); dst = self.iface.mapCanvas().mapSettings().destinationCrs()
        if src != dst:
            point = QgsCoordinateTransform(src, dst, QgsProject.instance().transformContext()).transform(point)
        return point

    def _draw_fat_point(self, info, color, size):
        band = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.PointGeometry)
        band.setColor(color); band.setWidth(2); band.setIcon(QgsRubberBand.ICON_CIRCLE); band.setIconSize(size)
        band.setToGeometry(QgsGeometry.fromPointXY(self._fat_point_canvas(info)), self.iface.mapCanvas().mapSettings().destinationCrs())
        return band

    def _refresh_fat_overlays(self):
        if not hasattr(self, "_fat_completed_bands"): return
        self._clear_fat_overlays()
        if self._engine is None: return
        completed = _all_completed_fat_ids(self)
        current = _current_link_fat_ids(self)
        for _, info in self._engine.points_of_type("FAT"):
            fid = int(info["feature_id"])
            if fid in current:
                self._fat_current_bands.append(self._draw_fat_point(info, QColor(255, 196, 0), 13))
            elif fid in completed:
                self._fat_completed_bands.append(self._draw_fat_point(info, QColor(150, 150, 150), 10))

    def start_design(self):
        result = super().start_design(); self._refresh_fat_overlays(); return result
    def _start_from_hit(self, info):
        result = super()._start_from_hit(info); self._refresh_fat_overlays(); return result
    def add_fat(self, info):
        result = super().add_fat(info); self._refresh_fat_overlays(); return result
    def add_fdt(self, info):
        result = super().add_fdt(info); self._refresh_fat_overlays(); return result
    def undo_last(self):
        result = super().undo_last(); self._refresh_fat_overlays(); return result
    def save_current_link(self):
        result = super().save_current_link(); self._refresh_fat_overlays(); return result
    def load_design_for_edit(self, index):
        result = super().load_design_for_edit(index); self._refresh_fat_overlays(); return result
    def _stop_tool(self):
        super()._stop_tool(); self._refresh_fat_overlays()

    def _refresh_ui(self):
        self.plan_label.setText("规划中" + (" · 修改" if self._editing_index is not None else ""))
        self.fdt_label.setText(f"规划中 FDT：{self._current_fdt or '—'} | Link：{self._current_link or '—'}")
        designs = _current_fdt_designs(self)
        fats = set()
        for _, design in designs:
            for item in design.get("nodes", []):
                try: fats.add(int(item[0]))
                except (TypeError, ValueError, IndexError): pass
        self.link_count_label.setText(f"当前 FDT 已规划链路：{len(designs)}")
        self.planned_fat_label.setText(f"当前 FDT 已规划 FAT：{len(fats)}")

        if not self._sequence:
            self.current_path_label.setText("当前路径：—")
            self.distance_label.setText("各段距离：—")
            self.total_distance_label.setText("链路总距离：—")
            self.save_btn.setEnabled(False)
            return

        self.current_path_label.setText("当前路径：" + " → ".join(item[2] for item in self._sequence))
        routes = []
        if self._engine:
            for first, second in zip(self._sequence[:-1], self._sequence[1:]):
                route = self._engine.route(first[0], first[1], second[0], second[1])
                if route: routes.append(route)
        self.distance_label.setText("各段距离：" + ("、".join(f"{r['distance']:.1f}m" for r in routes) if routes else "—"))
        total = sum(float(r["distance"]) for r in routes)
        self.total_distance_label.setText(f"链路总距离：{total:.1f}m" if routes else "链路总距离：—")
        self.save_btn.setEnabled(bool(self._current_fdt and any(x[0] == "FAT" for x in self._sequence)))

    def open_completed_designs(self):
        self._reconcile_written_state(); self._repair_saved_distances()
        dlg = CompletedDesignDialogV6(self); dlg.exec_()
        self._refresh_ui(); self._refresh_fat_overlays()

    def closeEvent(self, event):
        self._clear_fat_overlays(); super().closeEvent(event)


class CompletedDesignDialogV6(_BaseCompletedDesignDialog):
    """Completed design browser; written/planned are both presented as completed."""

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(10,10,10,10); root.setSpacing(6)
        title = QtWidgets.QLabel("已完成设计"); font = title.font(); font.setBold(True); font.setPointSize(12); title.setFont(font); root.addWidget(title)
        self.summary = QtWidgets.QLabel("—"); self.summary.setWordWrap(True); root.addWidget(self.summary)
        self.tree = QtWidgets.QTreeWidget(); self.tree.setHeaderLabels(["FDT / Link", "FAT数", "总距离", "状态"])
        self.tree.setColumnWidth(0, 190); self.tree.setColumnWidth(1, 55); self.tree.setColumnWidth(2, 80); self.tree.setAlternatingRowColors(True)
        self.tree.itemClicked.connect(self._on_clicked); self.tree.itemDoubleClicked.connect(self._on_double_clicked); root.addWidget(self.tree,1)
        self.info = QtWidgets.QLabel("点击 Link 查看路线；双击 Link 进入修改。")
        self.info.setWordWrap(True); self.info.setStyleSheet("color:#666;"); root.addWidget(self.info)
        row=QtWidgets.QHBoxLayout(); self.modify_btn=QtWidgets.QPushButton("修改选中 Link"); self.delete_btn=QtWidgets.QPushButton("删除选中 Link"); self.write_btn=QtWidgets.QPushButton("确定并写入图层"); close_btn=QtWidgets.QPushButton("关闭")
        for b in (self.modify_btn,self.delete_btn,self.write_btn,close_btn): row.addWidget(b)
        root.addLayout(row); self.modify_btn.clicked.connect(self._modify_selected); self.delete_btn.clicked.connect(self._delete_selected); self.write_btn.clicked.connect(self._write_all); close_btn.clicked.connect(self.accept)
        self.resize(520,500); self.setMinimumSize(470,420)

    def _refresh_tree(self):
        self.tree.clear(); grouped={}; all_fats=set(); total_distance=0.0
        for i,d in enumerate(self.main_dialog._designs): grouped.setdefault(str(d.get("fdt","未知 FDT")),[]).append((i,d))
        for fdt, entries in sorted(grouped.items()):
            fdt_fats=set(); fdt_distance=0.0
            for _,d in entries:
                for x in d.get("nodes",[]):
                    try: fdt_fats.add(int(x[0]))
                    except (TypeError,ValueError,IndexError): pass
                fdt_distance += float(d.get("length",0.0) or 0.0)
            all_fats.update(fdt_fats); total_distance += fdt_distance
            root=QtWidgets.QTreeWidgetItem([fdt,str(len(fdt_fats)),f"{fdt_distance:.1f}m","已完成"]); root.setData(0,Qt.UserRole,("fdt",fdt)); root.setExpanded(True); self.tree.addTopLevelItem(root)
            for i,d in sorted(entries,key=lambda x:str(x[1].get("link",""))):
                child=QtWidgets.QTreeWidgetItem([str(d.get("link","L?")),str(_fat_count(d)),f"{float(d.get('length',0.0) or 0.0):.1f}m","已完成"])
                child.setData(0,Qt.UserRole,("link",i)); child.setToolTip(0," → ".join(d.get("sequence",[]))); root.addChild(child)
        self.summary.setText(f"已完成 Link：{len(self.main_dialog._designs)}　已完成 FAT：{len(all_fats)}　总路径：{total_distance:.1f}m")

    def _on_clicked(self,item,column):
        target=item.data(0,Qt.UserRole)
        if not target:return
        self._last_target=target
        if target[0]=="fdt":
            entries=[(i,d) for i,d in enumerate(self.main_dialog._designs) if d.get("fdt")==target[1]]; self.main_dialog.show_saved_designs(entries)
            total=sum(float(d.get("length",0.0) or 0.0) for _,d in entries); fats=set()
            for _,d in entries:
                for x in d.get("nodes",[]):
                    try:fats.add(int(x[0]))
                    except (TypeError,ValueError,IndexError):pass
            self.info.setText(f"{target[1]}：{len(entries)} 条 Link，{len(fats)} 个 FAT，总路径 {total:.1f}m。")
        else:
            i=int(target[1])
            if 0<=i<len(self.main_dialog._designs):
                d=self.main_dialog._designs[i]; self.main_dialog.show_saved_designs([(i,d)])
                self.info.setText(f"{d.get('fdt','')}/{d.get('link','')}：{_fat_count(d)} 个 FAT，{float(d.get('length',0.0) or 0.0):.1f}m，已完成。")

    def _on_double_clicked(self,item,column):
        target=item.data(0,Qt.UserRole)
        if target and target[0]=="link": self._modify_selected()

    def _selected_index(self):
        current=self.tree.currentItem(); target=current.data(0,Qt.UserRole) if current else self._last_target
        return int(target[1]) if target and target[0]=="link" else None

    def _modify_selected(self):
        index=self._selected_index()
        if index is None: QtWidgets.QMessageBox.information(self,"修改 Link","请先选择一个 Link。"); return
        if self.main_dialog.load_design_for_edit(index): self.accept()

    def _delete_selected(self):
        index=self._selected_index()
        if index is None: QtWidgets.QMessageBox.information(self,"删除 Link","请先选择一个 Link。"); return
        if self.main_dialog.delete_link(index): self._refresh_tree(); self.main_dialog._refresh_fat_overlays()

    def _write_all(self):
        if self.main_dialog.write_planned_links(): self._refresh_tree(); self.main_dialog._refresh_fat_overlays()


class LinkDesignMapToolV6(_BaseMapTool):
    """Preview route plus hover distance; completed/current FAT styling lives on the dialog."""

    def _current_route_total(self, prospective):
        total=0.0
        for first,second in zip(self.dialog._sequence[:-1],self.dialog._sequence[1:]):
            route=self.dialog._engine.route(first[0],first[1],second[0],second[1]) if self.dialog._engine else None
            if route: total += float(route.get("distance",0.0))
        return total+float(prospective.get("distance",0.0))

    def canvasMoveEvent(self,event):
        if not self.dialog._draw_active or not self.dialog._sequence:
            self._clear_hover(); QtWidgets.QToolTip.hideText(); return
        hit=self._nearest(event.pos())
        if not hit:
            self._clear_hover(); QtWidgets.QToolTip.hideText(); return
        _,_,info=hit; route=self.dialog.prospective_route(info)
        if not route:
            self._clear_hover(); QtWidgets.QToolTip.hideText(); return
        self._draw_hover(route)
        total=self._current_route_total(route); label=info.get("label","")
        state=""
        if info["typ"]=="FAT":
            fid=int(info["feature_id"])
            if fid in _current_link_fat_ids(self.dialog): state="\n当前 Link 已选择"
            elif fid in _all_completed_fat_ids(self.dialog): state="\n已完成设计 FAT"
        QtWidgets.QToolTip.showText(event.globalPos(),f"{label}{state}\n当前段：{float(route['distance']):.1f} m\n链路总距离：{total:.1f} m",self.canvas)
        self.dialog._refresh_ui()
