from pathlib import Path

p = Path('cable_offset_core.py')
s = p.read_text(encoding='utf-8')
needle = 'def _lane_offset_line(node, direction, slot, spacing):\n'
first = s.find(needle)
second = s.find(needle, first + len(needle))
if first < 0 or second < 0:
    raise SystemExit('expected duplicate unified corner block not found')
# Keep the second, latest unified implementation and remove the first duplicate block.
s = s[:first] + s[second:]
p.write_text(s, encoding='utf-8')
