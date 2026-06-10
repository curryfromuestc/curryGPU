# curryGPU 功能级 ISS — 地基循环实现计划（Foundation Loop）

> 本计划由 `docs/implement/ISS/spec.md` 草稿经 Codex 首轮分析 + Claude/Codex 两轮收敛 + 用户决策生成。
> 语言约定：小节标题、`AC-*`/`FUT-*`/`DEC-*`/task ID、文件路径、API 名、ISA 助记符（如 `IADD3`/`ISETP`/`EXIT`）、命令 flag、`coding`/`analyze` 标签等为语言中立标识符，保持英文（RLCR 循环与 refine-plan 工具按英文标题/标识符解析）；正文用中文。
> 范围（DEC-1 已定 = 地基骨架）：本循环 = 草稿阶段 ① 编解码骨架 + 工程/构建/codegen/CI/状态-diff 这层不可逆地基。草稿阶段 ②–⑤（ITS 分歧、内存与同步、MMA+数值、Transformer block）作为后继循环，记于 `## Future Work / Out of Scope`（FUT-1..4）。完整草稿原文附于文末。

## Goal Description

为 curryGPU 功能级 ISS（行为级、bit-exact、无时序的 SIMT 指令集模拟器，作为 curryGPU ISA 的可执行规范 / 正确性 oracle）奠定不可逆的工程地基。具体地：建立 `isa/` 单一真相源子树（`schema` 公开 + `layout` 私有并带公开样例）与 `iss/` 功能核子树；让声明式 Python 编码表成为唯一权威源，在 build 时驱动生成 128-bit C++ 解码器 + Python `emit()` 汇编器 + JSON IR，并经完备性（completeness）与无重叠（no-overlap）门禁把关；Python/C++ 仅在粗粒度 pybind 边界（launch / step / state-diff）交互；定义架构状态 diff 契约与计数器；并通过 decode-once → masked 32-lane 执行跑通一个极小的标量+warp kernel（ALU + 谓词 + 分支 + EXIT 子集）。要求 encode↔decode round-trip 零差异、生成与执行确定可复现，且公开 CI 仅用公开 schema + 样例 layout 即可全绿，不把生成的 decoder/emit 与生产 kernel 二进制纳入版本库或对外分发。

## Acceptance Criteria

> 遵循 TDD：每条 AC 含正向测试（应通过）与负向测试（应被拒绝/失败），以便确定性验证。`AC-*` 是本 RLCR 循环的完成判据，均可在本循环内验证。

- AC-1：仓库布局与 `isa → iss` 单向依赖符合草稿 §11 契约。
  - Positive Tests (expected to PASS):
    - 在干净 checkout 下，`iss` 组件仅用公开 `isa/schema` + 样例 `isa/layout` 即可构建/安装。
    - 测试可通过 `currygpu` namespace package 同时导入 `currygpu.isa.*` 与 `currygpu.iss.*`。
    - 所有构建文件位于 `iss/`（纯 Python 部分位于 `isa/`）；仓库根目录没有任何 Python 包/构建文件。
  - Negative Tests (expected to FAIL):
    - 在仓库根放置 Python 包/构建文件的配置，被结构性检查拒绝。
    - 让 `isa/` 导入或构建依赖 `iss/`（反向依赖）的配置，被结构性检查拒绝。
- AC-2：声明式编码表为单一源，schema（公开）/layout（私有+样例）拆分可用。
  - Positive Tests (expected to PASS):
    - 在 `schema` 声明样例指令子集、并在样例 `layout` 绑定后，足以生成 decoder + `emit()` + JSON IR。
    - 编码（opcode 数值 / 字段偏移 / fsel）只在 `layout` 一处声明，语义与字段名+宽度在 `schema`；语义 handler 只消费已解码字段，不接触 raw bit。
  - Negative Tests (expected to FAIL):
    - 某 schema 指令在所选 layout 没有绑定时，生成阶段以清晰、具体的错误失败。
    - 移除样例 layout 后构建以清晰错误失败（无静默回退、不伪造/自动合成编码）。
- AC-3：build 时 codegen 生成 128-bit C++ 解码器 + `emit()` 钩子 + JSON IR，并经覆盖完整机器契约的符号化 completeness 与 no-overlap 门禁。
  - Positive Tests (expected to PASS):
    - 样例指令生成 bit 区分树解码器（APInt 式 128-bit 宽字段提取）、Python `emit()` 钩子与 JSON IR 至 `iss/build/generated/`。
    - completeness 门禁通过，其含义为：所选 layout 下每条 schema 指令都有合法解码绑定（**不是** 2^128 枚举），且每个声明的操作数字段都按正确宽度/偏移/符号扩展规则映射、guard 谓词字段被覆盖、21-bit 控制段（stall/yield/r-bar/w-bar/wait-mask/reuse）被解码进指令 struct 作为功能 no-op、reserved/ignored 位遵循文档化策略、声明的别名（如 MOV→`IADD3 Rd,RZ,RZ`）可解析。
    - JSON IR 暴露一组文档化、稳定的 post-decode 语义字段（指令名、operands、modifiers、控制段字段）。
  - Negative Tests (expected to FAIL):
    - 两条指令的 layout (mask,value) cube 重叠时，被符号化无重叠检查（非枚举）拒绝。
    - 某 schema 指令缺少 layout 绑定时，completeness 门禁失败。
    - 某操作数/控制字段声明的宽度+偏移溢出 128-bit 字、或与其他字段冲突时，生成失败；reserved 位被置非法值时，按文档化策略导致解码失败或触发陷阱。
- AC-4：encode↔decode round-trip 在样例子集、有限且可复现的边界值矩阵上零差异。
  - Positive Tests (expected to PASS):
    - 对每条样例指令，按固定规则确定性生成操作数矩阵——逐字段：寄存器 {RZ, 0, 最大索引, 一个中间索引}；guard 谓词 {PT, P0, 取反, 不取反}；立即数 {min, max, 0, 范围边界 ±1}；每个声明的 modifier 取值；控制段字段 {0, max, 一个中间值}——encode → decode → JSON-IR 在整个矩阵上零差异地还原原始符号字段。
    - 矩阵及其选取规则固定/带种子，使该 AC 可在本循环内完成并复现。
  - Negative Tests (expected to FAIL):
    - 非法寄存器索引、未对齐的寄存器对（64-bit 对用奇数索引、128-bit 组非 4-对齐）、非法 modifier 组合、越界立即数，在 assemble/encode 期（执行前）被拒绝。
- AC-5：极小标量+warp 执行核以 decode-once + masked 32-lane 循环跑通一个极小 kernel，且具备 ITS-ready 的 per-lane 基础设施。[指令子集见 DEC-2；陷阱形态见 DEC-3]
  - Positive Tests (expected to PASS):
    - SoA per-lane 寄存器堆（`vgpr[regID][32]`，无类型 32-bit word）+ per-warp 谓词 + per-lane active mask + per-lane `pc[32]` + per-lane `lane_state[32]`，执行一个手写 kernel（样例子集：IADD3 含 MOV-via-`IADD3 Rd,RZ,RZ` 别名；ISETP；再一条 ALU 如 LOP3；`@P` 谓词 guard；非分歧/uniform BRA；EXIT），产出精确的架构状态 fixture。
    - 每个 handler 计算 per-thread next-PC（本非分歧循环中活跃 lane 取值一致，但 per-lane PC 数组 + per-thread next-PC 路径被实际走到）；EXIT 将每个 lane 的 `lane_state=exited` 并从 active mask 清位；全部 lane 退出时 warp 退休。
    - active-mask=0 或 `@P` guard 为假的 lane 既不写寄存器也不写内存。
  - Negative Tests (expected to FAIL):
    - 以下每一种都产生确定性、结构化的陷阱/错误（绝不挂起、绝不静默给错值），遵循既定陷阱分类——assemble 期校验错误（错误寄存器/对/modifier/越界立即数）、decode 陷阱（未知/未绑定 opcode、非法 reserved 位值）、execute 陷阱（非法 PC / 分支目标）、max-step 陷阱（步数预算内未到 EXIT）。
    - 缺少 EXIT 的 kernel 不会无界运行，而是命中 max-step 陷阱。
- AC-6：架构状态 diff 契约 + 计数器被定义、在精确的粗粒度 pybind 边界暴露，且可复现。
  - Positive Tests (expected to PASS):
    - 导出既定的序列化架构状态快照：per-lane GPR（`vgpr[regID][32]`）、uniform 寄存器、per-warp 谓词、per-lane `pc[32]`、`lane_state[32]`、存在但默认值的收敛屏障（`Bx`）字段、陷阱状态，以及契约声明、本循环存在但为空的内存空间。
    - 导出计数器：instructions、warp-instructions、mem-ops（本循环 ==0），以及为阶段 ② 保留的 divergence-events 计数器（本循环 ==0）。
    - pybind 仅暴露 `launch` / `step(max_steps)` / `state-diff`，其中 `step(max_steps)` 在 C++ 内一直执行至 EXIT / 陷阱 / 步数预算，不会每解码一条指令就返回 Python。
    - 同一 kernel 两次运行产出逐字节一致的状态 JSON 与计数器。
  - Negative Tests (expected to FAIL):
    - 每条指令跨一次 Python 的 pybind 设计，被边界粒度检查拒绝（实测跨边界调用次数须为 O(launch+step)，与指令条数无关）。
    - 在期望 fixture 中篡改一个 GPR / 谓词 / PC 字节，diff 工具报告一处最小、局部化的差异（既非整体状态不匹配，也非误判通过）。（内存字节级 diff 推迟至 FUT-2，由其引入非空内存空间。）
- AC-7：确定性生成 + 私有化门禁（「regen-clean」在不入库前提下的形态），公开 CI 仅用样例 layout 全绿；生产 layout 通过私有仓库 submodule 显式注入，有权限者可拉取完整私有资产，无权限者仍可仅使用公有仓库。
  - Positive Tests (expected to PASS):
    - 两次运行生成器——独立进程、清空输出目录、固定 locale/env、变更 `PYTHONHASHSEED`、稳定排序、无时间戳、不内嵌绝对构建路径——产出逐字节一致的产物。
    - 生成的 decoder/`emit` 源文件不出现在 `git ls-files`；生产 kernel `.bin` 产物不出现在 `git ls-files`；私有生产 layout submodule 内容不被纳入公开 tracked files。
    - 若公开 CI 构建 wheel/sdist，其内容排除生成的 decoder/emit 源、生产 layout submodule 内容与生产二进制（公开产物只内嵌样例 layout）；JSON IR 仍可对外公开。
    - 公开 CI 与默认本地构建仅用公开 schema + 样例 layout 即可构建并测试全绿；生产 layout 只能通过显式选择（如构建选项或环境变量）启用，默认不会尝试拉取或依赖私有 submodule。
  - Negative Tests (expected to FAIL):
    - 非确定性的生成器改动（依赖 dict/set 顺序或 `PYTHONHASHSEED`、内嵌时间戳、内嵌绝对路径）被「两次构建/变更 hashseed」比对捕获。
    - 生成的 decoder/emit 源文件、生产 layout submodule 内容或生产 `.bin` 一旦被纳入公有版本库（出现在公开 `git ls-files`）、或出现在构建出的公开 wheel/sdist 内，门禁失败。

## Path Boundaries

> 草稿为高确定性设计（§7 多项「已定」），故路径边界较窄；多数实现选择由草稿固定。

### Upper Bound (Maximum Acceptable Scope)
全部 7 条 AC 在公开 CI、样例 layout 上达成：完整的 build 时确定性 codegen + 两道覆盖完整契约的门禁、边界值矩阵上的 round-trip 零差异、跑通 ALU + 谓词 guard + 非分歧分支 + EXIT 的极小多指令 kernel（含 ITS-ready 的 per-lane 基础设施）、完整的架构状态 diff + 计数器导出、精确的粗粒度 pybind、干净的确定性+私有化/打包门禁——并保留非破坏的 cluster/DSMEM 接缝（cluster_dim 默认 1 = 恒等），但不实现跨-CTA 行为。**不**实现 ITS 分歧重聚、超出极小 kernel 所需的通用内存空间 load/store、atomics、MMA/tensor、SFU 超越函数、或数值 oracle（MPFR / Tier-1 / Tier-2）。

### Lower Bound (Minimum Acceptable Scope)
仍能通过全部 7 条 AC 的最小实现：单一样例 layout；最小指令子集（DEC-2 已定的集合）；两道完整契约门禁；边界值矩阵上的 round-trip；ALU + 谓词 + EXIT 的极小 kernel（分支限于非分歧）；覆盖 per-lane GPR + 谓词 + PC + lane_state + 计数器的状态快照（外加为交接保留的「存在但为空」内存空间与「存在但默认」`Bx` 字段）；两次构建一致 + `git ls-files` 私有化门禁；公开 CI 全绿。

### Allowed Choices
- 可用：C++（C++17 或 C++20）实现功能核；pybind11 做粗边界；CMake + scikit-build-core 做构建；声明式 Python 编码表；自写的 128-bit bit 区分树生成器 + APInt 式宽提取（decodetree / LLVM FixedLenDecoderEmitter / Sail / riscv-opcodes 仅作设计参考）。
- 草稿已固定（确定性设计——窄边界，非开放选择）：Python 声明表为唯一权威源；schema（公开）/layout（私有+样例）拆分 + 引擎与编码无关（语义 handler 只吃已解码字段、不碰 raw bit）；生产 layout 放在私有仓库并以 submodule 接入，默认构建使用样例 layout，只有显式选择才启用生产 layout；`isa → iss` 单向依赖；粗粒度 pybind 边界（仅 launch / step / state-diff）；SoA per-lane 寄存器堆；decode-once + masked-lane 循环；无类型 32-bit GPR word + sub-word extend-to-32（无 partial-register merge）、偶对齐 64-bit 对、4-对齐 128-bit 组、packed SIMD-within-word；生成的 decoder/emit、生产 layout 内容与生产 `.bin` 永不入库或对外分发。
- 不可用：第二份手维护的编码源（CSV 仅为只读导出）；AoS per-thread 寄存器 hashmap；每条指令一次的 pybind 边界；把生成的 decoder/emit 或生产 kernel 二进制入库/对外分发；为跨-CTA 访问伪造远端态。

## Feasibility Hints and Suggestions

> 本节仅供参考与理解，是概念性建议，非强制要求。

### Conceptual Approach
Python `encoding_table.py`（schema）+ 样例 `layout` → `gen_decoder.py` 生成 128-bit bit 区分树（按 opcode/modifier 判别位分组；APInt 式宽字段提取）至 `decoded_inst.gen.h` + `decode.gen.cpp`，外加 `emit()` 钩子与 `isa_ir.json`；生成时检查 completeness（完整机器契约：绑定、字段宽度/偏移、符号扩展、guard、控制段、reserved 位策略、别名）与 no-overlap（符号化 (mask,value) cube 不相交）。生产 `layout` 不放在公有仓库正文中，而是作为私有仓库 submodule 接入：有私有仓库权限的开发者/私有 CI 可拉取并显式选择生产 layout；无权限者跳过该 submodule，仍可用公开 schema + 样例 layout 构建、测试和打包。C++ 核保有 SoA `vgpr[NUM_VREG][32]`、per-warp `pred`、per-lane `pc[32]`、`lane_state[32]`、存在但默认的 `Bx[]`；`step(max_steps)` 每个 PC 解码一次并跑 masked 32-lane 循环，每个 handler 返回 per-thread next-PC（ITS-ready，尽管阶段 ① 保持非分歧），在 C++ 内循环直到 EXIT/陷阱/预算。pybind 仅暴露 launch/step/state-diff。因生成物有意不入库，用「两次构建逐字节一致」+ `git ls-files`/包内容检查替代 git-diff 式 regen-clean。

### Relevant References
- `docs/implement/ISS/research-notes.md` — §4 数据结构蓝图、§5 ITS 状态机（未来）、§7 解码器生成、§8 速度技术、§10 最小骨架、§11 代码落位。
- `docs/design/isa.html` — §01 指令格式 / 21-bit 控制段、§02 寄存器与操作数、§07/§10 ALU + 控制 opcode、§11 编码/重定位不变式。
- `docs/implement/ISS/spec.md` — 文末附的原始草稿（权威的人类意图）。

## Dependencies and Sequence

### Milestones
1. 工程与打包骨架：`isa/` + `iss/` 子树、`currygpu` namespace package、CMake + scikit-build-core、无根包文件、单向依赖布线、CI 引导。
   - Phase A：目录 + 打包 + 导入冒烟。
   - Phase B：公开 CI 引导（样例上构建全绿）。
2. 编码表 + 样例 layout + 生成器：schema 表、样例 layout、带完整契约 completeness + no-overlap 门禁的 `gen_decoder`、JSON IR。
3. round-trip + emit()：`emit()` 汇编器、边界值矩阵上的 encode↔decode↔IR 等价、encode 期校验。
4. 执行核 + 极小 kernel + 状态-diff：SoA 寄存器 + per-lane PC/lane_state、decode-once masked-lane 核、样例子集 handler、陷阱分类、粗粒度 pybind、架构状态快照 + 计数器。
5. 确定性 + 私有化 + CI：两次构建逐字节一致、`git ls-files` + 包私有化门禁、公开 CI 在样例 layout 上全绿。

依赖关系：M2 依赖 M1；M3 依赖 M2；M4 依赖 M2（解码器）与 M1（pybind/构建）；M5 依赖 M2–M4。仅描述相对依赖，无时间估计。

## Task Breakdown

> 每个任务恰带一个路由标签：`coding`（Claude 实现）或 `analyze`（经 Codex / `/humanize:ask-codex` 执行）。每条 `AC-*` 至少被一个任务覆盖；每个任务至少指向一条当前 `AC-*`。

| Task ID | Description | Target AC | Tag (`coding`/`analyze`) | Depends On |
|---------|-------------|-----------|----------------------------|------------|
| task1 | 搭建 `isa/` + `iss/` 子树、`currygpu` namespace package、CMake + scikit-build-core、无根包文件；布线单向 isa→iss；结构性检查 | AC-1 | coding | - |
| task2 | 定义声明式 `schema` 表（样例子集）+ 样例 `layout`；强制 schema/layout 分离、「无绑定即报错」/「无样例 layout 即报错」/「不伪造编码」 | AC-2 | coding | task1 |
| task3 | 实现 `gen_decoder`（128-bit bit 区分树 + APInt 式提取）→ 解码器 + emit() 钩子 + JSON IR；完整契约 completeness + 符号化 no-overlap 门禁；21-bit 控制段 no-op 解码；稳定 JSON IR 字段 | AC-3 | coding | task2 |
| task4 | 对 128-bit 生成器/门禁设计做对抗式分析（layout 拆分下的 completeness 语义；符号化 cube-overlap 正确性；字段宽度/偏移/溢出 + reserved/控制段覆盖规则） | AC-3 | analyze | task3 |
| task5 | 实现 `emit()` 汇编器 + 有限边界值矩阵上的 round-trip（encode↔decode↔IR）；encode 期操作数校验（错误 reg/对/modifier/立即数） | AC-4 | coding | task3 |
| task6 | 实现 SoA 寄存器堆 + per-lane pc/lane_state + decode-once masked-lane 核 + 样例子集 handler（IADD3/MOV 别名、ISETP、LOP3、@P guard、非分歧 BRA、EXIT）+ per-thread next-PC 基础设施 + 确定性陷阱分类 | AC-5 | coding | task3 |
| task7 | 定义并实现架构状态快照（含存在但默认的 Bx + 存在但为空的内存空间）+ 计数器 + 状态-diff 工具；粗粒度 pybind（launch / step(max_steps) / state-diff）+ 边界粒度检查 | AC-6 | coding | task6 |
| task8 | 实现确定性生成（独立进程、清空目录、固定 locale、变更 PYTHONHASHSEED、稳定排序、无时间戳/绝对路径）→ 两次构建逐字节一致 | AC-7 | coding | task3 |
| task9 | 对架构状态 diff 契约 + 陷阱分类做对抗式评审，覆盖边界（未初始化状态、最小-diff 局部化、确定性来源、边界调用探针） | AC-6 | analyze | task7 |
| task10 | 实现私有化/打包/CI 门禁：生产 layout 只通过私有仓库 submodule 显式注入；生成的 decoder/emit + 生产 layout 内容 + 生产 `.bin` 不在公开 `git ls-files`；公开 wheel/sdist 排除之；公开 CI 仅用样例 layout 全绿 | AC-7 | coding | task3,task7,task8 |

## Future Work / Out of Scope

> 未来、推迟、后置、后继循环与范围外的目标记于此处，**不**放入 `## Acceptance Criteria`。

- FUT-1：阶段 ② ITS 分歧 —— per-PC grouping、收敛屏障状态机（BSSY/BSYNC/BREAK/CONT/YIELD/EXIT）、调度序无关性作为主门（≥3 种具名确定性调度策略 → 在同步、无竞争的分歧 corpus 上最终架构态逐 bit 一致）、每 BSYNC 重聚 mask 断言、以及对 UB/非法情形的显式检测（membermask ⊄ active、ELECT 非唯一、blocked/yielded 死锁）。
  - Source DEC: DEC-1
  - Current-loop handoff: AC-5（per-lane `pc[32]` + per-thread next-PC 基础设施 + `lane_state[32]` 在非分歧下被走到；EXIT 清 active mask）与 AC-6（状态快照内存在但默认的 `Bx` 屏障字段 + 保留的 divergence-events 计数器）。
  - Promotion trigger: 地基循环全绿；ITS 循环启动时。
- FUT-2：阶段 ③ 内存与同步 —— `memory_space` 抽象基类 + `memory_space_impl`（4KB 块稀疏 hashmap）覆盖 global/shared/local，并经阶段③用户裁定扩张到只读 const（`LDC` bank-indexed、launch 注入、snapshot 顶层 `const_memory`，`memory` 子树仍保持 global/shared/local 三键）、CTA barrier、atomics（单线程序列化 RMW）、以及 ordering/barrier 的负向误用测试。
  - Source DEC: DEC-1
  - Current-loop handoff: AC-6（内存空间在状态契约中存在但为空、`mem-ops==0`；FUT-2 为其填入字节与 load/store 语义）。
  - Promotion trigger: ITS 循环全绿。
- FUT-3：阶段 ④ MMA + 数值 —— 统一 FDA(F=25) fused-dot-add、leader-issue SINGLETON（ELECT 唯一）、C/D 驻 tensor-mem、MX/NVFP4 block-scale、correctly-rounded MUFU.EX2/LG2、与生产数值代码分离的 MPFR/fp64 精确参考 oracle、以及 conformance 门禁（fp16/bf16 在 CI 穷举；fp32 2^32 离线/夜间）。
  - Source DEC: DEC-1
  - Current-loop handoff: 无 —— MMA/tensor 指令不在样例子集，本循环不解码。FUT-3 在 MMA 循环里扩展表驱动生成器（AC-3）并新增自己的 schema/layout 项与数值 oracle；不声明任何本循环产物。
  - Promotion trigger: 内存循环全绿。
- FUT-4：阶段 ⑤ Transformer block —— attention（QKᵀ/softmax/×V）+ FFN + LayerNorm + residual，经 kernel_builder 层；依赖驱动的指令清单（把每条所需指令/语义角落映射到已实现 opcode）；Tier-1 GOLD 对自身 MPFR/fp64 精确规范 0-ULP（硬要求）；Tier-2 SANITY 对独立 fp64（numpy 主、PyTorch 交叉）用 `|a−b| ≤ atol + rtol·|b|`，容差暂定（rtol=2^-13、RMS≤2^-16、atol=2^-20），按阶段实测收紧、下限不低于解析界（方向性目标、非固定门）。
  - Source DEC: DEC-1
  - Current-loop handoff: AC-4（`emit()` 产出可解码的 128-bit 指令流——kernel_builder 层将建于其上的汇编器契约）与 AC-6（状态-diff 工具 = 端到端 oracle 基底）。
  - Promotion trigger: MMA+数值循环全绿。
- FUT-5：草稿声明的范围外（spec §2 推迟，独立于任何计划决策）—— cycle/timing 与性能模型；LLVM 后端；形式化内存一致性（litmus 作独立工具）；FPGA/RTL；超出非破坏接缝的完整 cluster/DSMEM；Transformer 用不到的指令。
  - Promotion trigger: 功能 ISS 完成后的路线图。
- FUT-6：生产 layout conformance（草稿 §7 编码私有化设计）—— 将生产 `layout` 放入独立私有仓库，并在公有仓库中以 submodule 路径注册；有私有仓库访问权限者可拉取该 submodule 并在私有 CI 跑完整真实编码 conformance，无权限者仅获得公有仓库与样例 layout；从 Python 表生成 CSV 只读导出（供 RTL/反汇编/文档）。
  - Current-loop handoff: AC-2（样例 layout 走通的 schema/layout 间接层，正是生产 layout 接入的同一接缝）与 AC-7（私有化门禁）。
  - Promotion trigger: 地基循环全绿 + 私有 layout submodule 就绪。

## Claude-Codex Deliberation

### Agreements
- 双方一致：loop-1 应为地基纵向切片（阶段 ① + 工程/构建/codegen/CI/状态-diff），阶段 ②–⑤ 作为后继循环。
- 双方一致：解码器 completeness 指「所选 layout 下全部 schema 指令的完整机器契约」（绑定、字段宽度/偏移、符号扩展、guard、控制段、reserved 位策略、别名），而非 2^128 枚举；no-overlap 为符号化 (mask,value) cube 不相交检查。
- 双方一致：本循环必须显式定义架构状态 diff 契约、陷阱分类、指令子集与 pybind 粒度。
- 双方一致：私有化门禁须用 tracked-file 检查（`git ls-files`）+ 包内容检查，而非仅靠 clean-working-tree 的 `git status`。

### Resolved Disagreements
- 「regen-clean」机制：草稿 §11 提到 `git diff --exit-code` regen-clean 门，但草稿同时禁止把生成的 decoder/emit 入库（红线 ①）。Codex 首轮指出冲突。采纳的解决：不入库红线优先；「regen-clean」实现为确定性生成（两次构建逐字节一致、变更 `PYTHONHASHSEED`）+ `git ls-files` 与包内容私有化门禁（落于 AC-7）。
- AC-6 内存过度声明：round-1 评审指出负向测试引用了 loop-1 从未实体化的「内存字节」。解决：loop-1 让内存空间存在但为空（`mem-ops==0`），局部化-diff 负向测试改用 GPR/谓词/PC 字节；内存字节级 diff 移至 FUT-2。
- FUT 交接准确性：round-1 评审发现 FUT-1/3/4 引用了 AC 并未要求的产物。解决：AC-5/AC-6 现显式要求 per-lane PC/lane_state/Bx/计数器等交接产物（供 FUT-1、FUT-2）；FUT-3 现声明无本循环产物；FUT-4 明确指名 AC-4 + AC-6。

### Convergence Status
- Final Status: `converged`（两轮收敛后结构无剩余 REQUIRED_CHANGES、无未决分歧；DEC-1..DEC-4 已由用户决策，FUT-1..4 与已决 DEC-1 联动闭合）。
- 收敛轮次：Codex 首轮分析 1 次 + Claude/Codex 收敛 2 轮（round-1 提出 7 项 REQUIRED_CHANGES 已全部在 v2 解决；round-2 判定 converged）。

## Pending User Decisions

> 以下决策已在生成阶段由用户确认（无剩余 PENDING 项）。

- DEC-1：本实现循环的范围。
  - Claude Position: 地基纵向切片（阶段 ① + 不可逆的工程/构建/codegen/CI/状态-diff 地基）；阶段 ②–⑤ 作为后继循环（FUT-1..4）。
  - Codex Position: 相同；另提出更细拆分（loop-1a codegen/round-trip/私有化，loop-1b ISS 核/pybind/状态-diff/极小 kernel）作为降风险备选。
  - Tradeoff Summary: 地基范围让「完成」可验证、降不可逆工程形态风险、把 MPFR/数值/Transformer 顾虑移出 loop-1；五阶段单循环贴合草稿字面但完成期长、判据到最后才明确。
  - Decision Status: 地基骨架（草稿阶段 ① + 工程/构建/codegen/CI/状态-diff 地基）；阶段 ②–⑤ 推迟为后继循环，见 FUT-1、FUT-2、FUT-3、FUT-4。
- DEC-2：阶段 ① 指令子集。
  - Claude Position: ALU（IADD3 含 MOV 别名、ISETP、LOP3）+ `@P` 谓词 guard + 非分歧/uniform BRA + EXIT。
  - Codex Position: 限于该集合即合理（IADD3/MOV 别名、ISETP、LOP3、guarded 执行、uniform BRA、EXIT）。
  - Tradeoff Summary: 仅 ALU 不测控制流；ALU+谓词+非分歧分支+EXIT 覆盖 guard/PC/停机，同时把分歧重聚留给阶段 ②。
  - Decision Status: ALU + 谓词 + 非分歧/uniform 分支 + EXIT（IADD3 含 MOV 别名、ISETP、LOP3、`@P` guard、BRA、EXIT）。
- DEC-3：畸形程序 / 陷阱模型。
  - Claude Position: 结构化确定性陷阱分类——assemble 期校验错误、decode 陷阱、execute 陷阱、max-step 陷阱——外加既定的 EXIT/停机状态。
  - Codex Position: 开放问题（询问 trap-in-ISS / assembler-reject / UB 三选一；需在 AC-5/AC-6 收敛前确认）。
  - Tradeoff Summary: 结构化陷阱给出确定性负向测试、对齐 ISA 非法情形措辞（SINGLETON、membermask⊆active、leader 唯一）；纯 UB 最简但不可测；仅汇编器拒绝会漏掉运行期条件。
  - Decision Status: 结构化陷阱 + 汇编期检查（运行期确定性 trap taxonomy + assemble 期校验）。
- DEC-4：地基循环的公开编码私有化姿态。
  - Claude Position: 样例 `layout` 用与生产完全不同的占位 opcode/字段值；生产 `layout` 置于独立私有仓库并通过 submodule 接入；生成的 decoder/emit、生产 layout 内容与生产 `.bin` 既不在公开 `git ls-files` 也不在任何公开 wheel/sdist（公开产物只内嵌样例 layout，生产编码只在私有 CI 跑）。
  - Codex Position: 开放问题（询问样例 layout 是否可用假位置，以及产物是仅排除 git 还是也排除分发包）；指出仅 git 私有显著弱于 git+包私有。
  - Tradeoff Summary: 假样例 + 公开产物仅样例，既保编码私有又使公开 CI 有意义；私有 submodule 让有权限者获得完整生产编码资产，无权限者仍能使用公有仓库；入库/分发任何生产 layout 或生产派生产物会经 layout 本身、内嵌区分树或二进制泄露编码。
  - Decision Status: 假样例 + 私有仓库 submodule + 公开物零泄露（占位 layout 值；生产 layout、生成物与 `.bin` 排除于公开 `git ls-files` 及公开 wheel/sdist）。

## Implementation Notes

### Code Style Requirements
- 实现代码与注释**不得**包含计划专用术语，如 "AC-"、"Milestone"、"Step"、"Phase"、"FUT-" 等工作流标记。
- 这些术语只属于计划文档，不进入最终代码库。
- 代码中用描述性、贴合领域的命名（如 `decode_once`、`masked_lane_loop`、`state_snapshot`、`roundtrip_check`、`completeness_gate`、`no_overlap_gate`）。
- 代码中只用英文；与用户的对话用中文（与本仓库既有约定一致）。

--- Original Design Draft Start ---

# curryGPU 功能级 ISS — 设计 spec

> 行为级 SIMT 指令集模拟器。bit-exact、无时序;既是 curryGPU ISA 的可执行规范 / 正确性 oracle,又是 workload 运行台。
> 深度技术依据与引用见同目录 `research-notes.md`。本文件只写方案、范围、风险与验收,不含实现代码。

## 1. 目标

在写任何 RTL 之前,用一台可执行模型确证 curryGPU ISA 的**功能完备与自洽**:能编码、能解码、能执行、能跑通真实 workload 并产出 bit-exact 正确结果。北极星 = 跑通一个小 Transformer block。

## 2. 范围

- **内**:声明式编码表 → 生成 C++ 解码器 + Python 汇编 API;标量 + warp + ITS 执行核;内存空间(global/shared/local)与 barrier;MMA(bit-exact)、SFU、归约、async-copy/TMA(eager);kernel-builder;验证框架。
- **外(后置)**:cycle/timing 与性能模型;Transformer 用不到的指令;LLVM 后端;形式化内存一致性(litmus 留独立工具);FPGA/RTL;cluster/DSMEM(留接口,不强依赖)。

## 3. 架构概览

单一真相源 = Python 声明式编码表。数据流:

```
encoding_table (Python,单一源)
   ├─ 生成 → C++ 解码器 (bit 区分树, 128-bit) ───────────┐
   └─ 生成 → emit() 汇编 API (Python) ──┐                 │
kernel_builder (Python emit) → 128-bit 指令流 ────────────┤
                                                          ▼
                       C++ 功能核: decode-once → per-PC grouping (ITS)
                                  → masked 32-lane execute → 架构状态
                                                          │
                       pybind (粗粒度: launch / step / state-diff)
                                                          ▼
                       test_harness (Python: 差分 / conformance)
```

要点(依据见 `research-notes.md`):decode-once / masked-lane-loop;SoA per-lane 寄存器堆;ITS = per-thread PC + 收敛屏障(非 IPDOM 栈);21-bit 控制段解码但功能 no-op;MMA = leader-issue bit-exact tiled matmul(ELECT 选 leader 发描述符;A/B 经 shared descriptor、C/D 驻 tensor-mem,无 GPR fragment);功能/timing 严格分离(留接口供日后包 timing)。

## 4. 组件与职责

| 组件 | 职责 | 输入 → 输出 | 依赖 |
|---|---|---|---|
| `encoding_table` | 声明每条指令的 bit 布局 / 操作数 / 语义类(单一源,双向) | — → 编码表数据 | — |
| `gen_decoder` | 由表生成 C++ 解码器 + JSON IR + emit() 钩子;做完备性 / 无重叠检查 | 表 → 生成代码 | `encoding_table` |
| `kernel_builder` | emit() API 程序化构造 kernel 指令流 | 表 + 用户 kernel → 128-bit 二进制 | `encoding_table` |
| C++ 功能核 | 解码、ITS 调度、masked-lane 执行,内存 / barrier / MMA / SFU 语义 | 二进制 + 输入 → 架构状态 | 生成的解码器 |
| `pybind` | 暴露 launch / step / state 检查(粗粒度边界,非 per-instruction) | — | C++ 核 |
| `test_harness` | 差分测试 + conformance gate + 架构状态计数器 diff | — | `pybind` |

数据结构蓝图与文件落位草案见 `research-notes.md`(数据结构蓝图 / 最小骨架两节)。

## 5. 分阶段交付与验收(approach A:纵向切片直奔 Transformer)

每阶段可独立运行、可验证、可回滚。

- **① 编码 / 解码骨架**:编码表 + 生成解码器 + emit();标量 + warp 核跑几条 ALU 指令。
  验收:解码器完备性 / 无重叠检查过;encode↔decode round-trip 零差异;手写 kernel 执行结果对。
- **② ITS 分歧**:per-thread PC + per-PC grouping + 收敛屏障(BSSY/BSYNC/BREAK/CONT/YIELD/EXIT)。
  验收:分歧 corpus(if/else、嵌套、循环 break/continue、early-exit、subwarp collective)最终态对;**调度序无关性**(≥3 种确定序最终架构态 bit 一致)= 主门;每 BSYNC 重聚 mask 断言。
- **③ 内存与同步**:memory_space(global/shared/local)+ barrier + atomics。
  验收:shared-mem reduction、barrier 类测试结果对。
- **④ 张量与数值**:MMA(bit-exact,统一 FDA(F=25))、SFU exp、归约、低精度(MX/NVFP4)量化。
  验收:GEMM tile 对 **FDA(F=25) 精确参考** bit-exact(非 `numpy.matmul`——累加方式不同,仅作量级 sanity);exp conformance ULP 达标。
- **⑤ 组装 Transformer block**:attention(QKᵀ / softmax / ×V)+ FFN + LayerNorm + residual,用 kernel-builder。
  验收:**两层**——Tier-1 对自身 MPFR/fp64 精确规范 0 ULP;Tier-2 对 fp64 numpy/PyTorch 参考用 `atol+rtol·|b|`(暂定 rtol 2⁻¹³,阶段实测收紧),非 bit-exact。

## 6. 整体验收标准(可运行 / 可验证 / 可回滚)

编码表单一源驱动 decoder + assembler、round-trip 零差异、完备性检查过;各阶段单测 + 差分测试过;小 Transformer block 端到端跑通且数值在文档容差内;架构状态计数器可导出(留 trace / timing / 对硬件 diff 接口)。

## 7. 关键决策(已定;个别参数留阶段实测收紧)

- **ITS 实现**(已定):per-PC grouping(per-lane PC + 收敛屏障状态机 BSSY/BSYNC/BREAK/CONT/YIELD/EXIT,最小-PC-优先确定序)——这是 post-Volta ITS 架构语义本身、非简化(Hanoi arXiv:2407.02944 §IV)。**最终架构态与调度序无关**(对尊重同步契约的程序可证)= 阶段 ② 主验收门。拒绝 Hanoi 双栈(纯 timing 拟合)。规范见 `research-notes.md` §5。
- **MMA 累加方案**(已定):统一 **FDA(F=25)** fused-dot-add——对齐 e_max 截 25 fraction bits、定点求和(顺序无关)、RNE 规格化到 FP32;保留 subnormal、无中间溢出、仅对称舍入;所有 `.kind` 一致。依据 MMA-Sim(arXiv:2511.10909),规范见 `research-notes.md` §6。阶段 ④ 仅落实 conformance 与端到端容差(#5)。
- **SFU exp**(已定):spec-faithful = **correctly-rounded `MUFU.EX2`**(`RN(2^x)`,≤0.5 ULP vs MPFR;`exp=exp2(x·log2e)`),并入 §9(a) 正确舍入类——clean-slate 自验证无需复刻 HW SFU 的 faithful 怪癖(Oberman & Siu ARITH'05;CR f32 exp2 已由 LLVM-libc/CORE-MATH 穷举可证)。穷举 conformance gate(fp16/bf16 全、fp32 全 2³²)。
- **cluster / DSMEM**(已定):延后,只留非破坏接缝——`cluster_state` 持 `block_state[N]`、`cluster_dim` 默认 1(=恒等,单-CTA 行为 bit 不变);**DSMEM 非第 7 空间**(=`shared`+remote 选择子,ISA §02)。size-1 退化合法、真·跨-CTA 访问干净 `unsupported` trap,绝不伪造远端态/别名到 self。北极星(单 / 独立 CTA)不需 cluster(FA-3 仅 ~2% 可选优化)。
- **数值容差**(已定方法学,数留实测):**两层**——Tier-1 GOLD 对自身 MPFR/fp64 精确规范(FDA(F=25)+CR EX2+IEEE-RNE)**0 ULP**;Tier-2 SANITY 对 **fp64 独立**参考(numpy 主、PyTorch 交叉)用 `|a−b|≤atol+rtol·|b|`,暂定 `rtol=2⁻¹³`、`RMS≤2⁻¹⁶`、`atol=2⁻²⁰`,阶段 ④/⑤ 按实测 max×安全系数收紧。**绝不对 numpy bit-exact**。规范见 `research-notes.md` §9。
- **代码落位**(已定):仓库根 = **整个 curryGPU 工程**(非仅 ISS)。顶层 `isa/`(ISA 单一源:bit 编码表 + codegen + emit/assembler + JSON IR,**被 ISS 现用 / 未来 RTL 解码器 / 汇编器 / 文档共享**)+ `iss/`(功能 ISS 独立子树:C++ 核 + 粗 pybind + tests)+ 现有 `docs/`;RTL / 验证(`rtl/`、`dv/`)/ 软件未来按需新增,**现在不建空目录**。构建**按组件自治**(build 文件在 `iss/`:CMake + scikit-build-core;`currygpu` 作 namespace package 横跨 isa+iss;**不在仓库根放 Python 包文件**),`iss` build 时单向调 `../isa` 生成器(**ISA→ISS 单向,严禁反向**);C++ 解码器 build 时生成(不入库 + CI regen-clean 门);粗 pybind 边界(仅 launch/step/state-diff)。布局见 `research-notes.md` §11。
- **单一事实源格式**(已定):声明式 **Python 编码表 = 权威源**,直接驱动 C++ 解码器 + `emit()` 汇编器 + JSON IR(双向,round-trip 可证);CSV 仅作**从 Python 表生成的只读导出**(供未来 RTL / 反汇编 / 文档),**绝不手维护第二份源**。表列含 per-instruction 机器契约:operands / modifiers / resource_domain / instruction_type / scoreboard 用法 / SASS 对齐。
- **编码私有化(开源工程 / 私有编码)**(已定):工程开源,但**精确 bit 编码(opcode 数值 / 字段偏移 / `fsel` 映射)私有**——延续「借模型、自定义编码」哲学(语义公开、编码私有)。`isa/` 拆两层:**`schema` 公开**(指令 / 操作数 / 语义 / 字段名 + 宽度)+ **`layout` 私有**(bit 偏移 / opcode 值 / fsel)。生产 `layout` 放入独立私有仓库,在公有仓库中以 submodule 注册;有私有仓库访问权限者可拉取完整生产编码资产,无权限者只能看到公有仓库与样例 layout。生成器 = `schema + layout → decoder / emit / IR`,**引擎与编码无关**(语义 handler 吃已解码字段、不碰 raw bit);公库带**样例 layout**(够 build / CI / demo,也是默认构建选择)、私有 submodule 注入**生产 layout**(需显式选择),公共 CI 跑样例 conformance、私有 CI 跑真实全量。三红线:① 生产 layout 内容与生成的 decoder / emit **不入库**(内嵌区分树可反推编码);② 真实编码 `.bin`(kernel 二进制)**不公开**——kernel 以 `kernel_builder` 源码(symbolic)公开、私有 build 现场编码;③ JSON IR 为 post-decode 语义、**可公开**。
- **GPR 数据类型/宽度语义**(已定):GPR = **无类型 32-bit word**(编码里的「8-bit」是寄存器索引、非数据宽度;物理容量 uarch 定),类型随指令 modifier。sub-word(8/16-bit) 写 **extend-to-32**(按签名 sign/zero,**无 partial-register merge**——循 ARM AArch64 / RISC-V,拒 x86 AL/AX 合并 hazard);`LD.U8/.S8/.U16/.S16` 扩展、`ST.U8/.U16` 写低位;64-bit=偶对齐对 `R(2n):R(2n+1)`、128-bit=4-对齐组;packed=SIMD-within-word(小端);bit-reinterpret 原样转译。规范见 `research-notes.md` §4。

## 8. 风险

- **ITS 功能实现**(已收敛):per-PC grouping 即 post-Volta ITS 架构语义(US10067768B2 + Hanoi §IV),最终态调度序无关可证;残留 = 分歧 corpus + 序无关性测试落实,阶段 ② 收口。
- **MMA / 低精度 bit-exact 数值**(已收敛):规范已定为统一 FDA(F=25)(见 §7 / `research-notes.md` §6),依据 MMA-Sim 实测;残留风险 = conformance harness 落实 + dequant/scale 边界处理,阶段 ④ 收口。
- **128-bit 定长解码器生成**:主流工具(decodetree)对 >32-bit 支持弱,需 APInt 式宽提取,可能要自写生成器核心。
- **Python / C++ 边界开销**:必须保持粗粒度(launch / step 级),避免 per-instruction 过界拖慢热循环。

## 9. 参考

深度技术依据、对照系统与引用见同目录 `research-notes.md`。
/home/yanggl/code/nv_patent
/home/yanggl/code/sm100a

--- Original Design Draft End ---
