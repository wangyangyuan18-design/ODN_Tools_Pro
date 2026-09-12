# ODN Offset Core — 当前待处理问题

> 本文件用于记录尚未最终修改的 Offset Core 问题，作为后续代码分析与测试基准。

## 问题 9：Lane / Cable Group 紧凑性与相对侧稳定

- Lane 是相对位置，不是永久绝对编号。
- 原有 `0,2,3` 出现空 Lane 时，不应长期保留松散的 `0,2,3`；应压缩为 `0,1,2`。
- Cable 离开 Group 后，剩余 Cable 应向主位方向压缩。
- 连续 Pole Edge 上，同一 Cable Group 的相对顺序和物理左右侧应保持稳定。
- Lane 重新编号不能导致 Cable 无工程原因从左侧跳到右侧，或从右侧跳到左侧。
- 对称示例：`-3,-2,0,+2,+4` 应压缩到约 `-2,-1,0,+1,+2`，同时保持左右关系。
- 当前版本已在 `_plan()` 中初步实现，后续继续用实际 QGIS 数据验证。

## 问题 10：多条蓝色 Cable 在非端点 Pole 共用同一根杆

- 现象：多条蓝色 Cable 在 Route 的非端点位置经过同一普通 Pole 时，可能出现多个独立 Cable 的几何真正落到同一个 Pole 节点。
- 正确规则：普通 Pole 只能由一条独立 Cable 真正落点/连接。
- 其它 Cable 可以在 Pole 附近以自己的 offset Lane 通过，但不能把几何点落到同一个普通 Pole。
- FDT、FAT Return、BB、SFC/CL Closure 等明确特殊节点可以按工程规则允许多 Cable 共点。
- 后续需要同时检查 Node Constraint Planner、Corner Geometry、最终 geometry validation，不能只依赖 route-level conflict detection。

## 问题 11：普通拐角出现“先向 Pole 延伸，再继续向外偏移，再反向拐入目标方向”

### 现象

典型场景：Cable 从上往下经过 Pole，在该 Pole 处向右或向左转。当前生成的几何有时表现为：

1. Cable 先向某一侧/目标 Pole 方向延伸；
2. 已经接近或到达 Pole 后，继续向该侧再延伸一段；
3. 然后才转入真正的目标方向；
4. 视觉上像“先拐到 Pole，再多走一段，再拐回来”。

### 当前初步代码定位

当前普通 Corner 由 `cable_offset_core.py` 中以下逻辑决定：

- `_corner_decision()`：根据 `prev_slot` 与 `next_slot` 选择 `SAME_LANE_TURN / EARLY_TURN / CROSS_MAIN_TURN / MAIN_REACH_TURN`。
- `_early_turn_corner()`：在到达 Pole 前开始转弯。
- `_main_reach_corner()`：先生成 `main_anchor`，再生成 `target_after`。
- `_cross_main_corner()`：生成 `incoming → main_hold → target` 三个控制点。
- `_same_lane_corner()`：通过两个 Lane offset line 的交点生成 Corner。
- `_geometry()`：把 Corner points 加入最终 Cable geometry。

当前需要重点分析 `_main_reach_corner()` 与 `_cross_main_corner()` 是否在某些方向组合下人为生成了不必要的“向 Pole 延伸/超过 Pole 后再转向”的控制点，以及 `_geometry()` 对原始端点的 `add(old[0]) / add(old[-1])` 是否造成额外回拉。

### 当前分析结论

先不要修改代码。下一步应使用实际问题路线的 `nodes + edge_sequence + prev_slot + next_slot` 逐步追踪：

`_corner_decision → corner geometry → add(points) → final geometry`

重点确认每一个控制点相对于 Pole 的方向和距离，再决定是修改 Corner Decision、Corner Geometry，还是最终 Geometry 拼接，而不是凭视觉现象直接调整 run 系数。

### 目标规则

- 普通 Corner 不应把 Cable 当成“必须先落 Pole 再继续”的线路。
- 普通非 0 Lane 不应为了转角人为回到物理 Pole。
- 转角应由两个实际 Pole Edge 的方向、目标 Lane 和 0.50 m spacing 直接形成连续 offset geometry。
- 只有真正的 Main Lane 物理连接点才允许落到普通 Pole。
- 不使用 0.30 m special takeoff 处理普通 Corner。
