# -*- coding: utf-8 -*-
"""Project-driven interactive pole-trace connection tool."""

import time
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtGui import QColor, QFont, QCursor
from qgis.core import (
    QgsCoordinateTransform, QgsFeature, QgsGeometry, QgsMapLayerType,
    QgsPointXY, QgsProject, QgsRectangle, QgsSpatialIndex, QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand

from . import odn_project_context as context


class PoleTraceDialog(QtWidgets.QDialog):
    """Entry dialog. Layer widgets remain visible but are project-driven/read-only."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("杆路轨迹自动连线")
        self.setMinimumWidth(430)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(9)

        title = QtWidgets.QLabel("杆路轨迹自动连线")
        f = QFont(); f.setPointSize(12); f.setBold(True); title.setFont(f)
        layout.addWidget(title)

        layout.addWidget(QtWidgets.QLabel("杆子图层（项目配置）"))
        self.point_list = QtWidgets.QListWidget()
        self.point_list.setMinimumHeight(75)
        self.point_list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.point_list.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self.point_list)

        layout.addWidget(QtWidgets.QLabel("连线图层（项目配置）"))
        self.line_combo = QtWidgets.QComboBox()
        self.line_combo.setEnabled(False)
        layout.addWidget(self.line_combo)

        tip = QtWidgets.QLabel(
            "图层自动读取当前 ODN Project。\n"
            "Space：开始轨迹；绘制中再次按 Space 结束当前轨迹。再次按 Space 开始下一条。\n"
            "Shift：暂停记录并进行地图缩放/平移；Backspace：回退；Esc：取消当前轨迹。\n"
            "保存请点击绘制面板中的“保存”，不再使用双击右键确认。"
        )
        tip.setWordWrap(True); tip.setStyleSheet("color:#666; padding:4px 0;")
        layout.addWidget(tip)

        buttons = QtWidgets.QDialogButtonBox()
        self.start_btn = buttons.addButton("开始", QtWidgets.QDialogButtonBox.AcceptRole)
        self.cancel_btn = buttons.addButton("关闭", QtWidgets.QDialogButtonBox.RejectRole)
        self.start_btn.clicked.connect(self._start)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._load_project_layers()

    def _load_project_layers(self):
        self.point_list.clear(); self.line_combo.clear()
        payload = context.require_project(self, "杆路轨迹自动连线")
        if not payload:
            self.start_btn.setEnabled(False)
            return
        existing = context.project_layer(payload, "Existing Pole")
        new = context.project_layer(payload, "New Pole")
        edge = context.project_layer(payload, "Pole Edge")
        for role, layer in (("Existing Pole", existing), ("New Pole", new)):
            if layer is not None and layer.type() == QgsMapLayerType.VectorLayer:
                item = QtWidgets.QListWidgetItem(f"{role}：{layer.name()}")
                item.setData(Qt.UserRole, layer.id())
                self.point_list.addItem(item)
        if edge is not None and edge.type() == QgsMapLayerType.VectorLayer:
            self.line_combo.addItem(edge.name(), edge.id())
        self.start_btn.setEnabled(self.point_list.count() > 0 and self.line_combo.count() == 1)

    def _start(self):
        point_ids = [self.point_list.item(i).data(Qt.UserRole) for i in range(self.point_list.count())]
        line_id = self.line_combo.currentData()
        if not point_ids or not line_id:
            QtWidgets.QMessageBox.warning(self, "杆路轨迹自动连线", "当前 ODN Project 未正确绑定 Existing Pole / New Pole / Pole Edge。\n\n请先在【项目配置】中修正图层绑定。")
            return
        self.accept()
        tool = PoleTraceMapTool(self.iface, point_ids, line_id)
        self.iface.mapCanvas().setMapTool(tool)
        tool.start()


class _StatusPanel(QtWidgets.QFrame):
    def __init__(self, canvas, tool):
        super().__init__(canvas); self.tool = tool
        self.setObjectName("PoleTraceStatusPanel")
        self.setStyleSheet("QFrame#PoleTraceStatusPanel { background: rgba(255,255,255,238); border:1px solid #999; border-radius:5px; } QLabel { padding:1px; }")
        self.state = QtWidgets.QLabel(); self.path = QtWidgets.QLabel(); self.path.setWordWrap(True); self.path.setMaximumWidth(390)
        self.info = QtWidgets.QLabel(); self.info.setWordWrap(True)
        lay = QtWidgets.QVBoxLayout(self); lay.setContentsMargins(10,8,10,8); lay.setSpacing(4)
        lay.addWidget(self.state); lay.addWidget(self.path); lay.addWidget(self.info)
        row = QtWidgets.QHBoxLayout()
        self.save_btn = QtWidgets.QPushButton("保存")
        self.cancel_btn = QtWidgets.QPushButton("取消")
        self.exit_btn = QtWidgets.QPushButton("退出")
        row.addWidget(self.save_btn); row.addWidget(self.cancel_btn); row.addWidget(self.exit_btn)
        lay.addLayout(row)
        self.save_btn.clicked.connect(tool.request_save)
        self.cancel_btn.clicked.connect(tool.cancel_confirmation)
        self.exit_btn.clicked.connect(tool.exit_without_save)
        self.move(12, 12); self.show(); self.raise_(); self.update_view()

    def update_view(self):
        if self.tool.state == "WAITING":
            state = "🟢 状态：等待轨迹"
        elif self.tool._shift_navigation_active():
            state = "🟡 状态：Shift 暂停记录（正在缩放/平移）"
        else:
            state = "🔴 状态：绘制轨迹"
        self.state.setText(state)
        if self.tool.current_path:
            self.path.setText("当前轨迹：" + " → ".join(self.tool.pole_label(x) for x in self.tool.current_path[-12:]))
        else:
            self.path.setText("当前轨迹：等待起点")
        self.info.setText(
            f"轨迹数量：{len(self.tool._completed_paths) + (1 if self.tool.current_path else 0)}    "
            f"待保存连接：{len(self.tool.pending_edges)} 条\n"
            "Space：结束当前轨迹；再次按 Space 开始下一条　|　Shift：暂停导航\n"
            "Backspace：回退　|　Esc：取消当前轨迹"
        )
        self.cancel_btn.setEnabled(self.tool._confirmation_open)
        self.adjustSize()


class PoleTraceMapTool(QgsMapTool):
    SNAP_PIXELS = 28
    HYSTERESIS_PIXELS = 5
    MIN_SAMPLE_PIXELS = 2

    def __init__(self, iface, point_layer_ids, line_layer_id):
        self.iface = iface; self.canvas = iface.mapCanvas(); super().__init__(self.canvas); self.setCursor(Qt.CrossCursor)
        self.point_layer_ids = list(point_layer_ids); self.line_layer_id = line_layer_id; self.state = "WAITING"
        self.current_path = []; self._completed_paths = []; self.pending_edges = set(); self._current_trace_edges = set()
        self._index = QgsSpatialIndex(); self._feature_map = {}; self._point_by_id = {}; self._labels = {}; self._next_index_id = 1
        self._preview_band = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self._preview_band.setColor(QColor(0,180,255,220)); self._preview_band.setWidth(4)
        self._last_candidate = None; self._last_mouse_px = None; self._pan_start = None
        self._shift_down = False; self._panel = None; self._existing_edges = set(); self._confirmation_open = False
        self._build_index()

    @property
    def line_layer(self): return QgsProject.instance().mapLayer(self.line_layer_id)

    def start(self):
        self._panel = _StatusPanel(self.canvas, self)
        self._show_message("杆路轨迹连线已启动：移动到起点后按 Space。")
        self._refresh_preview()

    def _show_message(self, text, level=0, duration=3):
        try: self.iface.messageBar().pushMessage("ODN Tools Pro", text, level=level, duration=duration)
        except Exception: pass

    def _panel_update(self):
        if self._panel: self._panel.update_view()

    def _shift_navigation_active(self):
        try: return self._shift_down or bool(QtWidgets.QApplication.keyboardModifiers() & Qt.ShiftModifier)
        except Exception: return self._shift_down

    def _build_index(self):
        self._index = QgsSpatialIndex(); self._feature_map.clear(); self._point_by_id.clear(); self._labels.clear(); self._next_index_id = 1
        dst = self.canvas.mapSettings().destinationCrs(); project = QgsProject.instance()
        for layer_id in self.point_layer_ids:
            layer = project.mapLayer(layer_id)
            if layer is None: continue
            transform = QgsCoordinateTransform(layer.crs(), dst, project.transformContext()) if layer.crs() != dst else None
            for feat in layer.getFeatures():
                geom = feat.geometry()
                if geom.isEmpty(): continue
                try:
                    pt = QgsPointXY(geom.centroid().asPoint())
                    if transform: pt = QgsPointXY(transform.transform(pt))
                except Exception: continue
                rid = (layer_id, int(feat.id())); iid = self._next_index_id; self._next_index_id += 1
                f = QgsFeature(); f.setId(iid); f.setGeometry(QgsGeometry.fromPointXY(pt)); self._index.addFeature(f)
                self._feature_map[iid] = (rid, pt); self._point_by_id[rid] = pt
                try:
                    self._labels[rid] = str(feat["Name"]) if "Name" in feat.fields().names() and feat["Name"] not in (None, "") else str(feat.id())
                except Exception:
                    self._labels[rid] = str(feat.id())
        self._existing_edges = self._read_existing_edges()

    def _pixel_rect(self, pos, tol):
        tr = self.canvas.getCoordinateTransform()
        p1 = tr.toMapCoordinates(int(pos.x()-tol), int(pos.y()-tol)); p2 = tr.toMapCoordinates(int(pos.x()+tol), int(pos.y()+tol))
        return QgsRectangle(min(p1.x(),p2.x()), min(p1.y(),p2.y()), max(p1.x(),p2.x()), max(p1.y(),p2.y()))

    def _nearest_feature(self, map_point, tolerance_pixels=None):
        if not self._feature_map: return None
        tol = self.SNAP_PIXELS if tolerance_pixels is None else tolerance_pixels
        screen = self.canvas.getCoordinateTransform().transform(map_point)
        candidates = self._index.intersects(self._pixel_rect(screen, tol))
        if not candidates: return None
        best = None; best_screen_d2 = float("inf"); tr = self.canvas.getCoordinateTransform()
        for iid in candidates:
            data = self._feature_map.get(iid)
            if not data: continue
            rid, pt = data; sp = tr.transform(pt); dx = sp.x()-screen.x(); dy = sp.y()-screen.y(); d2 = dx*dx+dy*dy
            if d2 <= tol*tol and d2 < best_screen_d2: best, best_screen_d2 = rid, d2
        return best

    def _read_existing_edges(self):
        edges = set(); layer = self.line_layer
        if layer is None: return edges
        project = QgsProject.instance(); dst = self.canvas.mapSettings().destinationCrs()
        transform = QgsCoordinateTransform(layer.crs(), dst, project.transformContext()) if layer.crs() != dst else None
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom.isEmpty(): continue
            try:
                pts = (geom.asMultiPolyline()[0] if geom.isMultipart() and geom.asMultiPolyline() else geom.asPolyline())
                if len(pts) < 2: continue
                a,b = QgsPointXY(pts[0]), QgsPointXY(pts[-1])
                if transform: a,b = QgsPointXY(transform.transform(a)), QgsPointXY(transform.transform(b))
                ia,ib = self._nearest_feature(a,8), self._nearest_feature(b,8)
                if ia and ib and ia != ib: edges.add(self._edge_key(ia,ib))
            except Exception: continue
        return edges

    @staticmethod
    def _edge_key(a,b): return tuple(sorted((a,b), key=lambda x:(x[0],x[1])))
    def pole_label(self,fid): return self._labels.get(fid,str(fid[1]))

    def _candidate_at(self,map_point):
        cand = self._nearest_feature(map_point)
        if cand == self._last_candidate: return cand
        old = self._point_by_id.get(self._last_candidate); new = self._point_by_id.get(cand)
        if old is not None and new is not None:
            tr=self.canvas.getCoordinateTransform(); m=tr.transform(map_point); o=tr.transform(old); n=tr.transform(new)
            od=(o.x()-m.x())**2+(o.y()-m.y())**2; nd=(n.x()-m.x())**2+(n.y()-m.y())**2
            if nd + self.HYSTERESIS_PIXELS**2 >= od: cand=self._last_candidate
        self._last_candidate=cand; return cand

    def _add_path_pole(self,fid):
        if fid is None or (self.current_path and fid == self.current_path[-1]): return
        if self.current_path and fid in self.current_path[-8:]: return
        if not self.current_path:
            self.current_path.append(fid); self._panel_update(); return
        prev=self.current_path[-1]; edge=self._edge_key(prev,fid); self.current_path.append(fid)
        if edge not in self._existing_edges and edge not in self.pending_edges:
            self.pending_edges.add(edge); self._current_trace_edges.add(edge); self._refresh_preview()
        self._panel_update()

    def _finish_current_trace(self):
        if self.current_path: self._completed_paths.append(list(self.current_path))
        self.current_path.clear(); self._current_trace_edges.clear(); self._last_candidate=None; self._last_mouse_px=None; self._panel_update()

    def _start_trace_from_mouse(self):
        self.current_path.clear(); self._current_trace_edges.clear(); self._last_candidate=None; self._last_mouse_px=None; self.state="DRAWING"; self._panel_update()
        pos=self.canvas.mapFromGlobal(QCursor.pos())
        if self.canvas.rect().contains(pos):
            self._last_mouse_px=QPoint(pos); fid=self._candidate_at(self.toMapCoordinates(pos))
            if fid is not None: self._add_path_pole(fid)

    def _remove_last_path_pole(self):
        if not self.current_path: return
        if len(self.current_path)>=2:
            edge=self._edge_key(self.current_path[-2],self.current_path[-1])
            if edge in self._current_trace_edges:
                self._current_trace_edges.discard(edge); self.pending_edges.discard(edge); self._refresh_preview()
        self.current_path.pop(); self._last_candidate=self.current_path[-1] if self.current_path else None; self._panel_update()

    def _cancel_current_trace(self):
        for edge in list(self._current_trace_edges): self.pending_edges.discard(edge)
        self.current_path.clear(); self._current_trace_edges.clear(); self._last_candidate=None; self._last_mouse_px=None; self.state="WAITING"; self._refresh_preview(); self._panel_update()

    def _refresh_preview(self):
        self._preview_band.reset(QgsWkbTypes.LineGeometry)
        lines=[]
        for a,b in self.pending_edges:
            pa,pb=self._point_by_id.get(a),self._point_by_id.get(b)
            if pa is not None and pb is not None: lines.append([pa,pb])
        if lines:
            self._preview_band.setToGeometry(QgsGeometry.fromMultiPolylineXY(lines),None); self._preview_band.show()
        else: self._preview_band.hide()
        self.canvas.refresh()

    def keyPressEvent(self,e):
        if e.isAutoRepeat(): return
        if e.key()==Qt.Key_Shift:
            self._shift_down=True; self._panel_update(); e.accept(); return
        if e.key()==Qt.Key_Space:
            if self._shift_navigation_active(): e.accept(); return
            if self.state=="WAITING": self._start_trace_from_mouse(); self._show_message("开始新轨迹：以当前鼠标位置作为起点。")
            else:
                self._finish_current_trace(); self.state="WAITING"; self._panel_update(); self._show_message("当前轨迹已结束。移动鼠标到下一条轨迹起点，再按 Space。")
            e.accept(); return
        if e.key()==Qt.Key_Escape:
            self._cancel_current_trace(); e.accept(); return
        if e.key()==Qt.Key_Backspace:
            self._remove_last_path_pole(); e.accept(); return
        super().keyPressEvent(e)

    def keyReleaseEvent(self,e):
        if e.key()==Qt.Key_Shift:
            self._shift_down=False; self._last_mouse_px=None; self._last_candidate=self.current_path[-1] if self.current_path else None; self._panel_update(); e.accept(); return
        super().keyReleaseEvent(e)

    def canvasMoveEvent(self,e):
        if self._pan_start is not None: self._pan_map(e.pos()); return
        if self.state!="DRAWING" or self._shift_navigation_active(): return
        pos=e.pos()
        if self._last_mouse_px is not None:
            dx=pos.x()-self._last_mouse_px.x(); dy=pos.y()-self._last_mouse_px.y()
            if dx*dx+dy*dy < self.MIN_SAMPLE_PIXELS**2: return
        self._last_mouse_px=QPoint(pos); fid=self._candidate_at(self.toMapCoordinates(pos))
        if fid is not None: self._add_path_pole(fid)

    def canvasPressEvent(self,e):
        if e.button()==Qt.MiddleButton:
            if self._shift_navigation_active() or self.state!="DRAWING": self._pan_start=QPoint(e.pos())
            e.accept(); return
        e.accept()

    def canvasReleaseEvent(self,e):
        if e.button()==Qt.MiddleButton: self._pan_start=None
        e.accept()

    def wheelEvent(self,e):
        if self.state=="DRAWING" and not self._shift_navigation_active(): e.accept(); return
        try:
            if e.angleDelta().y()>0: self.canvas.zoomIn()
            elif e.angleDelta().y()<0: self.canvas.zoomOut()
        except Exception: pass
        e.accept()

    def _pan_map(self,pos):
        if self._pan_start is None:return
        p0=self.toMapCoordinates(self._pan_start); p1=self.toMapCoordinates(pos); self.canvas.setCenter(self.canvas.center()+(p0-p1)); self._pan_start=QPoint(pos); self.canvas.refresh()

    def request_save(self):
        self._confirmation_open = True
        self._panel_update()
        box=QtWidgets.QMessageBox(self.canvas)
        box.setWindowTitle("杆路轨迹自动连线")
        box.setText("是否保存当前已生成的全部杆间连线？")
        box.setInformativeText(
            f"轨迹数量：{len(self._completed_paths)+(1 if self.current_path else 0)}\n"
            f"待保存连接：{len(self.pending_edges)}条"
        )
        save=box.addButton("保存",QtWidgets.QMessageBox.AcceptRole)
        cancel=box.addButton("Cancel",QtWidgets.QMessageBox.RejectRole)
        exit_btn=box.addButton("退出",QtWidgets.QMessageBox.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec_()
        self._confirmation_open = False
        clicked = box.clickedButton()
        if clicked is save: self._save_and_stop()
        elif clicked is exit_btn: self._exit_without_save()
        else: self._panel_update()

    def cancel_confirmation(self):
        self._confirmation_open = False
        self._panel_update()

    def _save_and_stop(self):
        layer=self.line_layer
        if layer is None:
            QtWidgets.QMessageBox.critical(self.canvas,"杆路轨迹自动连线","保存线图层不存在，无法保存。")
            return
        started=False
        try:
            if not layer.isEditable():
                if not layer.startEditing(): raise RuntimeError("无法进入线图层编辑状态。")
                started=True
            transform=QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(),layer.crs(),QgsProject.instance().transformContext()) if self.canvas.mapSettings().destinationCrs()!=layer.crs() else None
            added=0
            for a,b in self.pending_edges:
                pa,pb=self._point_by_id.get(a),self._point_by_id.get(b)
                if pa is None or pb is None: continue
                if transform: pa,pb=QgsPointXY(transform.transform(pa)),QgsPointXY(transform.transform(pb))
                f=QgsFeature(layer.fields()); f.setGeometry(QgsGeometry.fromPolylineXY([pa,pb]))
                if layer.addFeature(f): added+=1
            if started and not layer.commitChanges(): raise RuntimeError("线图层提交失败。")
            self._show_message(f"杆路轨迹连线完成：新增 {added} 条连接。")
        except Exception as exc:
            try:
                if started: layer.rollBack()
            except Exception: pass
            QtWidgets.QMessageBox.critical(self.canvas,"杆路轨迹自动连线",f"保存失败：{exc}")
            return
        self._cleanup()

    def exit_without_save(self):
        self._cleanup()
        self._show_message("杆路轨迹自动连线已退出：未保存当前待保存连接。")

    def _cleanup(self):
        try: self._preview_band.reset(QgsWkbTypes.LineGeometry); self._preview_band.hide(); self._preview_band.deleteLater()
        except Exception: pass
        if self._panel: self._panel.deleteLater(); self._panel=None
        try: self.canvas.unsetMapTool(self)
        except Exception: pass

    def deactivate(self):
        try: self._preview_band.hide()
        except Exception: pass
        if self._panel: self._panel.hide()
        try: super().deactivate()
        except Exception: pass
