# ODN 线路与偏移设计规则

> 这是 Link Design / Distribution Cable 偏移算法的长期规则记录。后续讨论和代码修改以本文件为准；已经确认的规则不要因为后续调试而遗忘或重新解释。

## 0. 图纸阅读与问题编号

- 统一把图纸视为 **从上往下看**。
- 图片中的蓝色小点 = **Pole Edge 节点/电杆节点**。
- 黄色、绿色等颜色只是为了对照某一条 Link/Cable，不代表图层类型；除非明确说明，不把黄色线理解为 Pole Edge。
- 后续问题按图片编号讨论：1、2、3……；同一问题中的线路用“黄色1、绿色1、绿色2”等称呼，便于对照。

## 1. 普通 Pole 节点不得被两条独立光缆同时占用

- 一个普通 Pole Edge 节点，正常情况下只允许 **1 条独立 Cable** 通过/连接。
- 如果另一条 Cable 与已占用的普通 Pole 节点发生共点，必须重新选择 Lane/过渡位置，不能让两条独立 Cable 都落到同一个杆点。
- 允许多缆共点的特殊工程节点：FDT、FAT Return、BB、SFC/CL Closure。
- 新增特殊节点类型必须显式加入白名单，不能靠模糊几何条件放宽普通 Pole 限制。

## 2. 相邻并行光缆 Lane 间距

- 普通相邻 Cable Lane 的物理间距 = **0.5 m**。
- 0.5 m 是普通并行路线的核心偏移距离。
- 不能为了处理普通转角而把 0.5 m 改成 0.3 m。

## 3. 0.3 m 的适用范围

- **0.3 m 不是普通 Pole Edge 转角参数。**
- 普通 Lane 转换、普通直角/折角，不使用 0.3 m control distance。
- 0.3 m 只用于**同一点、多缆进出/分叉/回流**等特殊几何控制，例如 FDT 出线、BB、SFC/CL、FAT Return。
- 多 Cable 从同一个特殊出线口出发并需要建立 0.5 m 平行间距时，从共同出线点开始采用 **0.30 m 控制距离 + 0.5 m 实际偏移**形成 takeoff。
- 单条 0.5 m 偏移时 takeoff 约 59°；角度由几何自然形成，不硬编码。

## 4. Route 选择不是简单最短路

路线优先级综合：
1. 连续方向上的实际经过长度；
2. 尽量减少不必要的拐弯；
3. 尽量减少回头/反向，尤其明显的 135°+ reversal；
4. 尽量减少对其他线路和已有 Lane 的干扰；
5. 满足工程约束后再比较总路线长度。

## 5. 主线与 Lane 必须按完整 Route 判断

- Link 编号是业务标识，不等于几何 Lane 编号。
- 几何处理考虑完整 Route 在多个 Pole Edge 上的连续性。
- 一条 Route 在连续道路段上应保持稳定 Lane，不允许每个 Edge 独立重新决定左右偏移。
- **主线不是“某个绝对方向的线”，而是完整 Route 中连续性最好、转弯/回头最少、干扰最小的几何通道。**
- 当主 Lane（slot 0）没有真实冲突且没有既有工程占用时，应优先使用主 Lane，不能为了保持某个非 0 偏移而故意空着主线。

## 6. 普通转角必须连续

- Cable 应沿原始 Pole Edge Route 做连续偏移。
- 同一 Lane 穿过 Pole 节点转角时，不能回到原始 Pole 节点再重新起步，也不能人为插入 0.3 m 普通控制段。
- 普通转弯直接完成几何转向，不能出现“进杆 → 拐出来”的假回路。
- 如果尖角导致 miter 不合理，可采用不回穿原始节点的 bevel 等连续几何处理。
- 共享出线口 takeoff 是唯一的特殊例外：从同一点以 0.30 m 控制距离展开到目标 0.5 m Lane。

## 7. 相对位置连续（问题2）

### 7.1 基本原则
- 光缆在连续共同路由上，应尽量保持与其他光缆的**相对位置**，而不是固定某个绝对 XY 方向。
- 例如从左往右时，绿色1开始位于黄色1下方 0.5 m；共同向上转弯后，绿色1继续保持黄色1的相对右侧 0.5 m；再共同向左转后，绿色1自然变成黄色1上方 0.5 m。
- Lane index 表示相对于当前 Route 方向的几何位置；道路方向变化不能导致 Cable 随意换边。

### 7.2 允许 Lane Change 的条件
只有存在明确工程/几何原因才允许改变相对位置：
- 某段路线停止、结束或分离；
- 两条路线后续拐弯方向不同；
- 当前 Lane 发生真实冲突；
- 目的方向明确要求另一侧；
- 新 Cable 加入共同路由，需要在既有 Cable Group 外侧增加 Lane。

不允许仅因为“当前 Edge 上另一个 slot 空着”就改变已经稳定的 Cable 相对位置。

### 7.3 Lane 关系
以左右方向道路为例：主线 0；上方 +1、+2、+3；下方 -1、-2、-3；相邻 Lane 间距 0.5 m。

## 8. 新 Cable 加入共同路由不得插入 Group 中间

- 已经共同运行的 Cable 视为一个临时 Cable Group。
- 新 Cable 从进入侧加入时，放到 Group 的最外侧。
- 不因为 Lane 编号有空位就插入已有 Cable 中间。
- Group 内部已有 Cable 的相对顺序优先保持。

## 9. 问题3：同一出线口的共同路由 takeoff

- 两条 Cable 从同一特殊出线点出发时，主线可保持 slot 0，第二条 Cable 从共同点使用 **0.30 m takeoff** 展开到 0.5 m 平行 Lane。
- takeoff 后严格保持 0.5 m。
- 这不是普通 Pole 转角规则。

## 10. 问题4：普通 Pole 直连与 Return takeoff

- 普通 Pole 没有其他 Cable 使用时，Cable 应**直接进入/离开 Pole**。
- 不允许为了得到非 0 Lane 而在空 Pole 前先做一个 0.30 m 斜切再回到路线。
- Return Cable 到达同一点时：到达端正常直连；从同一点再次出发的 Return 段属于特殊同点出线，可使用 0.30 m takeoff，再建立 0.5 m 平行 Lane。
- 因此必须区分 `NORMAL_ENDPOINT` 与 `SPECIAL_TAKEOFF_ENDPOINT`，不能仅用 `slot != 0` 判断是否 takeoff。

## 11. 问题5：Cable 几何、FAT Link 归属、FAT 偏移必须分离

问题5由三个独立规则组成：

### 11.1 线的拐弯方式
- 当前 Link 应首先按照自己的 Route/Lane **正常直接转弯**。
- 不能因为附近存在 FAT 或 FAT 所在 Pole，就“拐进杆子 → 再拐出来”。
- 普通转弯仍使用连续 0.5 m offset geometry。

### 11.2 FAT 不能同时属于/经过两条 Link
- FAT 是明确归属于某一个 Link/Sequence 的工程节点。
- FAT 所属 Link 才可以与该 FAT 建立连接。
- 其他 Link 即使经过同一 Pole 或空间位置接近，也**不得为了 FAT 改变自己的 Lane 或拐进该 Pole**。
- 因此“FAT 看起来在两条 Cable 中间”不能作为两条 Cable 共用 FAT 的理由。

### 11.3 FAT 偏移
- 如果 FAT 属于绿色 Link：先生成绿色 Link 的正确连续偏移几何，再根据最终绿色 Cable 的实际拐点/落点移动 FAT。
- 不允许先固定 FAT 在原始 Pole，再反过来扭曲 Cable 去迎合 FAT。
- 如果 FAT 属于黄色 Link，绿色 Link 完全忽略该 FAT，不得因为该 FAT 改线。
- FAT landing 的数据来源必须限定为**所属 Link 的最终 Cable geometry**。

## 12. 问题6+7：主线优先、连续通过 Pole、禁止往复横跳

问题6和问题7是连续的一段规则，重点是解决“主线空着、Cable 反复绕到非主线、再回主线”的问题。

### 12.1 空主线必须优先
- 如果 slot 0 没有真实冲突、没有既有 Cable 占用、也没有工程节点限制，**优先把主 Cable 放在 slot 0**。
- 不能因为另一条 Cable 的进入方向或某个局部空 slot，就让主 Cable 放到 +1/+2，而把 slot 0 空着。
- 图中如果主线没人使用且相邻杆之间已有约 1 m 的空间，说明存在可用主通道；算法应优先利用该主通道。

### 12.2 一个 Pole 只允许一条独立 Cable 真正落点
- 如果某个普通 Pole 已经被一条 Cable 占用，第二条独立 Cable 不得同时落到该 Pole。
- 第二条 Cable 应保持自己的 Lane 连续通过该区域，而不是“进 Pole → 再退出”。

### 12.3 已经进入主线的 Cable 要尽量持续走主线
- 主线一旦被完整 Route 判断为最佳连续通道，就应尽量持续。
- 不允许出现：
  `主线 → 非主线 → 主线 → 非主线 → 主线`。
- 这种往复横跳只有在明确的真实冲突、路线分叉、目的方向变化或工程节点要求时才允许。

### 12.4 Pole 间距不能成为虚假 Lane Change 理由
- 相邻 Pole 间如果已有约 1 m 的有效通行空间，应优先利用现有连续主通道。
- 不能仅因为两条 Cable 的局部空间关系，就主动把一条 Cable 从主线挪到旁边再挪回来。

## 13. 问题8：多 Cable 综合几何规则

问题8是问题1～7的综合场景，算法必须按照以下优先级统一处理：

1. **先确定完整 Link Route 和主线。**
2. **主线 slot 0 有空位且无真实冲突时优先占用。**
3. 对已经形成的 Cable Group，保持相对顺序和相对 Lane。
4. 新 Cable 加入 Group 时从外侧加入，不插入中间。
5. 普通 Pole 节点只允许一条独立 Cable 真正落点。
6. FAT 只影响自己的所属 Link，其他 Link 不得被 FAT 拉偏。
7. 普通转角直接连续转弯，不做“进杆再出来”。
8. 同点特殊节点才使用 0.30 m takeoff。
9. 普通并行间距保持 0.50 m。
10. 只有真实冲突/分叉/目的方向变化才允许 Lane Change。
11. Lane Change 应尽量少，并且不能形成无工程原因的 `+1 → -1 → +1` 或类似反复横跳。

### 13.1 多 Cable 过同一杆的判断
- “几何上经过 Pole 附近”与“Cable 真正连接 Pole 节点”必须分开判断。
- 一条 Cable 可以保持自己的偏移 Lane 经过该 Pole 的附近，但不能把几何点落到已被另一独立 Cable 使用的普通 Pole 节点。
- 特殊节点才允许多个 Cable 共点。

### 13.2 拐角判断
普通拐角不根据“当前 slot 是否非 0”决定是否使用 takeoff；必须根据端点类型和完整 Route 判断：
- `NORMAL_ENDPOINT`：直接连接 Pole；
- `NORMAL_CORNER`：连续 0.5 m offset 转弯；
- `SPECIAL_SHARED_OUTPUT`：0.30 m takeoff → 0.50 m parallel；
- `RETURN_OUTPUT`：0.30 m takeoff → 0.50 m parallel。

## 14. 算法实现原则（问题1～8统一）

### 14.1 两阶段模型
算法必须逻辑上分为：

**A. Route / Lane Planning**
- 根据完整 Route 计算方向连续性、转弯、回头、冲突；
- 决定主线和相对 Lane；
- 处理普通 Pole 独占和 Cable Group；
- 不让 FAT 原始位置强制改变其他 Link。

**B. Geometry / Node Landing**
- 根据已经确定的 Lane 生成最终 Cable geometry；
- 普通端点直接落 Pole；
- 普通角连续 offset；
- 特殊同点出线使用 0.30 m takeoff；
- 最后让所属 Link 的 FAT 跟随最终 Cable geometry。

不能把 FAT、Pole 原始位置、局部 slot 空缺混在一起作为同一个决策。

### 14.2 当前实现
- `cable_offset_layout_v10.py`：新的完整 Route 顺序 Lane allocator；slot 0 可用时优先；保持连续 Lane；新 Cable 从 Group 外侧加入；保留普通 Pole 独占。
- `cable_offset_layout_v8.py`：连续 offset geometry 基础实现。
- v10 对 v8 的 endpoint decision 做显式覆盖：普通 Pole endpoint 直接连接，只有明确特殊节点才调用 0.30 m takeoff。
- `cable_offset_fat_v2.py`：FAT landing 使用最终 Cable geometry；FAT 归属必须由 owning Link 决定。
- `cable_offset_layout_v5.py`：当前入口已切换到 v10 allocator。

## 15. 测试验收标准

每次修改后至少检查以下 8 类图形：

1. **问题1**：普通 Pole 不出现两条独立 Cable 共点；主线连续。
2. **问题2**：共同路由不来回换边；相对位置保持；新 Cable 从 Group 外侧加入。
3. **问题3**：同点共享出线有 0.30 m takeoff，之后保持 0.50 m。
4. **问题4**：空 Pole 直接连接；Return 出线才使用特殊 takeoff。
5. **问题5**：普通线直接转弯；FAT 只属于一个 Link；FAT 跟随 owning Link 最终拐点。
6. **问题6+7**：空主线优先使用；Cable 不因为杆点而进出往复；不出现无理由主线/旁线来回跳。
7. **问题8**：多 Cable 综合场景中不共用普通 Pole、不因 FAT 互相拉偏、不反复横跳，所有普通相邻 Lane 保持 0.50 m。
8. **ODN 2.1**：BB / SFC Closure 等特殊节点仍遵守特殊节点 0.30 m takeoff 和实际 Pole/Closure 拓扑规则。

## 16. 代码修改记录

- v9：全局 Route priority、相对 Lane continuity、普通 Pole exclusivity。
- v8：连续 offset corner、特殊同点 takeoff。
- FAT v2：FAT landing 跟随最终 owning-Link Cable geometry。
- **v10：针对问题6/7/8重新建立 Route-sequential allocator，并修正“主线空着”的分配问题；同时将普通端点与特殊 takeoff 明确区分。**
- v10 提交：`c349d98dad97cc0467031521c4fde9461d15276f`。
- v5 接入 v10 提交：`1273f54767ff3d6e3027334ca3114256ed43a788`。
