# -*- coding: utf-8 -*-
"""Scheme 3 - manual ODN link planner.

The module deliberately keeps design intent separate from formal Cable data.
Users choose FDT/Link/FAT order on the QGIS canvas. Each committed segment is
routed over the physical Pole Edge graph and shown as a temporary Actual Route.
No formal Cable feature is created until the user explicitly requests it.
"""

from collections import defaultdict
from heapq import heappush, heappop
from math import inf, sqrt
import json

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QPoint
from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (
    QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsGeometry,
    QgsMapLayerType, QgsPointXY, QgsProject, QgsRectangle, QgsSpatialIndex,
    QgsWkbTypes,
)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker


SETTINGS_KEY = 'ODNToolsPro/Scheme3'
TEMP_DESIGN_LAYER = 'ODN_S3_Temp_Design'
TEMP_ROUTE_LAYER = 'ODN_S3_Temp_Route'


def _name(layer, feat):
    names = feat.fields().names()
    for fld in ('Name', 'NAME', 'name'):
        if fld in names and feat[fld] not in (None, ''):
            return str(feat[fld])
    return str(feat.id())


def _node_key(p):
    return (round(float(p.x()), 8), round(float(p.y()), 8))


def _edge_key(a, b):
    return tuple(sorted((a, b)))


class Scheme3Dialog(QtWidgets.QDialog):
    """Scheme 3 UI and temporary design session."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle('方案3：人工链路规划')
        self.resize(920, 760)
        self.setModal(False)
        self._designs = []
        self._current = []
        self._current_fdt = None
        self._current_link = None
        self._tool = None
        self._engine = None
        self._build_ui()
        self._load_layer_candidates()
        self._load_saved_designs()

    def _section(self, parent, title, builder, expanded=True):
        b = QtWidgets.QToolButton()
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        b.setText(title)
        b.setCheckable(True); b.setChecked(expanded); b.setAutoRaise(True)
        f = b.font(); f.setBold(True); b.setFont(f)
        parent.addWidget(b)
        w = QtWidgets.QWidget(); lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(18, 2, 4, 6); builder(lay)
        w.setVisible(expanded); parent.addWidget(w)
        b.toggled.connect(lambda v: (b.setArrowType(Qt.DownArrow if v else Qt.RightArrow), w.setVisible(v)))

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self); root.setContentsMargins(10, 8, 10, 8)
        title = QtWidgets.QLabel('方案3：人工链路规划')
        f = QFont(); f.setPointSize(13); f.setBold(True); title.setFont(f); root.addWidget(title)

        def standard(lay):
            row = QtWidgets.QHBoxLayout()
            self.std2 = QtWidgets.QComboBox(); self.std2.addItems(['ODN 2.0', 'ODN 1.0（预留）', 'ODN 3.0（预留）'])
            row.addWidget(self.std2); row.addStretch(); lay.addLayout(row)

        def nodes(lay):
            row = QtWidgets.QHBoxLayout()
            self.fat_cb = QtWidgets.QCheckBox('FAT'); self.fat_cb.setChecked(True)
            self.bb_cb = QtWidgets.QCheckBox('BB'); self.bb_cb.setChecked(True)
            self.sfc_cb = QtWidgets.QCheckBox('SFC CL'); self.sfc_cb.setChecked(True)
            row.addWidget(self.fat_cb); row.addWidget(self.bb_cb); row.addWidget(self.sfc_cb); row.addStretch(); lay.addLayout(row)

        def params(lay):
            g = QtWidgets.QGridLayout(); g.setColumnStretch(1, 1)
            self.max_fats = QtWidgets.QSpinBox(); self.max_fats.setRange(1, 1000); self.max_fats.setValue(4)
            self.max_seg = QtWidgets.QLineEdit('455')
            self.attach = QtWidgets.QLineEdit('3')
            self.pole_tol = QtWidgets.QLineEdit('0.25')
            self.return_threshold = QtWidgets.QLineEdit('100')
            vals = [('每条 Link 最大 FAT：', self.max_fats), ('相邻 FAT 最大实际路径：', self.max_seg),
                    ('FAT 挂杆最大距离：', self.attach), ('Pole → Pole Edge 容差：', self.pole_tol),
                    ('回缆阈值：', self.return_threshold)]
            for r, (lab, widget) in enumerate(vals):
                g.addWidget(QtWidgets.QLabel(lab), r, 0); g.addWidget(widget, r, 1)
            lay.addLayout(g)
            note = QtWidgets.QLabel('固定规则：每个预连接段最多 1 个 SFC CL。参数仅用于检查，不会自动改写人工指定的 FAT 顺序。')
            note.setWordWrap(True); note.setStyleSheet('color:#666;'); lay.addWidget(note)

        def links(lay):
            g = QtWidgets.QGridLayout(); g.setColumnStretch(1, 1)
            self.fdt_combo = QtWidgets.QComboBox(); self.link_combo = QtWidgets.QComboBox()
            g.addWidget(QtWidgets.QLabel('FDT'), 0, 0); g.addWidget(self.fdt_combo, 0, 1)
            g.addWidget(QtWidgets.QLabel('Link'), 1, 0); g.addWidget(self.link_combo, 1, 1)
            self.start_btn = QtWidgets.QPushButton('＋开始画链路'); g.addWidget(self.start_btn, 2, 0, 1, 2)
            lay.addLayout(g)
            self.design_tree = QtWidgets.QTreeWidget(); self.design_tree.setHeaderLabels(['FDT / Link', 'FAT 数量'])
            self.design_tree.setMinimumHeight(145); lay.addWidget(self.design_tree)
            self.start_btn.clicked.connect(self.start_link)
            self.fdt_combo.currentIndexChanged.connect(self._selection_changed)
            self.link_combo.currentIndexChanged.connect(self._selection_changed)

        def current(lay):
            self.current_label = QtWidgets.QLabel('FDT01 / L1')
            self.current_path_label = QtWidgets.QLabel('等待开始')
            self.segment_label = QtWidgets.QLabel('当前段：—')
            self.route_label = QtWidgets.QLabel('Pole Edge：—')
            self.route_label.setWordWrap(True)
            lay.addWidget(self.current_label); lay.addWidget(self.current_path_label)
            lay.addWidget(self.segment_label); lay.addWidget(self.route_label)
            row = QtWidgets.QHBoxLayout()
            self.finish_btn = QtWidgets.QPushButton('完成Link')
            self.undo_btn = QtWidgets.QPushButton('撤销上一点')
            self.finish_btn.setEnabled(False); self.undo_btn.setEnabled(False)
            row.addWidget(self.finish_btn); row.addWidget(self.undo_btn); lay.addLayout(row)
            self.finish_btn.clicked.connect(self.finish_link); self.undo_btn.clicked.connect(self.undo_last)

        self._section(root, '① 设计标准', standard, True)
        self._section(root, '② 接入节点类型', nodes, True)
        self._section(root, '③ ODN 2.0 参数', params, False)
        self._section(root, '④ 链路设计', links, True)
        self._section(root, '⑤ 当前设计', current, True)

        self.status = QtWidgets.QLabel('状态：等待开始设计')
        self.status.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken)
        root.addWidget(self.status)
        buttons = QtWidgets.QHBoxLayout()
        for text, slot in [('保存设计', self.save_design), ('检查全部设计', self.check_all),
                           ('预览全部路由', self.preview_all), ('生成正式Cable', self.generate_cables), ('关闭', self.close)]:
            b = QtWidgets.QPushButton(text); buttons.addWidget(b); setattr(self, 'btn_' + text.replace(' ', '_'), b)
            b.clicked.connect(slot)
        root.addLayout(buttons)

    def _load_layer_candidates(self):
        self.fdt_combo.clear()
        self.link_combo.clear(); self.link_combo.addItems(['L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7', 'L8'])
        project = QgsProject.instance()
        for layer in project.mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer: continue
            if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.PointGeometry: continue
            n = layer.name().lower()
            if 'fdt' in n:
                self.fdt_combo.addItem(layer.name(), layer.id())
        if self.fdt_combo.count() == 0:
            for layer in project.mapLayers().values():
                if layer.type() == QgsMapLayerType.VectorLayer and QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.PointGeometry:
                    self.fdt_combo.addItem(layer.name(), layer.id())
        self._refresh_tree()

    def _layers(self):
        project = QgsProject.instance(); fdt = None; fats = []; poles = []; edge = None
        for layer in project.mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer: continue
            n = layer.name().lower()
            gt = QgsWkbTypes.geometryType(layer.wkbType())
            if gt == QgsWkbTypes.PointGeometry:
                if 'fdt' in n and fdt is None: fdt = layer
                if 'fat' in n: fats.append(layer)
                if 'pole' in n or '杆' in layer.name() or 'new_pole' in n: poles.append(layer)
            elif gt == QgsWkbTypes.LineGeometry and ('edge' in n or 'pole' in n or '杆' in layer.name()):
                if edge is None: edge = layer
        return fdt, fats, poles, edge

    def _selection_changed(self):
        if self._current: return
        fdt = self.fdt_combo.currentText() or 'FDT—'; link = self.link_combo.currentText() or 'L1'
        self.current_label.setText(f'{fdt} / {link}')

    def start_link(self):
        if self.std2.currentIndex() != 0:
            QtWidgets.QMessageBox.information(self, '方案3', '当前仅实现 ODN 2.0。'); return
        if not self.fdt_combo.currentData():
            QtWidgets.QMessageBox.warning(self, '方案3', '未找到 FDT 图层，请检查图层名称。'); return
        fdt_layer, fats, poles, edge = self._layers()
        if not fats or edge is None:
            QtWidgets.QMessageBox.warning(self, '方案3', '未自动找到 FAT 图层或 Pole Edge 图层。图层名称建议包含 FAT / Pole Edge。'); return
        self._engine = Scheme3Engine(self.iface, fdt_layer, fats, poles, edge, self._params())
        self._tool = Scheme3MapTool(self.iface, self._engine, self)
        self.iface.mapCanvas().setMapTool(self._tool)
        self._tool.start()
        self._current = []
        self._current_fdt = self.fdt_combo.currentText()
        self._current_link = self.link_combo.currentText()
        self.finish_btn.setEnabled(True); self.undo_btn.setEnabled(True)
        self._update_current()

    def _params(self):
        def num(w, default):
            try: return float(w.text())
            except Exception: return default
        return dict(max_fats=self.max_fats.value(), max_seg=num(self.max_seg,455), attach=num(self.attach,3),
                    pole_tol=num(self.pole_tol,.25), return_threshold=num(self.return_threshold,100),
                    allow_bb=self.bb_cb.isChecked(), allow_sfc=self.sfc_cb.isChecked())

    def add_node(self, typ, rid, label):
        if not self._current and typ != 'FDT': return
        if self._current and typ == 'FDT': return
        if typ != 'FAT': return
        if rid in [x[0] for x in self._current]: return
        if len(self._current) >= self.max_fats.value() and self.max_fats.value() > 0:
            self.status.setText('状态：已达到当前 Link 的 FAT 参数上限'); return
        self._current.append((rid, label))
        self._update_current()
        self._tool.refresh_route_preview()

    def set_fdt(self, rid, label):
        if self._current: return
        self._current_fdt = label
        self.fdt_combo.setCurrentText(label)
        self._update_current()

    def _update_current(self):
        path = [self._current_fdt] + [x[1] for x in self._current] if self._current_fdt else [x[1] for x in self._current]
        self.current_path_label.setText('     ↓\n'.join(path) if path else '等待开始')
        self.current_label.setText(f'{self._current_fdt or "FDT—"} / {self._current_link or self.link_combo.currentText()}')
        if len(self._current) >= 1 and self._engine:
            prev = self._current[-1][0] if len(self._current) == 1 else self._current[-2][0]
            cur = self._current[-1][0]
            info = self._engine.segment_info(self._current_fdt if len(self._current)==1 else self._current[-2][0], cur)
            self._set_segment_info(info)
        else:
            self._set_segment_info(None)
        self._refresh_tree()

    def _set_segment_info(self, info):
        if not info:
            self.segment_label.setText('当前段：—'); self.route_label.setText('Pole Edge：—'); return
        self.segment_label.setText(f"当前段：{info['from_label']}→{info['to_label']}  {info['distance']:.1f}m {'⚠ >455m' if info['distance'] > self._params()['max_seg'] else '✓'}")
        self.route_label.setText(f"Pole Edge：{info['distance']:.1f}m  |  {len(info['edge_sequence'])} 个边段")

    def finish_link(self):
        if not self._current: return
        self._designs.append(dict(fdt=self._current_fdt, link=self._current_link, nodes=list(self._current)))
        self._current = []
        self.finish_btn.setEnabled(False); self.undo_btn.setEnabled(False)
        if self._tool: self._tool.clear_current()
        self._refresh_tree(); self._update_current()
        self.status.setText(f'状态：已完成 {self._current_fdt}/{self._current_link}，可继续选择下一个 Link 或 FDT。')

    def undo_last(self):
        if self._current:
            self._current.pop(); self._update_current()
            if self._tool: self._tool.refresh_route_preview()

    def save_design(self):
        self._persist_designs()
        self.status.setText(f'状态：设计已保存到当前 QGIS 项目临时状态，共 {len(self._designs)} 条 Link。')

    def check_all(self):
        if not self._designs:
            QtWidgets.QMessageBox.information(self, '方案3', '当前没有已完成的 Link。'); return
        engine = self._engine or self._make_engine()
        if engine is None: return
        report = engine.check_designs(self._designs)
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle('方案3 · 全部设计检查'); dlg.resize(760,520)
        lay = QtWidgets.QVBoxLayout(dlg); edit=QtWidgets.QPlainTextEdit(); edit.setReadOnly(True); edit.setPlainText('\n'.join(report)); lay.addWidget(edit)
        ok=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok); ok.accepted.connect(dlg.accept); lay.addWidget(ok); dlg.exec()

    def preview_all(self):
        engine = self._engine or self._make_engine()
        if engine is None: return
        engine.clear_route_bands()
        for d in self._designs:
            seq=[d['fdt']] + [x[1] for x in d['nodes']]
            for a,b in zip(seq[:-1],seq[1:]):
                info=engine.segment_info(a,b)
                if info: engine.draw_route(info)
        self.status.setText(f'状态：已预览全部 Actual Route，共 {len(self._designs)} 条 Link。')

    def generate_cables(self):
        if not self._designs:
            QtWidgets.QMessageBox.information(self, '方案3', '当前没有已完成的 Link。'); return
        QtWidgets.QMessageBox.information(self, '方案3', '当前第一版先生成并验证临时 Actual Route；正式 Cable 写入接口已预留，下一阶段接入你指定的正式 Cable 图层/字段规则。')

    def _make_engine(self):
        fdt_layer, fats, poles, edge = self._layers()
        if not fdt_layer or not fats or not edge:
            QtWidgets.QMessageBox.warning(self, '方案3', '无法找到 FDT、FAT 或 Pole Edge 图层。'); return None
        self._engine = Scheme3Engine(self.iface, fdt_layer, fats, poles, edge, self._params())
        return self._engine

    def _refresh_tree(self):
        self.design_tree.clear()
        groups = defaultdict(list)
        for d in self._designs: groups[d['fdt']].append(d)
        for fdt, ds in groups.items():
            p=QtWidgets.QTreeWidgetItem([fdt,'']); self.design_tree.addTopLevelItem(p)
            for d in ds:
                QtWidgets.QTreeWidgetItem(p,[d['link'],str(len(d['nodes']))])
            p.setExpanded(True)

    def _persist_designs(self):
        QgsProject.instance().writeEntry(SETTINGS_KEY, 'designs', json.dumps(self._designs, ensure_ascii=False))

    def _load_saved_designs(self):
        ok, value = QgsProject.instance().readEntry(SETTINGS_KEY, 'designs', '')
        if ok and value:
            try: self._designs = json.loads(value)
            except Exception: self._designs=[]
        self._refresh_tree()


class Scheme3Engine:
    """Pole Edge graph, shortest-path routing and design validation."""

    def __init__(self, iface, fdt_layer, fat_layers, pole_layers, edge_layer, params):
        self.iface=iface; self.fdt_layer=fdt_layer; self.fat_layers=fat_layers
        self.pole_layers=pole_layers; self.edge_layer=edge_layer; self.params=params
        self.graph=defaultdict(list); self.edge_geom={}; self.edge_len={}; self.node_points={}
        self.node_features={}; self.route_bands=[]
        self._build_graph(); self._index_points()

    def _build_graph(self):
        for feat in self.edge_layer.getFeatures():
            geom=feat.geometry()
            if geom.isEmpty(): continue
            pts=[]
            if geom.isMultipart():
                for part in geom.asMultiPolyline(): pts.extend(part)
            else: pts=geom.asPolyline()
            if len(pts)<2: continue
            for a,b in zip(pts[:-1],pts[1:]):
                ak=_node_key(a); bk=_node_key(b)
                if ak==bk: continue
                length=self._measure(QgsGeometry.fromPolylineXY([QgsPointXY(a),QgsPointXY(b)]))
                eid=(feat.id(),_edge_key(ak,bk))
                self.graph[ak].append((bk,length,eid)); self.graph[bk].append((ak,length,eid))
                self.edge_geom[eid]=QgsGeometry.fromPolylineXY([QgsPointXY(a),QgsPointXY(b)])
                self.edge_len[eid]=length
                self.node_points[ak]=QgsPointXY(a); self.node_points[bk]=QgsPointXY(b)

    def _measure(self, geom):
        da=QgsDistanceArea(); da.setSourceCrs(self.edge_layer.crs(), QgsProject.instance().transformContext())
        try: return da.measureLength(geom)
        except Exception: return geom.length()

    def _index_points(self):
        self.points={}
        layers=[('FDT',self.fdt_layer)]+[('FAT',l) for l in self.fat_layers]
        for typ,layer in layers:
            for feat in layer.getFeatures():
                if feat.geometry().isEmpty(): continue
                self.points[(layer.id(),feat.id())]=dict(typ=typ,label=_name(layer,feat),point=feat.geometry().asPoint(),layer=layer)

    def _point_for(self, label):
        for info in self.points.values():
            if info['label']==label: return info
        return None

    def _attach(self, label):
        info=self._point_for(label)
        if not info: return None
        p=QgsPointXY(info['point']); best=None
        for n,np in self.node_points.items():
            d=self._point_distance(p,np)
            if best is None or d<best[0]: best=(d,n)
        limit=self.params['attach'] if info['typ']=='FAT' else max(self.params['attach'],10.0)
        if best is None or best[0]>limit: return None
        return best

    def _point_distance(self,a,b):
        geom=QgsGeometry.fromPolylineXY([QgsPointXY(a),QgsPointXY(b)])
        return self._measure(geom)

    def shortest(self,start_label,end_label):
        sa=self._attach(start_label); sb=self._attach(end_label)
        if not sa or not sb: return None
        sn,tn=sa[1],sb[1]
        dist={sn:0.0}; prev={}; heap=[(0.0,sn)]
        while heap:
            d,u=heappop(heap)
            if d!=dist.get(u): continue
            if u==tn: break
            for v,w,eid in self.graph.get(u,[]):
                nd=d+w
                if nd<dist.get(v,inf):
                    dist[v]=nd; prev[v]=(u,eid); heappush(heap,(nd,v))
        if tn not in dist: return None
        nodes=[]; edges=[]; cur=tn
        while cur!=sn:
            pu,eid=prev[cur]; nodes.append(cur); edges.append(eid); cur=pu
        nodes.append(sn); nodes.reverse(); edges.reverse()
        total=sa[0]+dist[tn]+sb[0]
        points=[self.node_points[n] for n in nodes]
        return dict(from_label=start_label,to_label=end_label,distance=total,edge_sequence=edges,points=points)

    def segment_info(self,a,b): return self.shortest(a,b)

    def draw_route(self,info):
        rb=QgsRubberBand(self.iface.mapCanvas(),QgsWkbTypes.LineGeometry)
        rb.setWidth(4); rb.setColor(QColor(0,120,255,180))
        rb.setToGeometry(QgsGeometry.fromPolylineXY(info['points']),None); self.route_bands.append(rb)

    def clear_route_bands(self):
        for rb in self.route_bands:
            try: self.iface.mapCanvas().scene().removeItem(rb)
            except Exception: pass
        self.route_bands=[]

    def check_designs(self,designs):
        report=['方案3 · 全部设计检查','====================']
        seen=set()
        for d in designs:
            key=(d['fdt'],d['link'])
            if key in seen: report.append(f'⚠ 重复 Link：{d["fdt"]}/{d["link"]}')
            seen.add(key)
            nodes=[d['fdt']]+[x[1] for x in d['nodes']]
            report.append(f'\n{d["fdt"]}/{d["link"]}：{len(d["nodes"])} FAT')
            if len(d['nodes'])>self.params['max_fats']: report.append('  ⚠ FAT 数量超过当前参数')
            usage=defaultdict(float)
            for a,b in zip(nodes[:-1],nodes[1:]):
                info=self.segment_info(a,b)
                if not info:
                    report.append(f'  ✗ {a} → {b}：无法沿 Pole Edge 建立路径'); continue
                report.append(f'  {a} → {b}：{info["distance"]:.1f}m' + (' ⚠' if info['distance']>self.params['max_seg'] else ' ✓'))
                for eid in info['edge_sequence']: usage[eid]+=self.edge_len.get(eid,0)
            repeated=sum(max(0,v-self.edge_len.get(e,0)) for e,v in usage.items() if v>self.edge_len.get(e,0))
            if repeated>0:
                report.append(f'  Return Cable：{repeated:.1f}m')
                if repeated>self.params['return_threshold'] and self.params['allow_bb']:
                    report.append('  ⚠ Return Cable 超阈值，建议评估 BB')
            else: report.append('  Return Cable：0m ✓')
            report.append('  SFC CL：每个预连接段最多 1 个（检查规则）')
        return report


class Scheme3MapTool(QgsMapTool):
    """Canvas interaction for FDT/FAT manual selection."""

    def __init__(self,iface,engine,dialog):
        super().__init__(iface.mapCanvas()); self.iface=iface; self.engine=engine; self.dialog=dialog
        self.setCursor(Qt.CrossCursor); self._features=[]; self._index=QgsSpatialIndex(); self._hover_band=None; self._markers=[]
        for key,info in engine.points.items():
            feat=QgsFeature(); feat.setId(key[1]); feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(info['point']))); self._index.addFeature(feat); self._features.append((key,info))

    def start(self): self.refresh_route_preview()

    def _nearest(self,pos):
        pt=self.toMapCoordinates(pos); tol=self.iface.mapCanvas().mapUnitsPerPixel()*24
        rect=QgsRectangle(pt.x()-tol,pt.y()-tol,pt.x()+tol,pt.y()+tol)
        ids=self._index.intersects(rect)
        best=None
        for key,info in self._features:
            if key[1] not in ids: continue
            p=QgsPointXY(info['point']); d=sqrt((p.x()-pt.x())**2+(p.y()-pt.y())**2)
            if best is None or d<best[0]: best=(d,key,info)
        return best

    def canvasReleaseEvent(self,event):
        if event.button()!=Qt.LeftButton: return
        hit=self._nearest(event.pos())
        if not hit: return
        _,key,info=hit
        if not self.dialog._current and info['typ']=='FDT': self.dialog.set_fdt(key[1],info['label']); return
        if self.dialog._current_fdt and info['typ']=='FAT': self.dialog.add_node('FAT',key[1],info['label'])

    def canvasMoveEvent(self,event):
        if not self.dialog._current: return
        hit=self._nearest(event.pos())
        if not hit: return
        _,_,info=hit
        if info['typ']!='FAT': return
        prev=self.dialog._current[-1][1] if self.dialog._current else self.dialog._current_fdt
        route=self.engine.segment_info(prev,info['label'])
        self.dialog._set_segment_info(route)

    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Backspace: self.dialog.undo_last(); return
        if event.key()==Qt.Key_Escape: self.clear_current(); return
        super().keyPressEvent(event)

    def refresh_route_preview(self):
        if self._hover_band:
            try: self.iface.mapCanvas().scene().removeItem(self._hover_band)
            except Exception: pass
            self._hover_band=None
        if len(self.dialog._current)<1: return
        seq=[self.dialog._current_fdt]+[x[1] for x in self.dialog._current]
        pts=[]
        for a,b in zip(seq[:-1],seq[1:]):
            info=self.engine.segment_info(a,b)
            if info: pts.extend(info['points'] if not pts else info['points'][1:])
        if len(pts)>=2:
            self._hover_band=QgsRubberBand(self.iface.mapCanvas(),QgsWkbTypes.LineGeometry); self._hover_band.setWidth(3); self._hover_band.setColor(QColor(0,180,80,170)); self._hover_band.setToGeometry(QgsGeometry.fromPolylineXY(pts),None)

    def clear_current(self):
        if self._hover_band:
            try: self.iface.mapCanvas().scene().removeItem(self._hover_band)
            except Exception: pass
            self._hover_band=None
        self.dialog._current=[]; self.dialog._update_current()
