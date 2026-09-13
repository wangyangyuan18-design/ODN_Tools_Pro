from pathlib import Path

code = Path("tools/boundary_corner_fat_fix.py.txt").read_text(encoding="utf-8")
exec(compile(code, "tools/boundary_corner_fat_fix.py.txt", "exec"), {})
