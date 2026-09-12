from pathlib import Path

p = Path('cable_offset_core.py')
s = p.read_text(encoding='utf-8')

# Targeted diagnostics only; no geometry/routing behavior is changed.
helper = '''\n\nFAT_TRACE_DESIGN = "DAR436_H1A1"\nFAT_TRACE_LINKS = {"L3", "L4"}\nFAT_TRACE_NODE = "N0020"\nFAT_TRACE_FAT = "FTTx DAR463_H1A1_CH3_ODP1"\n\ndef _fat_trace_focus(design, feature_name=""):\n    if not isinstance(design, dict):\n        return False\n    link = str(design.get("link", "")).upper().strip()\n    text = " ".join(str(design.get(k, "")) for k in ("name", "fdt", "link")).upper()\n    seq = " ".join(str(x) for x in (design.get("sequence", []) or [])).upper()\n    return (FAT_TRACE_DESIGN in text and link in FAT_TRACE_LINKS and\n            (FAT_TRACE_NODE in seq or FAT_TRACE_FAT in str(feature_name).upper()))\n\ndef _fat_trace_point(point):\n    try:\n        return f"({float(point.x()):.6f},{float(point.y()):.6f})"\n    except Exception:\n        return str(point)\n'''

if 'FAT_TRACE_DESIGN = "DAR436_H1A1"' not in s:
    anchor = 'LOG_TAG = "ODN_Tools_Pro / Cable Offset"\n'
    if anchor not in s:
        raise SystemExit('LOG_TAG anchor missing')
    s = s.replace(anchor, anchor + helper, 1)

# 1) Log the exact owning-link/fat-target decision.
needle = '    return nearest[2], anchor, {\n        "segment_index": nearest[1],\n        "distance": nearest[0],\n    }\n'
replacement = '''    if _fat_trace_focus(design, str(feature["Name"]) if feature.fields().indexOf("Name") >= 0 else ""):\n        _log(f"[FAT-TRACE][TARGET] link={design.get('link','')}; feature_id={feature.id()}; fat_name={feature['Name'] if feature.fields().indexOf('Name') >= 0 else ''}; seq_pos={position}; anchor={_fat_trace_point(anchor)}; segment={nearest[1]}; distance={nearest[0]:.6f}; target={_fat_trace_point(nearest[2])}")\n    return nearest[2], anchor, {\n        "segment_index": nearest[1],\n        "distance": nearest[0],\n    }\n'''
if needle not in s:
    raise SystemExit('fat target return anchor missing')
s = s.replace(needle, replacement, 1)

# 2) Log whether the move survives the FAT distance guard and its final coordinates.
needle = '        target_edge = _tp(target, work, edge_crs)\n        target_layer = _tp(target_edge, edge_crs, fat_layer.crs())\n'
replacement = '''        target_edge = _tp(target, work, edge_crs)\n        target_layer = _tp(target_edge, edge_crs, fat_layer.crs())\n        if _fat_trace_focus(design, str(feature["Name"]) if feature.fields().indexOf("Name") >= 0 else ""):\n            _log(f"[FAT-TRACE][MOVE-PREP] link={design.get('link','')}; feature_id={feature_id}; fat_name={feature['Name'] if feature.fields().indexOf('Name') >= 0 else ''}; current={_fat_trace_point(current)}; anchor={_fat_trace_point(anchor)}; target_edge={_fat_trace_point(target_edge)}; target_layer={_fat_trace_point(target_layer)}; anchor_distance={anchor_distance:.6f}; move_distance={hypot(current.x()-target.x(), current.y()-target.y()):.6f}; accepted=YES")\n'''
if needle not in s:
    raise SystemExit('fat move coordinate anchor missing')
s = s.replace(needle, replacement, 1)

# 3) Tag the reported design's segments and log final geometry endpoints.
needle = '        new_segments = []\n        total = 0.0\n        different = False\n'
replacement = '''        trace_design = _fat_trace_focus(design)\n        new_segments = []\n        total = 0.0\n        different = False\n'''
if needle not in s:
    raise SystemExit('pipeline anchor missing')
s = s.replace(needle, replacement, 1)

needle = '        for segment_index, segment in enumerate(design.get("segments", []) or []):\n            edge_count = len(segment.get("edge_sequence", []) or [])\n'
replacement = '''        for segment_index, segment in enumerate(design.get("segments", []) or []):\n            segment["_fat_trace_design"] = bool(trace_design)\n            segment["_fat_trace_segment_index"] = segment_index\n            edge_count = len(segment.get("edge_sequence", []) or [])\n'''
if needle not in s:
    raise SystemExit('pipeline segment anchor missing')
s = s.replace(needle, replacement, 1)

needle = '''            new_points = _geometry(\n                segment,\n                by_edge,\n                spacing,\n                work,\n                edge_crs,\n                control_distance_m,\n            )\n'''
replacement = needle + '''            if trace_design:\n                _log(f"[FAT-TRACE][FINAL-GEOMETRY] link={design.get('link','')}; segment={segment_index}; seq={design.get('sequence', [])}; slots={by_edge}; first={_fat_trace_point(new_points[0]) if new_points else 'None'}; last={_fat_trace_point(new_points[-1]) if new_points else 'None'}; corners={segment.get('_corner_decisions', [])}")\n'''
if needle not in s:
    raise SystemExit('geometry call anchor missing')
s = s.replace(needle, replacement, 1)

# 4) Log the critical special_end branch: FAT currently forces physical Pole endpoint.
needle = '''            if special_end:\n                add(b)\n            else:\n                add(_offset_lane_point(b, direction, slot, spacing))\n'''
replacement = '''            if special_end:\n                add(b)\n                if segment.get("_fat_trace_design"):\n                    _log(f"[FAT-TRACE][GEOMETRY-END] segment={segment.get('_fat_trace_segment_index')}; slot={slot}; special_end=TRUE; endpoint=PHYSICAL_NODE; node={_fat_trace_point(b)}")\n            else:\n                endpoint = _offset_lane_point(b, direction, slot, spacing)\n                add(endpoint)\n                if segment.get("_fat_trace_design"):\n                    _log(f"[FAT-TRACE][GEOMETRY-END] segment={segment.get('_fat_trace_segment_index')}; slot={slot}; special_end=FALSE; endpoint=OFFSET_LANE; node={_fat_trace_point(b)}; endpoint={_fat_trace_point(endpoint)}")\n'''
if needle not in s:
    raise SystemExit('geometry end anchor missing')
s = s.replace(needle, replacement, 1)

p.write_text(s, encoding='utf-8')
print('targeted FAT diagnostics patched')
