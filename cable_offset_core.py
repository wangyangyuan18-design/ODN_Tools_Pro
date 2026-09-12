# -*- coding: utf-8 -*-
"""Authoritative ODN cable offset engine.

Only offset planning/geometry implementation for Distribution Cable and final
FAT landing points. Link topology remains authoritative.
"""
from math import acos, degrees, hypot, tan, radians
from qgis.PyQt.QtCore import QSettings
from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
    QgsMessageLog, QgsPointXY, QgsProject, QgsSpatialIndex, QgsUnitTypes,
    QgsVectorLayer, Qgis)
from . import cable_offset_layout as _base

LOG_TAG="ODN_Tools_Pro / Cable Offset"
DEFAULT_SPACING_M=.50
DEFAULT_CONTROL_M=.30
DEFAULT_FAT_MAX_DISTANCE_M=3.0
SPACING_KEY="ODNToolsPro/CableOffsetLayout/spacing_m"
CONTROL_DISTANCE_KEY="ODNToolsPro/CableOffsetLayout/control_distance_m"
SHARED_NODE_TYPES={"FDT","BB","CL","CLOSURE","SFCCL","SFCCLOSURE","FATRETURN"}
ENDPOINT_TYPES=SHARED_NODE_TYPES|{"FAT"}

def _log(message,level=Qgis.Info):
    try: QgsMessageLog.logMessage(str(message),LOG_TAG,level)
    except Exception: pass

def get_settings():
    try: spacing=float(QSettings().value(SPACING_KEY,DEFAULT_SPACING_M))
    except Exception: spacing=DEFAULT_SPACING_M
    try: control=float(QSettings().value(CONTROL_DISTANCE_KEY,DEFAULT_CONTROL_M))
    except Exception: control=DEFAULT_CONTROL_M
    return max(.01,spacing),max(.01,control)

def save_settings(spacing,control_distance):
    s=QSettings();s.setValue(SPACING_KEY,max(.01,float(spacing)));s.setValue(CONTROL_DISTANCE_KEY,max(.01,float(control_distance)));s.sync()

def _kind(item): return "".join(ch for ch in str(item[0]).strip().upper() if ch.isalnum()) if item else ""
def _shared_node(item):
    k=_kind(item)
    return (k.startswith("FDT") or k.startswith("BB") or k in {"CL","CLOSURE"}
            or k.startswith("SFCCL") or k.startswith("SFCCLOSURE")
            or k.startswith("FATRETURN"))
def _endpoint_special(item):
    k=_kind(item)
    return _shared_node(item) or k.startswith("FAT")
def _metric_crs(edge,dc=None):
    for layer in (dc,edge):
        if layer is None: continue
        c=layer.crs()
        try:
            if c.isValid() and not c.isGeographic() and c.mapUnits()==QgsUnitTypes.DistanceMeters:return c
        except Exception: pass
    c=_base._choose_work_crs(edge)
    if c.isValid() and not c.isGeographic() and c.mapUnits()==QgsUnitTypes.DistanceMeters:return c
    raise RuntimeError("Offset Core: 无法建立米制工作 CRS")
def _tp(p,src,dst): return _base._transform_point(QgsPointXY(p),src,dst)
def _node_key(p): return round(float(p.x()),7),round(float(p.y()),7)
def _turn_angle(a,b,c):
    v1=_base._unit(a,b);v2=_base._unit(b,c);dot=max(-1.,min(1.,v1[0]*v2[0]+v1[1]*v2[1]));return degrees(acos(dot))

def _route_metrics(design_index,design,edge_crs,work_crs):
    total=longest=current=turn_sum=0.;turns=reversals=edges_count=0;seen=set()
    for segment in design.get("segments",[]) or []:
        edges=[e for raw in segment.get("edge_sequence",[]) or [] if (e:=_base._canonical_edge(raw))]
        if not edges: continue
        nodes=_base._extract_route_graph_nodes(segment,work_crs,edge_crs,edge_crs)
        if len(nodes)!=len(edges)+1: continue
        for i,e in enumerate(edges):
            a,b=_base._edge_points(e);a,b=_tp(a,edge_crs,work_crs),_tp(b,edge_crs,work_crs);length=hypot(b.x()-a.x(),b.y()-a.y());edges_count+=1
            if e not in seen: total+=length;seen.add(e)
            current+=length
            if i+1<len(edges):
                angle=_turn_angle(nodes[i],nodes[i+1],nodes[i+2]);turns+=1;turn_sum+=angle;reversals+=int(angle>=135.)
                if angle>=25.:longest=max(longest,current);current=0.
    longest=max(longest,current);score=longest*1000.+total*10.-turns*150.-reversals*500.
    return {"route_length":total,"longest_directional_run":longest,"turn_count":turns,"turn_sum":turn_sum,"reversal_count":reversals,"edge_count":edges_count,"priority_score":score}

def _collect_uses(designs,edge_crs,work_crs):
    pending=[];routes={}
    for di,d in enumerate(designs or []):
        if d.get("written") and not d.get("needs_resync"):continue
        routes[di]=_route_metrics(di,d,edge_crs,work_crs)
        for si,s in enumerate(d.get("segments",[]) or []):
            edges=[e for raw in s.get("edge_sequence",[]) or [] if (e:=_base._canonical_edge(raw))];points=s.get("points",[]) or []
            if not edges or len(points)<2:continue
            nodes=_base._extract_route_graph_nodes(s,work_crs,edge_crs,edge_crs)
            if len(nodes)!=len(edges)+1:continue
            start=_tp(QgsPointXY(float(points[0][0]),float(points[0][1])),edge_crs,work_crs);end=_tp(QgsPointXY(float(points[-1][0]),float(points[-1][1])),edge_crs,work_crs)
            for ei,e in enumerate(edges):
                a,b=_base._edge_points(e);a,b=_tp(a,edge_crs,work_crs),_tp(b,edge_crs,work_crs)
                u=_base._LayoutUse(di,si,ei,e,start,end,_base._route_side_hint(start,end,a,b));u.prev_edge_key=edges[ei-1] if ei else None;pending.append(u)
    return pending,routes

def _occupancy(dc,designs,work):
    mem=QgsVectorLayer(f"LineString?crs={work.authid()}","ODN Offset Occupancy","memory");idx=dc.fields().indexOf("_ODN_LINK_ID");cur={str(d.get("_link_id")) for d in designs or [] if d.get("_link_id")}
    for f in dc.getFeatures():
        try:
            if idx>=0 and f.attribute(idx) and str(f.attribute(idx)) in cur:continue
            g=_base._transform_geometry(f.geometry(),dc.crs(),work)
            if not g.isEmpty(): nf=QgsFeature();nf.setGeometry(g);mem.dataProvider().addFeature(nf)
        except Exception:pass
    mem.updateExtents();return mem

def _node_users(designs,edge_crs,work):
    out={}
    for di,d in enumerate(designs or []):
        if d.get("written") and not d.get("needs_resync"):continue
        ids=d.get("sequence_ids",[]) or []
        for si,s in enumerate(d.get("segments",[]) or []):
            edges=[e for raw in s.get("edge_sequence",[]) or [] if (e:=_base._canonical_edge(raw))]
            if not edges:continue
            nodes=_base._extract_route_graph_nodes(s,work,edge_crs,edge_crs)
            if len(nodes)!=len(edges)+1:continue
            for ni,p in enumerate(nodes):
                # Only sequence endpoints are Cable landings. Intermediate
                # graph nodes are merely passed-through Pole vertices and MUST
                # NOT be treated as independent Cable connections.
                item=ids[si] if ni==0 and si<len(ids) else ids[si+1] if ni==len(nodes)-1 and si+1<len(ids) else None
                if item is None:
                    continue
                out.setdefault(_node_key(p),[]).append({"design_index":di,"segment_index":si,"node_index":ni,"node_count":len(nodes),"special":_shared_node(item),"kind":_kind(item)})
    return out

def _validate_pole_exclusivity(designs,edge_crs,work):
    """Hard rule 1: an ordinary Pole may have only one independent cable landing.

    Shared landing is allowed only at FDT, FAT Return, BB and SFC/CL nodes.
    The check is endpoint/landing based; merely passing through an ordinary Pole
    is not treated as a second cable landing.
    """
    conflicts=[]
    for key,items in _node_users(designs,edge_crs,work).items():
        ordinary=[x for x in items if not x["special"]]
        independent={x["design_index"] for x in ordinary}
        if len(independent)>1:
            conflicts.append((key,sorted(independent),ordinary))
    if conflicts:
        details=[]
        for key,design_ids,items in conflicts[:20]:
            labels=[]
            for di in design_ids:
                d=designs[di] if 0<=di<len(designs) else {}
                labels.append(str(d.get("_link_id",d.get("name",di))))
            details.append(f"node={key}; links={labels}")
        raise RuntimeError("Offset Core: 普通 Pole 只允许 1 条独立 Cable 连接；发现共点冲突："+" | ".join(details))

def _plan(designs,dc,edge_layer,spacing):
    edge_crs=edge_layer.crs();work=_metric_crs(edge_layer,dc);pending,routes=_collect_uses(designs,edge_crs,work)
    _validate_pole_exclusivity(designs,edge_crs,work)
    ordered=sorted(routes,key=lambda d:(-float(routes[d].get("priority_score",0)),-float(routes[d].get("longest_directional_run",0)),-float(routes[d].get("route_length",0)),int(d)))
    occ=_occupancy(dc,designs,work);idx,geoms=_base._build_existing_index(occ,work)
    reserved_cache={};used_by_edge={};slots={};route_uses={d:[] for d in ordered}
    for u in pending:route_uses.setdefault(u.design_index,[]).append(u)
    rank={d:i for i,d in enumerate(ordered)};owners={}
    for key,items in _node_users(designs,edge_crs,work).items():
        ordinary=[x for x in items if not x["special"]]
        if ordinary:owners[key]=min(ordinary,key=lambda x:rank.get(x["design_index"],999999))["design_index"]
    def reserved(e):
        if e not in reserved_cache:
            a,b=_base._edge_points(e);a,b=_tp(a,edge_crs,work),_tp(b,edge_crs,work)
            reserved_cache[e]=_base._existing_slot_occupancy(QgsGeometry.fromPolylineXY([a,b]),spacing,idx,geoms)
        return set(reserved_cache[e])

    # 6-7 invariant:
    # For every traversed Pole Edge, if an existing independent cable does not
    # already occupy the main lane, exactly ONE planned cable occupies slot 0.
    # The primary cable is kept as the same route continues; other cables keep
    # their relative side/ordering and are added from the outside.
    edge_group_slots={}
    for e in {u.edge_key for u in pending}:
        edge_group_slots[e]=[]

    for di in ordered:
        prev_by_seg={}
        for u in sorted(route_uses.get(di,[]),key=lambda x:(x.segment_index,x.edge_index)):
            prev_by_seg[u.segment_index]=u

    # Process each edge as a group, not cable-by-cable. This is essential for
    # the "exactly one slot 0" invariant.
    uses_by_edge={}
    for u in pending:
        uses_by_edge.setdefault(u.edge_key,[]).append(u)

    for e,uses in uses_by_edge.items():
        used=set(reserved(e))
        main_taken=0 in used
        ordered_uses=sorted(uses,key=lambda u:(rank.get(u.design_index,999999),u.segment_index,u.edge_index))
        primary=None
        if not main_taken:
            for u in ordered_uses:
                prev_key=(u.design_index,u.segment_index,u.edge_index-1)
                if u.edge_index>0 and slots.get(prev_key)==0:
                    primary=u;break
            if primary is None:
                primary=ordered_uses[0] if ordered_uses else None

        assigned={}
        if primary is not None and not main_taken:
            key=(primary.design_index,primary.segment_index,primary.edge_index)
            assigned[key]=0
            used.add(0)
            edge_group_slots[e].append(0)

        for u in ordered_uses:
            key=(u.design_index,u.segment_index,u.edge_index)
            if key in assigned:continue
            prev=slots.get((u.design_index,u.segment_index,u.edge_index-1)) if u.edge_index>0 else None
            if prev is not None and int(prev) not in used:
                chosen=int(prev)
            else:
                sign=1 if u.side_hint>=0 else -1
                occupied_group=set(edge_group_slots[e])
                start=max(1,max([abs(int(x)) for x in used|occupied_group]+[0])+1)
                chosen=None
                for mag in range(start,101):
                    for cand in (sign*mag,-sign*mag):
                        if cand not in used and cand not in occupied_group:
                            chosen=cand;break
                    if chosen is not None:break
                if chosen is None:
                    chosen=(max([abs(int(x)) for x in used|occupied_group]+[0])+1)*sign
            assigned[key]=int(chosen);used.add(int(chosen));edge_group_slots[e].append(int(chosen))

        for u in ordered_uses:
            key=(u.design_index,u.segment_index,u.edge_index);chosen=int(assigned[key]);slots[key]=chosen;u.slot=chosen

        planned_zero=sum(1 for u in ordered_uses if slots.get((u.design_index,u.segment_index,u.edge_index))==0)
        if not main_taken and ordered_uses and planned_zero!=1:
            raise RuntimeError(f"Offset Core: Pole Edge main-lane invariant violated for edge {e}: planned_zero={planned_zero}")
        if main_taken and planned_zero:
            raise RuntimeError(f"Offset Core: Pole Edge has duplicate main-lane ownership for edge {e}")

    _log(f"[route-plan] links={len(ordered)}; main=EXACTLY_ONE_SLOT0_PER_EDGE; continuity=RELATIVE_POSITION; group-outside=ON; pole-exclusivity=ON")
    return edge_crs,work,ordered,routes,slots

def _intersection(p1,p2,q1,q2):
    rx,ry=p2.x()-p1.x(),p2.y()-p1.y();sx,sy=q2.x()-q1.x(),q2.y()-q1.y();den=rx*sy-ry*sx;scale=max(hypot(rx,ry)*hypot(sx,sy),1.)
    if abs(den)<=1e-10*scale:return None
    qpx,qpy=q1.x()-p1.x(),q1.y()-p1.y();t=(qpx*sy-qpy*sx)/den;return QgsPointXY(p1.x()+t*rx,p1.y()+t*ry)

def _same_lane_join(node,pa,pb,na,nb,slot,spacing):
    if slot==0:return QgsPointXY(node)
    pt=_base._unit(pa,pb);nt=_base._unit(na,nb);d=float(slot)*float(spacing);p1=_base._offset_point(node,pt,d);p2=_base._offset_point(QgsPointXY(pa.x()+pt[0],pa.y()+pt[1]),pt,d);q1=_base._offset_point(node,nt,d);q2=_base._offset_point(QgsPointXY(na.x()+nt[0],na.y()+nt[1]),nt,d);hit=_intersection(p1,p2,q1,q2)
    if hit is not None and hypot(hit.x()-node.x(),hit.y()-node.y())<=max(3*abs(d),5*max(float(spacing),.01)):return hit
    return (_base._offset_point(node,pt,d),_base._offset_point(node,nt,d))

def _transition_entry(a,b,slot,spacing):
    d=abs(int(slot))*float(spacing)
    if slot==0 or d<=1e-12:return QgsPointXY(a)
    length=hypot(b.x()-a.x(),b.y()-a.y())
    if length<=1e-12:return QgsPointXY(a)
    angle=60. if abs(int(slot))<=2 else 75. if abs(int(slot))<=4 else 90.;tr=tan(radians(angle));run=min(d/(tr if abs(tr)>1e-12 else 1.),length*.45);center=_base._point_along(a,b,run/length);return _base._offset_point(center,_base._unit(a,b),int(slot)*float(spacing))

def _takeoff_entry(a,b,slot,spacing,control):
    d=int(slot)*float(spacing)
    if slot==0 or abs(d)<=1e-12:return QgsPointXY(a)
    length=hypot(b.x()-a.x(),b.y()-a.y())
    if length<=1e-12:return QgsPointXY(a)
    run=min(max(.01,float(control)),length*.45);center=_base._point_along(a,b,run/length);return _base._offset_point(center,_base._unit(a,b),d)

def _geometry(segment,slot_by_edge,spacing,work,edge_crs,control):
    edges=[e for raw in segment.get("edge_sequence",[]) or [] if (e:=_base._canonical_edge(raw))];stored=segment.get("points",[]) or [];old=[_tp(QgsPointXY(float(p[0]),float(p[1])),edge_crs,work) for p in stored if len(p)>=2]
    if not edges or len(old)<2:return old
    nodes=_base._extract_route_graph_nodes(segment,work,edge_crs,edge_crs)
    if len(nodes)!=len(edges)+1:raise RuntimeError("Offset Core: edge_sequence 与 route nodes 数量不一致")
    slots=[int(slot_by_edge.get(i,0)) for i in range(len(edges))]
    if not any(slots):return old
    out=[]
    def add(p):
        p=QgsPointXY(p)
        if not out or hypot(out[-1].x()-p.x(),out[-1].y()-p.y())>1e-7:out.append(p)
    add(old[0]);special=bool(segment.get("_odn_special_start"))
    for i,slot in enumerate(slots):
        a,b=nodes[i],nodes[i+1]
        if i==0:add(a) if slot==0 or not special else add(_takeoff_entry(a,b,slot,spacing,control))
        if i>0:
            prev=slots[i-1]
            if prev==slot:
                joined=_same_lane_join(a,nodes[i-1],nodes[i],a,b,slot,spacing)
                if isinstance(joined,tuple):add(joined[0]);add(joined[1])
                else:add(joined)
            elif slot==0:add(a)
            else:add(_transition_entry(a,b,slot,spacing))
        if i==len(slots)-1:add(b)
        elif slot==0:add(b)
        else:add(_base._offset_point(b,_base._unit(a,b),slot*spacing))
    add(old[-1]);return out

def _set_flags(designs):
    for d in designs or []:
        ids=d.get("sequence_ids",[]) or []
        for i,s in enumerate(d.get("segments",[]) or []):
            s["_odn_special_start"]=_endpoint_special(ids[i]) if i<len(ids) else False;s["_odn_special_end"]=_endpoint_special(ids[i+1]) if i+1<len(ids) else False

def _validate(designs,edge_crs,work):
    for di,d in enumerate(designs or []):
        for si,s in enumerate(d.get("segments",[]) or []):
            edges=[e for raw in s.get("edge_sequence",[]) or [] if (e:=_base._canonical_edge(raw))]
            if not edges:continue
            nodes=_base._extract_route_graph_nodes(s,work,edge_crs,edge_crs)
            if len(nodes)!=len(edges)+1:raise RuntimeError(f"Offset Core: Link {di} segment {si} topology invalid")
            if len(s.get("points",[]) or [])<2:raise RuntimeError(f"Offset Core: Link {di} segment {si} points 无效")

def _feature_point(feature,layer,work):
    try:return _tp(QgsPointXY(feature.geometry().centroid().asPoint()),layer.crs(),work)
    except Exception:return None

def _crs(authid):
    try:c=QgsCoordinateReferenceSystem(str(authid));return c if c.isValid() else None
    except Exception:return None

def _fat_refs(designs):
    refs={};dups=set()
    for di,d in enumerate(designs or []):
        for pos,item in enumerate(d.get("sequence_ids",[]) or []):
            if len(item)<2 or _kind(item)!="FAT":continue
            try:fid=int(item[1])
            except Exception:continue
            if fid in refs:dups.add(fid)
            else:refs[fid]={"design_index":di,"sequence_pos":pos}
    return refs,dups

def _nearest_on_route(design,anchor,edge_crs,work):
    best=None
    for si,s in enumerate(design.get("segments",[]) or []):
        raw=s.get("points",[]) or [];pts=[_tp(QgsPointXY(float(p[0]),float(p[1])),edge_crs,work) for p in raw if len(p)>=2]
        if len(pts)<2:continue
        g=QgsGeometry.fromPolylineXY(pts);n=g.nearestPoint(QgsGeometry.fromPointXY(anchor))
        if n.isEmpty():continue
        p=QgsPointXY(n.asPoint());dist=hypot(p.x()-anchor.x(),p.y()-anchor.y());cand=(dist,si,p)
        if best is None or cand<best:best=cand
    return best

def _fat_target(ref,design,feature,fat_layer,edge_crs,work):
    segs=design.get("segments",[]) or [];pos=int(ref["sequence_pos"]);incoming=pos-1 if 0<=pos-1<len(segs) else None;outgoing=pos if 0<=pos<len(segs) else None;anchor=None
    if incoming is not None:
        n=_base._extract_route_graph_nodes(segs[incoming],work,edge_crs,edge_crs)
        if n:anchor=QgsPointXY(n[-1])
    if anchor is None and outgoing is not None:
        n=_base._extract_route_graph_nodes(segs[outgoing],work,edge_crs,edge_crs)
        if n:anchor=QgsPointXY(n[0])
    if anchor is None:anchor=_feature_point(feature,fat_layer,work)
    if anchor is None:return None,None,{"reason":"无法确定 FAT 锚点"}
    nearest=_nearest_on_route(design,anchor,edge_crs,work)
    if nearest is None:return None,anchor,{"reason":"无法从最终 Offset Cable 几何确定 FAT 落点"}
    return nearest[2],anchor,{"segment_index":nearest[1],"distance":nearest[0]}

def _prepare_fat_moves(designs,fat_layer,edge_layer,work,fat_limit):
    if fat_layer is None:return {},{"total":0,"skipped":0,"corner":0,"straight":0}
    edge_crs=edge_layer.crs();features={int(f.id()):f for f in fat_layer.getFeatures()};refs,dups=_fat_refs(designs)
    if dups:_log(f"[fat-landing] duplicate_fat_refs={sorted(dups)[:20]}",Qgis.Warning)
    moves={};stats={"total":len(refs),"skipped":0,"corner":0,"straight":0}
    for fid,ref in refs.items():
        feature=features.get(fid)
        if feature is None:stats["skipped"]+=1;continue
        current=_feature_point(feature,fat_layer,work);target,anchor,info=_fat_target(ref,designs[ref["design_index"]],feature,fat_layer,edge_crs,work)
        if target is None or anchor is None:stats["skipped"]+=1;continue
        anchor_dist=hypot(current.x()-anchor.x(),current.y()-anchor.y()) if current else 0.
        if anchor_dist>max(.01,float(fat_limit)):stats["skipped"]+=1;continue
        target_edge=_tp(target,work,edge_crs);target_layer=_tp(target_edge,edge_crs,fat_layer.crs())
        moves[fid]={"design_index":ref["design_index"],"sequence_pos":ref["sequence_pos"],"target_edge":QgsPointXY(target_edge),"target_layer":QgsPointXY(target_layer),"target_work":target,"anchor":anchor,"move_distance":hypot(current.x()-target.x(),current.y()-target.y()) if current else 0.,"mode":"final_route_geometry"};stats["straight"]+=1
    return moves,stats

def _replace_fat_endpoints(designs,moves,edge_crs):
    touched=set()
    for move in moves.values():
        di,pos=move["design_index"],move["sequence_pos"]
        if not 0<=di<len(designs):continue
        d=designs[di];source=_crs(d.get("source_crs")) or edge_crs;target=_tp(move["target_edge"],edge_crs,source);segs=d.get("segments",[]) or [];incoming=pos-1 if 0<=pos-1<len(segs) else None;outgoing=pos if 0<=pos<len(segs) else None
        if incoming is not None:
            p=list(segs[incoming].get("points",[]) or [])
            if p:p[-1]=[float(target.x()),float(target.y())];segs[incoming]["points"]=p;touched.add(di)
        if outgoing is not None:
            p=list(segs[outgoing].get("points",[]) or [])
            if p:p[0]=[float(target.x()),float(target.y())];segs[outgoing]["points"]=p;touched.add(di)
    for di in touched:
        total=0.;d=designs[di]
        for s in d.get("segments",[]) or []:
            p=s.get("points",[]) or [];length=sum(hypot(float(p[i][0])-float(p[i-1][0]),float(p[i][1])-float(p[i-1][1])) for i in range(1,len(p))) if len(p)>=2 else 0.;s["distance"]=round(length,3);total+=length
        d["length"]=round(total,3)
    return len(touched)

def _apply_fat_moves(layer,moves):
    moved=0
    for fid,move in moves.items():
        f=layer.getFeature(int(fid));target=move.get("target_layer")
        if not f or not f.isValid() or target is None:raise RuntimeError(f"FAT feature {fid} 无法更新")
        g=QgsGeometry.fromPointXY(QgsPointXY(target))
        if f.geometry().distance(g)>1e-9:
            f.setGeometry(g)
            if not layer.updateFeature(f):raise RuntimeError(f"无法写入 FAT feature {fid}")
            moved+=1
    return moved

def commit_fat_landing_points(fat_layer,summary):
    moved=_apply_fat_moves(fat_layer,(summary or {}).get("fat_moves") or {});summary["fat_written"]=moved;return moved

def apply_offset_layout(designs,distribution_layer,edge_layer,spacing=DEFAULT_SPACING_M,control_distance_m=DEFAULT_CONTROL_M,fat_layer=None,fat_max_distance_m=DEFAULT_FAT_MAX_DISTANCE_M):
    spacing=max(.01,float(spacing));control_distance_m=max(.01,float(control_distance_m));_set_flags(designs);edge_crs,work,ordered,routes,slot_map=_plan(designs,distribution_layer,edge_layer,spacing);changed=set();extra=0.
    for di,d in enumerate(designs or []):
        if d.get("written") and not d.get("needs_resync"):continue
        new=[];total=0.;diff=False
        for si,s in enumerate(d.get("segments",[]) or []):
            n=len(s.get("edge_sequence",[]) or []);by={i:slot_map.get((di,si,i),0) for i in range(n)};old=[_tp(QgsPointXY(float(p[0]),float(p[1])),edge_crs,work) for p in s.get("points",[]) if len(p)>=2];nw=_geometry(s,by,spacing,work,edge_crs,control_distance_m);cp=dict(s)
            if len(nw)>=2:
                g=QgsGeometry.fromPolylineXY(nw);og=QgsGeometry.fromPolylineXY(old) if len(old)>=2 else QgsGeometry();diff=diff or len(nw)!=len(old) or any(hypot(a.x()-b.x(),a.y()-b.y())>1e-7 for a,b in zip(nw,old));extra+=max(0.,g.length()-(og.length() if not og.isEmpty() else g.length()));out=[]
                for p in nw:
                    q=_tp(p,work,edge_crs);out.append([float(q.x()),float(q.y())])
                cp["points"]=out;cp["distance"]=round(g.length(),3);cp["layout_spacing"]=round(spacing,3);new.append(cp);total+=g.length()
            else:new.append(cp);total+=float(cp.get("distance",0.) or 0.)
        if diff:d["segments"]=new;d["length"]=round(total,3);changed.add(di)
        d["source_crs"]=edge_crs.authid();d["layout"]={"version":23,"engine":"OffsetCore","work_crs":work.authid(),"spacing_m":round(spacing,3),"fanout_control_distance_m":round(control_distance_m,3),"main_lane":"exactly_one_primary_slot0_per_edge","lane":"relative_position_continuity_group_outside","pole":"one_independent_cable_landing","corner":"continuous_offset","takeoff":"special_endpoint_only","fat":"owning_link_final_geometry"}
    moves,stats=_prepare_fat_moves(designs,fat_layer,edge_layer,work,fat_max_distance_m) if fat_layer is not None else ({},{"total":0,"skipped":0,"corner":0,"straight":0});endpoint_updates=_replace_fat_endpoints(designs,moves,edge_crs) if moves else 0;_validate(designs,edge_crs,work)
    summary={"changed_designs":len(changed),"changed_indices":sorted(changed),"spacing_m":spacing,"extra_length_m":round(extra,3),"version":23,"work_crs":work.authid(),"lane_allocator":"OffsetCore","priority":ordered,"slot_map":{str(k):int(v) for k,v in slot_map.items()},"corner_geometry":"continuous_offset","fanout_control_distance_m":control_distance_m,"fat_moves":moves,"fat_total":stats["total"],"fat_skipped":stats["skipped"],"fat_corner":stats["corner"],"fat_straight":stats["straight"],"fat_endpoint_updates":endpoint_updates,"fat_max_distance_m":float(fat_max_distance_m)}
    _log(f"[OffsetCore] links={len(ordered)}; changed={len(changed)}; spacing={spacing:.3f}m; control={control_distance_m:.3f}m; main=EXACTLY_ONE_SLOT0_PER_EDGE; relative=GROUP_CONTINUITY; ordinary_corner_control=NOT_USED; fat={stats['total']}; fat_skipped={stats['fat_skipped'] if False else stats['skipped']}")
    return summary
