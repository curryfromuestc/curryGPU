# curryGPU 功能级 ISS — 地基扩展计划：S2R 特殊寄存器读 + SR_LANEID

> 本计划是 `docs/implement/ISS/plan-foundation.md`（地基循环）的一个**聚焦扩展**，不修改已 converged 的地基计划正文。
> NV 指令模型依据项目内 `/home/yanggl/code/sm100a`（sm_100 SASS 参考）的 `output/S2R.html` 与 `isa.json`（`S2R R0, SR_LANEID`）；外部参考只作设计依据，不提升为本仓库硬约束，精确 bit 编码沿用占位私有 layout（plan-foundation.md DEC-4）。
> 语言约定：小节标题、`AC-*`/`FUT-*`/`DEC-*`/task ID、文件路径、API 名、ISA 助记符（`S2R`/`SR_LANEID`/`IADD3`/`ISETP`/`BRA`/`EXIT`）、命令 flag、`coding`/`analyze` 标签为语言中立标识符，保持英文；正文用中文。
> 范围：在地基已建立的单一源 codegen + 非分歧 masked 32-lane 执行核之上，新增**一条** per-lane 特殊寄存器读指令 `S2R` 及其**唯一**选择子 `SR_LANEID`，并把它的**首个消费者**——ITS north-star corpus 改写为以该原语驱动真·per-lane data-dependent 分歧——一并纳入本循环。其余 `SR_*`、`CS2R`、`S2UR`、uniform 寄存器数据通路记入 `## Future Work / Out of Scope`。

## Goal Description

为 curryGPU 功能级 ISS 的 ISA 增补一条 per-lane 特殊寄存器读原语 `S2R`（首个且唯一选择子 `SR_LANEID`），使范围内 ISA 的 kernel 获得**架构化的 per-lane 数据源**：每个活跃 lane 读到自己在 warp 内的下标（`0..31`）。沿用声明式 Python 编码表为单一权威源，在 build 时驱动生成 128-bit 解码器 + `emit()` 汇编器 + JSON IR，并经既有 completeness 与 no-overlap 门禁把关；执行核以现有 decode-once + masked 32-lane 循环写入 per-lane 结果，**不新增任何架构状态字段**。要求：encode↔decode round-trip 在边界值矩阵上零差异；既有地基/ITS 指令语义与状态快照逐 bit 不变（纯新增 opcode + handler；ITS corpus 改写只更换分歧源、不改 ISA 语义）；生成与执行确定可复现；公开 CI 仅用公开 schema + 样例 layout 即可全绿。本循环除交付并验证该原语本身外，还落地它的首个消费者：把 ITS north-star corpus 由 `ELECT` 式 1-vs-31 与 test 侧伪 per-lane 数据，改写为以 `S2R Rd, SR_LANEID` 驱动的真·per-lane data-dependent 分歧，并在既有 4-序 metamorphic 主门下保持调度序无关——使 `plan-its.md` 的 AC-5 满足其字面意图。

## Acceptance Criteria

> 遵循 TDD：每条 AC 含正向测试（应通过）与负向测试（应被拒绝/失败）。`AC-*` 均可在本循环内确定性验证。

- AC-1：单一源 schema/layout 新增 `S2R` + `SR_LANEID`，选择子以 `sreg` operand kind 表达，沿用 schema（公开语义/字段）/layout（私有编码）拆分。
  - Positive Tests (expected to PASS):
    - `schema` 声明 `S2R`：guard 段 + `rd`（register operand）+ `sr`（`sreg` operand，choices = `("SR_LANEID",)`）；样例 `layout` 绑定占位 opcode + 字段位偏移后，足以生成 decoder + `emit()` + JSON IR。
    - 汇编 `S2R Rd, SR_LANEID` 可编码，反汇编/JSON IR 能把选择子还原为符号名 `SR_LANEID`。
    - JSON IR 暴露 `S2R` 的稳定 post-decode 字段（指令名、operands 含 `sr` 的 kind=`sreg`、guard、控制段）。
  - Negative Tests (expected to FAIL):
    - `S2R` 在所选 layout 没有绑定时，生成阶段以清晰、具体的错误失败（无静默回退、不伪造编码）。
    - 汇编未知选择子（任何 ≠ `SR_LANEID` 的符号）在 assemble 期被拒。
    - 选择子字段声明的宽度+偏移溢出 128-bit 字、或与 `S2R` 其他字段冲突时，生成失败。

- AC-2：build 时 codegen 把 `S2R` 纳入解码器，并经覆盖其完整机器契约的 completeness 与 no-overlap 门禁。
  - Positive Tests (expected to PASS):
    - 生成的 128-bit 区分树解码器包含 `S2R`；completeness 通过，其含义为：`S2R` 有合法解码绑定，`rd`/`sr` 字段按正确宽度/偏移映射、guard 字段被覆盖、21-bit 控制段被解码进指令 struct 作功能 no-op、reserved 位遵循文档化策略。
    - no-overlap 通过，证明 `S2R` 的 (mask,value) cube 与既有全部指令不相交。
  - Negative Tests (expected to FAIL):
    - 若 `S2R` 占位 opcode 与既有指令 cube 重叠，被符号化 no-overlap 检查（非枚举）拒绝。
    - `S2R` 缺少 layout 绑定时，completeness 门禁失败。
    - `S2R` 的 reserved 位被置非法值时，按既有文档化策略导致解码失败或触发陷阱。

- AC-3：`S2R` 的 encode↔decode round-trip 在固定、可复现的边界值矩阵上零差异。
  - Positive Tests (expected to PASS):
    - 按固定规则确定性生成操作数矩阵——`rd` ∈ {RZ, 0, 最大索引, 一个中间索引}；`sr` ∈ {`SR_LANEID`}；guard 谓词 ∈ {PT, P0, 取反, 不取反}；控制段字段 ∈ {0, max, 一个中间值}——encode → decode → JSON-IR 在整个矩阵上零差异还原原始符号字段。
    - 矩阵及其选取规则固定/带种子，使该 AC 可在本循环内复现。
  - Negative Tests (expected to FAIL):
    - 把谓词当作 `rd`、非法寄存器索引、未知选择子、越界控制段字段，在 assemble/encode 期（执行前）被拒。

- AC-4：执行语义——`S2R Rd, SR_LANEID` 在 masked 32-lane 循环中为每个活跃 lane 写入其 lane 下标。
  - Positive Tests (expected to PASS):
    - 手写 kernel 执行 `S2R Rd, SR_LANEID` 后，每个 guard&active lane 的 `vgpr[Rd][lane] == lane`（即 `0..31`）；`RZ` 作 `rd` 时写入被丢弃。
    - `S2R` 不改 PC（per-thread next-PC 取 fallthrough）、不写谓词、不碰内存、不自发产生分歧（不增 `divergence_events`）；可与既有样例子集（`IADD3`/`ISETP`/`@P` guard/非分歧 `BRA`/`EXIT`）组合，产出精确的架构状态 fixture。
    - lane 下标取物理 lane 索引，与 active 状态无关地稳定（写入只发生在活跃 lane）。
  - Negative Tests (expected to FAIL):
    - active-mask=0 或 `@P` guard 为假的 lane，执行 `S2R` 既不写寄存器也不写内存。
    - `S2R` 不引入任何独有的 execute 陷阱（`rd` 越界沿用既有 GPR 越界处理）；缺 `EXIT` 的含 `S2R` kernel 仍命中既有 max-step 陷阱、不无界运行。

- AC-5：非破坏集成 + 确定性；公开 CI 仅用样例 layout 全绿。
  - Positive Tests (expected to PASS):
    - 既有 foundation 测试与未被本循环改写的 ITS 测试保持全绿、零回归（新增 opcode 不改任何既有指令语义、IR 或状态快照 schema）；改写后的 north-star 通过既有 4-序 metamorphic 主门（见 AC-6）；新增 `S2R` 正/负向测试全绿；整套 `0 skipped / 0 failed`。
    - 两次运行生成器（独立进程、清空输出目录、固定 locale/env、变更 `PYTHONHASHSEED`、稳定排序、无时间戳/绝对路径）产出逐字节一致的产物。
    - 默认本地构建与公开 CI 仅用公开 schema + 样例 layout 即可构建并测试全绿。
  - Negative Tests (expected to FAIL):
    - 任何使既有指令 round-trip/执行/状态快照发生变化的改动，被既有测试捕获。
    - 非确定性的生成器改动被「两次构建/变更 hashseed」比对捕获。

- AC-6：ITS north-star corpus 改写为消费 `SR_LANEID`，产生真·per-lane data-dependent 分歧，并在既有 4-序 metamorphic 主门下保持调度序无关。
  - Positive Tests (expected to PASS):
    - `tests/iss/its_corpus.py` 的 north-star 成员（至少 `variable_reduction_loop`）改用 `S2R Rd, SR_LANEID` 推导 per-lane 控制（如 per-lane 变长 trip count / 阈值分支），取代 `ELECT P0, 0xFFFFFFFF` 的 1-vs-31 结构性分歧与 test 侧 32-元素序列伪数据。
    - 改写后的成员在执行中出现真正的 per-lane 分歧（同时存活多个 PC、按 laneid 划分的非平凡分歧模式），由显式 divergence 断言确认非平凡。
    - 改写后的 corpus 仍通过既有 `tests/iss/test_its_metamorphic.py` 的 4 种确定序（`min_pc_first`/`max_pc_first`/`round_robin`/`oldest_group_first`）主门：最终架构态（排除 `counters`）逐 bit 一致；每 `BSYNC` 重聚 mask 断言成立。
  - Negative Tests (expected to FAIL):
    - 把 laneid 推导的阈值/trip count 退化为 uniform 常量、使分歧塌缩，被 divergence 断言捕获（防止回退到伪分歧）。
    - 在改写后的 loop-carried-barrier 上丢弃 `BSYNC` 或破坏重聚，被既有 metamorphic / mutation 主门捕获（序无关性被破坏）。

## Path Boundaries

### Upper Bound (Maximum Acceptable Scope)
仅新增 `S2R` 这一条指令、`SR_LANEID` 这一个选择子，走完 `schema → layout → assembler → codegen 门禁 → native 执行 → tests` 全链路并通过全部 AC：含 `sreg` operand kind、占位私有 layout、边界值矩阵 round-trip、per-lane 写下标执行 fixture、非破坏回归与确定性复核；并把 ITS north-star corpus 改写为以 `S2R Rd, SR_LANEID` 驱动真·per-lane 分歧、在既有 4-序 metamorphic 主门下保持序无关。**不**新增任何其他 `SR_*`、**不**实现 `CS2R`/`S2UR`、**不**实现 uniform 寄存器数据通路、**不**修改 `plan-its.md`/`plan-foundation.md` 计划正文、**不**新增架构状态字段。

### Lower Bound (Minimum Acceptable Scope)
仍能通过全部 AC 的最小实现：单一样例 layout 的占位 opcode；`sreg` 仅含 `SR_LANEID`；执行核新增一个无状态 handler 写入 lane 下标；round-trip + 执行 + 非破坏回归 + 两次构建一致 + 公开 CI 全绿；ITS corpus 至少改写主 north-star 成员 `variable_reduction_loop` 为以 `SR_LANEID` 驱动，并保持 4-序 metamorphic 主门全绿。

### Allowed Choices
- 选择子建模：新增 operand kind `sreg`（**推荐**，对齐 ITS 已建立的 `barrier`/`membermask` 先例，IR 自然、未来可扩展）；或复用既有 `modifier` 枚举机制（轻量替代）。见 DEC-1。
- 占位 opcode：任选未被既有指令使用的值，由 no-overlap 门禁把关不相交。
- 选择子字段宽度：可镜像 NV 的 8-bit 选择子，仅承载 `SR_LANEID` 占位值，余位 reserved。

### Disallowed Choices
- 把 NV 的真实 `SR_*` 选择子数值/精确编码入库（沿用占位私有 layout，DEC-4）。
- 为未来 `SR_*` 族提前建通用 special-register 框架/抽象/配置（违反「不为未请求需求提前泛化」）。
- 在本循环修改 `plan-its.md` 或 `plan-foundation.md` 计划正文（改写 ITS corpus 测试是允许且必要的，但不改这两份计划文档）。
- 让 `S2R` 引入新的架构状态字段、或改变既有指令的语义/IR/快照。
- 在 codegen 引擎里按指令名/选择子名做语义特判（引擎须保持与编码无关，只吃已解码字段）。

## Feasibility Hints and Suggestions

> 本节为概念性建议，非强制。

### Conceptual Approach
NV 模型（sm100a `S2R_R_SR`）：`S2R` = guard predicate + 目标寄存器（operand 0）+ 8-bit 特殊寄存器选择子（operand 1）+ 标准 21-bit 控制段——控制段与寄存器/谓词/立即数等基元 curryGPU 已建模。接入路径与 ITS 当初新增 `barrier`/`membermask` 同构：在 `schema` 声明指令与 `sreg` operand、在样例 `layout` 绑定占位编码、在 `assembler` 增 `sreg` 的正/反向解析分支、在 native 执行核加一条 dispatch 与一个 handler。

关键可行性事实：`codegen` 引擎对 operand/field 的 `kind` 是**透传**的（`kind` 作为元数据进入 IR），解码与门禁按字段宽度/偏移/符号/绑定/重叠**通用**校验，**无 kind 白名单**——因此新增 `sreg` kind **无需改动** decoder 生成器或两道门禁，仅触及 `assembler` 的 per-kind 分支。执行核中 lane 下标即 masked 32-lane 循环的循环变量，故写 lane 下标复用既有 per-lane 写寄存器路径（含 `RZ` 丢弃与 GPR 越界处理），**零新增状态**。`S2R` 是 ALU 类 per-lane 取值，**自身不分歧**——它只产生 per-lane 值，分歧由后续 `ISETP`/`@P`/`BRA` 触发——故干净落入地基的非分歧执行核。

原语就位后，本循环的消费者步骤把 ITS north-star corpus 的分歧源从 `ELECT` 式 1-vs-31 / test 侧 32-元素序列伪数据，换成 `S2R Rd, SR_LANEID` 推导的 per-lane 控制（如 per-lane 变长 trip count），使分歧真正 data-dependent；改写须保持既有 4-序 metamorphic 主门（最终架构态排除 `counters` 后逐 bit 一致）全绿，否则视为破坏同步契约。

验证门：`PYTHONPATH=isa:iss python -m pytest tests -q`（须 `0 skipped / 0 failed`）+ 重建 native（`cmake --build`）+ `ctest`；两次构建逐字节一致 + `git ls-files` 私有化检查沿用地基既有手段。

### Relevant References
- `docs/implement/ISS/plan-foundation.md` — 父循环：AC-5（ITS-ready per-lane 基础设施、`vgpr[regID][32]`）、DEC-2（地基指令子集）、DEC-3（结构化陷阱分类）、DEC-4（编码私有化/占位 layout）。
- `/home/yanggl/code/sm100a/output/S2R.html` 与 `isa.json`（`S2R_R_SR`、`CS2R_R_SR`、`S2UR`）— NV `S2R Rd, SR_LANEID` 指令模型。
- `isa/currygpu/isa/{schema.py, layout/sample.py, assembler.py, codegen.py}` — 单一源编码表、样例 layout、汇编器（per-kind 分支位置）、生成/门禁引擎（kind 透传）。
- `iss/binding/native.cpp` — masked 32-lane 执行核、per-lane 写寄存器路径、指令 dispatch。
- `tests/iss/its_corpus.py` 与 `tests/iss/test_its_metamorphic.py` — ITS north-star corpus（`variable_reduction_loop` 等）与 4-序 metamorphic 主门（`SCHED_ORDERS`/`architectural_subset`/`pre_screen`/`corpus_cases`）；本循环改写其分歧源为 `S2R SR_LANEID`。

## Dependencies and Sequence

### Milestones
1. schema/layout/assembler：声明 `S2R` + `sreg` operand kind + `SR_LANEID` 选择子；样例 layout 占位绑定；汇编器正/反向解析与负向校验（未知选择子、谓词当 `rd`）。
2. codegen 门禁：确认表驱动解码器纳入 `S2R`；completeness/no-overlap 覆盖新 opcode；边界值矩阵 round-trip 零差异。
3. native 执行：dispatch + per-lane 写 lane 下标 handler；执行 fixture（写入 `0..31`、`RZ` 丢弃、guard/active 关断、fallthrough、不写谓词/内存）；既有指令零回归。
4. ITS corpus 消费 `SR_LANEID`：改写 north-star 成员（至少 `variable_reduction_loop`）以 `S2R Rd, SR_LANEID` 推导 per-lane 控制，取代 `ELECT` 式 1-vs-31 与 test 侧伪数据；加显式非平凡 divergence 断言；在既有 4-序 metamorphic 主门下保持序无关。
5. 确定性 + 私有化 + CI：两次构建逐字节一致；公开 CI 仅用样例 layout 全绿复核（覆盖原语 + 改写后的 corpus）。

依赖：M2 依赖 M1；M3 依赖 M1（`emit()`）与既有执行核；M4 依赖 M1–M3（需原语可汇编 + 可执行）；M5 依赖 M1–M4。仅相对依赖，无时间估计。

### Task Breakdown

> 每个任务恰带一个路由标签：`coding`（主线 Agent 实现）或 `analyze`（独立只读对抗式评审）。每条 `AC-*` 至少被一个任务覆盖。

| Task ID | Description | Target AC | Tag | Depends On |
|---------|-------------|-----------|-----|------------|
| task1 | 在 `schema` 声明 `S2R`（guard + `rd` register + `sr` `sreg`，choices=`SR_LANEID`）；样例 `layout` 占位 opcode + 字段位；`assembler` 增 `sreg` 正/反向解析 + 负向（未知选择子、谓词当 `rd`、越界） | AC-1, AC-3 | coding | - |
| task2 | 确认/打通生成器对 `S2R` 的解码器产出与 JSON IR；completeness + 符号化 no-overlap 覆盖新 opcode；边界值矩阵 round-trip 零差异 | AC-2, AC-3 | coding | task1 |
| task3 | 对 `sreg` 建模与 no-overlap 做对抗式评审：占位 opcode 不撞既有 cube、选择子枚举唯一性、字段宽/偏移/溢出、引擎与编码无关性（无 kind 特判） | AC-1, AC-2 | analyze | task2 |
| task4 | native dispatch + per-lane 写 lane 下标 handler；执行 fixture（`0..31`、`RZ` 丢弃、guard/active 关断、fallthrough、不写谓词/内存、不增 divergence 计数） | AC-4 | coding | task1 |
| task5 | 非破坏回归（既有 foundation 测试 + 未改动 ITS 测试零回归；改写后 north-star 通过 4-序主门；整套 `0 skipped / 0 failed`）+ 两次构建逐字节一致 + 公开 CI 样例 layout 全绿复核 | AC-5, AC-6 | coding | task2, task4, task7 |
| task6 | 对执行语义边界做对抗式评审：inactive/exited/guard-false lane 不写、laneid=物理下标稳定、`S2R` 不自发分歧、与既有子集组合的状态一致性 | AC-4 | analyze | task4 |
| task7 | 改写 `tests/iss/its_corpus.py` north-star 成员（至少 `variable_reduction_loop`）以 `S2R Rd, SR_LANEID` 推导 per-lane 控制；加显式非平凡 divergence 断言；确认 `tests/iss/test_its_metamorphic.py` 4-序主门在改写后全绿 | AC-6 | coding | task4 |
| task8 | 对改写后的 north-star 做对抗式评审：真·per-lane 分歧非平凡、laneid 退化为 uniform 被 divergence 断言捕获、丢 `BSYNC`/破坏重聚被序无关主门捕获、ITS 其余成员无回归 | AC-6 | analyze | task7 |

## Future Work / Out of Scope

> 后继与范围外目标记于此处，**不**放入 `## Acceptance Criteria`。

> 原 FUT-1（ITS corpus 消费 `SR_LANEID`）已按用户决策提升为本循环 in-scope（见 AC-6 / Milestone 4 / task7–task8），不再列为 Future Work。

- FUT-1：其余特殊寄存器 —— `SR_TID.{X,Y,Z}`/`SR_CTAID.*`/`SR_NTID`/`SR_WARPID`/`SR_NWARPID`/`SR_LANEMASK_{EQ,LT,LE,GT,GE}`（即 PTX `%lanemask_*`）/`SR_CLOCK*` 等；多数依赖 CTA/grid 维度或 warp-mask 基础设施。逐个按需经同一 `sreg` 接缝扩充。
  - Promotion trigger: 相应内存/线程层基础设施就绪时按需。
- FUT-2：`CS2R` / `S2UR` —— 便宜/uniform 变体（`CS2R.32/.64`）与 uniform 寄存器数据通路（`S2UR UR, SR`）。
  - Promotion trigger: uniform 数据通路循环。
- FUT-3：生产 layout 对 `S2R` 的真实编码 conformance —— 经私有仓库 submodule 注入生产 `layout`，私有 CI 跑真实选择子编码 conformance（沿用 plan-foundation.md FUT-6 接缝）。
  - Promotion trigger: 本扩展循环全绿 + 私有 layout submodule 就绪。

## Implementation Notes

### Code Style Requirements
- 实现代码与注释**不得**包含计划专用术语（`AC-`、`Milestone`、`Step`、`Phase`、`FUT-`、`DEC-`、`task` 等工作流标记）；用描述性、贴合领域的命名（如 `exec_s2r`、`parse_sreg`/`special_register`、`lane_index`/`laneid`、`sreg`）。
- 代码中只用英文；与用户的对话用中文。代码注释、commit message、PR body 不出现 AI 工具名称。
- 不照搬 NV 的私有选择子数值/编码；样例 layout 用占位值；外部参考只作设计依据。

## Pending User Decisions

> 实现启动前需用户确认；计划已给默认取向。

- DEC-1：选择子建模方式。
  - Option A（计划默认）：新增 operand kind `sreg`，对齐 `barrier`/`membermask` 先例，选择子在 IR 中读作 `operands.sr.kind = "sreg"`、值为符号 `SR_LANEID`；汇编器加正/反向分支，引擎透传不特判。
  - Option B：复用既有 `modifier` 枚举机制（`choices=("LANEID",)`），零新 operand kind，但语义上把「源操作数」当成「修饰符」，与 NV「operand 1 = SR 选择子」模型略偏。
  - Decision Status: 待确认（倾向 A）。
- DEC-2：指令/选择子子集。
  - 计划默认：仅 `S2R` + `SR_LANEID`；`CS2R`/`S2UR`/其余 `SR_*` 入 Future Work。
  - Decision Status: 待确认（倾向「仅此」，符合范围纪律）。
- DEC-3：落地进程。
  - 已定：新开本聚焦扩展计划（独立 RLCR 小循环），不改动已 converged 的 `plan-foundation.md`/`plan-its.md` 正文。
  - Decision Status: 已由用户决策。
- DEC-4：ITS corpus 改写范围（原 FUT-1 提升后引入）。
  - 计划默认：至少改写主 north-star `variable_reduction_loop`（per-lane 变长 trip count）；`causal_mask_control_divergent` 等其余成员可选改写（若其分歧本应由 laneid 驱动）。
  - Decision Status: 待确认（倾向「主 north-star 必改，其余可选」）。
