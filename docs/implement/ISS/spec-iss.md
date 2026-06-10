# curryGPU ISS ITS 操作层契约

本文件是 `docs/implement/ISS/plan-its.md` 的 repo-local 操作层契约。权威来源限于本仓库内的 `docs/implement/ISS/spec.md`、`docs/implement/ISS/research-notes.md`、`docs/implement/ISS/plan-foundation.md`、`docs/design/iss.html` 与当前实现/测试；仓库外资料只可作为历史参考，不能覆盖本文件。

## Scope

本文件当前同时记录阶段②已实现合同与阶段③内存同步实现合同。阶段②实现 single-warp、32 lane、功能级 Independent Thread Scheduling；阶段③在该内核上扩展 memory/atomics、CTA barrier、multi-warp、独立多 CTA grid、const memory 和 opt-in race checking。MMA、tensor 字节、CALL/RET、间接分支、timing、mbarrier/async transaction barrier 建模仍不在本阶段。

## Grouping And Scheduling

`build_groups()` 只收集 `lane_state == active` 且 `active_mask == true` 的 lane，并按裸 `pc[lane]` 分组。裸 PC 只用于调度，不作为重聚身份。重聚身份由 live `Bx` barrier token 承载；这是 DEC-1 的结论。

`select_group()` 支持四个具名确定序：`min_pc_first`、`max_pc_first`、`round_robin`、`oldest_group_first`。四序必须对每个 runnable group 给出确定选择；`oldest_group_first` 使用 group 创建序号作为主键，PC 只作 tie-break。

主验收门比较最终架构态子集：`active_mask`、`pc`、`lane_state`、`vgpr`、`predicates`、`uniform_registers`、`memory`、`bx`、`trap`。`counters` 明确排除，因为不同调度序可合法改变 group 合并机会和计数。

## Barrier State

每个 warp 拥有 16 个 `Bx` 槽。snapshot schema 固定为 `participation_mask`、`reconv_pc`、`valid` 三字段，其中 `valid` 等价于私有 `barrier_phase == armed`。

私有状态包括 `blocked_on[32]` 和 `barrier_phase[16]`。`blocked_on[lane]` 记录 lane 当前阻塞在哪个 `Bx`，未阻塞为 sentinel。`barrier_phase` 取 `unarmed`、`armed`、`dissolved`，用于区分从未 arm/已消费与被 `BREAK` 清空的槽。

Barrier transition table:

| Operation | Transition |
|---|---|
| `BSSY Bx,target` | `Bx` 必须不是 `armed`。记录当前 PC group mask 为 `participation_mask`，记录 `target` 为 `reconv_pc`，置 `phase=armed` 和 `valid=true`。`BSSY` 本身不支持谓词化。 |
| `BSYNC Bx` | `phase=unarmed` 触发 `bsync_invalid_barrier`。`phase=dissolved` 直接 fallthrough。`phase=armed` 时，当前 group lane 进入 `blocked`，`blocked_on=Bx`。当 participation 内每个 lane 均为 `blocked_on==Bx` 或 `exited` 时 fire；实际到达的存活 lane 被恢复为 `active`，清 `blocked_on`，并从被消费的 `BSYNC` fallthrough 继续执行，随后 `phase=unarmed` 和 `valid=false`。`BSYNC` 本身不支持谓词化。 |
| `BREAK Bx` | 对 guard-true lane 清 `participation_mask` 中对应 bit。若清空最后一个参与者，置 `phase=dissolved` 和 `valid=false`。 |
| `YIELD` | 对 guard-true lane 置 `lane_state=yielded`，无寄存器、PC、barrier 架构效果。调度器在无 active group 时先尝试 fire barrier，再把 yielded lane 提升回 active。 |
| `EXIT` | 对 guard-true lane 置 `exited` 并清 active bit，同时从所有 `armed` barrier 的 participation 中清该 lane。清空的 barrier 进入 `dissolved`。 |
| `CONT` | 不进入 ISA 表，前端 lower 为普通 `BRA` 回边。loop 重聚仍由该 loop 自己的 live `Bx` token 管理。 |

Nested barrier 的 fire predicate 必须检查 `blocked_on==Bx`，不能把阻塞在其他 barrier 上的 lane 当作当前 barrier 的到达者。任何从 `blocked` 转出、`EXIT` 或 fire reactivation 都必须清理 stale `blocked_on`。

## Convergence Traps

ITS 相关 UB 使用 `trap.kind == "convergence"`。`detail` 至少包含 `trap_reason` 与 `pc`，并在适用时包含 `barrier_index`、`thread_id`、`membermask`、`participant_mask` 或 `target`。

当前实现可 emit 的 convergence reasons:

| Reason | Meaning |
|---|---|
| `membermask_not_subset` | Collective 的 `membermask` 包含当前参与集之外的 lane。 |
| `self_not_in_membermask` | Collective 的 `membermask` 为 zero mask，或等价地没有任何可参与 lane。 |
| `elect_not_unique` | `ELECT` 防御性断言未得到唯一 leader。 |
| `bsync_invalid_barrier` | `BSYNC` 命中 `unarmed` 或非法 barrier 槽。`dissolved` 不触发此 trap。 |
| `bssy_clobbers_live_barrier` | `BSSY` 试图覆盖 `armed` barrier。 |
| `barrier_slots_exhausted` | barrier operand 指向可实现槽范围外，或未来出现超过 16 live barrier 的情形。 |
| `deadlock_no_progress` | 尝试 fire barrier 和提升 yielded 后仍无 runnable group，且仍有 blocked lane。 |
| `illegal_reconv_pc` | `BSSY` 的 reconvergence target 越界。 |
| `predicated_barrier_unsupported` | `BSSY` 或 `BSYNC` 使用非默认 guard。 |
| `debug_bsync_resume_mismatch` | Debug-mode per-`BSYNC` 断言失败，实际恢复 mask 不等于 `{lane: blocked_on==Bx} ∩ participation`。 |

`non_uniform_pc` 和 `non_uniform_branch` 是 foundation 阶段的旧 trap reason。ITS runtime 不再 emit 它们；历史设计文档中出现这些字符串时只能解释为已删除行为。

## Collectives

Collective 的参与集为当前执行 PC group 中 guard-true 且 active 的 lane。`membermask` 是 32-bit 无符号立即数，表示命名参与 lane。运行时要求 `membermask` 是参与集的子集；可以是真子集，membermask 外的 guard-true lane 不参与、不写输出。

`ELECT Pd, membermask` 选择 `membermask` 内最小 lane id 为 leader。leader 的 `Pd` 写 true，membermask 内其他参与 lane 的 `Pd` 写 false，membermask 外 lane 的 `Pd` 保持原值。

`VOTE.<mode> Pd, Psrc, membermask` 支持 `ANY`、`ALL`、`EQ`、`BALLOT`。`ANY` 对 membermask 内 `Psrc` 做 OR，`ALL` 做 AND，`EQ` 要求所有 member lane 的 `Psrc` 相等，结果广播到 membermask 内参与 lane 的 `Pd`。`BALLOT` 同时把 membermask 内 `Psrc==true` 的 lane bitmask 写入 `Rd`；`Rd=RZ` 时丢弃寄存器结果。

Collective 归约使用固定 lane-id 升序定义。当前布尔归约本身交换结合，但固定顺序为后续非交换/浮点 collective 留契约接缝。

## Phase 3 Memory And Synchronization Contract

阶段③把 `memory_space` 填入实体，并把序无关主门从 warp 内 group 选择扩展到 CTA 内 warp 调度。`memory_space_impl` 使用 4096-byte sparse block，global 为 grid 级共享，shared 为 per-CTA，local 为 per-thread private，const 为只读第四空间并通过 `const_memory` 顶层 snapshot 键序列化。snapshot 的 `memory` 子树仍只包含 `global`、`shared`、`local` 三键；const 不进入阶段③序无关比较子集。

Load/store 的唯一语义入口是 32-lane gather/scatter。普通 load/store 遵守 read -> compute -> commit 三相规则，guard-false 与 inactive lane 不读写、不分配内存、不计入 `mem_ops`。Atomic/RED 是受控例外：32 lane 的 RMW 在 commit 相位按 pinned lane-id 升序串行执行，普通 load/store 不享有该例外。固定 lane-id 升序同时是 collective 与 atomic/RED 的单一定序律。

Global/generic-global 访问在 launch 声明 `global_allocations` 时必须落入某个半开区间，否则触发 `global_oob`；未声明时保持 sparse unbounded 兼容语义。Shared/local 使用 launch 容量边界。Generic `LD/ST` 只解析 shared/local 窗口，窗口外 fall through 到 global；const 只由 `LDC` bank-indexed 访问，不参与 generic window model。`CVTA` 是纯地址算术，不访存。

CTA barrier 使用 16 个 named barrier slot。`BAR.SYNC` 与 `BAR.ARV` 均记录 per-thread arrival；实现必须保留 `arrived_thread_set`，不得用 per-warp arrival 近似。blocked lane 使用 `cta_blocked` 状态与 warp 内 `blocked` 区分。CTA 层 barrier deadlock 使用 `synchronization`/`barrier_deadlock`；single-warp Bx 层 deadlock 保持 `convergence`/`deadlock_no_progress`。

Warp scheduler 的具名顺序为 `warp_round_robin`、`warp_min_id_first`、`warp_max_id_first`。默认 `warp_round_robin` 是 runnable warp 集合上的 fair permutation，并承载 unconditional `weak fairness`、`forward progress` 和 D 档终止性断言。`warp_min_id_first` 与 `warp_max_id_first` 是固定优先级确定序，只进入 A/B 档最终态等价比较，不承载终止性断言。调度量子是一个 `step_one_group`，禁止 `run-to-block` 或 run-to-blocking-point 调度。`livelock` 表现为步数预算耗尽，不是同步 trap。

`MEMBAR` 与 `FENCE` 解码 scope/order 操作数但功能 no-op。阶段③不在 schema 或 snapshot 中加入 mbarrier/async transaction barrier；后续阶段若加入，必须先以 decode + no-op 接缝进入。

## Phase 3 Snapshot And Gates

`num_warps == 1` 时保留阶段②平铺 snapshot，并允许新增顶层 `cta_barriers` 以及使用 const 时的 `const_memory`。阶段③最终架构态比较子集包含 `active_mask`、`pc`、`lane_state`、`vgpr`、`predicates`、`uniform_registers`、`memory`、`bx`、`trap`、`cta_barriers`。`counters` 仍整体排除，即使其中包含 `mem_ops`；`arrived_thread_set` 仅为 barrier debug/diagnostic 元数据，不进入序无关比较子集。多 warp/多 CTA 形态按计划使用 per-warp/per-CTA 包裹 schema，并保持单 warp、单 CTA 退化投影 bit-identical。

四档验证域由 INV-GATE-DOMAIN-1 固定：A 档为 barrier-DRF 程序，B 档为交换 atomic 且不消费旧值的程序，C 档为序相关但单序确定的程序，D 档为需要 fair `warp_round_robin` 才能证明 forward progress 的程序。B-OI-1、B-OI-2、B-OI-3、B-OI-4 以及 grid 级 B-OI-5 是边界类，不得混入 A/B 主门。

Opt-in runtime race checking 由 launch `race_check` 控制，默认关闭。关闭时 racy 程序确定执行但排除出序无关主门；开启时同 epoch 内不同 thread 对 overlapping byte 的冲突访问触发 `memory`/`data_race`。Barrier release 与 atomic access 是保守 epoch 分界；全 atomic 同址访问不应产生 race 误报。

## Phase 3 Trap Reasons

阶段③新增 `memory` 与 `synchronization` trap kind。`detail` 至少保留 `trap_reason` 与 `pc`；适用时补充 `address`、`space`、`width`、`thread_id`、`bar_id`、`racing_lanes`、`access_kinds`。

Memory trap reasons:

| Reason | Meaning |
|---|---|
| `misaligned_address` | Load/store/atomic 地址不满足 width、register group 或 atomic 对齐要求。 |
| `shared_oob` | Shared memory offset 超出 launch `shared_mem_bytes`。 |
| `local_oob` | Local memory offset 超出 per-thread `local_mem_bytes`。 |
| `global_oob` | Declared `global_allocations` 存在且 global access 未落入任何区间。 |
| `const_oob` | `LDC` 访问未注入 bank 或超出 bank byte range。 |
| `unsupported_space_access` | 本阶段不支持的 space、remote rank、cluster/DSMEM 或 space/instruction 组合；cluster dim=1 且直达/generic 指令均锚定受支持空间，当前编码面不可达时仍保留文档条目。 |
| `generic_resolve_failure` | Generic address window 推断无法给出合法 space/offset；当前窗口模型对未命中窗口的地址 fall-through 到 global、推断为全函数，不可达时仍保留文档条目。 |
| `atomic_on_local_unsupported` | Atomic/RED 指向 local space。 |
| `atomic_on_readonly_space` | Atomic/RED 指向 readonly const space；当前编码面不可达时仍保留文档条目。 |
| `atomic_misaligned` | Atomic/RED 地址或 width 不满足 atomic 对齐。 |
| `atomic_unsupported_op` | Atomic/RED op modifier 不在阶段③整数 op 集。 |
| `red_has_destination` | `RED` 编码或 IR 非法携带 destination register。 |
| `data_race` | `race_check=True` 时检测到同 epoch byte conflict。 |

Synchronization trap reasons:

| Reason | Meaning |
|---|---|
| `barrier_deadlock` | CTA 层没有 runnable warp/lane 且存在无法 fire 的 CTA barrier arrival。 |
| `barrier_count_not_warp_multiple` | `BAR` thread-count operand 不是 32 的倍数或与合法 CTA thread count 不匹配。 |
| `barrier_id_out_of_range` | `BAR` operand 指向 0..15 之外的 named barrier slot。 |

## Static Pre-Screen

Static pre-screen 是 metamorphic 门之前的单边准入滤器，只拒绝结构上可证 ill-formed 的程序。当前 repo-local 测试侧实现拒绝 K2 `collective_placement` negative control；它不替代运行时 `membermask_not_subset`、`self_not_in_membermask`、`elect_not_unique` 动态门。

Pre-screen 必须接受 well-formed corpus 成员，包括 if/else、nested、loop break/continue、early-exit、subwarp collective、variable reduction loop、causal-mask control divergence 和 K1 YIELD-arrival。K2 被拒绝后不得进入四序最终态比较。

## Corpus And Gates

Test-side corpus builder 位于 `tests/iss/its_corpus.py`，不进入 `isa/` 或 `iss/` 发布包。它以 label 到 word index 的两遍 back-patch 生成 word-list，branch 和 `BSSY` target 按 `word_index * 16` 字节值交给 assembler。

Metamorphic gate 对每个通过 pre-screen 的 corpus 成员运行四个调度序，并比较最终架构态子集。测试必须确认 corpus 生成确定、K2 被 pre-screen 拒绝、需要真实控制分歧的成员 `divergence_events > 0`，且四序最终架构态一致。Debug-mode 运行还必须在每个 fire 的 `BSYNC` 上检查恢复 mask 等于 `{lane: blocked_on==Bx} ∩ participation`。
