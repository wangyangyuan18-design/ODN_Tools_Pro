# -*- coding: utf-8 -*-
"""Unified ODN Offset Core.

One engine: complete-route priority -> relative lane planning -> node rules
-> continuous geometry. No runtime monkey-patching of legacy offset modules.
"""
from math import hypot
from qgis.core import QgsGeometry, QgsMessageLog, QgsPointXY, QgsUnitTypes, Qgis, QgsFeature, QgsVectorLayer
from . import cable_offset_layout as _base
from . import cable_offset_layout_v8 as _geom
from . import cable_offset_layout_v9 as _route

LOG_TAG = "ODN_Tools_Pro / Cable Offset"
SPECIAL = {"FDT", "FAT", "BB", "CL", "CLOSURE", "SFCCL", "SFCCLOSURE"}
DEFAULT_SPACING_M = 0.50
DEFAULT_CONTROL_M = 0.30

def _log(s, level=Qgis.Info):
    try: QgsMessageLog.logMessage(str(s), LOG_TAG, level)
    except Exception: pass

def _kind(item):
    return "".join(ch for ch in str(item[0]).strip().upper() if ch.isalnum()) if item else ""

def _special(item):
    k=_kind(item); return k in SPECIAL or (k.startswith("SFC") and ("CL" in k or "CLOSURE" in k))

def _metric_crs(edge_layer, dc_layer):
    for layer in (dc_layer, edge_layer):
        c=layer.crs()
        try:
            if c.isValid() and not c.isGeographic() and c.mapUnits()==QgsUnitTypes.DistanceMeters: return c
        except Exception: pass
    c=_base._choose_work_crs(edge_layer)
    if c.isValid() and not c.isGeographic() and c.mapUnits()==QgsUnitTypes.DistanceMeters: return c
    raise RuntimeError("Offset Core: 无法建立米制工作 CRS")

def _tp(p,src,dst): return _base._transform_point(QgsPointXY(p),src,dst)

def _occupancy(dc,designs,work):
    mem=QgsVectorLayer(f"LineString?crs={work.authid()}","ODN Offset Occupancy","memory")
    idx=dc.fields().indexOf("_ODN_LINK_ID"); current={str(d.get("_link_id")) for d in designs or [] if d.get("_link_id")}
    for f in dc.getFeatures():
        try:
            if idx>=0 and f.attribute(idx) and str(f.attribute(idx)) in current: continue
            g=_base._transform_geometry(f.geometry(),dc.crs(),work)
            if g.isEmpty(): continue
            nf=QgsFeature(); nf.setGeometry(g); mem.dataProvider().addFeature(nf)
        except Exception: pass
    mem.updateExtents(); return mem

def _node_users(designs,edge_crs,work):
    out={}
    for di,d in enumerate(designs or []):
        if d.get("written") and not d.get("needs_resync"): continue
        ids=d.get("sequence_ids",[]) or []
        for si,s in enumerate(d.get("segments",[]) or []):
            edges=[e for x in s.get("edge_sequence",[]) or [] if (e:=_base._canonical_edge(x))]
            if not edges: continue
            nodes=_base._extract_route_graph_nodes(s,work,edge_crs,edge_crs)
            if len(nodes)!=len(edges)+1: continue
            for ni,p in enumerate(nodes):
                item=ids[si] if ni==0 and si<len(ids) else ids[si+1] if ni==len(nodes)-1 and si+1<len(ids) else None
                key=(round(p.x(),7),round(p.y(),7)); out.setdefault(key,[]).append((di,si,ni,len(nodes),_special(item),_kind(item)))
    return out

def _plan(designs,dc,edge_layer,spacing):
    edge_crs=edge_layer.crs(); work=_metric_crs(edge_layer,dc)
    pending,routes=_route._collect_uses(designs,edge_crs,work)
    ordered=sorted(routes,key=lambda d:(-float(routes[d].get("priority_score",0)),-float(routes[d].get("longest_directional_run",0)),-float(routes[d].get("route_length",0)),int(d)))
    occ=_occupancy(dc,designs,work); idx,geoms=_base._build_existing_index(occ,work)
    cache={}; used={}; slots={}; uses={d:[] for d in ordered}
    for u in pending: uses.setdefault(u.design_index,[]).append(u)
    nodes=_node_users(designs,edge_crs,work); rank={d:i for i,d in enumerate(ordered)}; owners={}
    for key,items in nodes.items():
        ordinary=[x for x in items if not x[4]]
        if ordinary: owners[key]=min(ordinary,key=lambda x:rank.get(x[0],999999))[0]
    def reserve(edge):
        if edge not in cache:
            a,b=_base._edge_points(edge); a=_tp(a,edge_crs,work); b=_tp(b,edge_crs,work)
            cache[edge]=_base._existing_slot_occupancy(QgsGeometry.fromPolylineXY([a,b]),spacing,idx,geoms)
        return set(cache[edge])
    for di in ordered:
        prev=None; prevseg=None
        for u in sorted(uses.get(di,[]),key=lambda x:(x.segment_index,x.edge_index)):
            if prevseg is not None and u.segment_index!=prevseg: prev=None
            edge=u.edge_key; usededge=used.setdefault(edge,set(reserve(edge)))
            a,b=_base._edge_points(edge); a=_tp(a,edge_crs,work); b=_tp(b,edge_crs,work)
            ka=(round(a.x(),7),round(a.y(),7)); kb=(round(b.x(),7),round(b.y(),7))
            main_ok=0 not in usededge and owners.get(ka,di)==di and owners.get(kb,di)==di
            candidates=[]
            if prev is None and main_ok: candidates.append(0)
            if prev is not None:
                candidates.append(int(prev)); sign=1 if prev>0 else -1 if prev<0 else (1 if u.side_hint>=0 else -1)
                candidates += [sign*m for m in range(max(1,abs(int(prev))+1),101)] + [-sign*m for m in range(1,101)]
            else:
                sign=1 if u.side_hint>=0 else -1
                candidates += [sign*m for m in range(1,101)] + [-sign*m for m in range(1,101)]
            chosen=next((int(c) for c in candidates if int(c) not in usededge),None)
            if chosen is None: chosen=(max([abs(int(x)) for x in usededge]+[0])+1)*(1 if u.side_hint>=0 else -1)
            key=(di,u.segment_index,u.edge_index); slots[key]=chosen; u.slot=chosen; usededge.add(chosen); prev=chosen; prevseg=u.segment_index
    _log(f"[route-plan] links={len(ordered)}; main=available-first; continuity=ON; group-outside=ON; pole-exclusivity=ON")
    return edge_crs,work,ordered,routes,slots

def _set_flags(designs):
    for d in designs or []:
        ids=d.get("sequence_ids",[]) or []
        for i,s in enumerate(d.get("segments",[]) or []):
            s["_odn_special_start"]=_special(ids[i]) if i<len(ids) else False
            s["_odn_special_end"]=_special(ids[i+1]) if i+1<len(ids) else False

def _geometry(seg,slots,spacing,work,edge_crs,control):
    edges=[e for x in seg.get("edge_sequence",[]) or [] if (e:=_base._canonical_edge(x))]; stored=seg.get("points",[]) or []
    if not edges or len(stored)<2: return [_tp(QgsPointXY(float(p[0]),float(p[1])),edge_crs,work) for p in stored if len(p)>=2]
    nodes=_base._extract_route_graph_nodes(seg,work,edge_crs,edge_crs)
    if len(nodes)!=len(edges)+1: raise RuntimeError("Offset Core: edge_sequence 与 route nodes 数量不一致")
    sl=[int(slots.get(i,0)) for i in range(len(edges))]; old=[_tp(QgsPointXY(float(p[0]),float(p[1])),edge_crs,work) for p in stored if len(p)>=2]
    if not any(sl): return old
    out=[]
    def add(p):
        p=QgsPointXY(p)
        if not out or hypot(out[-1].x()-p.x(),out[-1].y()-p.y())>1e-7: out.append(p)
    add(old[0]); special=bool(seg.get("_odn_special_start"))
    for i,slot in enumerate(sl):
        a,b=nodes[i],nodes[i+1]
        if i==0:
            add(a) if slot==0 or not special else add(_geom._takeoff_entry(a,b,slot,spacing,control))
        if i>0:
            prev=sl[i-1]
            if prev==slot:
                add(a) if slot==0 else add(_geom._same_lane_join(a,nodes[i-1],nodes[i],a,b,slot,spacing))
            elif slot==0: add(a)
            else: add(_geom._transition_entry(a,b,slot,spacing))
        if i==len(sl)-1: add(b)
        elif slot==0: add(b)
        else: add(_base._offset_point(b,_base._unit(a,b),slot*spacing))
    add(old[-1]); return out

def _validate(designs,edge_crs,work):
    for di,d in enumerate(designs or []):
        for si,s in enumerate(d.get("segments",[]) or []):
            edges=[e for x in s.get("edge_sequence",[]) or [] if (e:=_base._canonical_edge(x))]; pts=s.get("points",[]) or []
            if edges and len(pts)<2: raise RuntimeError(f"Offset Core: Link {di} segment {si} points 无效")
            if edges:
                nodes=_base._extract_route_graph_nodes(s,work,edge_crs,edge_crs)
                if len(nodes)!=len(edges)+1: raise RuntimeError(f"Offset Core: Link {di} segment {si} topology invalid")
                wp=[_tp(QgsPointXY(float(p[0]),float(p[1])),edge_crs,work) for p in pts]
                if len(wp)>1 and QgsGeometry.fromPolylineXY(wp).length()>200000: raise RuntimeError(f"Offset Core: Link {di} segment {si} geometry abnormal")

def apply_offset_layout(designs,distribution_layer,edge_layer,spacing=DEFAULT_SPACING_M,control_distance_m=DEFAULT_CONTROL_M):
    spacing=max(0.01,float(spacing)); control=max(0.01,float(control_distance_m)); _set_flags(designs)
    edge_crs,work,ordered,routes,slot_map=_plan(designs,distribution_layer,edge_layer,spacing)
    changed=set(); extra=0.
    for di,d in enumerate(designs or []):
        if d.get("written") and not d.get("needs_resync"): continue
        new=[]; total=0.; diff=False
        for si,s in enumerate(d.get("segments",[]) or []):
            ss={ei:slot_map.get((di,si,ei),0) for ei,x in enumerate(s.get("edge_sequence",[]) or []) if _base._canonical_edge(x) is not None}
            nw=_geometry(s,ss,spacing,work,edge_crs,control); old=[_tp(QgsPointXY(float(p[0]),float(p[1])),edge_crs,work) for p in s.get("points",[]) if len(p)>=2]
            if len(nw)>=2:
                g=QgsGeometry.fromPolylineXY(nw); og=QgsGeometry.fromPolylineXY(old) if len(old)>=2 else QgsGeometry()
                diff |= len(nw)!=len(old) or any(hypot(a.x()-b.x(),a.y()-b.y())>1e-7 for a,b in zip(nw,old))
                out=[_tp(p,work,edge_crs) for p in nw]; cp=dict(s); cp["points"]=[[float(p.x()),float(p.y())] for p in out]; cp["distance"]=round(g.length(),3); cp["layout_spacing"]=round(spacing,3); new.append(cp); total+=g.length()
                if not og.isEmpty() and g.length()>og.length(): extra+=g.length()-og.length()
            else: new.append(s)
        if diff: d["segments"]=new; d["length"]=round(total,3); changed.add(di)
        d["source_crs"]=edge_crs.authid(); d["layout"]={"version":20,"engine":"OffsetCore","spacing_m":round(spacing,3),"fanout_control_distance_m":round(control,3),"main_lane":"complete_route_priority_available_0","lane":"relative_continuity_group_outside","pole":"one_independent_cable_landing","corner":"continuous_offset","takeoff":"explicit_special_endpoint_only","fat":"owning_link_final_geometry"}
    _validate(designs,edge_crs,work)
    _log(f"[OffsetCore] links={len(ordered)}; changed={len(changed)}; spacing={spacing:.3f}m; control={control:.3f}m; ordinary_corner_control=NOT_USED")
    return {"changed_designs":len(changed),"changed_indices":sorted(changed),"spacing_m":spacing,"extra_length_m":round(extra,3),"version":20,"work_crs":work.authid(),"lane_allocator":"OffsetCore","priority":ordered,"slot_map":{str(k):int(v) for k,v in slot_map.items()},"corner_geometry":"continuous_offset","fanout_control_distance_m":control}
