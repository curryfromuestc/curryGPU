# Stage-2 ITS（Independent Thread Scheduling）— 调研要点（设计输入）

> curryGPU 功能级 ISS 阶段 ② 的设计参考，对应 `plan_foundation.md` FUT-1。综合 foundation 代码现状（`iss/binding/native.cpp`、`isa/currygpu/isa/*`）、curryGPU 自有契约（`spec.md` §5/§7、`research-notes.md` §5、`nv_patent/spec/isa/control-sync-uniform.md`）、SASS ground-truth（`sm100a`）、NVIDIA 专利与微架构分析，以及 2024–2026 的形式语义 / 规范工作，按主题内联引用。本文件只写方案、范围、依据与决策记录，**不写实现代码、不是 plan**，喂入后续 planning loop。技术术语 / 助记符 / 文件路径 / 标识符保留 English，量化结论附适用条件。冲突处显式标注；被对抗评审驳回的设计选择反映其修正，不掩盖。

## 1. 摘要

阶段 ② = ITS：把 foundation 已就绪但**惰性**的 per-lane 基础设施激活，使 warp 内 lane 能在不同 PC 上分歧、并在显式收敛屏障处重聚。**「具体怎么实现」一段话答案**：

把 `step()` 的「统一-PC fetch」换成 **per-PC grouping 调度器**——每步把 active lane 按相同 `pc_[lane]` 分组，按一个**确定的全序**（min-PC-first 为默认）选一组，decode 一次，仅对该组 mask 执行；handler 算 per-thread next-PC，lane 自然分裂/推进。新增一套 **16 槽收敛屏障状态机**（`Bx{participation_mask, reconv_pc, valid}`）由 `BSSY`/`BSYNC`/`BREAK`/`CONT`/`YIELD`/`EXIT` 驱动：`BSSY` 把当前组 mask 快照进 `Bx`、记 reconv 目标；`BSYNC` 把到达 lane 置 `blocked`，当该 `Bx` 全部参与 lane 到达后整组在 `reconv_pc` 重激活；`BREAK` 把 lane 从参与集清位；`EXIT` 退休并从**所有** `Bx` 清位。删掉 foundation 的 `non_uniform_pc` / `non_uniform_branch` 两个 trap，新增分歧/非法情形的结构化 trap。**主验收门 = 序无关性 metamorphic 测试**：≥3 个具名确定序在尊重同步契约的分歧 corpus 上产出 bit-identical 最终架构态。per-PC grouping 即 post-Volta ITS 架构语义本身（Hanoi arXiv:2407.02944 §IV；NVIDIA Volta whitepaper 2017-08；Khronos maximal reconvergence 2024-01），非简化、非 IPDOM 栈。

**对抗评审的净结论**：核心闭包性质（纯分支 + `BSSY`/`BSYNC`/`BREAK`/`EXIT` 子语言）成立；查实的两处 spec 漏洞（`YIELD` arrival 语义、collective 摆放 well-formedness）与三个 fork（OD-1/2/3）已由用户逐个裁决并锁定（§11 决策记录：participation = GROUP-mask、屏障谓词策略 A、collectives 最小 NV 集 IN），勘误已落实于 `research-notes.md`。

## 2. Foundation 现状与改动面

foundation 核（`iss/binding/native.cpp`，731 行，commit `edb638b`）已带 ITS-ready 但当前惰性的 per-lane 基础设施。改动面分 STAYS / CHANGE / ADD 三类（来源 G1/G2，行号已对当前树核验）。

**惰性钩子（现状）**：

- **`pc_[32]`**（`native.cpp:641`）：per-thread PC，`fill(0)`，commit 路径已是 per-lane——`for lane: if(issued_mask[lane]) pc_[lane]=next_pc[lane]`（L360-364）。**这是最关键的事实**：handler 已返回 `std::array<int,kLanes>`，writeback 已尊重 per-lane PC；唯一强制统一的是 *fetch*（`current_pc()`），不是 *commit*。
- **`lane_state_[32]`**（`native.cpp:642`，`std::array<std::string,kLanes>`）：仅用 `"active"`/`"exited"`（L277/292/625）。阶段 ② 扩展 `"blocked"`/`"yielded"`。
- **`Bx[16]`**：**不是成员字段**——`snapshot()`（L430-440）发 16 个硬编码字典 `{participation_mask:0, reconv_pc:0, valid:false}`。收敛屏障态完全未实现，只存在为快照 schema 槽。
- **`divergence_events`**（`native.cpp:47`，发于 L388，测试断言 `==0`）：从不自增。
- **两个分歧 trap**：`current_pc()`（L449-462）扫 active lane，PC 不一致返回 `-1` → `step` L307-309 触发 `non_uniform_pc`（fetch-time 门）；`exec_bra`（L592-619）施加分支后再扫，`next[lane]` 不一致 → L614 触发 `non_uniform_branch`（branch-time 门）。

**CHANGE（阶段 ② 的核心）**：

1. **替换 fetch 模型**：`current_pc()`（L449-462）停止在非统一 PC 上返回 `-1`；新调度器 `build_groups()`+`select_group()` 按 `pc_[lane]` 分组、确定序选一组。删 `non_uniform_pc` trap（L307-309）。
2. **`step` masking**（L322-333）：`lane_mask`/`issued_mask` 与所选组求交，非 `active_mask_` 单独（L331）。`counters.instructions`（L333）只计执行子组——**这改变 `instructions==192` 不变式，仅对分歧程序**；现有 uniform 测试仍 32-wide，保持绿。
3. **`exec_bra`**（L592-619）：删去统一性 re-scan（L606-617）+ `non_uniform_branch` trap；保留 target 界检（L594-599）与 per-lane `next[lane]=target` 写（L600-605）；当 `next` 切分 active lane 时自增 `divergence_events`。
4. **`snapshot`**（L430-440）：发真实 `bx_` 成员而非字面量（同 3-field shape，测试 L147 在无分歧时保持绿）。

**ADD**：真实 `std::array<Barrier,16> bx_` 成员；`BSSY`/`BSYNC`/`BREAK`/`CONT`/`YIELD` 的 decode（`instruction_from_word` L211-251，现仅 IADD3/ISETP/LOP3/BRA/EXIT）+ dispatch arm（L336-350）；`maybe_reactivate_barriers()`；`barrier_deadlock` trap；`lane_state_=="blocked"/"yielded"` 维度被调度器消费。

**STAYS（载荷，勿动）**：全部 `exec_*` ALU/predicate body（L521-590）；`guard_mask`（L469-480）；per-lane commit（L360-364）；`exec_exit`（L621-628）；`fallthrough`（L482-488）；decode/trap plumbing；pybind 边界（L709-731）。**uniform 程序回归保证**：`build_groups()` 产单组 → `min_pc_first` ≡ 当前行为 → bit-identical，`divergence_events==0`。

**回归基线（核验）**：`tests/iss/test_native.py` = **19** 个具名 test fn（非提案中称的 23；parametrization 可能放大）；`tests/isa/` = 30 个（codegen 16 + assembler 14）。阶段 ② 须保持这些 bit-identical（min-PC 在 uniform 下退化）。

## 3. ITS 语义依据（per-PC grouping = post-Volta 架构语义；收敛屏障 vs 栈）

**per-PC grouping 是 ITS 语义本身，非简化**——多源一致：

- **Hanoi**（arXiv:2407.02944 v1，2024-07-03，UPC；逆向 Turing 二进制/trace）：post-Volta ITS 由**单一规则**定义——「同 PC 的 warp 内线程可在任意周期同调」（L362-364），重聚可「早/晚/完全省略」相对 IPDom（L367-369）；显式框架为它逆向出的*真实*语义、非简化（E11）。**重要修正**：Hanoi 给的是 *plausible* 语义、**无形式闭包定理**（E11 §3）；`research-notes.md` §5 将闭包归因 Hanoi §IV 是误植——应改引 Habermaier-Knapp（见下）。Hanoi 双栈（WS+REC）是 *timing* 重构、应拒（E11 §4，与既定决策一致）。
- **NVIDIA Volta whitepaper**（WP-08608-001 v1.1，2017-08，E6）：「Volta maintains per-thread … PC and call stack」——`pc_[32]` *就是* 规范，激活它是补全正确性、非扩展。`__syncwarp` 是**唯一**硬重聚保证：「all execution pathways … complete before any thread reaches the statement after」——即 `BSYNC` 的精确语义。CUDA Guide v13.3（2026-current，E6）是最新规范 UB 条款：无 `__syncwarp`/`BSYNC` 隔离的 warp 内数据交换 = UB，其最终态**无需**序无关。
- **NVIDIA 专利**（US10067768B2 / US20160019066A1，Family A，E8/G4）：`ADD`/`WAIT` ≡ `BSSY`/`BSYNC`；收敛从栈项 `(pc,mask)` 升为独立 barrier 对象，blocked lane 的 PC 存于 per-thread register、解锁时从该 PC 续跑。**前向进度是硬契约**（「no thread can indefinitely block … any other thread」）。**新增 Family B（US11442795B2，granted 2022-09-13；US11847508B2，~2023-12，E8）**——比现有引用更新：明确 soft/opportunistic barrier clearing 是「for performance, not correctness, and are not guaranteed」，即 **timing-only，功能 ISS 只实现 hard all-arrive `BSYNC`、绝不实现 threshold-clear**，否则破坏序无关门。
- **SASS ground-truth**（`sm100a`，Blackwell SM100，G6/E18）：`BSSY`/`BSYNC` 带 `RELIABLE RECONVERGENT` 标志位，`BREAK` 仅 `RELIABLE`（**非 RECONVERGENT**——载荷区分：`BREAK` 改 mask 不重聚）。模型 Volta→Blackwell **前向稳定**（E9：16 B-register 在 RTX A6000 实测确认；E18：Blackwell tcgen05 单线程发射 + ELECT-leader 反而*移除* MMA 收敛前提）。

**形式闭包/序无关的正确引用**（比 `research-notes.md` 现有列表新）：

- **Habermaier & Knapp, ESOP 2012**（LNCS 7211，E11/E13/GAP-parallelization）：构造 SIMT 语义与标准交错多线程语义间的 **simulation**，证明正确性——这才是「最终态与调度序无关是闭包性质」的引用，metamorphic 门是其 simulation relation 的可执行实例。**载荷边界条件**：序无关仅因「所有 active lane 在任何 lane 写之前先求值 RHS」成立，冲突写显式留 undefined。**警告**：min-PC 是 *unfair* 的（自旋循环可被饿死），序无关只对*终止、无竞争、尊重契约*的程序成立——须作门的*文档化前提*，非 bug。Caveat：该证明是 stack-based / pre-Volta，simulation 论证是模板、需对收敛屏障 mask 重铸。
- **LLVM Convergent Operations / Convergence and Uniformity**（trunk 23.0.0git，2026-06-06，E12/E13）：convergence token = instruction 的 *dynamic instance*；**m-converged** = 收敛关系在所有 cycle hierarchy 下不变（独立于遍历）——序无关门的形式同源。**载荷**：「同 PC + 不同 loop-trip ⇒ 非收敛」，重聚须 key on barrier-token 身份 + trip index，非 raw PC。
- **Khronos maximal reconvergence**（SPV_KHR / VK_KHR，ratified 2024-01-25，GAP-Khronos）：vendor-neutral、且为 LLVM m-converged 的上游规范，**此前无 agent 引用**。tangle = 执行同一 *dynamic instance* 的 invocation 集合；loop 迭代显式为不同 dynamic instance；switch/unstructured 显式**不保证**单一行为——为「BRX 入阶段 ② 须 trap 或限制」提供规范依据。
- **2024–2026 confluence/semantics（新）**：Dubey et al.「Equivalence Checking of ML GPU Kernels」（arXiv:2511.12638 / 2511.10909 对应工具，2025-11，E13）——首个 sound（结构化 CTA 类下 complete）GPU kernel 等价检查器，核心是 **confluence**，是序无关门的可机械化形式；SIMT-Step（PLDI 2026，E16）——vendor-neutral TLA+-validated operational semantics 显式建模 independent 模式，是最近邻工件（语义/TLA+、非 bit-exact C++ 核）。

**对比基线（拒绝的栈式，多源确认我们与主流不同）**：GPGPU-Sim / Accel-Sim（单 IPDOM 栈 `(PC,RPC,mask)`；Accel-Sim trace-driven 模式*根本不重建*分歧、靠真硬件 trace mask——功能 oracle 无此选项，E1）、Vortex/SimX（`std::stack<ipdom_entry_t>`，E2）、Ventus（软件 SIMT-stack + `setrpc`/`join`，E3）、AMD GCN/CDNA（单 wave PC + EXEC mask，E19）、Intel Xe（单 EU IP + per-channel disable counter，E19）。**唯一同表示者 = Simty/Collange + NVIDIA 收敛屏障专利**：Simty（CARRV 2017，E4/E5）的 path-list「按 (call-depth, min-PC) 排序、合并同 PC path」是我们调度器的 RTL 存在性证明，且其作者明言「order of paths does not affect correctness」。**2024–2026 RISC-V-GPU 前沿仍是 IPDOM-stack**（Ventus ICCD 2024、MDPI Electronics 15(1):125 2025、VOLT 2025-11），故我们的 per-PC 功能 oracle 占据一个*未被占据*的 niche（E5/E16/E17 一致）。

## 4. 收敛屏障状态机（transition table + 数据结构）

数据结构（`native.cpp` 私有成员，来源 D2，经 V2/V-synth 修正）：

```cpp
struct Barrier {                       // 映射 snapshot {participation_mask, reconv_pc, valid}
    std::uint32_t participation_mask = 0;  // BSSY 时快照的 GROUP mask（见 §11 OD-1）
    int           reconv_pc          = 0;  // BSSY 操作数：BSYNC 重激活目标
    bool          valid              = false;
};
std::array<Barrier, 16> bx_{};         // 16 槽，silicon 实测确认（E9 §5.2）
// lane_state_ 扩展 "blocked"/"yielded"，命名对齐 cuda-gdb info cuda lanes（GAP-cuda-gdb）
// 不新增 resume_pc_：pc_[lane] 即 per-thread resumption PC（US10977037B2，G4）
```

**关键裁决（修正 D2/D5 分歧）**：

- **不新增 `Bx.blocked_mask` 第 4 字段**（D2 §1 曾引入，V2 D-MAJOR-3 驳回）：`research-notes.md` §5 与 committed snapshot（`native.cpp:433-435`）冻结为 3 字段 `{participation_mask, reconv_pc, valid}`。`blocked` 状态在 ≤16 槽 rescan 时由 `lane_state_[l]=="blocked" && (participation>>l)&1` **派生**，保持快照 bit-identical、不重启已定契约。（替代方案 = 显式把「加 `blocked_mask` 改快照 schema」列为 open decision；本文取派生方案。）
- **不新增独立 `resume_pc_[]`**（修正 D5 与 D1/D2 的静默分歧，G-D）：`pc_[lane]` 复用为 resume 寄存器，对齐 US10977037B2 的「per-thread resumption PC is a register」。`BSYNC` 重激活时直接写 `pc_[l]=reconv_pc`。

**Transition table**（语义 Volta→Blackwell 冻结，E9/E18；`M` = guard mask ∩ 所选 PC-group 的执行子组）：

| op | 前提 | `Bx` 效果 | `lane_state`/`pc_` 效果 | 重聚触发 |
|---|---|---|---|---|
| **`BSSY Bx,t`** | guard | `Bx={participation=GROUP_mask, reconv_pc=t, valid=true}` | 执行 lane → fallthrough | — （arms barrier；分歧前必有 BSSY，E11 L518） |
| **`BSYNC Bx`** | `Bx.valid` | — | `M` lane → `blocked`，`pc_` 冻结 | **FIRE** 当 `participation ⊆ (blocked \| exited)` 时：survivors = `blocked ∩ participation` → `active`，`pc_=reconv_pc`，`Bx.valid=false` |
| **`BREAK Bx`** | `Bx.valid` | `participation &= ~bits(M)`（仅 active 执行 lane；inactive 成员不动，GAP-Collabora F2） | `M` lane → fallthrough | 可使 pending `BSYNC` cohort 完整 → 重评估 |
| **`CONT`** | guard | 无（**非** barrier op） | `M` lane → loop-header（BRA 回边） | — （重聚由 loop 自己的 `Bx` 管） |
| **`YIELD`** | guard | 无 | `M` lane → `yielded`（架构 no-op） | 强制调度器下步选另一 PC-group；`yielded`→`active` 当被重选 |
| **`EXIT/KILL/RET`** | guard | **∀ valid Bx: `participation &= ~bits(M)`** | `M` lane → `exited`，`active_mask_=false`（沿用 L621-628） | 该清位可使 pending `BSYNC` cohort 完整（starvation-free，G3/G4） |

**载荷不变式（勿略）**：

1. **`BSYNC` fire predicate = `participation ⊆ (blocked | exited)`，`yielded` 不计入 fire 集**（V1 REFUTATION-1 + V2 D-MAJOR-1 的修正，**直接修正 `research-notes.md` §5:61 与 control-sync-uniform.md:59 的「blocked/yielded/exited」措辞**）：`yielded` lane 是 *runnable-but-deprioritized*，**会**再到 `BSYNC` 翻为 `blocked`；若让 `yielded` 满足 fire predicate 但又不在 survivor 集，该 lane 被静默搁浅、或被传送到 `reconv_pc` 从未跑完其 body——两种设计（D1/D4 vs D2）在同一 kernel 上给出不同最终态，即序相关 bug。修正后 `YIELD` 成纯调度提示、零架构效果（对齐 G3/E10「YIELD must not change final state」）。
2. **survivor/resume 集 = `blocked_mask`（实际到达的 lane）**，拒绝 D2 §2 的「`participation ∩ ¬exited`」（V1 FIX-2）——否则从未到达的 lane 被传送到 `reconv_pc`。加 per-`BSYNC` 断言：fire 时 `resumed_mask == blocked_mask ∩ participation`。
3. **`BSYNC` 到达已 dissolved barrier**（`valid==false`，因所有参与者被 `BREAK` 清空）= **fallthrough，非 trap**（V2 D-MAJOR-2，与 D5 C4 调和）——否则 false-trap 合法的 break-all 循环。
4. **`EXIT`-clears-ALL-`Bx`** 是载荷 liveness（D5 C10，非 error）：退休 lane 留在 participation 会死锁对应 `BSYNC`。每次 `BSYNC`/`BREAK`/`EXIT` 后 re-scan 全部 valid barrier（≤16，廉价）。
5. **谓词化策略（OD-2 已定 A，§11）**：屏障 op 效果只施于 guard-true lane，guard-false in-group lane fallthrough、participation 成员资格不被该 op 改动（成员资格只在 `BSSY` 设定、之后仅由 lane 自执的 `BREAK`/`EXIT` 改）。**`BSSY`/`BSYNC` 必须无谓词**（predicated 形式由 §7 静态 pre-screen + §8 trap 判 out-of-scope——predicated `BSYNC` 会让 guard-false 成员越过同步却留在 participation → 死锁）；**`BREAK`/`YIELD` 可谓词**（只移除/提示、不 block，对终止结构化程序可证无死锁）。

**deferred（阶段 ② 外，但建模为 handle 以备后续）**：`BMOV`（Bx↔GPR spill；silicon 确认 `BMOV.32 B,R`/`BMOV.64 ATEXIT_PC`，`sm100a`；16 槽对深分歧不足、NVIDIA 在 >16 时溢出，GAP-Collabora F5）——阶段 ② 把 `Bx` 当不透明 handle，深分歧超 16 槽时显式 trap「Bx exhausted」（见 §8 C5），corpus 限 ≤16 live barrier。`CONT` 无独立 SASS 助记符（G6/isa.json 确认），建模为 BRA 回边、零新 decode 面。

## 5. per-PC grouping 调度器 + ≥3 确定序

替换统一-PC `step()` 循环（来源 D1，经 V1 修正）。结构（伪码）：

```
step(max_steps):
  while not all_exited() and trap==none:
    if issued >= max_steps: trap(max_steps); break
    groups = build_groups()              # map<pc, mask> over lanes where state=="active"
    if groups.empty():
        try_fire_barriers()              # 可能把 blocked→active
        recompute groups
        if still empty:
            if any lane in {blocked,yielded}: trap("convergence","barrier_deadlock"); break
            else: break                  # 全 exited → 退休
    (pc, lane_mask) = select_group(groups, order_)   # SEAM：确定序，§下
    inst = program_[pc]                  # decode-once，沿用 L315-321
    guard = guard_mask(inst.guard) ∩ lane_mask        # CHANGE：与所选组求交（原 L331 用 active_mask_）
    issued_mask = lane_mask
    next_pc = fallthrough(); dispatch(inst, guard) -> 改 next_pc / 改 bx_ / 改 lane_state
    if trap: break
    for lane in issued_mask: pc_[lane] = next_pc[lane]   # 沿用 L360-364
    ++issued
  return snapshot()
```

**read→compute→commit 三相**（序无关门的 soundness 基础，GAP-parallelization arXiv:2502.14691）：一步内所有源操作数从 *pre-step* 态读、per-lane RHS 算入私有临时、再 commit GPR/PC/lane_state/`bx_`——无 lane 观察到另一 lane 的同步写。任何跨-lane 归约（`VOTE`/`ELECT`）**必须**消费 pre-step 态、且用 **pinned 升序-lane 归约序**（arXiv:2105.00069 的 total-order tie-break：(PC, then lane-id)；arXiv:2408.05148 的 FP 非结合警告），否则 bit-exact 门静默破裂。

**≥3 具名确定序**（metamorphic 门的自由参数，E4/E5/G8/GAP-cuda-gdb）。单 `enum SchedOrder` 喂 `select_group()`；均为**对 distinct group-PC 的全序**，故对契约程序最终态可证序无关（E4「order does not affect correctness」、E13 confluence）。**每序须 fair**（永不无限跳过 runnable group，E4/E13/E14/GAP-busy-wait）否则自旋/YIELD corpus 在一序挂、另一序不挂——这本身是序相关（deadlock vs progress）：

1. **`min_pc_first`**——最低 PC 组（canonical/spec 默认，G3/E4）。
2. **`max_pc_first`**——最高 PC 组（逆比较器；压 join 对称性）。
3. **`round_robin`**——按 PC 排序后游标轮转，一步一组（模 warp-RR，G8；映射 cuda-gdb `step_divergent_lanes` On/Off，GAP-cuda-gdb F4）。
4. **`oldest_group_first`**（可选第 4）——按 `BSSY`/分歧处盖的单调 group-creation seqno（PC-无关，直接压前向进度，G8）。

distinct PC 分区 lane，故 {1,2} 无需次级 tie-break；{3,4} 带显式游标/seqno。门：实例化 {1,2,3}(+4)，在无竞争分歧 corpus 上断言 `snapshot()` bit-identical；并在每 `BSYNC` 断言 `divergent_mask==0`（GAP-cuda-gdb F2）。

**调度器状态命名对齐 cuda-gdb（免费 silicon oracle，GAP-cuda-gdb）**：`info cuda lanes`（`Ln,State,PC,ThreadIdx` → 我们的 per-lane 快照）、`info cuda warps`（`Active/Divergent Lanes Mask, Active Physical PC` → 当前 PC-group）、`info cuda barriers`（`Thread State, Active, Exited, Warp Convergence Barrier(s)` → `Bx[16]` 槽）。**拒绝**把 cuda-gdb 的 `"divergent"` 加入 `lane_state_`（relitigate 已冻结的 `research-notes.md:49` 四态枚举 `{active,blocked,yielded,exited}`）——`divergent` 是*派生视图*（active lane 处于 >1 PC），在 snapshot 计算、非存储态（D6/V-synth G-D 一致）。

## 6. 单一源表 / codegen / assembler / kernel_builder 扩展

来源 D3，经 V3 **survived（minor）** 实测确认：把 D3 提案 schema + sample-layout + parser 加入真实树并跑 codegen 门 + round-trip，baseline `validate_layout(SAMPLE_LAYOUT)` 与 30 个 ISA 测试全过。

**两个新 operand kind + 一个 branch-target refinement**：

- **`barrier` kind（Bx，16 槽）**：4-bit 无符号；`_parse_operand`（`assembler.py:219`）+ `_symbolize_operand`（`assembler.py:334`）各加一臂。C++ 解码器**无需改**（4-bit 无符号已支持）。**载荷修正（V3-A）**：`_symbolize_operand` 的 `barrier` 臂返回 `f"B{value}"` 是 **load-bearing**——缺它 bx 解为裸 int `5` 而非 `"B5"`，且标准 `encoded.ir == decode_like_ir(word)` round-trip **不会**捕获（两侧同 decode），须加一条断言字面串 `"B3"`。
- **`branch_target` kind**：复用 `immediate` + `aligned:16`（`assembler.py:227` 已强制）。**过度规范修正（V3-B）**：`_symbolize_operand` 的 `branch_target` 臂是确认的 **no-op**（stock 对非 register/predicate kind 原样返回），**只** `_parse_operand` 需要它（为 `aligned` 检查 + range）。

**新 `InstructionSchema`**（append 到 `INSTRUCTIONS`，全部复用 `_base_fields` = `guard_pred(3)+guard_neg(1)`，`schema.py:84-89`）：`BSSY`（bx + signed 24-bit target）、`BSYNC`/`BREAK`（bx）、`YIELD`（仅 base）。`CONT` **不入表**（无 opcode，BRA 回边）；`YIELD` 必须是 distinct opcode、**绝非** control-segment `yield` bit（后者是 scoreboard hint、功能惰性，G8）。

**Sample layout**（append 到 `SAMPLE_LAYOUT.instructions`）：opcode 须过 `_validate_no_overlap`（`codegen.py:497`）。现有低字节 `0x11/0x23/0x34/0x45/0x7F`（`sample.py`，`control_lsb=107`）；D3 取非冲突的 `BSSY=0x41, BSYNC=0x42, BREAK=0x46, YIELD=0x48`。**V3 机器验证**：bit 覆盖 0-127 精确、零重叠（`_validate_disjoint`/`_validate_full_instruction_coverage`/`_validate_no_overlap` 全过），control 段 107-127 落入尾部 reserved span（与「control = decode-only no-op」一致）。

**V3 补 D3 漏项（load-bearing）**：D3 §4 只提 `assembler.py`+`codegen.py`，但 `native.cpp instruction_from_word`（L211-251）**也需**新 dispatch 臂填 `inst.operands["bx"]`（及 `BSSY` 的 `target`，套用 BRA `target/16` 绝对约定，L247），镜像现有 IADD3/BRA 臂——此项 G1/G2 隐含但 D3 自身工作清单缺失。

**kernel_builder 增长**：现测试用裸 `[assembler.emit(...)]` word-list（`test_native.py:41-48`），**无 kernel_builder 抽象**。分歧 corpus 需薄 builder：(i) **label → 绝对 word-index** 解析（`BSSY` target、BRA target、`BSYNC` join PC 引用前向 label，positional 立即数做不到，E10）；(ii) 发 canonical **diamond**（`BSSY Bx,L_join / @P BRA L_else / <then> / BRA L_join / L_else: <else> / L_join: BSYNC Bx`，E10/E11/GAP-Collabora F3）；(iii) loop+BREAK 与 early-exit 形状。最小 builder = label 表 + two-pass back-patch；保持 out-of-tree（同生成物）以维持 build 确定性。

**已定（OD-6/OD-3，§11）**：`branch_target` 取**绝对**（零 native.cpp 改）；`CONT` omitted（BRA 回边）；predicate-producing `BSSY P,Bx,imm` / `BMOV` **deferred**（单 guard 模型够结构化分歧 corpus，predicate-BSSY/BMOV 只为 >16 槽深嵌套需要，GAP-Collabora F5）。但 collective 的 `pd` predicate-producer 字段 + `membermask` operand kind 因 **OD-3 IN** 现入 scope（`ELECT`/`VOTE` 用），须随本节 schema 一并过 no-overlap/completeness 门。

## 7. 序无关性主门 + 分歧 corpus + harness + per-BSYNC mask 断言

来源 D4，经 V4 **partial（major）** 修正。

**主门 = 序无关性 metamorphic**（`spec.md` §5「主门」、`research-notes.md` §5、FUT-1:133）：≥3 具名确定序（§5）→ 在尊重同步契约的无竞争分歧 corpus 上 bit-identical 最终 `snapshot()`。**门的精确前提须收紧**（V1 FIX-3 + GAP-Khronos/E12/E13）：从「respects the synchronization contract」收紧为 **LLVM-m-converged / Khronos-tangle 定义**——每个 collective 在每条 lane path 上须被一个**重聚其完整 membermask 的 `BSYNC`/`WARPSYNC` 支配**；按 `(PC, barrier-token)` 分组、**绝不**跨 loop-trip 用裸 PC。

**分歧 corpus**（`spec.md` §5:55 + control-sync-uniform.md:247 + 北极星补充）：
- 基础：if/else、nested（≤4，匹配 Collabora CTS nesting4，GAP-Collabora F4）、loop break/continue、early-exit、subwarp collective。
- **北极星（Transformer）形状（D6/V4 补，否则 corpus 不实例化 north-star 的分歧）**：(1) **per-row 变长归约循环**（每 lane data-dependent trip count、在自身 bound 处 BREAK）——masked-softmax / seqlen 模式，且*恰是* loop-carried-barrier 测试，解决「raw-PC vs (PC,token,trip)」grouping-key 的 OPEN DECISION（每个 proposal 都旗标）；(2) **causal-mask control-divergent early-exit**——**须用 control-divergent 编码**（纯 causal masking 常是 predication / data-parallel、不触发 ITS，否则是 vacuous 测试）。
- **YIELD 的 corpus 地位（V4 over-build 修正）**：`spec.md` §5 现有 corpus *无*成员真正运动 `YIELD` 作前向进度原语（其唯一真实用途 = spin/producer-consumer，需 FUT-2 的 cross-lane shared-mem flag）。二选一：(a) 加 memory-free 的 **warp-specialization** 成员（两 lane-subset 入 disjoint never-reconverging loop，仅 `YIELD` 让两者推进，E18 §4）；(b) 把 `YIELD` 降为 decode+lane_state-only、其前向进度门 deferred 到 FUT-2 与 mutex corpus 同行。**不要**把 `YIELD` 作 gated 门发布而无 (a)。

**negative control（V1/V4 强制）**：把 V1 的 K1（YIELD-arrival）、K2（collective-placement）加入 corpus——K1 须在修正(1)+(2)后全序 bit-identical；K2 须被静态 pre-screen 拒绝、若到引擎则全序同 trap。

**static well-formedness pre-screen**（V1 FIX-4 + GAP-WGSL）：在 metamorphic 门**之前**跑一个 WGSL-uniformity 式 implication-graph 检查（`RequiredToBeUniform`→`MayBeNonUniform` reachability，Naga `analyzer.rs` 式 `non_uniform_origin` 句柄），拒绝 K2 类程序（collective 的 membermask 超出支配重聚集），使序相关 trap **永不**作为门的 flakiness 浮现。pre-screen 是 sound 的*单边*准入滤器、**非** dynamic membermask 检查的替代。

**harness**（E15/GAP-cuda-gdb）：采 Hanoi 的 per-instruction `(PC, active_mask)` trace 作比较对象；**mutation-test 引擎**（drop 一个 `BSYNC` / off-by-one Bx / 错 tie-break）须被门*杀*每个 mutant——把「≥3 序」从断言变为可测的 mutation-kill-rate（MC Mutants ASPLOS'23）。corpus 生成可借 ShaDiv（PLDI 2025，divergence+liveness 引导的扰动，GAP-WGSL）与 ThreadFuser（MICRO 2024，MIMD→SIMT，E15/GAP-patents）。

**per-BSYNC reconv-mask 断言（V4 REFUTATION-2 + D6 OD3 的 fork）**：`spec.md` §5 命名此断言为门的一部分。但 V4 证明：在**部分重聚**下，gate collective 的 `active` 集本身序相关——若 collective 位于不同时刻重聚的 lane 可达的 PC（K2），一序 trap `membermask_not_subset`、另一序 collective 成功，**直接驳回 D4「所有序到同一 trap」**。修正：(a) per-BSYNC 断言 = **debug-mode**（每步检查，对合法重聚-timing 自由度 brittle，E11/E13）；(b) 最终态 snapshot = **release gate**；(c) K2 类由 static pre-screen 拦在门外，order-dependent trap 不浮现为 flaky。

**幸存子语言（V1/V2 确认）**：嵌套循环 break+continue（若 break/continue lower 到 `BREAK Bx`+BRA 回边、每个 `BSSY` 在每条 lane path 被其 `BSYNC`-or-`BREAK` 支配）、不同时刻进入 barrier region 的子组（laggard 自成 min-PC group 被吸收，`reconv_pc` 固定故 resume-PC 序不变）、IADD3/ISETP/LOP3 datapath（per-lane private、read-before-write 已成立于 `exec_*`）——核心性质成立，漏洞**专在** YIELD-arrival 与 collective-placement。

## 8. UB / 非法情形检测 + trap 分类扩展

来源 D5。frame：foundation 有结构化 `Trap{kind,reason,pc,detail}`（`native.cpp:36-41`）。删 `non_uniform_pc`/`non_uniform_branch` 两 reason；新增**一个 kind `convergence`** 承载 ITS 非法/UB，复用 `execute` 承 branch-target/PC error。`detail` 采 TRT-entry schema `{trap_reason, pc, thread_id|mask, barrier_index}`（US10289418B2，G8）。**为何结构化 trap 而非 hang/silent-wrong**：序无关门只对契约程序 sound；违约 = UB，oracle 须使其*可观测且确定*——hang 让 metamorphic harness 非确定超时，silent-wrong false-pass/fail 门。每个 trap 是其检测点架构态的纯函数，故所有 ≥3 序到*同一* trap（**例外见 §7/§10 collective-placement，须 pre-screen 前置**）。

trap 表（精简）：

| # | 非法/UB 条件 | 检测点 | kind / reason | 依据 |
|---|---|---|---|---|
| C1 | `membermask ⊄ active∧converged`（任一 collective） | `exec_collective` 前检 | `convergence` / `membermask_not_subset` | G3/G5:108/E6/E7 — NVIDIA 定义非收敛 lane 的 mask 位为 UB |
| C2 | self-bit ∉ 自身 mask | 同前检，per-lane | `convergence` / `self_not_in_membermask` | E7 blog rule 1 |
| C3 | `ELECT` 非唯一 leader | `exec_elect` 后检 | `convergence` / `elect_not_unique` | G3/G5 §6；G7 指其结构上 = C1（见 §11 OD） |
| C4 | `BSYNC` on invalid `Bx`（`valid==false` *且非* BREAK-dissolved） | `exec_bsync` | `convergence` / `bsync_invalid_barrier` | G5:64/G6/GAP-Collabora F2 — **须与 §4 不变式 3 调和**：dissolved-by-BREAK 是 fallthrough 非 trap |
| C5 | `BSSY` 覆盖 live `Bx` / Bx 耗尽（>16） | `exec_bssy` | `convergence` / `bssy_clobbers_live_barrier` | E10/E8 — 深分歧需 BMOV spill（deferred），clobber-without-spill trap；§9 限 corpus ≤16 |
| C6 | DEADLOCK：无 runnable group 但非全 exited | `select_group` 终检 | `convergence` / `deadlock_no_progress` | G3/E1 MICRO16 SIMT-deadlock |
| C9 | `reconv_pc` 越界/未对齐 | `exec_bssy`，复用 L594-599 界检 | `execute` / `illegal_reconv_pc` | G6/E10 |
| C10 | （**非 trap，是 fix**）`EXIT`/`BREAK` 从所有 valid `Bx` 清退出 lane | `exec_exit` extend L621-628 | — | G3/G4/E11 — 防 C6 的载荷 liveness |

**deferred / open**：C7（branch into barrier region 中部）依赖 structured single-entry region 假设（E10/GAP-Khronos）；irreducible CFG / 分歧 BRX 入 region 规范上**不保证**（GAP-Khronos F4）——推荐**阶段 ② 整体 trap 分歧 BRX 为 out-of-scope**（spec-conformant，E22 F5），使 C7 退化为廉价 dominator 检查、非全 reaching-analysis。C8（BREAK/CONT 无 enclosing barrier）可作 deferral 标注。

## 9. 范围与里程碑

**single-warp 充分性：SURVIVES**（D4/D6/V4，contract-frozen：`spec.md:33`、`research-notes.md:27`、G3:E）。每个在范围 collective 都是 intra-warp via membermask——`sm100a` 确认 `WARPSYNC`/`VOTE`/`ELECT` 写 P/UR/R（per-lane register 态）、从不跨 warp；`VOTE`/`ELECT` 输出落 `vgpr`/`predicates`、**两者已在 snapshot()**（L398-416）——故 subwarp-collective **可观测、零 FUT-2 memory**。**驳回**任何「collectives 需 shared-mem」之说。

**IN scope**：per-thread PC + per-PC grouping + 6-op barrier FSM（`BSSY`/`BSYNC`/`BREAK`/`CONT`/`YIELD`/`EXIT`）；divergent BRA（删 `non_uniform_branch`）；序无关门 + per-BSYNC 断言（debug）+ 分歧 corpus；§8 的 UB trap。

**collectives 入 scope = 契约强制、非 over-build**（V4 关键澄清）：`spec.md:55` 命名「subwarp collective」为 corpus 成员、FUT-1:133 命名「membermask ⊄ active」+「ELECT 非唯一」为 REQUIRED UB 门——两者都 collective-specific。故 `ELECT` + 一个 mask-consuming collective（`VOTE` 或 `WARPSYNC`）被契约自身的验收门*强制*。**但 G-C/V4 查实一个 under-build**：D1（调度器）只加 6-op FSM、无 collective handler；D3（codegen）完全省略 `VOTE`/`ELECT`/`WARPSYNC` schema 项。collective path 需 D3 未加的新 codegen 面：一个 `pd`-式 predicate-PRODUCER 字段（`_base_fields` 只给 guard_pred/guard_neg，需 ISETP-式 `pd`）+ 一个 membermask/uniform-mask operand kind（`_parse_operand` 只知 register/predicate/immediate）。**已定（OD-3 IN，§11）**：collectives 入阶段 ②，最小 NV 集 = `ELECT` + `VOTE`（`WARPSYNC` 可选）；D3 须补 `pd` predicate-producer 字段 + `membermask` operand kind 的完整 schema+layout、过 no-overlap 门——半-specified 裂缝由此闭合。

**OUT scope**（deferred）：`BMOV`/Bx-spill/ATEXIT_PC、NANOSLEEP、CALL/RET、BRX/BRXU/JMX、SYNCS、PREEXIT、ENDCOLLECTIVE-semantics、`.EXCLUSIVE`/`.COLLECTIVE` 语义（单 warp 下功能 no-op）；general memory load/store + CTA barrier + atomics（FUT-2）；MMA/tensor（FUT-3）；Transformer block（FUT-4）；cluster/DSMEM（接缝已留）；21-bit 控制段保持功能 no-op（含 `yield` bit 作 scheduler hint 的角色被**有意丢弃**——G-F 显式 narrowing：control-sync-uniform.md:75 授予 yield-bit 调度角色，但 G8(c) 决定整 21-bit 段 no-op）。

**里程碑分解**（镜像 plan_foundation tasks）：(1) codegen 增项（barrier/branch_target/membermask/pd 字段 + schema/sample-layout + 过 completeness/no-overlap 门 + native.cpp dispatch 臂）；(2) 调度器替换（`current_pc`→`select_group`，一个 comparator seam，删 `non_uniform_pc`）；(3) barrier 状态机 + 状态（真 `bx_` 成员、`lane_state` 扩展、handler、`pc_[lane]`-as-resume）；(4) corpus + 门（≥3 序 bit-identical 主门 + per-BSYNC debug 断言 + static pre-screen + mutation-test）；(5) UB/traps（§8）。**每步独立可验**（严格验证模式）；现有 19+30 测试须保持 bit-identical（min-PC 在 uniform 退化）。

## 10. 风险与对抗发现

对抗评审对 6 个设计切片 + 整体 grounding 出 5 个 verdict，净 **partial（major）×4 + survived（minor）×1 + survived（minor，D3）×1**。逐项：

- **D1+D4「序无关对所有契约程序成立」= partial（major）**：纯分支 + `BSSY`/`BSYNC`/`BREAK`/`EXIT` 子语言**幸存**；但 (R1) `YIELD` 的 reactivation trigger 跨 D1/D2/D4 不一致（D2 的 survivor=`participation∩¬exited` 让从未到达的 yielded lane 被传送，序相关）；(R2) gate collective 的 `active` 集在部分重聚下序相关，使「所有序到同一 trap」FALSE。**修正已纳入 §4 不变式 1-2、§7 pre-screen、§8 C4 调和**。
- **D2 barrier FSM = partial（major）**：4 个 flagged transition 保真；但 3 个 fire-condition/bookkeeping 缺陷 + 1 个静默契约漂移（加 `Bx.blocked_mask` 第 4 字段改了冻结快照 schema）。**修正纳入 §4**：fire predicate 排除 yielded、dissolved-barrier fallthrough、派生 blocked 状态保 3 字段快照。
- **D3 schema/codegen = survived（minor）**：实测过门 + round-trip。两处精确修正（`_symbolize_operand` 的 `barrier` 臂 load-bearing；`branch_target` 臂是 no-op）+ 一处漏项（native.cpp dispatch 臂）。纳入 §6。
- **D4+D6 scope/corpus = partial（major）**：single-warp 充分、collectives 契约强制（均幸存）；但 collective data-path under-build（无人设计）、`YIELD` over-build（无 corpus 成员真正运动它）、缺 north-star 分歧形状（变长归约 / causal-mask）。**已裁决（§11）**：OD-3 IN（最小 NV 集 `ELECT`+`VOTE`，闭合 data-path 裂缝）、OD-6 `YIELD` 仅 decode+lane_state、OD-5/§7 补变长归约与 causal-mask 形状。**修正纳入 §7/§9**；并**拒绝**把 cuda-gdb `"divergent"` 加 `lane_state_`（relitigate 冻结枚举）。
- **整体研究完整性 = partial（major）**：隐藏一个载荷原语矛盾（`BSSY` participation = group-mask vs guard-mask，§11 OD-1）+ 数个未规范交互（guard-false-in-group 屏障行为 OD-2、collective 数据通路归属 OD-3、resume_pc 冗余 G-D、per-BSYNC 断言门 vs debug G-E、control-segment yield-bit narrowing G-F）。**均已裁决并锁定于 §11**（OD-1 GROUP-mask、OD-2 A、OD-3 IN、OD-4 断言 debug、resume_pc 复用 `pc_[lane]` 不加字段、yield-bit narrowing 确认）。

**主要风险（须缓解）**：① **collectives**（OD-3 已定 IN，§11）——最小 NV 集 `ELECT`+`VOTE` 入阶段 ②，残留 = D3 补 `pd`+`membermask` schema 并过门（非裁决）；② **grouping-key（raw PC vs (PC,token,trip)）**——5 源 + 4 proposal 旗标，loop-carried-barrier 下 raw-PC 错并 trip（GAP-Khronos/E12/Collabora F3）；③ **fairness**——min-PC 可证 unfair（Habermaier-Knapp），未约束则自旋 corpus 在一序挂；④ **per-BSYNC 断言 brittle**——合法重聚-timing 自由度下作 release 门会 false-fail（E11/E13）；⑤ **>16 Bx 槽 / BMOV deferred**——深分歧 false-trap，corpus 须限 ≤16。缓解分见 §4/§5/§7/§8。

## 11. 决策记录（Resolved Decisions，已锁定）

§10 的分叉已由用户逐个裁决（OD-1/2/3 走 NVIDIA 对齐；OD-4/5/6 取推荐默认）。下列保留每个 fork 的原始论证，并在其前标注**锁定结论**；§3/§4 的勘误已同步落实于 `research-notes.md`。

**锁定结论速览**：OD-1 = GROUP-mask；OD-2 = A（效果只施 guard-true，`BSSY`/`BSYNC` 无谓词、`BREAK`/`YIELD` 可谓词）；OD-3 = IN（最小 NV 集 `ELECT`+`VOTE`，`WARPSYNC` 可选）；OD-4 = 断言 debug / 最终态 release；OD-5 = 限终止程序 + fair 序、spin 前向进度顺延 FUT-2；OD-6 = `branch_target` 绝对、`CONT` 不入表、`YIELD` 仅 decode+lane_state。

- **【已定：GROUP-mask，跟 NVIDIA 对齐】OD-1 `BSSY` participation_mask = GROUP-mask 还是 GUARD-mask？** 权威本地 spec 明确 group-mask（control-sync-uniform.md:58「记录当前 PC group 的 participation mask」、research-notes.md §5:61「当前组 mask」）；但 D1/D2 的 transition 表写 guard-true 子集，D6/spec 写完整 co-scheduled group。`@P0 BSSY` 时二者不同（G6 确认 silicon 有 predicated `BSSY_P_B_I`）。若 = guard，group 内但 BSSY 时 guard-false 的 lane 从未入册、却**会**到 `BSYNC` → 死锁或错重聚集。**本文按 spec 取 group-mask（§4 表已写 GROUP_mask），但须用户确认并使所有设计一致。**
- **【已定：A — 效果只施 guard-true；`BSSY`/`BSYNC` 无谓词（predicated→trap out-of-scope），`BREAK`/`YIELD` 可谓词】OD-2 guard-false-in-group lane 的每个屏障 op 规则？** `native.cpp:331` 现 `issued_mask=active_mask_`、L360-364 对所有 issued lane（含 guard-miss）commit fallthrough。对 `@P0 BSYNC Bx`：guard-false lane 须**不** block、推进 fallthrough；但若其在 `BSSY` 已入册 participation，则 reactivation predicate（全 participant blocked/exited）永不 fire。control-sync spec L91 说 guard-miss lane「走 fallthrough」却从未调和 fallthrough-on-guard-miss 与 barrier 入册。**推荐**：屏障 op 在 `BSSY` 对 GROUP 入册/动作，但 `BSYNC`/`BREAK`/`YIELD` 效果只施于 guard-true lane，guard-false in-group lane fallthrough 而**不**从 participation 移除——然后证明此不死锁；**或** 要求阶段 ② corpus 中 `BSSY`/`BSYNC` 不带谓词、并 trap predicated 屏障 op 为 out-of-scope。
- **【已定：IN — 最小 NV 集 `ELECT`+`VOTE`（`WARPSYNC` 可选），`SHFL`/`MATCH`/`REDUX`(FP) 顺延 FUT-3；codegen 补 `pd`+`membermask`】OD-3 collectives 入阶段 ②、还是删「subwarp collective」corpus 项？** 契约自身（`spec.md:55` + FUT-1:133）强制需要 `ELECT` + 一个 mask-collective 来建 corpus 与 3 个 UB 门；但其数据通路无人设计、需新 `pd` predicate-producer 字段 + membermask operand kind（D3 半-add）。**推荐 IN**（最便宜的「区分正确 vs 有 bug 的 per-PC 调度器」的可观测，E7/GAP-Collabora），但须把 codegen 增项、UB 门、corpus 项**收敛为一个决策**，否则 collective 半-specified。
- **【已定：是】OD-4（gate 强度）** per-BSYNC reconv-mask 断言 = **debug-mode**（每步、对重聚-timing 自由度 brittle）、最终态 snapshot = **release gate**？（推荐如此，E11/E13）。
- **【已定：是】OD-5（fairness）** 把 corpus 限于*终止*程序 + 要求每个具名序是 *fair* permutation（推荐作门）；spin/mutex/producer-consumer 前向进度门 deferred 到 FUT-2 作*独立非门*进度测试（GAP-busy-wait/E14）。
- **【已定：绝对 / omitted / decode-only】OD-6（次要）** `branch_target` 绝对 vs PC-relative（推荐绝对，零 native.cpp 改，OD 来源 D2/D3）；`CONT` omitted（推荐）；`YIELD` 作 gated 门需先加 warp-specialization corpus 成员、否则降 decode-only（§7）。

## 12. 参考

> annotated；本地文件 + 找到的最佳 web 源，**清楚标注比 research-notes.md 现有引用更新的 2024–2026 工作**。

**本地（契约 / 代码 / ground-truth）**：
- `iss/binding/native.cpp`（foundation 核，731 行）：惰性钩子 L47/430-440/641-642、统一-PC trap L307-309/449-462、`exec_bra` L592-619、per-lane commit L360-364、`exec_exit` L621-628。
- `isa/currygpu/isa/{schema.py,codegen.py,assembler.py}` + `layout/sample.py`：单一源 codegen；`_base_fields` schema.py:84-89、operand 入口 assembler.py:219/334、no-overlap 门 codegen.py:497、现有 opcode `0x11/0x23/0x34/0x45/0x7F`、`control_lsb=107`。
- `docs/implement/ISS/{spec.md §5/§7, research-notes.md §5, plan_foundation.md FUT-1}`：冻结的阶段 ② 契约。**勘误**：research-notes.md §5 将闭包归因 Hanoi §IV 应改引 Habermaier-Knapp ESOP 2012；§5:61 / control-sync-uniform.md:59 的 `BSYNC` 「blocked/yielded/exited」措辞须修为「blocked/exited」（§4 不变式 1）。
- `nv_patent/spec/isa/control-sync-uniform.md`（ISS 自定义 convergence family，L11 标「定稿/ISS 自定义」——SASS 作编码参考、本地为权威）：`BSSY`/`BSYNC`/`BREAK` L52-69、reactivation L59、guard[4] fallthrough L91、membermask⊆active L108、ITS 测试 L247-253。
- `nv_patent/sm/{independent_thread_scheduling.md, branch_reconvergence.md, scheduling.md, trap_exception.md}`、`architecture_evolution/{volta_generation,blackwell}.md`：机制重构（栈→barrier 对象、CBU + per-thread resumption PC、starvation-free）。
- `sm100a/output/{BSSY,BSYNC,BREAK,BMOV,YIELD,EXIT,ELECT,VOTE,WARPSYNC}.html` + `isa.json`：操作数形、`RELIABLE RECONVERGENT` 标志、`BREAK` 非 RECONVERGENT、`BMOV.32/.64`、`ELECT` 读 UR membermask。

**Web — 已在 research-notes.md 引用（更新/补强）**：Hanoi（arXiv:2407.02944 v1，2024-07-03，确认仍 v1）、NVIDIA US10067768B2/US20160019066A1（Family A）、Simty/Collange（CARRV 2017 + HAL hal-00622654 2011）、GPGPU-Sim/Accel-Sim（E1，确认 trace-driven 不重建分歧）。

**Web — 比 research-notes.md 新 / 此前未引用（2024–2026，重点标注）**：
- **Habermaier & Knapp, ESOP 2012**（LNCS 7211，10.1007/978-3-642-28869-2_16）——序无关闭包的*正确*形式引用（simulation 证明 + min-PC unfair 警告）；研究 notes 缺此 backbone。
- **LLVM Convergent Operations / Convergence and Uniformity**（trunk，2026-06-06）——convergence token = dynamic instance、m-converged；「同 PC 不同 trip ⇒ 非收敛」的规范。
- **Khronos SPV_KHR / VK_KHR maximal reconvergence**（ratified **2024-01-25**）——vendor-neutral tangle/loop-trip 规范，LLVM m-converged 的上游，此前**无 agent 引用**。
- **NVIDIA Family B 专利 US11442795B2（2022-09-13）/ US11847508B2（~2023-12）**——soft barrier = performance-not-correctness（确证 timing-only、不实现 threshold-clear）+ barrier deconfliction 规则。
- **Dubey et al.「Equivalence Checking of ML GPU Kernels」**（arXiv:2511.12638，**2025-11-16**）——sound（结构化 CTA complete）GPU kernel 等价检查器，核心 confluence = 序无关门的可机械化形式。
- **SIMT-Step（PLDI 2026）**——vendor-neutral TLA+-validated operational semantics 显式建模 independent 模式，最近邻工件。
- **Analyzing Modern NVIDIA GPU Cores**（arXiv:2503.20481，**2025-03-26**，UPC）——RTX A6000 实测确认 ≥16 B-register、software 控制位胜 HW scoreboard（确证 21-bit 段 no-op）。
- **Collabora / Ekstrand「Re-converging control flow on NVIDIA GPUs」**（**2024-04-25**，Mesa NAK war-story）——naive bssy/bsync-per-region 被驳、BREAK-cascade 规范、F2「inactive invocation 的 barrier 值不动」、>16 槽 BMOV spill 实践；此前无 agent 引用。
- **cuda-gdb v13.1**（**2026-01-08**）——`info cuda lanes/warps/barriers` 是免费 silicon per-lane oracle，schema 直接借作 `state()` 列与 golden vector。
- **GAP-parallelization**（arXiv:2502.14691，**2025-02-20**，Parallelizing a Modern GPU Simulator）——read→commit 相位 + privatize-then-reduce 是序无关门的架构性 soundness；arXiv:2105.00069 total-order tie-break、arXiv:2408.05148 FP 非结合警告。
- **GAP-WGSL**：WGSL/Naga uniformity analysis（implication graph 静态 pre-screen 模板）+ **ShaDiv（PLDI 2025，10.1145/3729305）** divergence-perturbation corpus 生成器。
- **GAP-patents**：ElTantawy & Aamodt HPCA'14（per-PC ST/RT，非栈，opportunistic early-merge 序相关，是 metamorphic 须压的 adversarial case）/ MICRO'16（SIMT-deadlock、loose-fairness 契约）/ HPCA'18（DDOS/BOWS busy-wait，BOWS 是 timing-only 勿建）；ThreadFuser（MICRO 2024，MIMD→SIMT corpus 生成）。
- **E14/E15 testing**：MC Mutants（ASPLOS'23，引擎 mutation-kill-rate）、GPUMC（arXiv:2505.20207，**2025-05-26**，scoped-RC11 barrier-divergence 检查器）、Varity-numerics（arXiv:2410.09172，2024-10）。
