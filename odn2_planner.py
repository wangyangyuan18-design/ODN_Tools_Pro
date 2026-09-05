# -*- coding: utf-8 -*-
"""ODN 2.0 Stage-1 global FAT / Chain planner.

The planner deliberately stops before FDT assignment. It first classifies the
whole FAT population against the physical Pole Edge network, then partitions
all reachable FATs into Chains. A Chain is later intended to become one FDT
Link, but no FDT is chosen in this stage.
"""
from collections import defaultdict, deque
from math import inf, sqrt
import heapq

from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QDoubleValidator
from qgis.core import (QgsCoordinateTransform, QgsDistanceArea, QgsFeature,
    QgsGeometry, QgsMapLayerType, QgsPointXY, QgsProject, QgsSpatialIndex,
    QgsWkbTypes, QgsRectangle)
from qgis.gui import QgsRubberBand, QgsVertexMarker


class Odn2PlannerDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("ODN 网络规划 · ODN 2.0")
        self.resize(600, 700)
        self._build_ui()
        self._load_layers()

    def _section(self, layout, title, builder, expanded=True):
        b = QtWidgets.QToolButton()
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        b.setText(title); b.setCheckable(True); b.setChecked(expanded); b.setAutoRaise(True)
        f = b.font(); f.setBold(True); b.setFont(f)
        layout.addWidget(b)
        w = QtWidgets.QWidget(); wl = QtWidgets.QVBoxLayout(w); wl.setContentsMargins(18,2,4,6)
        builder(wl); w.setVisible(expanded); layout.addWidget(w)
        def toggle(v):
            b.setArrowType(Qt.DownArrow if v else Qt.RightArrow); w.setVisible(v)
        b.toggled.connect(toggle)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("ODN 网络规划 · ODN 2.0")
        f = title.font(); f.setPointSize(13); f.setBold(True); title.setFont(f); root.addWidget(title)

        def standard(il):
            row = QtWidgets.QHBoxLayout()
            self.std2 = QtWidgets.QRadioButton("ODN 2.0"); self.std2.setChecked(True)
            row.addWidget(self.std2); row.addWidget(QtWidgets.QRadioButton("ODN 1.0（预留）")); row.addWidget(QtWidgets.QRadioButton("ODN 3.0（预留）")); row.addStretch()
            il.addLayout(row)

        def nodes(il):
            row = QtWidgets.QHBoxLayout()
            self.fat_cb = QtWidgets.QCheckBox("FAT"); self.fat_cb.setChecked(True)
            self.bb_cb = QtWidgets.QCheckBox("BB"); self.bb_cb.setChecked(True)
            self.sfc_cb = QtWidgets.QCheckBox("SFC CL"); self.sfc_cb.setChecked(True)
            row.addWidget(self.fat_cb); row.addWidget(self.bb_cb); row.addWidget(self.sfc_cb); row.addStretch(); il.addLayout(row)

        def params(il):
            g = QtWidgets.QGridLayout(); g.setColumnStretch(1,1)
            g.addWidget(QtWidgets.QLabel("每条 Link 最大 FAT："),0,0)
            self.max_fats = QtWidgets.QSpinBox(); self.max_fats.setRange(1,1000); self.max_fats.setValue(4); g.addWidget(self.max_fats,0,1)
            g.addWidget(QtWidgets.QLabel("相邻 FAT 最大实际路径："),1,0)
            self.max_seg = QtWidgets.QLineEdit("455"); self.max_seg.setValidator(QDoubleValidator(1,1000000,2,self)); g.addWidget(self.max_seg,1,1)
            g.addWidget(QtWidgets.QLabel("FAT 挂杆最大距离："),2,0)
            self.attach = QtWidgets.QLineEdit("3"); self.attach.setValidator(QDoubleValidator(.01,1000,2,self)); g.addWidget(self.attach,2,1)
            g.addWidget(QtWidgets.QLabel("Pole → Pole Edge 容差："),3,0)
            self.pole_tol = QtWidgets.QLineEdit("0.25"); self.pole_tol.setValidator(QDoubleValidator(.001,100,3,self)); g.addWidget(self.pole_tol,3,1)
            g.addWidget(QtWidgets.QLabel("回缆阈值："),4,0)
            self.return_threshold = QtWidgets.QLineEdit("100"); self.return_threshold.setValidator(QDoubleValidator(0,1000000,2,self)); g.addWidget(self.return_threshold,4,1)
            il.addLayout(g)
            note=QtWidgets.QLabel("默认：4 FAT / Link、455 m、回缆 100 m。当前阶段只生成 Chain，不分配 FDT。")
            note.setStyleSheet("color:#666;"); il.addWidget(note)

        def data(il):
            row=QtWidgets.QHBoxLayout()
            a=QtWidgets.QVBoxLayout(); a.addWidget(QtWidgets.QLabel("FAT 点图层（可多选）"))
            self.fat_list=QtWidgets.QListWidget(); self.fat_list.setMinimumHeight(115); a.addWidget(self.fat_list)
            b=QtWidgets.QVBoxLayout(); b.addWidget(QtWidgets.QLabel("Pole 点图层（可多选）"))
            self.pole_list=QtWidgets.QListWidget(); self.pole_list.setMinimumHeight(115); b.addWidget(self.pole_list)
            row.addLayout(a,1); row.addLayout(b,1); il.addLayout(row)
            er=QtWidgets.QHBoxLayout(); er.addWidget(QtWidgets.QLabel("Pole Edge：")); self.edge_combo=QtWidgets.QComboBox(); er.addWidget(self.edge_combo,1); il.addLayout(er)

        self._section(root,"设计标准",standard,True)
        self._section(root,"接入节点类型",nodes,False)
        self._section(root,"ODN 2.0 参数",params,True)
        self._section(root,"输入数据",data,True)
        info=QtWidgets.QLabel("第一步：扫描整个 Pole Edge 网络，排除不在网 FAT；再把所有在网 FAT 按参数组成 Chain。第二步 FDT 规划暂不执行。")
        info.setWordWrap(True); info.setStyleSheet("color:#555; padding:4px 0;"); root.addWidget(info)
        box=QtWidgets.QDialogButtonBox(); go=box.addButton("① 分析全网 FAT / Chain",QtWidgets.QDialogButtonBox.AcceptRole); cancel=box.addButton("取消",QtWidgets.QDialogButtonBox.RejectRole)
        go.clicked.connect(self._start); cancel.clicked.connect(self.reject); root.addWidget(box)

    def _load_layers(self):
        self.fat_list.clear(); self.pole_list.clear(); self.edge_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type()!=QgsMapLayerType.VectorLayer: continue
            gt=QgsWkbTypes.geometryType(layer.wkbType())
            if gt==QgsWkbTypes.PointGeometry:
                for widget in (self.fat_list,self.pole_list):
                    it=QtWidgets.QListWidgetItem(layer.name()); it.setData(Qt.UserRole,layer.id()); it.setFlags(it.flags()|Qt.ItemIsUserCheckable); it.setCheckState(Qt.Unchecked); widget.addItem(it)
            elif gt==QgsWkbTypes.LineGeometry: self.edge_combo.addItem(layer.name(),layer.id())

    @staticmethod
    def _checked(widget):
        return [widget.item(i).data(Qt.UserRole) for i in range(widget.count()) if widget.item(i).checkState()==Qt.Checked]

    def _start(self):
        if not self.std2.isChecked():
            QtWidgets.QMessageBox.information(self,"ODN 网络规划","当前实际规划引擎为 ODN 2.0。"); return
        fats=self._checked(self.fat_list); poles=self._checked(self.pole_list); edge=self.edge_combo.currentData()
        if not fats or not poles or not edge:
            QtWidgets.QMessageBox.warning(self,"ODN 网络规划","请至少选择 FAT、Pole 和 Pole Edge。"); return
        try:
            vals=(self.max_fats.value(),float(self.max_seg.text() or 455),float(self.attach.text() or 3),float(self.pole_tol.text() or .25),float(self.return_threshold.text() or 100))
        except ValueError:
            QtWidgets.QMessageBox.warning(self,"ODN 网络规划","参数必须是有效数字。"); return
        self.accept(); Odn2Planner(self.iface,fats,poles,edge,*vals,self.bb_cb.isChecked(),self.sfc_cb.isChecked()).run()


class Odn2Planner:
    def __init__(self,iface,fat_layer_ids,pole_layer_ids,edge_layer_id,max_fats,max_segment,fat_attach,pole_tol,return_threshold,allow_bb,allow_sfc):
        self.iface=iface; self.canvas=iface.mapCanvas(); self.project=QgsProject.instance()
        self.fat_layer_ids=list(fat_layer_ids); self.pole_layer_ids=list(pole_layer_ids); self.edge_layer_id=edge_layer_id
        self.max_fats=int(max_fats); self.max_segment=float(max_segment); self.fat_attach=float(fat_attach); self.pole_tol=float(pole_tol); self.return_threshold=float(return_threshold); self.allow_bb=bool(allow_bb); self.allow_sfc=bool(allow_sfc)
        self.dst_crs=None; self.da=QgsDistanceArea(); self.base_edges=[]; self.graph=defaultdict(list); self.graph_points={}; self.graph_edge_length={}
        self.poles=[]; self.pole_points={}; self.pole_index=QgsSpatialIndex(); self.network_poles={}; self.pole_keys={}
        self.fats=[]; self.in_network_fats=[]; self.excluded_fats=[]; self.route_cache={}; self.pair_cache={}; self.chain_plan=[]; self.preview_bands=[]; self.preview_markers=[]

    @property
    def edge_layer(self): return self.project.mapLayer(self.edge_layer_id)
    @staticmethod
    def _node_key(p): return (round(float(p.x()),8),round(float(p.y()),8))
    @staticmethod
    def _edge_key(a,b): return tuple(sorted((a,b)))
    @staticmethod
    def _index_feature(fid,p):
        f=QgsFeature(); f.setId(int(fid)); f.setGeometry(QgsGeometry.fromPointXY(p)); return f

    def run(self):
        if self.edge_layer is None: self._msg("Pole Edge 图层不存在。",2); return
        self._load_geometry()
        if not self.base_edges or not self.fats: self._msg("Pole Edge 或 FAT 数据不足，无法规划。",2); return
        self._phase1()
        if not self.in_network_fats: self._msg("没有 FAT 位于可达 Pole Edge 网络上。",1); return
        self._show_chain_results()

    def _load_geometry(self):
        self.dst_crs=self.edge_layer.crs(); self.da=QgsDistanceArea(); self.da.setSourceCrs(self.dst_crs,self.project.transformContext())
        self.base_edges=[]; self.graph=defaultdict(list); self.graph_points={}; self.graph_edge_length={}; self.poles=[]; self.pole_points={}; self.network_poles={}; self.pole_keys={}; self.pole_index=QgsSpatialIndex(); self.fats=[]; self.route_cache={}; self.pair_cache={}
        for feat in self.edge_layer.getFeatures():
            g=feat.geometry()
            if g.isEmpty(): continue
            try: parts=g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            except Exception: continue
            for pts in parts:
                for a0,b0 in zip(pts[:-1],pts[1:]):
                    a=QgsPointXY(a0); b=QgsPointXY(b0); w=self.da.measureLine(a,b)
                    if w>0: self.base_edges.append((a,b,w,int(feat.id())))
        sid=1
        for lid in self.pole_layer_ids:
            layer=self.project.mapLayer(lid)
            if layer is None: continue
            tr=QgsCoordinateTransform(layer.crs(),self.dst_crs,self.project) if layer.crs()!=self.dst_crs else None
            for feat in layer.getFeatures():
                if feat.geometry().isEmpty(): continue
                p=QgsPointXY(feat.geometry().centroid().asPoint())
                if tr: p=QgsPointXY(tr.transform(p))
                self.poles.append((lid,int(feat.id()),p,sid)); self.pole_points[sid]=p; self.pole_index.addFeature(self._index_feature(sid,p)); sid+=1
        attachments={}
        for _,_,p,psid in self.poles:
            att=self._nearest_edge(p)
            if att and att[0]<=self.pole_tol+1e-9:
                attachments[psid]=att; self.network_poles[psid]=p
        self._build_graph(attachments)
        for lid in self.fat_layer_ids:
            layer=self.project.mapLayer(lid)
            if layer is None: continue
            tr=QgsCoordinateTransform(layer.crs(),self.dst_crs,self.project) if layer.crs()!=self.dst_crs else None
            for feat in layer.getFeatures():
                if feat.geometry().isEmpty(): continue
                p=QgsPointXY(feat.geometry().centroid().asPoint())
                if tr: p=QgsPointXY(tr.transform(p))
                names=feat.fields().names()
                try: label=str(feat["Name"]) if "Name" in names and feat["Name"] not in (None,"") else str(feat.id())
                except Exception: label=str(feat.id())
                obj={"rid":(lid,int(feat.id())),"label":label,"point":p,"pole_sid":None,"pole_key":None,"attach_distance":inf,"network":False,"network_reason":""}
                pole=self._nearest_network_pole(p)
                if pole is None:
                    anyp=self._nearest_any_pole(p)
                    obj["network_reason"]="未找到 FAT 挂接 Pole" if anyp is None else "最近 Pole 不在 Pole Edge 网络中"
                else:
                    psid,_,d=pole; obj["pole_sid"]=psid; obj["attach_distance"]=d; obj["pole_key"]=self.pole_keys.get(psid)
                    if obj["pole_key"] is None: obj["network_reason"]="Pole 在网但未成功建立图节点"
                    else: obj["network"]=True
                self.fats.append(obj)

    def _nearest_edge(self,p):
        best=None
        for a,b,w,fid in self.base_edges:
            try:
                r=QgsGeometry.fromPolylineXY([a,b]).closestSegmentWithContext(p); cp=QgsPointXY(r[1]); d=sqrt(max(0.0,float(r[0])))
            except Exception: continue
            if best is None or d<best[0]:
                t=self.da.measureLine(a,cp)/w if w else 0; best=(d,a,b,w,fid,cp,max(0,min(1,t)))
        return best

    def _build_graph(self,attachments):
        groups=defaultdict(list)
        for a,b,w,fid in self.base_edges:
            ak=self._node_key(a); bk=self._node_key(b); groups[(ak,bk,fid)]=[(0,ak,a),(1,bk,b)]
        for psid,att in attachments.items():
            _,a,b,w,fid,cp,t=att; groups[(self._node_key(a),self._node_key(b),fid)].append((t,self._node_key(cp),cp))
        for key,pts in groups.items():
            uniq={k:(t,p) for t,k,p in pts}; ordered=sorted(((t,k,p) for k,(t,p) in uniq.items()),key=lambda x:x[0])
            for _,k,p in ordered: self.graph_points[k]=p
            for (_,ka,pa),(_,kb,pb) in zip(ordered[:-1],ordered[1:]):
                w=self.da.measureLine(pa,pb)
                if w<=0: continue
                self.graph[ka].append((kb,w)); self.graph[kb].append((ka,w)); ek=self._edge_key(ka,kb); self.graph_edge_length[ek]=self.graph_edge_length.get(ek,0)+w
        for psid,att in attachments.items(): self.pole_keys[psid]=self._node_key(att[5])

    def _nearest_network_pole(self,p):
        best=None; bd=inf
        for sid,pp in self.network_poles.items():
            d=self.da.measureLine(p,pp)
            if d<=self.fat_attach and d<bd: best=(sid,pp,d); bd=d
        return best
    def _nearest_any_pole(self,p):
        best=None; bd=inf
        for sid,pp in self.pole_points.items():
            d=self.da.measureLine(p,pp)
            if d<bd: best=(sid,pp,d); bd=d
        return best if best and bd<=self.fat_attach else None
    def _component(self,start):
        seen={start}; q=deque([start])
        while q:
            u=q.popleft()
            for v,_ in self.graph.get(u,[]):
                if v not in seen: seen.add(v); q.append(v)
        return min(seen) if seen else None

    def _phase1(self):
        self.in_network_fats=[f for f in self.fats if f["network"]]
        self.excluded_fats=[f for f in self.fats if not f["network"]]
        for f in self.in_network_fats: f["component"]=self._component(f["pole_key"])
        self._build_pair_routes(); self._build_chains()
        attached=sum(f["attach_distance"]<inf for f in self.fats)
        self._msg(f"第1步·全网扫描：FAT {len(self.fats)}；FAT→Pole 成功 {attached}；Pole Edge 网络内 FAT {len(self.in_network_fats)}；网络外/不可达 {len(self.excluded_fats)}。",0,8)
        rs=defaultdict(int)
        for f in self.excluded_fats: rs[f["network_reason"] or "未连接 Pole Edge"]+=1
        if rs: self._msg("排除原因："+"；".join(f"{k} {v}" for k,v in rs.items()),1,8)
        covered=sum(len(c["fats"]) for c in self.chain_plan)
        self._msg(f"第1步·Chain：形成 {len(self.chain_plan)} 条；覆盖在网 FAT {covered}/{len(self.in_network_fats)}。",0,8)

    def _build_pair_routes(self):
        self.pair_cache={}
        n=len(self.in_network_fats)
        for i,a in enumerate(self.in_network_fats):
            for j in range(i+1,n):
                b=self.in_network_fats[j]
                if a["component"]!=b["component"]: continue
                info=self._pair_route(a,b)
                if info is not None: self.pair_cache[(i,j)]=info

    def _pair_route(self,a,b):
        d,path=self._shortest(a["pole_key"],b["pole_key"])
        if d==inf: return None
        sfc=False
        if d>self.max_segment+1e-6:
            if self.allow_sfc and d<=2*self.max_segment+1e-6: sfc=True
            else: return None
        return {"distance":d,"path":path,"edges":self._path_edges(path),"sfc":sfc}
    def _pair(self,i,j): return self.pair_cache.get((min(i,j),max(i,j)))

    def _build_chains(self):
        assigned=set(); chains=[]; n=len(self.in_network_fats)
        while len(assigned)<n:
            rem=[i for i in range(n) if i not in assigned]
            start=min(rem,key=lambda i:(self._degree(i,assigned),self.in_network_fats[i]["label"]))
            seq=[start]; segs=[]; assigned.add(start); cur=start
            while len(seq)<self.max_fats:
                cand=[]
                for j in rem:
                    if j in assigned or j==cur: continue
                    info=self._pair(cur,j)
                    if info: cand.append((info["distance"],self._degree(j,assigned),self.in_network_fats[j]["label"],j,info))
                if not cand: break
                _,_,_,j,info=min(cand); seq.append(j); segs.append(info); assigned.add(j); cur=j
            fats=[self.in_network_fats[i] for i in seq]; cid=len(chains)+1
            for f in fats: f["chain_id"]=cid
            chains.append(self._make_chain(cid,fats,segs))
        self.chain_plan=chains

    def _degree(self,i,assigned):
        return sum(1 for j in range(len(self.in_network_fats)) if j!=i and j not in assigned and self._pair(i,j) is not None)
    def _make_chain(self,cid,fats,segs):
        total=sum(x["distance"] for x in segs); ret=self._return_length(segs); sfc=sum(x["sfc"] for x in segs); bb=ret>self.return_threshold+1e-6
        return {"id":cid,"fats":fats,"segments":segs,"length":total,"return":ret,"sfc":sfc,"bb":bb}

    def _shortest(self,start,goal):
        key=(start,goal)
        if key in self.route_cache: return self.route_cache[key]
        if start==goal: return (0.0,[start])
        dist={start:0.0}; prev={}; heap=[(0.0,start)]
        while heap:
            d,u=heapq.heappop(heap)
            if d!=dist.get(u): continue
            if u==goal: break
            for v,w in self.graph.get(u,[]):
                nd=d+w
                if nd<dist.get(v,inf): dist[v]=nd; prev[v]=u; heapq.heappush(heap,(nd,v))
        if goal not in dist: out=(inf,[])
        else:
            path=[goal]; cur=goal
            while cur!=start: cur=prev[cur]; path.append(cur)
            path.reverse(); out=(dist[goal],path)
        self.route_cache[key]=out; return out
    def _path_edges(self,path): return [self._edge_key(a,b) for a,b in zip(path[:-1],path[1:])]
    def _return_length(self,infos):
        c=defaultdict(int)
        for x in infos:
            for e in x["edges"]: c[e]+=1
        return sum(self.graph_edge_length.get(e,0)*(v-1) for e,v in c.items() if v>1)

    def _show_chain_results(self):
        dlg=QtWidgets.QDialog(self.iface.mainWindow()); dlg.setWindowTitle("ODN 2.0 · 全网 FAT / Chain 分析"); dlg.resize(1000,650)
        root=QtWidgets.QVBoxLayout(dlg)
        root.addWidget(QtWidgets.QLabel(f"在网 FAT：{len(self.in_network_fats)} / {len(self.fats)}    Chain：{len(self.chain_plan)}    每条 Link 最大 FAT：{self.max_fats}    最大相邻路径：{self.max_segment:g} m"))
        sp=QtWidgets.QSplitter(Qt.Horizontal); root.addWidget(sp,1); lst=QtWidgets.QListWidget(); sp.addWidget(lst)
        right=QtWidgets.QWidget(); rv=QtWidgets.QVBoxLayout(right); sp.addWidget(right); detail=QtWidgets.QPlainTextEdit(); detail.setReadOnly(True); rv.addWidget(detail,1)
        br=QtWidgets.QHBoxLayout(); locate=QtWidgets.QPushButton("定位该 Chain"); select=QtWidgets.QPushButton("选择 / 高亮该 Chain"); clear=QtWidgets.QPushButton("清除高亮")
        br.addWidget(locate); br.addWidget(select); br.addWidget(clear); br.addStretch(); rv.addLayout(br)
        for ch in self.chain_plan:
            labels=" → ".join(f["label"] for f in ch["fats"]); lst.addItem(f"L{ch['id']}   ({len(ch['fats'])} FAT)   {ch['length']:.1f}m   {labels}")
        def cur():
            i=lst.currentRow(); return self.chain_plan[i] if 0<=i<len(self.chain_plan) else None
        def show():
            ch=cur()
            if not ch: detail.clear(); return
            lines=[f"L{ch['id']}",f"FAT 数量：{len(ch['fats'])}","","FAT Chain："]
            for i,f in enumerate(ch["fats"]):
                if i==0: lines.append(f"  {f['label']}")
                else:
                    s=ch["segments"][i-1]; lines.append(f"  → {f['label']}   {s['distance']:.1f} m"+("  [SFC CL]" if s["sfc"] else ""))
            lines += ["",f"Chain 路径长度：{ch['length']:.1f} m",f"实际回缆长度：{ch['return']:.1f} m",f"SFC CL：{ch['sfc']}",f"BB：{1 if ch['bb'] else 0}"]
            detail.setPlainText("\n".join(lines))
        def locate_chain():
            ch=cur()
            if not ch: return
            self._clear_preview(); self._draw_chain(ch,False); pts=[f["point"] for f in ch["fats"]]
            if pts:
                r=QgsRectangle(pts[0],pts[0])
                for p in pts[1:]: r.combineExtentWith(p.x(),p.y())
                r.scale(1.35); self.canvas.setExtent(r); self.canvas.refresh()
        def select_chain():
            ch=cur()
            if not ch: return
            self._clear_preview(); self._draw_chain(ch,True); self._select_chain(ch); self.canvas.refresh()
        lst.currentRowChanged.connect(lambda _:show()); locate.clicked.connect(locate_chain); select.clicked.connect(select_chain); clear.clicked.connect(self._clear_preview); dlg.finished.connect(lambda _:self._clear_preview())
        if self.chain_plan: lst.setCurrentRow(0)
        dlg.show(); self._result_dialog=dlg

    def _draw_chain(self,ch,markers):
        for s in ch["segments"]: self._draw_path(s["path"],6)
        if markers:
            for f in ch["fats"]:
                m=QgsVertexMarker(self.canvas); m.setCenter(f["point"]); m.setColor(Qt.yellow); m.setIconType(QgsVertexMarker.ICON_RHOMBUS); m.setIconSize(16); m.setPenWidth(3); m.show(); self.preview_markers.append(m)
    def _draw_path(self,path,width=6):
        if len(path)<2:return
        rb=QgsRubberBand(self.canvas,QgsWkbTypes.LineGeometry); rb.setColor(Qt.red); rb.setWidth(width); rb.setToGeometry(QgsGeometry.fromPolylineXY([self.graph_points[k] for k in path]),None); rb.show(); self.preview_bands.append(rb)
    def _select_chain(self,ch):
        by=defaultdict(list)
        for f in ch["fats"]: by[f["rid"][0]].append(f["rid"][1])
        for lid in self.fat_layer_ids:
            layer=self.project.mapLayer(lid)
            if layer: layer.removeSelection()
        for lid,ids in by.items():
            layer=self.project.mapLayer(lid)
            if layer: layer.selectByIds(ids)
    def _clear_preview(self):
        for rb in self.preview_bands:
            try:self.canvas.scene().removeItem(rb)
            except Exception:pass
        for m in self.preview_markers:
            try:self.canvas.scene().removeItem(m)
            except Exception:pass
        self.preview_bands.clear(); self.preview_markers.clear(); self.canvas.refresh()
    def _msg(self,text,level=0,duration=5):
        try:self.iface.messageBar().pushMessage("ODN Tools Pro",text,level=level,duration=duration)
        except Exception:pass
