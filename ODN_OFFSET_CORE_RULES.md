# ODN Offset Core — 1~8 设计规则与示意图说明

> 本文件是 Offset Core 的实现基准。它把此前 1~8 号问题与本次 L1~L6 示意图说明合并记录。代码不得通过局部经验规则重新解释这些要求。

## 1. 图纸与对象

- 统一从图纸上方向下方向阅读。
- 圆形 = Pole / Pole Edge 节点。
- 长方形 = FAT。
- FDT = 左侧带文字的方块。
- 黄色、绿色等颜色只是用于区分 Link，不代表图层类型。
- Link 编号是业务标识，不等于 Lane 编号。

## 2. 完整 Route 优先于局部 Edge

Offset 必须先取得每条 Link 的完整 Pole Edge Route，再做主线和 Lane 规划；不能每条 Edge 独立选择左右偏移。

主线选择优先级：
1. 连续方向上的实际经过长度；
2. 连续性；
3. 尽量少转弯；
4. 尽量少反向/回头；
5. 尽量少干扰其它 Cable；
6. 满足工程约束后再比较总长度。

### L1~L6 示意图的核心判断

- L4 占横向主位，因为它在横向连续走线中经过的长度最长。
- 向上方向虽然 L1 比 L2 长，但 L1 后续向右转、L3 也向右转；若让 L1 占向上主位，连续运行中更容易与其它线路发生切换/交叉，视觉和工程布线都较差。
- 因此向上主位选择 L2。
- L1 若占主位并非绝对错误，只是不作为强制禁用条件；算法应把连续性、转弯、干扰作为综合优先级。

## 3. Lane 与距离

- 普通相邻 Cable Lane 间距固定为 **0.50 m**。
- 横向、纵向以及普通连续转角均保持 0.50 m 的实际偏移关系。
- Lane index 是相对位置：0 为主位，+1/+2/+3 与 -1/-2/-3 依次相隔 0.50 m。
- 不能因为道路方向改变而把同一 Cable 任意换到另一绝对地图侧。

## 4. 相对位置连续与 Cable Group

共同 Route 上已经存在的 Cable 形成临时 Cable Group：

- 已有 Cable 保持相对顺序。
- 新 Cable 加入 Group 时从进入侧的外侧加入，不插入已有 Cable 中间。
- 已建立的 Lane 尽量连续保持，例如 0→0、+1→+1。
- Lane Change 只有在真实冲突、路线分叉、目的方向改变、线路停止/分离或特殊工程节点要求时才允许。
- 不允许无工程原因的 +1→-1→+1、0→+1→0 等往复横跳。

## 5. A 点与 0.30 m

A 点表示特殊同点出线的控制位置：

- **0.30 m 只属于特殊同点出线/回缆控制。**
- 普通 Pole Corner、普通 Lane Change、普通连续转角绝不使用 0.30 m。
- 同一点多 Cable 从共同出口展开到 0.50 m 平行 Lane 时，使用 0.30 m control distance。
- 0.50 m offset 与 0.30 m control distance 自然形成 takeoff 角度，不硬编码 90°。

允许使用 0.30 m 的节点：
- FDT 出线；
- FAT Return；
- BB；
- SFC/CL Closure；
- 其它明确标记为 same-point multi-cable output 的工程节点。

## 6. D / E / F 普通拐角规则

普通拐角不要求固定 90°，而是优先与原 Pole Edge 的路线方向平行并保持 0.50 m 偏移。

### D

- 如果拐弯后的 Route 更接近该 Cable 的出发侧，可以在尚未到达主线之前开始拐弯。
- 目标是保持与主线 0.50 m 间距，并减少不必要的横向穿越。

### E

- 如果后续 Route 需要占据主线另一侧/主位，应先通过主线，再完成转弯。
- 转弯过程中仍保持与主线 0.50 m 的工程间距。

### D~E 之间黄色线

- 其转角与 F 点属于同一类判断。
- 因为接下来的 Route 占据主线，所以先到达主线对应的上/下位置，再转角。

### F

- 普通连续转角直接根据最终 Lane 做 offset join。
- 不进入 Pole 后再返回。
- 不因为 FAT 靠近某个 Pole 就改变 Cable Route。

## 7. 普通 Pole 与特殊节点

### 普通 Pole

- 一个普通 Pole 节点只能被一条独立 Cable 真正落点/连接。
- 第二条 Cable 可以在 Pole 附近保持自己的 offset Lane 通过，但不能把几何点落到同一个普通 Pole。
- 普通 Pole 上不存在其它 Cable 时，Cable 应直接连接 Pole。
- 不允许为了非 0 Lane 人为制造 0.30 m takeoff 再回到 Pole。

### 特殊节点

FDT、FAT Return、BB、SFC/CL Closure 等可允许多 Cable 同点。

特殊节点必须显式识别，不能用“附近距离小”之类模糊条件把普通 Pole 变成共享节点。

## 8. 回缆 B 点

B 点是 Return Cable 场景：

1. Cable 先正常到达目标点；
2. 到达端保持正常直接连接；
3. Return 从同一点重新出发；
4. Return 根据目的地决定向上还是向下展开；
5. Return 出线属于特殊同点出线，可使用 0.30 m control distance；
6. Return 与本线路来时 Route 保持 0.50 m；
7. 再沿目标方向继续。

Return 不能被当作普通 Corner，也不能把 0.30 m 应用于整个普通路线。

## 9. FAT 与 Cable 完全解耦

FAT 是所属 Link 的工程节点，而不是 Cable 的强制几何控制点。

处理顺序必须是：

**Link ownership → complete Route → main/Lane → final Cable geometry → FAT landing**

因此：

- Yellow FAT 只影响 Yellow 所属 Link。
- Green Link 经过附近 Pole/FAT 时不得被 Yellow FAT 拉过去。
- 如果 FAT 实际由 L6 连接，即使它原本位于第四根 Pole 附近，也应在 L6 最终 Cable 的拐点/落点上跟随偏移。
- FAT 不得同时属于两个独立 Link。
- 同一 Link 的 Forward/Return 可以在 FAT 工程点形成允许的特殊共点。

## 10. 拐角几何

- 普通 Corner 使用连续 offset geometry。
- 同一 Lane 在 Pole Edge 转角处采用 offset-line intersection / miter，必要时 bevel fallback。
- 不把原 Pole 节点作为普通 offset Cable 的“进杆再出杆”中间点。
- 角度不固定 90°；优先由实际 Pole Edge 方向、Lane 间距和连续几何自然形成。
- 0.30 m 不得出现在普通 Corner。

## 11. 主线与 Pole 的连续使用

- 主线一旦由完整 Route 判断为最佳通道，应尽量连续使用。
- 如果 slot 0 无真实冲突、无既有 Cable 占用且无工程节点限制，优先 slot 0。
- 不允许因为局部 Edge 有其它空 slot 就放弃主线。
- 不允许出现主线→非主线→主线的无理由往复。
- 相邻 Pole 之间已有约 1 m 的有效空间时，应优先使用连续主通道。

## 12. 问题 1~8 的统一处理顺序

1. 读取完整 Link Route。
2. 计算 Route priority / longest directional run / turn / reversal。
3. 决定主 Route / slot 0。
4. 按完整 Route 顺序规划 Lane。
5. 保持已有 Cable Group 相对顺序。
6. 新 Cable 从 Group 外侧加入。
7. 检查普通 Pole 独占与特殊节点例外。
8. 根据 Lane 生成连续 Cable geometry。
9. 普通 Pole 直接 landing；特殊同点输出才使用 0.30 m。
10. 最后根据 owning Link 的 final Cable geometry 做 FAT landing。
11. 对最终结果执行 CRS、拓扑、异常长度和规则验证。

## 13. Offset Core 架构要求

Offset 不再继续使用 v5→v3→v2→v9→v8 的运行时 monkey-patch 链。

唯一主流程：

```text
Link Design
  ↓
Route Priority Planner
  ↓
Lane Planner
  ↓
Node Constraint Planner
  ↓
Continuous Geometry Builder
  ↓
FAT Landing
  ↓
Final Distribution Cable geometry
```

所有距离和偏移计算必须在一个米制 projected CRS 中完成；原始 Pole Edge `edge_sequence` 是权威拓扑，Offset 不能改变原始路由拓扑。

## 14. 验收标准

一次完整测试必须同时检查 1~8：

- L4 横向主位符合完整 Route 连续性判断；
- L2 向上主位符合转弯/干扰综合判断；
- 相邻 Cable 普通间距 0.50 m；
- 普通 Corner 连续，不出现进 Pole→出 Pole；
- 普通 Pole 不被两个独立 Link 同时真正落点；
- 普通非 0 Lane 不自动触发 0.30 m；
- 特殊同点出线使用 0.30 m → 0.50 m；
- Return B 点先正常到达，再特殊 takeoff 返回；
- FAT 只跟随 owning Link 的 final geometry；
- FAT 不迫使其它 Link 改线；
- 新 Cable 从 Cable Group 外侧加入；
- 不出现无原因的 Lane 往复横跳；
- 主线不应无原因空置；
- 最终 geometry 使用统一米制 CRS 并通过异常几何检查。
