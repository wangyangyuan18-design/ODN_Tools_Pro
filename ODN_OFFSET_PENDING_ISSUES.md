# ODN Offset Core — 当前问题与解决记录

> 本文件用于记录 Offset Core 的问题、根因、解决方案以及后续测试基准。

## 问题 9：Lane / Cable Group 紧凑性与相对侧稳定

- Lane 是相对位置，不是永久绝对编号。
- 原有 `0,2,3` 出现空 Lane 时，不应长期保留松散的 `0,2,3`；应压缩为 `0,1,2`。
- Cable 离开 Group 后，剩余 Cable 应向主位方向压缩。
- 连续 Pole Edge 上，同一 Cable Group 的相对顺序和物理左右侧应保持稳定。
- Lane 重新编号不能导致 Cable 无工程原因从左侧跳到右侧，或从右侧跳到左侧。
- 对称示例：`-3,-2,0,+2,+4` 应压缩到约 `-2,-1,0,+1,+2`，同时保持左右关系。
- 当前 `_plan()` 已实现相对 Lane 压缩、物理侧稳定和连续性保护，后续继续用实际 QGIS 数据验证。

## 问题 10：多条蓝色 Cable 在非端点 Pole 共用同一根杆

- 现象：多条蓝色 Cable 在 Route 的非端点位置经过同一普通 Pole 时，可能出现多个独立 Cable 的几何真正落到同一个 Pole 节点。
- 正确规则：普通 Pole 只能由一条独立 Cable 真正落点/连接。
- 其它 Cable 可以在 Pole 附近以自己的 offset Lane 通过，但不能把几何点落到同一个普通 Pole。
- FDT、FAT Return、BB、SFC/CL Closure 等明确特殊节点可以按工程规则允许多 Cable 共点。
- 当前 Offset Core 已增加普通 Pole exclusivity 校验；后续仍需用实际数据验证。

## 问题 11：普通拐角出现“先向 Pole 延伸，再继续向外偏移，再反向拐入目标方向”

### 现象

典型场景：Cable 从上往下经过 Pole，在该 Pole 处向右或向左转。旧版本生成的几何有时表现为：

1. Cable 先向某一侧/目标 Pole 方向延伸；
2. 已经接近或到达 Pole 后，继续向该侧再延伸一段；
3. 然后才转入真正的目标方向；
4. 视觉上像“先拐到 Pole，再多走一段，再拐回来”。

### 根因

问题不是单一的 `0.225 m` 参数，而是旧 D/E/F Corner Geometry 都人为加入了纵向 `run_*` 控制距离，例如：

- `run_in`
- `run_out`
- `cross_run`
- `turn_run`
- `main_anchor`
- `main_hold`

这些距离会随 Lane magnitude 和 Edge 长度变化，因此才会出现约 `0.225 m`、`0.928 m`、`1.265 m`、`1.437 m` 等不同长度的回折/狗腿。

### 解决方案

普通 Corner 已统一到 `_unified_lane_corner()`：

`实际 Incoming Offset Lane Line + 实际 Outgoing Offset Lane Line → 求交点`

- 不再使用人为纵向 `run_*` 作为 Corner 的主要几何依据。
- 交点合理时直接使用交点。
- 平行、近似平行或交点过远时，只使用两个真实 Lane Anchor 做直接 bevel。
- `_same_lane_corner()`、`_early_turn_corner()`、`_main_reach_corner()`、`_cross_main_corner()` 保留原有决策入口，但统一委托给同一个 Corner Primitive。
- 不再通过“先回到 Pole / Main Lane，再向外走一段，再转回目标 Lane”的方式制造普通拐角。

### 当前状态

**问题 11 的主要根因已经解决。** 实际 QGIS 数据仍需继续验证极端角度、平行边和大 Lane magnitude 场景。

---

## 问题 12：FAT 未正确跟随 Owning Link 的最终 Offset Corner

### 现象

部分 FAT 在 Link Design 完成 Offset 后，没有移动到其所属 Link 的最终 Offset Corner，表现为：

- Link `L` 的 Cable Offset 已经生成正确 Corner；
- FAT 的 `Owning Link` 理论上也是 `L`；
- 但 FAT 实际位置仍停留在原始 Route / Pole 附近，或者没有准确落到 `L` 的最终 Offset Corner。

### 正确设计思路

当前认可的目标链路为：

`先生成 L 的正确 Offset Corner`

`        ↓`

`L 的 Corner 成为最终几何的一部分`

`        ↓`

`FAT 查询 Owning Link = L`

`        ↓`

`FAT 移到 L 的 Corner`

这个架构本身是正确的，不应该改成 FAT 自己重新计算一套 Offset。

### 本次排查策略

本问题暂不直接修改 FAT 几何算法，先做定点日志追踪，必须把同一个 FAT / Link 从 Offset Core 到 FAT 最终写入完整串起来。

需要重点记录以下 6 个检查点：

1. **Owning Link 判定**
   - FAT ID / Name
   - Owning Link ID / Link Name
   - FDT / Link
   - FAT 在设计数据中的 node / sequence 位置

2. **Link 最终 Offset Geometry**
   - Link ID
   - segment index
   - FAT 对应的 Route Node index
   - Corner 是否实际生成
   - Corner 类型
   - Corner 最终坐标

3. **FAT Corner 查询**
   - FAT ID
   - Owning Link
   - 查询到的 Corner 数量
   - 查询命中的 segment / node / vertex index
   - 命中坐标

4. **FAT 移动前后坐标**
   - 原始 FAT 坐标
   - Offset Corner 坐标
   - 移动距离

5. **最终 FAT 写入**
   - FAT ID
   - Owning Link
   - 最终写入坐标
   - 是否实际调用了 feature geometry 更新 / commit

6. **失败原因分类**
   - `NO_OWNING_LINK`
   - `LINK_NOT_FOUND`
   - `NO_OFFSET_CORNER`
   - `CORNER_QUERY_MISS`
   - `CORNER_COORD_EMPTY`
   - `MOVE_NOT_APPLIED`
   - `WRITE_NOT_COMMITTED`
   - `OK`

### 诊断目标

最终必须能够从一条日志直接回答：

`FAT → Owning Link → Link Segment → Corner → Corner Coordinate → FAT New Coordinate`

到底在哪一步断掉。

**在没有拿到这条完整链路之前，不修改 FAT 偏移算法。**
