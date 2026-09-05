# -*- coding: utf-8 -*-
"""Overlength pole insertion tool for ODN Tools Pro."""
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QDoubleValidator
from qgis.core import (
    QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsField,
    QgsGeometry, QgsMapLayerType, QgsPointXY, QgsProject, QgsSpatialIndex,
    QgsVectorLayer, QgsWkbTypes
)


class OverlengthPoleDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("超距增点")
        self.setMinimumWidth(460)
        lay = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("超距增点")
        f = title.font(); f.setBold(True); f.setPointSize(12); title.setFont(f)
        lay.addWidget(title)
        lay.addWidget(QtWidgets.QLabel("杆子点图层（可多选）"))
        self.point_list = QtWidgets.QListWidget()
        self.point_list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.point_list.setMinimumHeight(150)
        lay.addWidget(self.point_list)
        lay.addWidget(QtWidgets.QLabel("连线图层"))
        self.line_combo = QtWidgets.QComboBox(); lay.addWidget(self.line_combo)
        row = QtWidgets.QHBoxLayout(); row.addWidget(QtWidgets.QLabel("最大允许距离（米）："))
        self.distance_edit = QtWidgets.QLineEdit("60")
        self.distance_edit.setValidator(QDoubleValidator(0.01, 1000000.0, 2, self))
        row.addWidget(self.distance_edit); lay.addLayout(row)
        tip = QtWidgets.QLabel(
            "规则：先删除重复线；超长线优先利用线路附近 3m 内的已有杆。\n"
            "已有杆只有在明显改善分段长度时才采用；改善小于 5m 的候选忽略。\n"
            "仍超距的区段自动新增临时杆。新增杆不命名，并在完成后自动选中。"
        )
        tip.setWordWrap(True); tip.setStyleSheet("color:#666; padding:5px 0;"); lay.addWidget(tip)
        buttons = QtWidgets.QDialogButtonBox()
        ok = buttons.addButton("开始", QtWidgets.QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton("取消", QtWidgets.QDialogButtonBox.RejectRole)
        ok.clicked.connect(self._start); cancel.clicked.connect(self.reject); lay.addWidget(buttons)
        self._load_layers()

    def _load_layers(self):
        self.point_list.clear(); self.line_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer: continue
            gt = QgsWkbTypes.geometryType(layer.wkbType())
            if gt == QgsWkbTypes.PointGeometry:
                item = QtWidgets.QListWidgetItem(layer.name()); item.setData(Qt.UserRole, layer.id())
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable); item.setCheckState(Qt.Unchecked)
                self.point_list.addItem(item)
            elif gt == QgsWkbTypes.LineGeometry:
                self.line_combo.addItem(layer.name(), layer.id())

    def _start(self):
        ids = [self.point_list.item(i).data(Qt.UserRole) for i in range(self.point_list.count())
               if self.point_list.item(i).checkState() == Qt.Checked]
        line_id = self.line_combo.currentData()
        try: max_dist = float(self.distance_edit.text())
        except Exception: max_dist = 0
        if not ids:
            QtWidgets.QMessageBox.warning(self, "超距增点", "请至少选择一个杆子点图层。"); return
        if not line_id:
            QtWidgets.QMessageBox.warning(self, "超距增点", "请选择连线图层。"); return
        if max_dist <= 0:
            QtWidgets.QMessageBox.warning(self, "超距增点", "最大允许距离必须大于 0。"); return
        self.accept()
        OverlengthPoleProcessor(self.iface, ids, line_id, max_dist).run()


class OverlengthPoleProcessor:
    SEARCH_METERS = 3.0
    IMPROVEMENT_METERS = 5.0

    def __init__(self, iface, point_layer_ids, line_layer_id, max_distance):
        self.iface = iface; self.project = QgsProject.instance()
        self.point_layer_ids = point_layer_ids; self.line_layer_id = line_layer_id
        self.max_distance = max_distance
        self.da = QgsDistanceArea(); self.da.setEllipsoid(self.project.ellipsoid())
        self._pole_points = []
        self._index = QgsSpatialIndex(); self._idx_map = {}
        self._created = []
        self._stats = {"duplicates": 0, "over": 0, "new": 0, "segments": 0}

    @property
    def line_layer(self): return self.project.mapLayer(self.line_layer_id)

    def _prepare_poles(self):
        layer = self.line_layer; dst = layer.crs()
        self._pole_points = []; self._index = QgsSpatialIndex(); self._idx_map = {}
        iid = 1
        for lid in self.point_layer_ids:
            lyr = self.project.mapLayer(lid)
            if lyr is None: continue
            tr = QgsCoordinateTransform(lyr.crs(), dst, self.project) if lyr.crs() != dst else None
            for feat in lyr.getFeatures():
                g = feat.geometry()
                if g.isEmpty(): continue
                try:
                    p = QgsPointXY(g.centroid().asPoint())
                    if tr: p = QgsPointXY(tr.transform(p))
                    f = QgsFeature(); f.setId(iid); f.setGeometry(QgsGeometry.fromPointXY(p)); self._index.addFeature(f)
                    self._idx_map[iid] = (lid, int(feat.id()), p); self._pole_points.append((lid, int(feat.id()), p)); iid += 1
                except Exception: continue

    def _meters_per_unit(self, p):
        self.da.setSourceCrs(self.line_layer.crs(), self.project.transformContext())
        try:
            mx = self.da.measureLine(p, QgsPointXY(p.x()+1, p.y()))
            my = self.da.measureLine(p, QgsPointXY(p.x(), p.y()+1))
            vals = [v for v in (mx, my) if v > 1e-12]
            return min(vals) if vals else 1.0
        except Exception: return 1.0

    def _query_poles(self, line_geom):
        p = QgsPointXY(line_geom.centroid().asPoint())
        r = self.SEARCH_METERS / self._meters_per_unit(p)
        bb = line_geom.boundingBox(); bb.grow(r)
        out = []
        for iid in self._index.intersects(bb):
            data = self._idx_map.get(iid)
            if not data: continue
            pt = data[2]
            try:
                sqr = line_geom.closestSegmentWithContext(pt)[0]
                if sqr <= r*r: out.append((iid, data, pt))
            except Exception: pass
        return out

    def _parts(self, geom):
        return geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]

    def _cum_lengths(self, pts):
        self.da.setSourceCrs(self.line_layer.crs(), self.project.transformContext())
        cum=[0.0]; total=0.0
        for a,b in zip(pts[:-1], pts[1:]):
            total += self.da.measureLine(QgsPointXY(a), QgsPointXY(b)); cum.append(total)
        return cum, total

    def _project_location(self, pts, p):
        geom = QgsGeometry.fromPolylineXY([QgsPointXY(x) for x in pts])
        try:
            res = geom.closestSegmentWithContext(QgsPointXY(p)); proj = QgsPointXY(res[1]); after = int(res[2])
        except Exception: return None
        if len(pts) < 2: return None
        seg = max(0, min(len(pts)-2, after-1))
        cum, total = self._cum_lengths(pts)
        a,b = QgsPointXY(pts[seg]), QgsPointXY(pts[seg+1])
        self.da.setSourceCrs(self.line_layer.crs(), self.project.transformContext())
        seglen = self.da.measureLine(a,b)
        frac = 0.0
        if seglen > 1e-12: frac = max(0.0, min(1.0, self.da.measureLine(a, proj)/seglen))
        return cum[seg] + seglen*frac, proj, seg, total

    def _point_at_distance(self, pts, distance):
        cum,total=self._cum_lengths(pts)
        if distance <= 0: return QgsPointXY(pts[0]), 0
        if distance >= total: return QgsPointXY(pts[-1]), len(pts)-2
        for i in range(len(pts)-1):
            if cum[i] <= distance <= cum[i+1]:
                span=cum[i+1]-cum[i]; frac=(distance-cum[i])/span if span>1e-12 else 0
                a,b=QgsPointXY(pts[i]),QgsPointXY(pts[i+1])
                return QgsPointXY(a.x()+(b.x()-a.x())*frac, a.y()+(b.y()-a.y())*frac), i
        return QgsPointXY(pts[-1]), len(pts)-2

    @staticmethod
    def _midpoint(cuts):
        return (min(cuts)+max(cuts))/2.0 if cuts else 0.0

    def _choose_existing(self, pts, candidates):
        locs=[]; _,total=self._cum_lengths(pts)
        for _,data,p in candidates:
            loc=self._project_location(pts,p)
            if not loc: continue
            d,proj,seg,_=loc
            if d <= 0.001 or total-d <= 0.001: continue
            locs.append((d,p,data))
        if not locs: return []
        selected=[]; cuts=[0.0,total]; remaining=locs[:]
        while True:
            cuts_sorted=sorted(cuts)
            current_max=max(b-a for a,b in zip(cuts_sorted[:-1],cuts_sorted[1:]))
            best=None; best_gain=self.IMPROVEMENT_METERS
            for d,p,data in remaining:
                if any(abs(d-x)<0.01 for x in cuts): continue
                nc=sorted(cuts+[d]); newmax=max(b-a for a,b in zip(nc[:-1],nc[1:]))
                gain=current_max-newmax
                if gain >= best_gain and (best is None or gain>best[0] or (abs(gain-best[0])<1e-9 and abs(d-self._midpoint(cuts))<abs(best[1]-self._midpoint(cuts)))):
                    best=(gain,d,p,data)
            if best is None: break
            _,d,p,data=best; selected.append((d,p,data)); cuts.append(d)
            remaining=[x for x in remaining if abs(x[0]-d)>=0.01]
            cs=sorted(cuts)
            if max(b-a for a,b in zip(cs[:-1],cs[1:])) <= self.max_distance: break
        return sorted(selected,key=lambda x:x[0])

    def _make_new_cuts(self, cuts):
        result=[]; cs=sorted(cuts)
        for a,b in zip(cs[:-1],cs[1:]):
            gap=b-a
            if gap <= self.max_distance + 1e-8: continue
            n=max(2, int((gap + self.max_distance - 1e-9)//self.max_distance))
            step=gap/n
            for k in range(1,n): result.append(a+step*k)
        return result

    def _subline(self, pts, cut_specs):
        cum,total=self._cum_lengths(pts)
        cuts=[(0.0,QgsPointXY(pts[0]))]+sorted(cut_specs,key=lambda x:x[0])+[(total,QgsPointXY(pts[-1]))]
        out=[]
        for (da,pa),(db,pb) in zip(cuts[:-1],cuts[1:]):
            if db-da <= 1e-8: continue
            arr=[pa]
            for i in range(1,len(pts)-1):
                if da < cum[i] < db: arr.append(QgsPointXY(pts[i]))
            arr.append(pb)
            if len(arr)>=2: out.append(QgsGeometry.fromPolylineXY(arr))
        return out

    @staticmethod
    def _duplicate_key(pts):
        vals=[(round(p.x(),9),round(p.y(),9)) for p in pts]
        rev=list(reversed(vals)); return tuple(vals if tuple(vals)<=tuple(rev) else rev)

    def _remove_duplicates(self, features):
        seen=set(); unique=[]; dup_ids=[]
        for feat in features:
            g=feat.geometry(); keys=[]
            if g.isMultipart():
                for part in g.asMultiPolyline(): keys.append(self._duplicate_key([QgsPointXY(p) for p in part]))
            else: keys.append(self._duplicate_key([QgsPointXY(p) for p in g.asPolyline()]))
            key=tuple(keys)
            if key in seen: dup_ids.append(feat.id())
            else: seen.add(key); unique.append(feat)
        return unique,dup_ids

    def _ensure_temp_layer(self):
        crs=self.line_layer.crs(); authid=crs.authid() or "EPSG:4326"
        lyr=QgsVectorLayer(f"Point?crs={authid}","ODN_超距增点","memory")
        lyr.dataProvider().addAttributes([QgsField("Name", QVariant.String)])
        lyr.updateFields(); self.project.addMapLayer(lyr); return lyr

    def _add_temp_point(self, layer, p):
        f=QgsFeature(layer.fields()); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p))); f["Name"]=""; self._created.append(f)

    def run(self):
        layer=self.line_layer
        if layer is None: self._msg("找不到连线图层。",2); return
        self._prepare_poles()
        feats=list(layer.getFeatures()); unique,dup_ids=self._remove_duplicates(feats)
        if dup_ids:
            layer.startEditing()
            for fid in dup_ids: layer.deleteFeature(fid)
            layer.commitChanges(); self._stats["duplicates"]=len(dup_ids)
        replacements=[]; temp=self._ensure_temp_layer()
        for feat in unique:
            g=feat.geometry()
            if g.isEmpty(): continue
            parts=self._parts(g); changed=False; new_geoms=[]
            for pts in parts:
                if len(pts)<2: continue
                _,total=self._cum_lengths(pts)
                if total <= self.max_distance + 1e-8:
                    new_geoms.append(QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in pts])); continue
                self._stats["over"]+=1; changed=True
                candidates=self._query_poles(QgsGeometry.fromPolylineXY([QgsPointXY(p) for p in pts]))
                existing=self._choose_existing(pts,candidates)
                cut_specs=[(d,QgsPointXY(p)) for d,p,_ in existing]
                cuts=[0.0]+[x[0] for x in cut_specs]+[total]
                new_dists=self._make_new_cuts(cuts)
                for d in new_dists:
                    p,_=self._point_at_distance(pts,d); cut_specs.append((d,p)); self._add_temp_point(temp,p); self._stats["new"]+=1
                allcuts=sorted(cut_specs,key=lambda x:x[0]); segs=self._subline(pts,allcuts); new_geoms.extend(segs); self._stats["segments"]+=len(segs)
            if changed: replacements.append((feat.id(),feat,new_geoms))
        if replacements:
            layer.startEditing()
            for fid,feat,geoms in replacements:
                layer.deleteFeature(fid)
                for geom in geoms:
                    nf=QgsFeature(layer.fields()); nf.setGeometry(geom); nf.setAttributes(feat.attributes()); layer.addFeature(nf)
            layer.commitChanges()
        if self._created:
            temp.startEditing(); temp.dataProvider().addFeatures(self._created); temp.commitChanges()
            temp.selectByIds([f.id() for f in temp.getFeatures()])
        else:
            self.project.removeMapLayer(temp.id())
        self.iface.mapCanvas().refresh()
        self._msg(f"超距增点完成：重复线删除 {self._stats['duplicates']} 条；超距线 {self._stats['over']} 条；新增临时杆 {self._stats['new']} 根；生成线段 {self._stats['segments']} 条。",0)

    def _msg(self, text, level=0):
        try: self.iface.messageBar().pushMessage("ODN Tools Pro", text, level=level, duration=5)
        except Exception: pass
