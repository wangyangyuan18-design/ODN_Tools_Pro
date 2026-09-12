from pathlib import Path
import re

PATH = Path("cable_offset_core.py")
s = PATH.read_text(encoding="utf-8")

# 1. Replace trace selector with the real sequence_ids data model.
selector = '''FAT_TRACE_DESIGN = "DAR463_H1A1"
FAT_TRACE_LINKS = {"L3", "L4"}
FAT_TRACE_NODE = "N0020"
FAT_TRACE_FAT = "FTTx DAR463_H1A1_CH3_ODP1"
FAT_TRACE_EPS_M = 0.05


def _fat_trace_point(point):
    try:
        return f"({float(point.x()):.6f},{float(point.y()):.6f})"
    except Exception:
        return str(point)


def _fat_trace_feature_name(feature):
    try:
        index = feature.fields().indexOf("Name")
        return str(feature[index] or "") if index >= 0 else ""
    except Exception:
        return ""


def _fat_trace_item_text(item):
    if isinstance(item, (list, tuple)):
        return " ".join(str(x) for x in item).upper()
    return str(item).upper()


def _fat_trace_seq_match(item):
    return FAT_TRACE_NODE in _fat_trace_item_text(item)


def _fat_trace_link_match(design):
    if not isinstance(design, dict):
        return False
    link = str(design.get("link", "")).upper().strip()
    text = " ".join(
        str(design.get(k, ""))
        for k in ("name", "fdt", "link", "_link_id")
    ).upper()
    return FAT_TRACE_DESIGN in text and link in FAT_TRACE_LINKS


def _fat_trace_focus(design, feature_name=""):
    if not _fat_trace_link_match(design):
        return False
    return any(_fat_trace_seq_match(x) for x in (design.get("sequence_ids", []) or [])) or FAT_TRACE_FAT.upper() in str(feature_name).upper()
'''
start = s.index("FAT_TRACE_DESIGN =")
end = s.index("DEFAULT_SPACING_M =", start)
s = s[:start] + selector + s[end:]

# 2. Make diagnostics visible; do not swallow logging errors.
s = re.sub(
r"def _log\(message, level=Qgis\.Info\):\n\s+try:\n\s+QgsMessageLog\.logMessage\(str\(message\), LOG_TAG, level\)\n\s+except Exception:\n\s+pass\n",
"def _log(message, level=Qgis.Info):\n    QgsMessageLog.logMessage(str(message), LOG_TAG, level)\n",
s, count=1)

# 3. Add 01/02 immediately after FAT references are built.
needle = '''    moves = {}\n    stats = {\n        "total": len(references),\n        "skipped": 0,\n        "corner": 0,\n        "straight": 0,\n    }\n\n    for feature_id, reference in references.items():\n'''
insert = '''    moves = {}\n    stats = {\n        "total": len(references),\n        "skipped": 0,\n        "corner": 0,\n        "straight": 0,\n    }\n\n    for feature_id, reference in references.items():\n        feature = features.get(int(feature_id))\n        if feature is None:\n            continue\n        fat_name = _fat_trace_feature_name(feature)\n        if FAT_TRACE_FAT.upper() not in fat_name.upper():\n            continue\n        owner_index = int(reference.get("design_index", -1))\n        owner = designs[owner_index] if 0 <= owner_index < len(designs) else {}\n        _log(\n            f"[FAT-TRACE][01 DISCOVERED] fat={fat_name}; feature_id={feature_id}; "\n            f"expected_owner=L3; ref_design_index={owner_index}; "\n            f"ref_seq_pos={reference.get('sequence_pos')}"\n        )\n        _log(\n            f"[FAT-TRACE][02 OWNER] fat={fat_name}; owner={owner.get('link','')}; "\n            f"expected=L3; design={owner.get('_link_id', owner.get('name', ''))}"\n        )\n\n    for feature_id, reference in references.items():\n'''
if needle not in s:
    raise RuntimeError("01/02 anchor not found")
s = s.replace(needle, insert, 1)

# 4. Add 03 + per-segment trace metadata.
needle = '''        trace_design = _fat_trace_focus(design)\n        new_segments = []\n'''
insert = '''        trace_design = _fat_trace_link_match(design)\n        if trace_design:\n            seq = design.get("sequence_ids", []) or []\n            _log(\n                f"[FAT-TRACE][03 OFFSET-ENTER] link={design.get('link','')}; "\n                f"node={FAT_TRACE_NODE}; sequence_count={len(seq)}; "\n                f"node_positions={[i for i,x in enumerate(seq) if _fat_trace_seq_match(x)]}"\n            )\n        new_segments = []\n'''
if needle not in s:
    raise RuntimeError("03 anchor not found")
s = s.replace(needle, insert, 1)

needle = '''            segment["_fat_trace_design"] = bool(trace_design)\n            segment["_fat_trace_segment_index"] = segment_index\n'''
insert = '''            segment["_fat_trace_design"] = bool(trace_design)\n            segment["_fat_trace_segment_index"] = segment_index\n            if trace_design:\n                segment["_fat_trace_sequence_ids"] = list(design.get("sequence_ids", []) or [])\n                segment["_fat_trace_link"] = str(design.get("link", ""))\n'''
if needle not in s:
    raise RuntimeError("trace metadata anchor not found")
s = s.replace(needle, insert, 1)

# 5. Add 04 after original route nodes are resolved.
needle = '''    nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)\n    if len(nodes) != len(edges) + 1:\n'''
insert = '''    nodes = _base._extract_route_graph_nodes(segment, work, edge_crs, edge_crs)\n    if segment.get("_fat_trace_design"):\n        seq = segment.get("_fat_trace_sequence_ids", []) or []\n        seg_index = int(segment.get("_fat_trace_segment_index", -1))\n        for seq_pos, item in enumerate(seq):\n            if _fat_trace_seq_match(item):\n                local_index = seq_pos - seg_index\n                if 0 <= local_index < len(nodes):\n                    _log(\n                        f"[FAT-TRACE][04 ORIGINAL-NODE] link={segment.get('_fat_trace_link','')}; "\n                        f"segment={seg_index}; node={FAT_TRACE_NODE}; "\n                        f"node_index={local_index}; original={_fat_trace_point(nodes[local_index])}"\n                    )\n    if len(nodes) != len(edges) + 1:\n'''
if needle not in s:
    raise RuntimeError("04 anchor not found")
s = s.replace(needle, insert, 1)

# 6. Add 05 at every final-corner construction; only the traced node is logged.
needle = '''            corner_decisions.append(decision)\n            _log(\n                f"[corner] segment={segment.get('name', '')}; edge={edge_index}; "\n'''
insert = '''            if segment.get("_fat_trace_design"):\n                seq = segment.get("_fat_trace_sequence_ids", []) or []\n                seg_index = int(segment.get("_fat_trace_segment_index", -1))\n                node_positions = {pos - seg_index for pos, item in enumerate(seq) if _fat_trace_seq_match(item)}\n                if (edge_index + 1) in node_positions:\n                    _log(\n                        f"[FAT-TRACE][05 FINAL-CORNER] link={segment.get('_fat_trace_link','')}; "\n                        f"segment={seg_index}; node={FAT_TRACE_NODE}; edge={edge_index}; "\n                        f"decision={decision}; physical_node={_fat_trace_point(b)}; "\n                        f"corner={_corner_debug_points(corner_points)}"\n                    )\n                    segment["_fat_trace_final_corner"] = [\n                        [float(p.x()), float(p.y())] for p in corner_points\n                    ]\n            corner_decisions.append(decision)\n            _log(\n                f"[corner] segment={segment.get('name', '')}; edge={edge_index}; "\n'''
if needle not in s:
    raise RuntimeError("05 anchor not found")
s = s.replace(needle, insert, 1)

# 7. Add 06 without disturbing existing TARGET output.
needle = '''    if _fat_trace_focus(design, str(feature["Name"]) if feature.fields().indexOf("Name") >= 0 else ""):\n        _log(f"[FAT-TRACE][TARGET] link={design.get('link','')}; feature_id={feature.id()}; fat_name={feature['Name'] if feature.fields().indexOf('Name') >= 0 else ''}; seq_pos={position}; anchor={_fat_trace_point(anchor)}; segment={nearest[1]}; distance={nearest[0]:.6f}; target={_fat_trace_point(nearest[2])}")\n'''
insert = '''    fat_name = _fat_trace_feature_name(feature)\n    if _fat_trace_link_match(design) and FAT_TRACE_FAT.upper() in fat_name.upper():\n        source = "FINAL_ROUTE_SEGMENT"\n        target = nearest[2]\n        for seg_index, seg in enumerate(design.get("segments", []) or []):\n            corner = seg.get("_fat_trace_final_corner")\n            if not corner:\n                continue\n            distances = [\n                (hypot(float(x)-target.x(), float(y)-target.y()), i, x, y)\n                for i, (x, y) in enumerate(corner)\n            ]\n            if distances:\n                distance, i, x, y = min(distances)\n                if distance <= FAT_TRACE_EPS_M:\n                    source = f"FINAL_CORNER segment={seg_index}; corner_index={i}; corner=({float(x):.6f},{float(y):.6f}); distance={distance:.6f}m"\n        _log(\n            f"[FAT-TRACE][06 TARGET] link={design.get('link','')}; feature_id={feature.id()}; "\n            f"fat_name={fat_name}; route_segment={nearest[1]}; anchor={_fat_trace_point(anchor)}; "\n            f"target={_fat_trace_point(target)}; source={source}"\n        )\n'''
if needle not in s:
    raise RuntimeError("06 anchor not found")
s = s.replace(needle, insert, 1)

# 8. Add 07 reject + accept logs.
needle = '''        if target is None or anchor is None:\n            stats["skipped"] += 1\n            continue\n'''
insert = '''        if target is None or anchor is None:\n            if FAT_TRACE_FAT.upper() in _fat_trace_feature_name(feature).upper():\n                _log(f"[FAT-TRACE][07 MOVE] feature_id={feature_id}; link={trace_design.get('link','')}; accepted=False; reason=NO_TARGET_OR_ANCHOR; info={info}")\n            stats["skipped"] += 1\n            continue\n'''
if needle not in s:
    raise RuntimeError("07 reject anchor not found")
s = s.replace(needle, insert, 1)

needle = '''        if anchor_distance > max(0.01, float(fat_limit)):\n            stats["skipped"] += 1\n            continue\n'''
insert = '''        if anchor_distance > max(0.01, float(fat_limit)):\n            if FAT_TRACE_FAT.upper() in _fat_trace_feature_name(feature).upper():\n                _log(f"[FAT-TRACE][07 MOVE] feature_id={feature_id}; link={trace_design.get('link','')}; accepted=False; reason=ANCHOR_DISTANCE; anchor_distance={anchor_distance:.6f}; limit={float(fat_limit):.6f}")\n            stats["skipped"] += 1\n            continue\n'''
if needle not in s:
    raise RuntimeError("07 distance anchor not found")
s = s.replace(needle, insert, 1)

needle = '''        moves[feature_id] = {\n'''
insert = '''        if FAT_TRACE_FAT.upper() in _fat_trace_feature_name(feature).upper():\n            _log(f"[FAT-TRACE][07 MOVE] feature_id={feature_id}; link={trace_design.get('link','')}; accepted=True; move_distance={hypot(current.x()-target.x(), current.y()-target.y()):.6f}; target={_fat_trace_point(target)}")\n        moves[feature_id] = {\n'''
if needle not in s:
    raise RuntimeError("07 accept anchor not found")
s = s.replace(needle, insert, 1)

# 9. Add 08 exactly at updateFeature.
needle = '''            if not layer.updateFeature(feature):\n                raise RuntimeError(f"无法写入 FAT feature {feature_id}")\n            moved += 1\n'''
insert = '''            if not layer.updateFeature(feature):\n                raise RuntimeError(f"无法写入 FAT feature {feature_id}")\n            moved += 1\n            if FAT_TRACE_FAT.upper() in _fat_trace_feature_name(feature).upper():\n                _log(f"[FAT-TRACE][08 WRITEBACK] feature_id={feature_id}; written=True; target_layer={_fat_trace_point(target)}")\n'''
if needle not in s:
    raise RuntimeError("08 anchor not found")
s = s.replace(needle, insert, 1)

# 10. Existing final trace must use sequence_ids for truth.
s = s.replace("seq={design.get('sequence', [])}", "seq={design.get('sequence_ids', [])}", 1)

required = [
    '[FAT-TRACE][01 DISCOVERED]',
    '[FAT-TRACE][02 OWNER]',
    '[FAT-TRACE][03 OFFSET-ENTER]',
    '[FAT-TRACE][04 ORIGINAL-NODE]',
    '[FAT-TRACE][05 FINAL-CORNER]',
    '[FAT-TRACE][06 TARGET]',
    '[FAT-TRACE][07 MOVE]',
    '[FAT-TRACE][08 WRITEBACK]',
]
missing = [x for x in required if x not in s]
if missing:
    raise RuntimeError("missing markers: " + ", ".join(missing))

PATH.write_text(s, encoding="utf-8")
print("FAT trace patch applied: 8/8")
