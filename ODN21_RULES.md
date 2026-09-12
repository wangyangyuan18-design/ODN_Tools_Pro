# ODN 2.1 自动节点规划规则

本规则不修改 Project Configuration UI。工程配置已有的 ODN Version、节点类型和长度参数作为唯一输入。

## 1. 基础路线

Link Design 继续使用现有 Pole Edge 路由。ODN 2.1 不建立第二套物理路由算法。

## 2. BB

- 回缆必须从实际 Pole Edge 路由判断，不能仅按方向或拐角判断。
- 回缆定义为同一 Pole Edge 被去程和回程重复经过，例如 A→B→A。
- A→B = 45m 时，回缆长度 = 90m。
- 当回缆超过工程配置的 BB 触发条件时，自动规划 BB。
- BB 优先放在实际 Pole Edge 的叉路口/分岔节点，而不是简单按超限距离截断。
- 候选叉路口需要比较上游 FAT→BB 以及 BB→下游 FAT 的实际 Pole Edge 路由；在满足约束的候选节点中优先选择三段（或实际分支段）总路由距离较短的节点。
- BB 插入后成为新的 ODN 网络节点，后续 DC 长度计算不得跨越 BB 继续累计。

## 3. SFC Closure

- 预链接/Distribution Cable 的长度限制来自工程配置，当前默认 455m。
- 判断必须基于实际 Pole Edge 路由的逐杆累计距离。
- 如果下一根杆会使累计距离超过 455m，则不能把 SFC Closure 放在下一根杆。
- 选择距离起点最近方向上最后一个累计距离仍 <=455m 的实际 Pole 节点。例如 410m 后下一根为 460m，则 SFC Closure 放在 410m 节点。
- SFC Closure 插入后，从该节点重新开始累计下一段 DC 长度。
- 已经由 BB 切开的 DC 段必须分别计算，不能把 BB 前后的距离合并成 FAT→FAT 总长度再判断。

## 4. 实施原则

- 不增加新的 Link Design UI。
- 不增加新的 Project Configuration UI。
- 不修改现有 0.5m offset、corner、return cable geometry 和 FAT landing 引擎。
- ODN 2.0 行为保持不变。
- ODN 2.1 只在项目配置明确启用 ODN 2.1 且 BB/SFC Closure 节点已配置时启用对应规则。
- 所有自动判断先写入详细 QGIS Message Log，至少记录：Link、Segment、实际长度、限制值、回缆边、候选 BB 节点、SFC 最后合法杆节点及累计距离。

<!-- rebuild marker: endpoint-only Pole occupancy rules restored -->
