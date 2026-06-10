# curryGPU ISS ITS 操作层契约

本文件是 `docs/implement/ISS/plan-its.md` 的 repo-local 操作层契约。权威来源限于本仓库内的 `docs/implement/ISS/spec.md`、`docs/implement/ISS/research-notes.md`、`docs/implement/ISS/plan-foundation.md`、`docs/design/iss.html` 与当前实现/测试；仓库外资料只可作为历史参考，不能覆盖本文件。

## Scope

本循环实现 single-warp、32 lane、功能级 Independent Thread Scheduling。lane 可以拥有不同 `pc[lane]`，调度器按 per-PC group 执行，同步与 collective 的正确性由显式 barrier token 和 membermask 门保证。不包含 memory/atomics、CTA barrier、MMA、多 warp、CALL/RET、间接分支和 timing 行为。

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

## Static Pre-Screen

Static pre-screen 是 metamorphic 门之前的单边准入滤器，只拒绝结构上可证 ill-formed 的程序。当前 repo-local 测试侧实现拒绝 K2 `collective_placement` negative control；它不替代运行时 `membermask_not_subset`、`self_not_in_membermask`、`elect_not_unique` 动态门。

Pre-screen 必须接受 well-formed corpus 成员，包括 if/else、nested、loop break/continue、early-exit、subwarp collective、variable reduction loop、causal-mask control divergence 和 K1 YIELD-arrival。K2 被拒绝后不得进入四序最终态比较。

## Corpus And Gates

Test-side corpus builder 位于 `tests/iss/its_corpus.py`，不进入 `isa/` 或 `iss/` 发布包。它以 label 到 word index 的两遍 back-patch 生成 word-list，branch 和 `BSSY` target 按 `word_index * 16` 字节值交给 assembler。

Metamorphic gate 对每个通过 pre-screen 的 corpus 成员运行四个调度序，并比较最终架构态子集。测试必须确认 corpus 生成确定、K2 被 pre-screen 拒绝、需要真实控制分歧的成员 `divergence_events > 0`，且四序最终架构态一致。Debug-mode 运行还必须在每个 fire 的 `BSYNC` 上检查恢复 mask 等于 `{lane: blocked_on==Bx} ∩ participation`。
