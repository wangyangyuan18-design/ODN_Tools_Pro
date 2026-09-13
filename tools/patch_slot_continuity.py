from pathlib import Path

path = Path("cable_offset_core.py")
text = path.read_text(encoding="utf-8")

old = '''    reserved_cache = {}\n    slots = {}\n    uses_by_edge = {}\n    for use in pending:\n        uses_by_edge.setdefault(use.edge_key, []).append(use)\n    for uses in uses_by_edge.values():\n        uses.sort(key=lambda use: (rank.get(use.design_index, 999999), use.segment_index, use.edge_index))\n'''

new = '''    reserved_cache = {}\n    slots = {}\n    uses_by_edge = {}\n    segment_edge_counts = {}\n    for design_index, design in enumerate(designs or []):\n        for segment_index, segment in enumerate(design.get("segments", []) or []):\n            edges = [\n                e\n                for raw in segment.get("edge_sequence", []) or []\n                if (e := _base._canonical_edge(raw))\n            ]\n            segment_edge_counts[(design_index, segment_index)] = len(edges)\n    for use in pending:\n        uses_by_edge.setdefault(use.edge_key, []).append(use)\n    for uses in uses_by_edge.values():\n        uses.sort(key=lambda use: (rank.get(use.design_index, 999999), use.segment_index, use.edge_index))\n'''

if old not in text:
    raise SystemExit("ERROR: planner anchor block not found")
text = text.replace(old, new, 1)

old = '''    def previous_slot(use):\n        if use.edge_index <= 0:\n            return None\n        return slots.get((use.design_index, use.segment_index, use.edge_index - 1))\n'''

new = '''    def previous_slot(use):\n        if use.edge_index > 0:\n            return slots.get((use.design_index, use.segment_index, use.edge_index - 1))\n\n        # A route may be split into multiple segments at FAT/FDT boundaries.\n        # The first edge of the next segment is still the continuation of the\n        # same cable, so inherit the last assigned lane from the immediately\n        # preceding non-empty segment.  This affects lane continuity only;\n        # final corner geometry remains responsible for the Physical Pole.\n        previous_segment = use.segment_index - 1\n        while previous_segment >= 0:\n            count = segment_edge_counts.get((use.design_index, previous_segment), 0)\n            if count:\n                return slots.get((use.design_index, previous_segment, count - 1))\n            previous_segment -= 1\n        return None\n'''

if old not in text:
    raise SystemExit("ERROR: previous_slot anchor not found")
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
