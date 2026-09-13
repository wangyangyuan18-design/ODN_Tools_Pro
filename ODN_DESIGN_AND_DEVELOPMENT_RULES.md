# ODN Tools Pro — 设计原则、开发规范与 Build 事故记录

> 本文件用于记录 ODN Tools Pro 的长期设计原则、开发规范，以及已经发生并确认根因的问题。
> 现有几何规则仍以 `ODN_DESIGN_RULES.md`、`ODN_DESIGN_SPEC.md`、`ODN_OFFSET_CORE_RULES.md` 为正式规则来源；本文件重点补充“如何开发、验证、打包、诊断”，防止后续调试再次破坏已经确认的设计原则。

---

## 1. 已确认的 Link Design 核心设计原则

### 1.1 Offset Core 唯一化

- `cable_offset_core.py` 是 Offset Core 的唯一真正执行入口。
- Route Priority、Main Lane、Relative Lane Continuity、Group Outside、Pole Exclusivity、普通 0.5 m 间距、特殊 0.3 m takeoff、Continuous Corner、FAT → Owning Link Final Geometry 等核心规则必须在统一 Core 中执行。
- 不允许重新建立另一套平行的 offset 算法，也不应继续通过 `v2/v3/v4` 等临时版本复制核心逻辑。
- 外层 UI / Link Design 只负责准备数据、调用 Core、显示结果和写回图层；不能偷偷实现第二套几何规则。

### 1.2 0.5 m 与 0.3 m 必须严格区分

- 普通相邻 Cable Lane 的核心间距为 **0.5 m**。
- **0.3 m 不是普通转角参数**。
- 0.3 m 只用于同一点多缆进出、FDT/BB/SFC/CL/FAT Return 等特殊出线 takeoff。
- 普通 Pole Corner 必须连续偏移，不得通过“进杆 → 再拐出”制造普通转角。

### 1.3 主线与相对位置连续

- 主线 slot 0 在无真实冲突、无既有工程占用时应优先使用。
- Cable 在连续共同 Route 上应保持相对位置和 Lane 连续性。
- 新 Cable 加入已有 Cable Group 时，应从 Group 外侧加入，不得因为 slot 空缺而插入已有 Cable 中间。
- 无真实工程原因时，不允许形成 `+1 → -1 → +1` 等无意义 Lane 横跳。

### 1.4 Pole Exclusivity

- 普通 Pole Edge 节点正常情况下只允许 1 条独立 Cable 真正落点。
- 第二条独立 Cable 即使经过同一 Pole 附近，也不能因为局部几何方便而同时落到该普通 Pole。
- FDT、FAT Return、BB、SFC/CL Closure 等特殊工程节点必须显式列入允许多缆共点的白名单。

### 1.5 FAT 与 Cable Geometry 分离

- FAT 必须明确归属于某一个 Owning Link / Sequence。
- 其他 Link 不得为了迎合该 FAT 而修改自己的 Lane 或转角。
- 正确顺序是：先得到所属 Link 的最终连续 Cable Geometry，再根据该最终 Geometry 决定 FAT 的 Target / Move。
- FAT 不是反过来扭曲 Cable Route 的控制点。

### 1.6 Corner / Target / Writeback 必须可追踪

对于问题 FAT，诊断至少必须能够完整回答：

`FAT → Owner → Offset Enter → Original Node → Final Corner → Target Source → Move Decision → Writeback`

不得只记录“FAT 有进入处理”这种无法定位根因的半链路日志。

---

## 2. Link Design 调试规范

### 2.1 先定位，后修改算法

当出现单个 FAT、Node、Corner 或 Lane 异常时：

1. 先增加最小范围的全链路诊断；
2. 用日志确认实际 Owning Link、Sequence、Node、Final Corner、Target 和 Writeback；
3. 根因明确后再修改算法；
4. 调试阶段不得同时修改已确认正确的 Corner / Offset Core 规则。

### 2.2 FAT Trace 采用固定 8 点链

当前标准诊断链：

1. `DISCOVERED` — FAT 是否被发现；
2. `OWNER` — 实际 Owning Link；
3. `OFFSET-ENTER` — 对应 Link 是否真正进入 Offset Core；
4. `ORIGINAL-NODE` — 原始 Node 坐标；
5. `FINAL-CORNER` — 最终 Corner 坐标；
6. `TARGET` — FAT Target 来源；
7. `MOVE` — Move 接受/拒绝及原因；
8. `WRITEBACK` — 最终写回图层的坐标。

### 2.3 Trace Selector 必须使用真实数据结构

- Offset Core 当前使用的是 `sequence_ids`，不能重新使用已经淘汰的 `sequence` 字段作为 FAT Trace 的选择依据。
- 任何诊断 selector 修改，都必须以当前生产数据结构为准并经过实际运行验证。

### 2.4 日志异常不得被静默吞掉

- 诊断阶段 logger 不得使用 `try/except: pass` 隐藏关键错误。
- 日志写入失败本身就是需要暴露的问题，否则会出现“没有日志”与“代码没有执行”无法区分的情况。

---

## 3. 本次 Build 长时间没有正确打包的真实原因

### 3.1 第一层原因：源码提交与标准 Build 没有形成同一 SHA 的闭环

本次调试的目标源码已经在 commit：

`1269f920216a2b3f601a511834165a0d5f288790`

该提交已经包含完整的 FAT Trace 8 点实际代码，并通过了源码层面的 marker / compile 检查。

但后续 Build 并没有自动保证：

`目标源码 commit → Build Run → Artifact`

三者是同一个源码状态。

因此曾经出现过：Build Job 显示 Success、ZIP 正常生成，但 Artifact 实际来自旧的 `head_sha` 的情况。

### 3.2 第二层原因：使用 GITHUB_TOKEN 由 Workflow 推送的新 commit 不会可靠地再次触发另一个 Workflow

本次使用过“Workflow 自动修改源码并 commit/push，再等待标准 Build Workflow 自动跟进”的方案。

问题在于：使用 GitHub Actions `GITHUB_TOKEN` 推送产生的新 commit，不会像普通用户 push 一样可靠地触发后续 Workflow。

结果就是：

- 源码已经被修改；
- 新 SHA 已经存在；
- 但标准 Build 没有针对这个新 SHA 再跑；
- 最后看到的是旧 SHA 的成功 Artifact。

### 3.3 第三层原因：曾经出现“只验证 marker，不真正包含补丁”的 Workflow

历史 commit：

`78e62bdc19bd1074cf4d7a87cebd37c266d09cc2`

这个阶段的 Workflow 表面上验证 FAT Trace 相关 marker，但实际上删除了真正的 Python injection，只留下 marker-only 检查。

因此它可以出现：

- Workflow Success；
- Marker Success；
- Python Compile Success；
- Artifact 正常生成；

但是 Artifact 内部并没有完整的实际诊断逻辑。

**结论：CI 的“检查通过”不等于目标代码已经被正确打包。**

### 3.4 第四层原因：Build Run 编号与 Run ID 被混淆

本次排查中还出现了另一个重要人为错误：

- GitHub Actions 的 **Run ID** 是一个大整数，例如 `3470...`；
- 用户看到的 **Build #332** 是 Workflow 的 `run_number`；
- 两者不是同一个概念。

因此不能仅凭“某个 Run 成功”就认定它是 `#332`，也不能仅凭 Artifact 存在就认定它来自当前目标源码。

### 3.5 第五层原因：之前没有把 Artifact 的 `head_sha` 作为安装前硬性验收条件

本次错误判断的关键教训是：

只检查下面这些条件是不够的：

- Job = Success；
- Compile = Success；
- ZIP = Success；
- Artifact = 存在。

还必须检查：

`Artifact.head_sha == 目标源码 commit（或其明确后继 commit）`

并且最好再检查 Artifact 内关键文件的内容，确认真实代码已进入 ZIP。

---

## 4. Build / CI 长期规范

### 4.1 “源码 SHA”是 Build 的第一身份标识

以后判断一个插件包是否可安装，不以：

- Build 编号；
- Artifact 名称；
- Job Success；
- ZIP 时间；

作为最终依据。

最终依据必须是：

`Source SHA → Workflow head_sha → Artifact 内容`

三者一致。

### 4.2 不允许“marker-only”假验证

- Marker 只能证明某段文本存在。
- Marker 不能代替真实逻辑代码。
- 任何“自动补丁”Workflow 必须验证实际函数/代码片段存在，而不是只写一个 `.md/.txt` 标记文件后宣布完成。

### 4.3 Build 前必须先确认源码状态

正式 Build 前至少确认：

1. 目标代码已提交；
2. commit SHA 已知；
3. 目标文件中真实逻辑存在；
4. Python compile / Ruff 等静态检查通过。

### 4.4 Build 后必须做 Artifact 验收

至少检查：

1. Workflow = Success；
2. Artifact 存在；
3. Artifact `head_sha` 对应目标源码；
4. ZIP 结构正确；
5. 关键生产文件包含本次修改；
6. 未混入旧版本入口或旧临时文件。

只有全部通过，Artifact 才能交给用户安装。

### 4.5 Workflow 不得形成隐式级联依赖

不再采用：

`Workflow A 修改源码 → GITHUB_TOKEN push → 期待 Workflow B 自动 Build`

作为主要发布链路。

需要自动触发下游 Build 时，必须使用明确支持的触发机制；否则直接由用户/外部受信任身份 push 触发正式 Build，或者使用明确的 workflow_dispatch / API 触发。

### 4.6 调试 Workflow 与生产 Build 分离

- 调试用 patcher / trace injector 不应长期承担正式发布职责。
- 最终生产包必须来自可审计的源码 commit，而不是“某个 Workflow 临时改过的工作区状态”。
- 调试 Workflow 可以帮助产生补丁，但最终源码必须落成正式 commit，再由标准 Build 打包。

---

## 5. 本次 FAT Trace 调试的正确状态

最终确认的真实源代码提交为：

`1269f920216a2b3f601a511834165a0d5f288790`

该提交实现了完整 8 点 FAT Trace：

`DISCOVERED → OWNER → OFFSET-ENTER → ORIGINAL-NODE → FINAL-CORNER → TARGET → MOVE → WRITEBACK`

并修正了：

- Trace selector 使用 `sequence_ids`；
- logger 静默异常问题；
- Original Node 追踪；
- Final Corner 追踪；
- Target Source 追踪；
- Move decision 追踪；
- Writeback 追踪；
- 最终 Geometry 日志中的 Sequence 信息。

以后继续定位 `DAR463_H1A1 / FTTx DAR463_H1A1_CH3_ODP1 / L3 / N0020` 时，应直接基于这一完整链路取日志，不再重复改 Offset Core 几何规则。

---

## 6. 最重要的长期原则

> **设计问题先用可验证日志定位；代码问题先固定源码 SHA；Build 问题必须追到 Artifact SHA；只有“源码正确 + Build 对应 + Artifact 内容正确”三者同时成立，才允许安装测试。**

这条原则适用于后续所有 Link Design、Offset Core、FAT Trace、CI Build 和插件发布工作。
