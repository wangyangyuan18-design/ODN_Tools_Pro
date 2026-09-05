# -*- coding: utf-8 -*-
"""Project-driven overlength pole insertion tool."""

from math import ceil
from qgis.PyQt import QtWidgets
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QDoubleValidator
from qgis.core import (
    QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsField,
    QgsGeometry, QgsMapLayerType, QgsPointXY, QgsProject, QgsSpatialIndex,
    QgsVectorLayer, QgsWkbTypes
)

from . import odn_project_context as context


class OverlengthPoleDialog(QtWidgets.QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("超距增点")
        self.setMinimumWidth(470)
        lay = QtWidgets.QVBoxLayout(self); lay.setSpacing(9)
        title = QtWidgets.QLabel("超距增点")
        f = title.font(); f.setBold(True); f.setPointSize(12); title.setFont(f); lay.addWidget(title)

        lay.addWidget(QtWidgets.QLabel("杆子图层（项目配置）"))
        self.pole_label = QtWidgets.QLabel("Existing Pole + New Pole")
        self.pole_label.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken); lay.addWidget(self.pole_label)
        lay.addWidget(QtWidgets.QLabel("连线图层（项目配置）"))
        self.line_label = QtWidgets.QLabel("Pole Edge")
        self.line_label.setFrameStyle(QtWidgets.QFrame.StyledPanel | QtWidgets.QFrame.Sunken); lay.addWidget(self.line_label)

        box = QtWidgets.QGroupBox("最大允许距离（米）")
        g = QtWidgets.QGridLayout(box)
        self.ee = self._distance_edit(65.0); self.en = self._distance_edit(65.0); self.nn = self._distance_edit(50.0)
        for r, (text, widget) in enumerate((
            ("Existing Pole-Existing Pole", self.ee),
            ("Existing Pole-New Pole", self.en),
            ("New Pole-New Pole", self.nn),
        )):
            g.addWidget(QtWidgets.QLabel(text), r, 0); g.addWidget(widget, r, 1)
        lay.addWidget(box)

        tip = QtWidgets.QLabel(
            "图层自动读取当前 ODN Project。\n"
            "处理保持原有逻辑：优先利用线路附近已有杆；仍超距时自动新增 New Pole，并在完成后自动选中新杆。"
        )
        tip.setWordWrap(True); tip.setStyleSheet("color:#666; padding:4px 0;"); lay.addWidget(tip)
        buttons = QtWidgets.QDialogButtonBox()
        ok = buttons.addButton("开始", QtWidgets.QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton("取消", QtWidgets.QDialogButtonBox.RejectRole)
        ok.clicked.connect(self._start); cancel.clicked.connect(self.reject); lay.addWidget(buttons)
        self._ok_button = ok
        self._load_project_layers()

    @staticmethod
    def _distance_edit(value):
        edit = QtWidgets.QLineEdit(str(value))
        edit.setValidator(QDoubleValidator(0.01, 1000000.0, 2, edit)); return edit

    def _load_project_layers(self):
        payload = context.require_project(self, "超距增点")
        if not payload: self._ok_button.setEnabled(False); return
        existing = context.project_layer(payload, "Existing Pole")
        new = context.project_layer(payload, "New Pole")
        edge = context.project_layer(payload, "Pole Edge")
        names = []
        if existing is not None: names.append(f"Existing Pole：{existing.name()}")
        if new is not None: names.append(f"New Pole：{new.name()}")
        self.pole_label.setText("\n".join(names) if names else "未绑定")
        self.line_label.setText(edge.name() if edge is not None else "未绑定")
        self._ok_button.setEnabled(
            edge is not None and edge.type() == QgsMapLayerType.VectorLayer
            and QgsWkbTypes.geometryType(edge.wkbType()) == QgsWkbTypes.LineGeometry
            and bool(existing or new)
        )

    def _start(self):
        def num(w, default):
            try: return max(0.01, float(w.text()))
            except Exception: return default
        payload = context.require_project(self, "超距增点")
        if not payload: return
        existing = context.project_layer(payload, "Existing Pole")
        new = context.project_layer(payload, "New Pole")
        edge = context.project_layer(payload, "Pole Edge")
        if edge is None or QgsWkbTypes.geometryType(edge.wkbType()) != QgsWkbTypes.LineGeometry:
            QtWidgets.QMessageBox.warning(self, "超距增点", "项目配置中没有有效的 Pole Edge 图层。"); return
        self.accept()
        OverlengthPoleProcessor(
            self.iface,
            [("Existing Pole", existing), ("New Pole", new)],
            edge,
            {"EE": num(self.ee, 65.0), "EN": num(self.en, 65.0), "NN": num(self.nn, 50.0)},
        ).run()


class OverlengthPoleProcessor:
    SEARCH_METERS = 3.0
    IMPROVEMENT_METERS = 5.0

    def __init__(self, iface, pole_layers, line_layer, limits):
        self.iface = iface; self.project = QgsProject.instance()
        self.pole_layers = [(role, lyr) for role, lyr in pole_layers if lyr is not None]
        self._line_layer = line_layer; self.limits = dict(limits)
        self.da = QgsDistanceArea(); self.da.setEllipsoid(self.project.ellipsoid())
        self._index = QgsSpatialIndex(); self._idx_map = {}; self._created = []
        self._stats = {"duplicates": 0, "over": 0, "new": 0, "segments": 0}

    @property
    def line_layer(self): return self._line_layer

    def _prepare_poles(self):
        dst = self.line_layer.crs(); self._index = QgsSpatialIndex(); self._idx_map = {}; iid = 1
        for role, lyr in self.pole_layers:
            if lyr.type() != QgsMapLayerType.VectorLayer: continue
            tr = QgsCoordinateTransform(lyr.crs(), dst, self.project.transformContext()) if lyr.crs() != dst else None
            for feat in lyr.getFeatures():
                g = feat.geometry()
                if g.isEmpty(): continue
                try:
                    p = QgsPointXY(g.centroid().asPoint())
                    if tr: p = QgsPointXY(tr.transform(p))
                    f = QgsFeature(); f.setId(iid); f.setGeometry(QgsGeometry.fromPointXY(p)); self._index.addFeature(f)
                    self._idx_map[iid] = (lyr.id(), int(feat.id()), p, role); iid += 1
                except Exception: continue

    def _meters_per_unit(self, p):
        self.da.setSourceCrs(self.line_layer.crs(), self.project.transformContext())
        try:
            vals = [self.da.measureLine(p, QgsPointXY(p.x() + 1, p.y())), self.da.measureLine(p, QgsPointXY(p.x(), p.y() + 1))]
            vals = [v for v in vals if v > 1e-12]; return min(vals) if vals else 1.0
        except Exception: return 1.0

    def _query_poles(self, line_geom):
        p = QgsPointXY(line_geom.centroid().asPoint()); r = self.SEARCH_METERS / self._meters_per_unit(p)
        bb = line_geom.boundingBox(); bb.grow(r); out = []
        for iid in self._index.intersects(bb):
            data = self._idx_map.get(iid)
            if not data: continue
            try:
                if line_geom.closestSegmentWithContext(data[2])[0] <= r * r: out.append((iid, data, data[2]))
            except Exception: pass
        return out

    def _parts(self, geom): return geom.asMultiPolyline() if geom.isMultipart() else [geom.asPolyline()]

    def _cum_lengths(self, pts):
        self.da.setSourceCrs(self.line_layer.crs(), self.project.transformContext())
        cum = [0.0]; total = 0.0
        for a, b in zip(pts[:-1], pts[1:]): total += self.da.measureLine(QgsPointXY(a), QgsPointXY(b)); cum.append(total)
        return cum, total

    def _project_location(self, pts, p):
        geom = QgsGeometry.fromPolylineXY([QgsPointXY(x) for x in pts])
        try: res = geom.closestSegmentWithContext(QgsPointXY(p)); proj = QgsPointXY(res[1]); after = int(res[2])
        except Exception: return None
        if len(pts) < 2: return None
        seg = max(0, min(len(pts) - 2, after - 1)); cum, total = self._cum_lengths(pts)
        a, b = QgsPointXY(pts[seg]), QgsPointXY(pts[seg + 1]); seglen = self.da.measureLine(a, b)
        frac = max(0.0, min(1.0, self.da.measureLine(a, proj) / seglen)) if seglen > 1e-12 else 0.0
        return cum[seg] + seglen * frac, proj, total

    @staticmethod
    def _midpoint(cuts): return (min(cuts) + max(cuts)) / 2.0 if cuts else 0.0

    def _choose_existing(self, pts, candidates):
        locs = []; _, total = self._cum_lengths(pts)
        for _, data, p in candidates:
            loc = self._project_location(pts, p)
            if not loc: continue
            d, _, _ = loc
            if d <= 0.001 or total - d <= 0.001: continue
            locs.append((d, p, data[3]))
        if not locs: return []
        selected = []; cuts = [0.0, total]; remaining = locs[:]
        while remaining:
            current = max(b - a for a, b in zip(sorted(cuts)[:-1], sorted(cuts)[1:])); best = None; best_gain = self.IMPROVEMENT_METERS
            for d, p, role in remaining:
                if any(abs(d - x) < 0.01 for x in cuts): continue
                trial = sorted(cuts + [d]); newmax = max(b - a for a, b in zip(trial[:-1], trial[1:])); gain = current - newmax
                if gain >= best_gain and (best is None or gain > best[0] or (abs(gain - best[0]) < 1e-9 and abs(d - self._midpoint(cuts)) < abs(best[1] - self._midpoint(cuts)))):
                    best = (gain, d, p, role)
            if best is None: break
            _, d, p, role = best; selected.append((d, QgsPointXY(p), role)); cuts.append(d)
            remaining = [x for x in remaining if abs(x[0] - d) >= 0.01]
        return sorted(selected, key=lambda x: x[0])

    def _allowed(self, left_role, right_role):
        if left_role == "Existing Pole" and right_role == "Existing Pole": return self.limits["EE"]
        if left_role == "New Pole" and right_role == "New Pole": return self.limits["NN"]
        return self.limits["EN"]

    def _endpoint_role(self, point, candidates):
        best = None
        for _, data, _ in candidates:
            try: d = self.da.measureLine(QgsPointXY(point), data[2])
            except Exception: continue
            if best is None or d < best[0]: best = (d, data[3])
        return best[1] if best and best[0] <= self.SEARCH_METERS else None

    def _has_overlength(self, specs, total, start_role, end_role):
        ordered = [(0.0, start_role)] + [(d, role) for d, _, role in specs] + [(total, end_role)]
        return any(b[0] - a[0] > self._allowed(a[1], b[1]) + 1e-8 for a, b in zip(ordered[:-1], ordered[1:]))

    def _point_at_distance(self, pts, distance):
        cum, total = self._cum_lengths(pts)
        if distance <= 0: return QgsPointXY(pts[0])
        if distance >= total: return QgsPointXY(pts[-1])
        for i in range(len(pts) - 1):
            if cum[i] <= distance <= cum[i + 1]:
                span = cum[i + 1] - cum[i]; frac = (distance - cum[i]) / span if span > 1e-12 else 0
                a, b = QgsPointXY(pts[i]), QgsPointXY(pts[i + 1])
                return QgsPointXY(a.x() + (b.x() - a.x()) * frac, a.y() + (b.y() - a.y()) * frac)
        return QgsPointXY(pts[-1])

    def _make_new_cuts(self, cut_specs, total, start_role, end_role):
        boundaries = [(0.0, QgsPointXY(self._last_pts[0]), start_role)]
        boundaries.extend(sorted(cut_specs, key=lambda x: x[0]))
        boundaries.append((total, QgsPointXY(self._last_pts[-1]), end_role))
        result = []
        for left, right in zip(boundaries[:-1], boundaries[1:]):
            cursor_d, cursor_p, cursor_role = left; right_d, right_p, right_role = right
            while right_d - cursor_d > self._allowed(cursor_role, right_role) + 1e-8:
                step = self._allowed(cursor_role, "New Pole")
                if step <= 0 or cursor_d + step >= right_d - 1e-8: break
                nd = cursor_d + step; np = self._point_at_distance(self._last_pts, nd)
                result.append((nd, np, "New Pole")); self._add_temp_point(self._temp_layer, np); self._stats["new"] += 1
                cursor_d, cursor_p, cursor_role = nd, np, "New Pole"
        return sorted(cut_specs + result, key=lambda x: x[0])

    def _subline(self, pts, cut_specs):
        cum, total = self._cum_lengths(pts)
        cuts = [(0.0, QgsPointXY(pts[0]))] + [(d, QgsPointXY(p)) for d, p, _ in sorted(cut_specs, key=lambda x: x[0])] + [(total, QgsPointXY(pts[-1]))]
        out = []
        for (da, pa), (db, pb) in zip(cuts[:-1], cuts[1:]):
            if db - da <= 1e-8: continue
            arr = [pa]
            for i in range(1, len(pts) - 1):
                if da < cum[i] < db: arr.append(QgsPointXY(pts[i]))
            arr.append(pb)
            if len(arr) >= 2: out.append(QgsGeometry.fromPolylineXY(arr))
        return out

    @staticmethod
    def _duplicate_key(pts):
        vals = [(round(p.x(), 9), round(p.y(), 9)) for p in pts]; rev = list(reversed(vals)); return tuple(vals if tuple(vals) <= tuple(rev) else rev)

    def _remove_duplicates(self, features):
        seen = set(); unique = []; dup_ids = []
        for feat in features:
            g = feat.geometry(); keys = []
            if g.isMultipart():
                for part in g.asMultiPolyline(): keys.append(self._duplicate_key([QgsPointXY(p) for p in part]))
            else: keys.append(self._duplicate_key([QgsPointXY(p) for p in g.asPolyline()]))
            key = tuple(keys)
            if key in seen: dup_ids.append(feat.id())
            else: seen.add(key); unique.append(feat)
        return unique, dup_ids

    def _ensure_temp_layer(self):
        crs = self.line_layer.crs(); authid = crs.authid() or "EPSG:4326"
        lyr = QgsVectorLayer(f"Point?crs={authid}", "ODN_超距增点", "memory")
        lyr.dataProvider().addAttributes([QgsField("Name", QVariant.String)]); lyr.updateFields(); self.project.addMapLayer(lyr); return lyr

    def _add_temp_point(self, layer, p):
        f = QgsFeature(layer.fields()); f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p))); f["Name"] = ""; self._created.append(f)

    def run(self):
        layer = self.line_layer
        if layer is None: self._msg("项目配置中的 Pole Edge 不存在。", 2); return
        self._prepare_poles()
        feats = list(layer.getFeatures()); unique, dup_ids = self._remove_duplicates(feats)
        if dup_ids:
            layer.startEditing()
            for fid in dup_ids: layer.deleteFeature(fid)
            layer.commitChanges(); self._stats["duplicates"] = len(dup_ids)

        replacements = []; self._temp_layer = self._ensure_temp_layer()
        for feat in unique:
            g = feat.geometry()
            if g.isEmpty(): continue
            parts = self._parts(g); changed = False; new_geoms = []
            for pts in parts:
                if len(pts) < 2: continue
                self._last_pts = [QgsPointXY(p) for p in pts]; _, total = self._cum_lengths(pts)
                candidates = self._query_poles(g)
                start_role = self._endpoint_role(pts[0], candidates) or "Existing Pole"
                end_role = self._endpoint_role(pts[-1], candidates) or "Existing Pole"
                existing = self._choose_existing(pts, candidates)
                if not self._has_overlength(existing, total, start_role, end_role):
                    new_geoms.append(QgsGeometry.fromPolylineXY(self._last_pts)); continue
                self._stats["over"] += 1; changed = True
                dynamic = self._make_new_cuts(existing, total, start_role, end_role)
                # Safety check: if dynamic cuts still leave a gap, keep adding until all pairwise limits pass.
                while self._has_overlength(dynamic, total, start_role, end_role):
                    before = len(dynamic); dynamic = self._make_new_cuts(dynamic, total, start_role, end_role)
                    if len(dynamic) == before: break
                segs = self._subline(pts, dynamic); new_geoms.extend(segs); self._stats["segments"] += len(segs)
            if changed: replacements.append((feat.id(), feat, new_geoms))

        if replacements:
            layer.startEditing()
            for fid, feat, geoms in replacements:
                layer.deleteFeature(fid)
                for geom in geoms:
                    nf = QgsFeature(layer.fields()); nf.setGeometry(geom); nf.setAttributes(feat.attributes()); layer.addFeature(nf)
            layer.commitChanges()

        if self._created:
            self._temp_layer.startEditing(); self._temp_layer.dataProvider().addFeatures(self._created); self._temp_layer.commitChanges()
            self._temp_layer.selectByIds([f.id() for f in self._temp_layer.getFeatures()])
        else:
            self.project.removeMapLayer(self._temp_layer.id())
        self.iface.mapCanvas().refresh()
        self._msg(f"超距增点完成：检查超距线 {self._stats['over']} 条，新增 New Pole {self._stats['new']} 个，删除重复线 {self._stats['duplicates']} 条。")

    def _msg(self, text, level=0):
        try: self.iface.messageBar().pushMessage("ODN Tools Pro", text, level=level, duration=5)
        except Exception: pass
