# 阶段③ 内存与同步实现计划（memory_space + atomics + CTA barrier + multi-warp）

## Goal Description

把功能级 ISS 从阶段② 的 single-warp ITS 引擎升级为 multi-warp / CTA 执行模型，并按 `plan-foundation.md` FUT-2 把 foundation 留空的内存空间填入实体，使序无关性主门从『warp 内组选择序无关』扩展到『warp 调度序无关』：

1. `memory_space` 抽象基类 + `memory_space_impl`（4KB 块稀疏 hashmap），覆盖 global/shared/local/**const** 四空间（const 经用户裁定 DEC-1 推翻草稿 DEC-MS-SCOPE-1 纳入本阶段：只读、`LDC` bank-indexed、launch 注入、不进序无关比较子集，R-8；tensor 仍占位不填）。
2. general memory load/store（`LD/ST/LDG/STG/LDS/STS/LDL/STL/LDC` + `CVTA` + generic 窗口推断）：32-地址 gather/scatter 唯一入口、byte-masked 写、sub-word extend-to-32、64/128/**256**-bit 寄存器组（`.256` 经 DEC-8 裁定纳入）、packed word 搬运；global 访问经 launch 声明的 allocation 表做 bounds check（DEC-7 裁定纳入）。
3. 整数 atomic / RED（`ATOM/ATOMG/ATOMS/RED/REDG/REDS`，10-op 集）单线程序列化、不可分 RMW，固定 lane-id 升序定序律；float atomic 仅留语义接缝。
4. CTA named barrier（`BAR.SYNC`/`BAR.ARV`）per-thread 到达计数三态状态机（16 槽）。
5. multi-warp / CTA 执行模型（`cluster_state` → `block_state` → `warp_state`）：指令级交错（量子 K=1）、unconditional weak fairness、默认 `warp_round_robin`；并经 DEC-5 裁定纳入**独立无交互多 CTA grid 执行**（`nctaid` 驱动、确定性逐 CTA 顺序执行、global/const 共享、其余态 per-CTA 隔离）。
6. `MEMBAR`/`FENCE` 解码 scope/order 操作数但功能 no-op；mbarrier/async 本阶段不建模、不加入 schema（R-7：『可解码 + no-op』义务转化为对引入阶段的接缝承诺）。
7. **opt-in 运行时 data-race 检测**（DEC-6 裁定纳入，R-9：`race_check` launch 开关、默认关，barrier/atomic-epoch 保守 shadow，`memory`/`data_race` trap）。
8. trap 体系新增 `memory`/`synchronization` 两 kind；四档序无关验证门（A/B/C/D）、corpus、mutation 全杀、spec 反向校验全量落地。

规范性输入 = 本文末尾嵌入的原始调研草稿（`research-memsync.md` 全文）。草稿的全部裁定（DEC-MS-*/DEC-Q*）、不变式（MS/OWN/ADDR/ATM/INV/B/VP/SS 系列）、验证点（VH-1..VH-16）、corpus 表（MC-*）与红线汇总（草稿 §8.5）均为本计划的硬约束。本计划在其上补充：(a) 落地排序与任务化；(b) 验收测试形态；(c) 经 Claude/Codex 收敛后的修订点 R-1..R-10 与用户评审裁定（DEC-1/5/6/7/8 的范围扩张 + DEC-10 的源码模块化，见 Claude-Codex Deliberation 与 Pending User Decisions；与草稿冲突处以本计划 AC 文本为准——其中 DEC-1 是用户对草稿 DEC-MS-SCOPE-1 的显式推翻，按草稿 §1.3 自带的条件路径执行）。

## Acceptance Criteria

Following TDD philosophy, each criterion includes positive and negative tests for deterministic verification.
The `AC-*` items are current RLCR completion gates for this implementation loop.

验证命令基线：`cmake -S iss -B iss/build/cmake -Dpybind11_DIR="$(python -m pybind11 --cmakedir)" && cmake --build iss/build/cmake --target _native -j` 重建原生模块；`PYTHONPATH=isa:iss python -m pytest tests -q` 跑全量测试；`python tools/check_structure.py` 跑结构门。

- AC-1: 结构与契约基线同步（首批结构任务，先于一切行为变更收口）
  - AC-1.1: `tests/structure/test_spec_iss.py` 反向校验扩展为三 kind 全覆盖
    - Positive Tests (expected to PASS):
      - 提取器同时抓取 `set_trap("memory", ...)`、`set_trap("synchronization", ...)` 与既有 `set_convergence_trap(...)` / `set_trap("convergence", ...)` 模式；`iss/binding/native.cpp` 中出现的每个上述 kind 的 reason 都在 `docs/implement/ISS/spec-iss.md` 以反引号文档化，测试全绿。
      - required_reasons 断言通过：`barrier_deadlock`、`barrier_count_not_warp_multiple`、`barrier_id_out_of_range`、`misaligned_address`、`shared_oob`、`local_oob`、`global_oob`、`const_oob`、`unsupported_space_access`、`generic_resolve_failure`、`atomic_on_local_unsupported`、`atomic_on_readonly_space`、`atomic_misaligned`、`atomic_unsupported_op`、`red_has_destination`、`data_race` 全部在 spec-iss.md 出现；`deadlock_no_progress`（convergence kind，单 warp 退化）保留。（`global_oob`/`const_oob`/`data_race` 为 DEC-7/DEC-1/DEC-6 扩张新增；`atomic_on_readonly_space` 若因编码面不可达而无 emit 点，保留文档条目并注明。）
      - required_terms 断言通过：`warp_round_robin`、`warp_min_id_first`、`warp_max_id_first`、`weak fairness`、`livelock`、`forward progress`、`run-to-block`、`per-thread arrival`、`cta_blocked`、`arrived_thread_set` 出现于 spec-iss.md。
    - Negative Tests (expected to FAIL):
      - 提取器单元 fixture：对包含 `set_trap("memory", "undocumented_reason")` 的源码样本，在 spec 未收录该 reason 时校验失败（堵死『新 kind reason 不被门捕获而 CI 仍绿』缺口）。
      - 从 spec-iss.md 删除任一 required term（如 `cta_blocked`）或任一已 emit 的 reason 条目，test_spec_iss 失败。
  - AC-1.2: spec-iss.md 阶段③ 契约扩写
    - Positive Tests (expected to PASS):
      - spec-iss.md 含：(a) `memory`/`synchronization` trap reason 表（草稿 §9.4 全表）与 detail schema 增量（适用时含 `address, space, width, thread_id, bar_id`）；(b) 三相（read→compute→commit）条目旁的 atomic/RED 受控豁免标注（ATM-5：32 lane 的 RMW 在 commit 相位按 pinned lane-id 升序串行，普通 load/store 不享）；(c) INV-GATE-DOMAIN-1 四档适用域与 B-OI-1..4 边界；(d) 阶段③ 比较子集键清单（含 `cta_barriers` 四纯量；`counters` 含 `mem_ops` 与 `arrived_thread_set` 排除）；(e) 单 warp `convergence`/`deadlock_no_progress` 与 CTA 层 `synchronization`/`barrier_deadlock` 并存条目；(f) collective 定序律（lane-id 升序）延伸至 atomic/RED 的条目（INV-1）；(g) 公平性契约（unconditional weak fairness，标注强于 NV conditional parallel-forward-progress、不反向声称）。
      - 上述条目均被 AC-1.1 的 required_terms/required_reasons 机器可验形态覆盖。
    - Negative Tests (expected to FAIL):
      - test_spec_iss 在上述任一机器可验条目缺失时失败。
      - spec-iss.md 引用 `nv_patent`/仓库外路径作硬依赖的既有负向断言保持失败语义（repo-locality 门不放松）。

- AC-2: ISA 单一源扩展（encode/decode 层）
  - AC-2.1: `address` operand kind 与访存指令 schema
    - 范围：`isa/currygpu/isa/assembler.py` `_parse_operand`/`_symbolize_operand` 新增 address 臂（base-reg + 可选 uniform-reg + imm-offset 寻址形态）；`isa/currygpu/isa/schema.py` 新增 `LDG/STG/LDS/STS/LDL/STL/LD/ST` 八条指令（width modifier 覆盖 U8/S8/U16/S16/32/64/128/256——`.256` 经 DEC-8 用户裁定纳入：8-寄存器组、组基址 8-对齐）；`LD/ST` 为 generic 形态；`LDC`（const bank-indexed 寻址）的 schema 归 AC-10 范围、共用本 AC 的 address 机制；`isa/currygpu/isa/layout/sample.py` 分配无冲突 opcode；`tests/isa/test_assembler.py` schema-driven 边界矩阵 helper 支持新 kind（现 helper 对未知 kind 直接 AssertionError）。
    - Positive Tests (expected to PASS):
      - 全部新指令 encode↔decode round-trip 零 diff（边界矩阵：基址寄存器/UR/imm 边界值 × width 全枚举（含 256）× guard 全枚举 × 控制段四组）。
      - codegen 完备性门与无重叠门（`_validate_no_overlap`/`_validate_full_instruction_coverage`）全绿。
    - Negative Tests (expected to FAIL):
      - 奇数寄存器索引承载 64-bit 地址/数据对、`.128` 非 4-对齐寄存器组、`.256` 非 8-对齐寄存器组、越界 imm offset、未知 width 在 assemble/encode 期被拒（承接 plan-foundation 编码期拒绝契约）。
      - decode 对 reserved 位非零的 word 拒绝。
  - AC-2.2: atomic/RED 指令 schema
    - 范围：`ATOM/ATOMG/ATOMS/RED/REDG/REDS`；op modifier = 整数 10-op `{ADD,MIN,MAX,INC,DEC,AND,OR,XOR,EXCH,CAS}`；CAS 三操作数；数据宽度本阶段 32-bit（64-bit 为上界可选项）；float 类型本阶段不进可编码集（接缝=schema 机制可扩展 + spec 接缝声明，见 AC-7 与 R-6）。
    - Positive Tests (expected to PASS): 全 op × space × guard 边界矩阵 round-trip 零 diff。
    - Negative Tests (expected to FAIL): 不在集合内的 op 编码值 decode 拒绝；缺操作数 assemble 拒绝。
  - AC-2.3: BAR / MEMBAR / FENCE / CVTA schema
    - 范围：`BAR`（mode modifier `{SYNC, ARV}` + bar_id 操作数 0..15 + 可选 thread-count 操作数，缺省哨兵=block-wide）；`MEMBAR`/`FENCE`（scope `{CTA,SM,GPU,SYS}` 与 order 操作数，解码保留）；`CVTA`（方向 modifier + 寄存器对操作数，纯地址算术）。
    - Positive Tests (expected to PASS): round-trip 零 diff；`BAR.SYNC 0`、`BAR.SYNC 0, 64`、`BAR.ARV 0, 32` 均可编码且语义字段正确。
    - Negative Tests (expected to FAIL): bar_id > 15 assemble 拒绝；字面 count 非 32 倍数 assemble 拒绝（B-3 编码期半）；`BAR.ARV` 缺显式非零 count assemble 拒绝（PTX A1）。
  - AC-2.4: S2R 选择子扩展（DEC-MS-PRE-1 全量落地）
    - 范围：三处同步扩展——schema 的 sr `FieldSchema.choices`、`OperandSchema.choices` 与 assembler 的 `SREG_VALUES`——从 `{SR_LANEID}` 扩到 `{SR_LANEID, SR_TID.X/Y/Z, SR_NTID.X/Y/Z, SR_CTAID.X/Y/Z, SR_NCTAID.X/Y/Z, SR_WARPID, SR_NWARPID}`（共 15 个选择子）。
    - Positive Tests (expected to PASS): 每个新选择子 round-trip 零 diff；执行语义由 AC-5.4 验收。
    - Negative Tests (expected to FAIL): 未列入 choices 的选择子在 assemble 与 decode 双侧拒绝（沿用现 S2R 拒绝测试模式）。

- AC-3: memory_space 核心、snapshot 序列化与字节级 diff
  - 范围：`memory_space` 抽象基类 + `memory_space_impl`（BLOCK=4096 稀疏 hashmap），满足 MS-1（惰性分配 + 零初始化 + 只序列化非全零块）、MS-2（空间无关核）、MS-3（小端唯一）、MS-4（per-lane per-byte 写使能；inactive/guard-false lane 完全压制）；per-space 归属 OWN-1/2/3（global 单例、shared per-CTA、local per-lane）；`counters.mem_ops` 激活（INV-6：每条访存/atomic 指令 += popcount(participants)）。
  - snapshot 契约（SS-1）：`memory` 子树仍恰好 `{global, shared, local}` 三键；global/shared 为 `{block_id(十六进制，升序): 字节序列}` 稀疏映射（INV-MEM-SER-1 确定序、INV-MEM-SER-2 稀疏）；local 按线性 tid 字符串键（`"0".."num_warps*32-1"`），单 warp 退化即 `"0".."31"`（统一两形态键形）。
  - Positive Tests (expected to PASS):
    - 单 warp `STG` 后 snapshot `memory.global` 含且仅含被写块；块内未写字节为 0；写后全清零的块不出现（『写后清零 ↔ 从未写』snapshot 等价）。
    - `state_diff` 对篡改单字节内存报告一处最小局部化差异（path 形如 `$.memory.<space>...`，VH-8，承接 foundation AC-6 推迟项）。
    - 现有 `tests/iss/test_native.py` 对无访存程序的 `memory` 三空键精确断言与 `counters` 精确断言 0 改动通过（稀疏序列化保证空程序输出不变）。
    - `mem_ops` 单序 1:1 diff 回归用例通过。
  - Negative Tests (expected to FAIL):
    - 读未分配块返回全 0 且不分配（snapshot 不出现该块；分配则为实现错误，由序列化断言捕获）。
    - guard-false / inactive lane 的 store：不分配块、不改字节、不计 mem_ops（MS-4 压制负向用例）。
    - `mem_ops` 进入序无关比较子集的实现被 VH-9 拒绝（counters 整体排除）。

- AC-4: load/store 执行语义（直达 + generic + conformance）
  - 范围：直达 `LDG/STG/LDS/STS/LDL/STL` decode 期锚定空间（ADDR-1）；generic `LD/ST` 运行期窗口推断纯函数（ADDR-2，半开区间 `[base, base+size)`；窗口仅 shared/local 两类、fall-through 到 global——const 不在 generic 窗口模型内、仅经 `LDC` 可达）；`CVTA` 纯地址加减不访存（ADDR-3）；generic→local 窗口 per-lane 别名（ADDR-4）；窗口基址数值 layout 私有、样例 layout 给可 CI 占位值（ADDR-5）；访存唯一入口 = gather/scatter（decode-once + masked-lane-loop）；64-bit 地址承载于偶对齐寄存器对小端（草稿 §2.5）；global bounds check（DEC-7 裁定纳入）——launch 声明 `global_allocations`（(base, size) 区间表）时，global/generic-global 访问地址须落入任一区间，否则 `global_oob` trap；未声明时回退稀疏无界语义（向后兼容、零破坏）。
  - R-3 修订（新 MS-5）：同一条 store 指令内多 lane 命中同字节（非原子）时，commit 相位按 lane-id 升序落字节、最高 lane 终值生效——确定、不随外层调度变；该程序仍属 race、不进序无关主门（VP-2），但执行行为确定。
  - Positive Tests (expected to PASS):
    - conformance 差分 CF-1（sub-word extend-to-32 oracle 精确）、CF-2（byte-masked 只写低位）、CF-3（gather/scatter 逐 lane 地址→块→字节一致 + 最小局部化 diff）、CF-4（`.64` 偶对小端 / `.128` 4-对齐组 / `.256` 8-对齐组）、CF-5（packed word 字节布局，SIMD 解释不在访存层）全部通过。
    - 声明 `global_allocations` 的程序：区间内访问行为与未声明时逐 bit 一致（bounds check 只增加判别力、不改变合法访问语义）。
    - corpus MC-8（local per-lane 私有性）、MC-9（generic 窗口命中预期 space、无跨 space 别名、`CVTA` 往返）、MC-10/11/12 按各自 oracle 通过。
  - Negative Tests (expected to FAIL):
    - 先写 GPR 满 1 再 `LD.U8`，断言高 24 位清零（拒 partial-register merge，CF-1 关键负向）。
    - mask=0 lane 对应字节终态 == 写前值（CF-2 关键负向）。
    - `misaligned_address` / `shared_oob` / `local_oob` / `global_oob` / `unsupported_space_access` / `generic_resolve_failure` 各有 state-determined trap 负向用例（MC-N1/MC-N2/MC-N9），且全部调度序到同一 trap（VP-4）。
    - 非对齐 `.64`/`.128`/`.256` 地址 → `misaligned_address`（CF-4 负向）。

- AC-5: multi-warp / CTA 执行模型与调度契约
  - AC-5.1: 三层状态聚合与 warp 内核冻结
    - 范围：`block_state`（shared_memory + tensor_memory 占位不填字节 + barriers + warps）；`cluster_state` dim=1 恒等薄壳，rank≠self → `unsupported_space_access` trap、绝不伪造远端态（DEC-MS-MW-1/MW-4）；warp 态语义冻结（INV-AGG-1：3 字段 Bx snapshot、4 态 lane_state、阶段② transition table 不变）；warp 间仅经 CTA barrier / atomic / shared-mem 三接缝交互（INV-AGG-3）。
    - Positive Tests (expected to PASS): 现有全部 foundation/ITS/metamorphic 测试在 CTA 引擎下 0 改动通过；`num_warps==1` 时 snapshot 的 legacy 键投影（阶段② 的 10 键：9 架构键 + `counters`）逐 bit 等于阶段② 输出，顶层增量仅恒定输出的 `cta_barriers` 一键（VH-1 的『warps[0] 视图 bit-identical』即此投影口径，第二轮修订：与 AC-5.5 的新增键自洽）。
    - Negative Tests (expected to FAIL): 跨-CTA shared 访问（remote-rank 形态）→ 干净 `unsupported_space_access` trap，不别名 self。
  - AC-5.2: 两层调度（DEC-Q1 三裁定落地）
    - 范围：调度原子单位 = 一个 `step_one_group`（指令级交错，量子 K=1，INV-SCH-3/INV-SCH-5；禁止 run-to-blocking-point）；`warp_sched_order ∈ {warp_round_robin(默认), warp_min_id_first, warp_max_id_first}`；R-2 修订后的公平性契约：unconditional weak fairness 由 fair 序承载——`warp_round_robin` 必须是 runnable 集上的 fair permutation 且为 D 档终止断言唯一载体；`warp_min_id_first`/`warp_max_id_first` 为有意非 fair 的固定优先级确定全序，仅入 A/B 档主门叉积（对终止/barrier-DRF 程序所有序同终态），不用于终止性断言（消解 INV-SCH-1 原文『每个序都是 fair permutation』与 DEC-MS-MW-2『固定优先级可 starve』的字面冲突，语义以本条为准）；runnable/blocked/yielded 三态分类（INV-SCH-6，spinning lane 算 runnable）；deadlock = 纯快照函数 trap、livelock = 步数预算非 trap（INV-SCH-7）；三段恢复级联（`try_fire_cta_barriers` + `try_fire_bx` + `promote_yielded`）。
    - Positive Tests (expected to PASS): VH-15 调度粒度回归（每 macro-step 恰推进一个 `step_one_group`）；INV-SCH-4 单 warp 下任意 `warp_sched_order` 退化 bit 等价；YIELD 跨 warp 层 no-op-for-state（DEC-MS-YIELD-CROSS-1：A/B 档插入 YIELD 终态 bit 不变，单 warp 退化与阶段② bit-identical）。
    - Negative Tests (expected to FAIL): 具名 mutant `warp_run_to_block`（跑到阻塞才切）在含 spin 的 D 档 corpus 上死锁或超预算被杀（VH-15）；`warp_sched_unfair`、`skip_yielded_promotion` 被杀（VH-12）。
  - AC-5.3: launch 契约扩展（DEC-MS-PRE-2 单一收口）
    - 范围：`launch`/`launch_words` 新增 kwargs（全部带默认值、向后兼容）：`num_warps=1`（per-CTA warp 数）、`warp_sched_order="warp_round_robin"`、`ntid`（per-CTA 维度，默认 `(32*num_warps, 1, 1)`，约束 `prod(ntid) == num_warps*32`）、`nctaid=(1,1,1)`（grid 维度；`prod(nctaid)` 即独立 CTA 数，DEC-5 裁定纳入，各 CTA 坐标按字典序自动派生、不设 `ctaid` kwarg）、`shared_mem_bytes=49152`、`local_mem_bytes=16384`（per-lane）（DEC-7 已裁定采纳该默认值，launch 可覆盖）、`global_allocations=None`（(base, size) 区间表，DEC-7；None = 稀疏无界）、`const_banks=None`（bank → 字节串映射，DEC-1/AC-10）、`race_check=False`（DEC-6/AC-12）；既有 kwarg `sched_order` 名义与含义完全不变（= warp 内 `SchedOrder`），`warp_sched_order` 是新增的 CTA 层正交参数（INV-SCH-2）；`iss/currygpu/iss/native.py` wrapper 同步转发全部新参数。
    - Positive Tests (expected to PASS): 全部旧签名调用零改动可用（现有测试即证）；`sched_order` 与 `warp_sched_order` 可独立组合设置；VH-10 `boundary_calls()` 在 `num_warps` 与 `nctaid` 变化下仍 O(launch+step)、不随 warp/CTA 数增长。
    - Negative Tests (expected to FAIL): `prod(ntid) != num_warps*32` → launch 期 ValueError（非运行期 trap）；未知 `warp_sched_order` 字符串 → ValueError（对齐现 `sched_order` 行为）；`global_allocations` 区间重叠/size 为 0、`const_banks` 键非法 → launch 期 ValueError。
  - AC-5.4: S2R 几何寄存器执行语义
    - 范围：`SR_WARPID` = warp 在 `block_state.warps` 的下标；`SR_NWARPID` = warps 数；`tid = warp_id*32 + lane` 按 `ntid` 三维分解为 `SR_TID.X/Y/Z`；`SR_NTID.*`/`SR_NCTAID.*` 直读 launch 入参；`SR_CTAID.*` = 本 CTA 在 grid 中的派生坐标（单 CTA 即 (0,0,0)，多 CTA 见 AC-11）。
    - Positive Tests (expected to PASS): 各选择子在 `num_warps ∈ {1,2,4}` 与非平凡三维 `ntid` 下逐 lane 值正确；`nctaid` 非平凡时各 CTA 的 `SR_CTAID` 互异且与字典序一致；`SR_LANEID` 行为与阶段② bit 不变。
    - Negative Tests (expected to FAIL): 非法选择子已被 AC-2.4 编码侧拒绝；`ntid` 非法组合被 AC-5.3 launch 校验拒绝（执行期不可达）。
  - AC-5.5: CTA snapshot schema 双形态与比较子集三处同步（SS-2）
    - 范围：`num_warps==1` 沿用阶段② 平铺 schema + 新增顶层 `cta_barriers` 键（以及使用 const 注入时的顶层 `const_memory` 键，R-8）；`num_warps>1` 输出 `{warps: [per-warp {active_mask, pc, lane_state, vgpr, predicates, uniform_registers, bx}], memory: {global, shared, local}, cta_barriers, trap, counters}`（per-warp 视图 7 键；`memory`/`trap`/`counters` 为 CTA 级——对草稿 INV-AGG-4『per-warp 9-key』措辞的精确化：9 键中 `memory`/`trap` 上提为 CTA 级，R-5）；`prod(nctaid)>1` 的 grid 形态见 AC-11。`cta_barriers` = 16 槽 × `{phase, arrived_count, expected_count, phase_parity}` 四纯量，序列化确定序；`const_memory` 为只读注入内容、不进比较子集（R-8）。三处同步：spec-iss.md 键清单、`tests/iss/its_corpus.py` `ARCH_STATE_KEYS`（加 `cta_barriers`；`const_memory` 不加入）与形态感知 `architectural_subset`、`tests/structure/test_spec_iss.py` 反向校验。
    - Positive Tests (expected to PASS): 单 warp 模式下现有断言 0 改动通过（test_native.py 不锁顶层键全集，已核实）；新增兼容性回归测试显式锁定该契约——单 warp snapshot 恰为『阶段② 10 键 + `cta_barriers`』、`counters` 子树精确键集不变；metamorphic 比较子集含 `cta_barriers` 四纯量、quiescent 终态比较点（终态 `arrived_count` 全 0、parity 收敛）。
    - Negative Tests (expected to FAIL): `arrived_thread_set` 出现在比较子集 → VH-9 失败（仅 debug 序列化，按 (warp_id, lane) 升序确定输出）；`counters`（含 `mem_ops`）进比较子集 → VH-9 失败。

- AC-6: CTA named barrier 状态机
  - 范围：`CtaBarrier {phase, arrived_count, expected_count, expected_pinned, arrived_thread_set, phase_parity}`（arrived_thread_set 取 per-warp lane bitmask 数组，B-11）；三态 FSM Idle→Gathering→Released（草稿 §6.3）；不变式 B-1..B-17 全部落地：per-thread 到达不要求 warp 收敛（B-6，Volta+ 语义）、跨 call site 同 bar_id 累计且 fire 后各自 next PC 恢复（B-14）、block-wide 动态非退出 expected（B-12，依赖 SR_NTID）与显式 count 静态固定（B-13）并存、EXIT 三路收口（Bx 清 participation→dissolved / block-wide 调减并重检 fire / 显式 count 不调减，草稿 §8.1）、`BAR.ARV` 不阻塞 + 完成自动 reinit 复用（B-17）、谓词化 CTA barrier 接受 guard-true 子集到达不 trap（B-10 裁定）、warp 级 `cta_blocked` 标志承载阻塞对象（不侵入 lane_state 四态、不写 `blocked_on`）、两层 fire 谓词正交（B-16）、B-15 统一 deadlock 谓词（覆盖 D-a/D-b/D-c/D-d 四类交叉死锁；单 warp 纯 Bx 死锁保留 `convergence`/`deadlock_no_progress` 并存）。
  - Positive Tests (expected to PASS):
    - corpus MC-3（到达计数 FSM + divergent 分批到达）、MC-13（divergent if/else 两支同 bar_id：两支到达汇入同一 `arrived_count`、全员到齐后各自 next PC 恢复，VH-3 子断言）、MC-14（`BAR.ARV` producer + `BAR.SYNC` consumer split-barrier 复用）通过并进 A 档主门。
    - VH-6a：一 warp 提前 EXIT（从未到 barrier），block-wide expected 动态扣减、不死锁。
    - B-8/B-17：同 bar_id 循环复用——phase_parity 翻转、槽回收、无假 release。
  - Negative Tests (expected to FAIL):
    - VH-6b / MC-N7：显式 count N 不调减，EXIT 致 arrived 永不达 N → 确定性 `synchronization`/`barrier_deadlock`（D-d）。
    - MC-N4 / MC-N6：部分 warp 不到 barrier / divergent 单支 barrier 不到达不退出 → `barrier_deadlock`（D-c），所有 `warp_sched_order × SchedOrder` 组合到同一 trap（B-15 纯快照函数，VP-4）。
    - VH-13 混合阻塞矩阵：D-a（Bx 等 bar）/ D-b（bar 等 Bx）下 `Bx` 的 `barrier_ready` 面对 blocked-on-CTA-bar lane 不误 fire（B-16）、全 blocked 确定 trap、全部 `warp_sched_order × SchedOrder` 组合到同一 trap。
    - bar_id 越界 → `barrier_id_out_of_range`；运行期非 32 倍数 count / 同 phase 不同 expected → `barrier_count_not_warp_multiple`；重复到达 → debug 断言 `cta_barrier_double_arrive`（B-2）；arrive-overuse → debug 断言 `cta_barrier_arrive_overuse`（B-17）。
    - 具名 mutant `per_warp_arrival`、`static_expected_no_exit_decrement`、`cross_callsite_not_counted`、`bx_fires_on_cta_blocked_lane` 全部被杀（VH-12）。

- AC-7: 整数 atomic / RED 串行化 RMW
  - 范围：ATM-1（ATOM 返回旧值 / RED 无返回；RED 出现非 RZ dst → `red_has_destination` trap）；ATM-2 整数 10-op 语义（含 CUDA atomicInc/Dec wrap 语义、min/max 有/无符号区分）；ATM-3 交换性分类；ATM-4 空间合法性（仅 global/shared；local → `atomic_on_local_unsupported`、const 形态 → `atomic_on_readonly_space`）；ATM-5 commit 相位 pinned lane-id 升序串行 RMW（对三相 V2 的受控豁免，spec 标注由 AC-1.2 承载）；ATM-6 不可分性；INV-1..INV-6 定序不变式；atomic scope/ordering 后缀解码保留、功能忽略（草稿 §3.4）。
  - R-1 修订（INV-5a 收窄，消解跨 warp atomic 序相关；含第二轮精确化）：B 档（跨 warp bit-equality）成员必须同时满足两个条件——(1) op 属交换结合集 `{ADD(整数), MIN, MAX, AND, OR, XOR}`（终态内存序无关的前提，B-OI-4）；(2) 不消费旧值（`RED.<op>` 形态，或 `Rd=RZ` 的 `ATOM.<op>` 形态）。顺序敏感 op `{EXCH, CAS, INC, DEC}` 无论 RED/ATOM 形态、无论 Rd 是否为 RZ，其最终内存态本身依赖串行化顺序，一律归 C 档确定性基线。带旧值返回的交换结合 `ATOM` 的跨调度序断言仅限单 warp 成员（INV-3b 适用域 = warp 内 pinned lane 序；跨 warp 时旧值由 warp 交错决定，同归 C 档）。MC-4/MC-5 的 multi-warp 形态按此构造（交换结合 op + RED 或 Rd=RZ）。
  - R-6 修订（float atomic 接缝形态钉死）：float atomic 本阶段不进可编码集（AC-2.2）；spec 接缝声明按 DEC-MS-FATOM-1 记录（逐次独立 IEEE RNE fadd、不折叠 FDA、INV-1 定序），数值与 Tier-1/Tier-2 验证随阶段④（FUT-1）。
  - Positive Tests (expected to PASS):
    - VH-4：整数 atomic 实测终态与 AR-oracle 一致，oracle 分三形态（第二轮修订：pinned 序仅 warp 内有效）——(a) 交换结合 op：任意串行序 fold 的唯一参考值（唯一性来自交换结合律，与 fold 顺序无关）；(b) warp 内顺序敏感 op：按 INV-1/INV-2 pinned lane-id 升序 fold（含 per-lane 旧值中间态）；(c) 跨 warp 顺序敏感 op：无闭式 oracle，不与 fold 参考比对，归 C 档（VH-5）。交换结合 op 的多 warp RED 在 12 序组合上 bit-identical（INV-3a/INV-5）；单 warp ATOM 旧值在四 SchedOrder 下一致（INV-3b）。
    - MC-4（N lane ATOM.ADD 同址，终值 == 参与 lane 数）、MC-5（MIN/MAX/AND/OR/XOR + RED fold 与单线程序列化参考一致）通过。
    - INV-4：异址 lane 互不影响；CAS 成功/失败两路径返回值差分正确。
  - Negative Tests (expected to FAIL):
    - VH-5 / MC-N5：多 warp `ATOM.EXCH/CAS` 同址为 C 档——固定单序两次运行 bit-exact、不跨序断言，且被正确排除出 VH-2 主门（防 false-fail）。
    - `atomic_misaligned`、`atomic_unsupported_op`、`atomic_on_local_unsupported` trap 负向用例，state-determined、全部序组合到同一 trap。
    - 具名 mutant `non_atomic_rmw`（RMW 退化为可插入的 LD-ADD-ST → lost-update / 序相关终值）被杀。

- AC-8: fence/MEMBAR 功能 no-op 与 mbarrier 非扩展
  - 范围：`MEMBAR`/`FENCE` handler 空体（仅推进 PC、消费 active mask，DEC-MS-FENCE-1）；解码器提取 scope/order 操作数且 round-trip；soundness 四环论证以契约形式落 spec（AC-1.2 承载）；mbarrier/async 指令族（SYNCS/ARRIVES/LDGSTS/DEPBAR/ACQBULK 等）本阶段不加入 schema、不建模、不进 snapshot。
  - R-7 修订（对草稿『mbarrier/async 可解码且功能 no-op』的显式取代，第二轮收敛）：草稿 §1.2/§7.1 与红线 6 的字面要求是这些指令『可解码且功能 no-op』，但当前 ISA 单一源中这些 opcode 完全不存在——满足字面要求须现在发明其编码，而 ARRIVES 的 ARVCNT/TRANSCNT 双 modifier、mbarrier 的 shared-地址操作数、LDGSTS 的双地址形态等操作数面恰恰依赖阶段④ async-copy 数据通路设计，提前冻结编码与『编码私有』红线及最小面原则冲突。本计划裁定：该『可解码 + no-op』义务转化为对阶段④ 的接缝承诺——这些指令在任何后续阶段进入 ISA 时必须首先以 decode + 功能 no-op 形态落地（不实现 tx-count 记账、不新增 trap reason、不进 snapshot），本阶段交付物为 §7.2 语义基线的契约化文档 + 本负向断言集。红线 6 的其余内容（无 tx-count 握手等）全部保持。
  - Positive Tests (expected to PASS):
    - VH-7（单 warp）与 VH-16（multi-warp A/B 档）：在 well-formed corpus 任意位置插入/删除 `MEMBAR`/`FENCE`，所有 fair 序组合最终架构态逐 bit 不变（仅 counters 可变、已排除）。
    - scope/order 操作数 encode↔decode round-trip 正确。
  - Negative Tests (expected to FAIL):
    - snapshot 不存在 `mbarriers` 键（SS-3）；schema 不含 mbarrier 指令。
    - `BAR.SYNC` 不被实现为 no-op：删除其到达阻塞语义的 mutant（`drop_cta_barrier_arrive`）被 A 档主门或 deadlock 门杀（防『把整条 BAR 当 no-op』误读）。

- AC-9: 四档序无关验证门、corpus 与 mutation 全杀
  - 范围：
    - corpus（`tests/iss/its_corpus.py`）：新增 `LDS/STS/LDG/STG/LDL/STL/LD/ST/LDC/ATOM*/RED*/BAR/MEMBAR/CVTA` 与新 S2R 选择子的 emit 路径；新增成员 MC-1..MC-14 与 MC-N1..N7（草稿 §9.2 表），及扩张新增 MC-15（grid 独立多 CTA 不相交 global 写，AC-11）、MC-16（`LDC` const 读取，AC-10）、MC-N8（`const_oob`）、MC-N9（`global_oob`）；tags 沿用并新增 `progress_test`（MC-6/MC-7 自 north_star 迁出标 progress_test，归 D 档）；corpus→四档映射表（INV-GATE-DOMAIN-1）落地。
    - 门：VH-2 主门 = A 档（barrier-DRF：MC-1/2/3/13/14）+ B 档（交换 atomic：MC-4/5 按 R-1 形态）在 `warp_sched_order(3) × SchedOrder(4) = 12` 组合全叉积上最终架构态子集 bit-identical（草稿开放的组合规模在此钉死为全叉积：corpus 规模小、原生执行廉价，强于对角线方案）；VH-5 C 档单序确定性基线（MC-N5）；VH-14 D 档独立进度测试（MC-6 CAS spin-lock、MC-7 producer-consumer；fair 序 `warp_round_robin`；步数预算 = 2000 macro-steps/成员；超预算表现为 `max_steps`/`budget_exhausted` trap = 测试失败，不新增诊断 hook；语义不变式断言：MC-6 临界区互斥可观测、MC-7 consumer 读到 producer 数据）；VH-9 比较子集精确性。
    - mutation（VH-12，具名全杀、非百分比）：新增 `drop_cta_barrier_arrive`、`wrong_expected_set`、`non_atomic_rmw`、`wrong_extend`、`byte_mask_ignored`、`warp_sched_bias`、`per_warp_arrival`、`static_expected_no_exit_decrement`、`cross_callsite_not_counted`、`bx_fires_on_cta_blocked_lane`、`warp_sched_unfair`、`skip_yielded_promotion`、`warp_run_to_block`；阶段② 既有 5 mutant 继续全杀。
    - data-race 分流（草稿 §9.6 统一规则 + DEC-6 扩张）：结构显然者 static pre-screen 拒（复用单边滤器）；MC-N3 经双序 memory diff 标记（HOOK-RACE-2，零新设施：复用 `state_diff` + 多序执行）；运行时 per-byte epoch shadow（HOOK-RACE-1）经 DEC-6 裁定**本阶段以 opt-in 形态实现**（AC-12，`race_check=False` 默认关）——默认模式下分流规则与草稿一致（racy 程序确定执行、不 trap），`race_check=True` 时 MC-N3 类成员触发 `memory`/`data_race` trap；D 档（MC-6/MC-7）在默认模式下运行。
  - Positive Tests (expected to PASS): 全 corpus 成员按其档位门通过；12 组合主门绿；mutant 集全杀；`budget_exhausted` 与 livelock 谓词不进序无关比较（INV-SCH-7/VP-4 排除得到断言看护）。
  - Negative Tests (expected to FAIL): MC-N1/N2/N4/N6/N7 各按预期 trap 且全部序组合到同一 trap（state-determined 类）；MC-N3 经双序 memory diff 被标记为非序无关（racy 标记类，不适用同 trap 断言）；MC-N5 按 C 档单序基线看护；racy 成员混入主门比较集 → 门失败（VP-2 负向看护）；任一具名 mutant 存活 → 全杀断言失败。

- AC-10: const 第四内存空间（用户裁定 DEC-1 推翻草稿 DEC-MS-SCOPE-1；按草稿 §1.3 条件路径落地，R-8）
  - 范围：
    - 文档前置（草稿 §1.3 要求的纳入手续，先于实现落地）：`docs/implement/ISS/spec.md` §2/§5 与 `docs/implement/ISS/research-notes.md` §4 蓝图显式补 const 为只读第四空间，并重述 `plan-foundation.md` FUT-2 scope-line（标注经用户裁定扩张）；草稿红线 10 据此修订为『snapshot `memory` 子树仍三键 + const 以顶层 `const_memory` 键序列化』。
    - 实现：const = 第四个 `memory_space_impl` 实例（grid 级共享、只读）；`LDC` 指令（bank-indexed 寻址 `c[bank][R+imm]`，width modifier 与 LD 族一致，复用 AC-2.1 address 机制与 gather 入口）；内容经 launch kwarg `const_banks`（bank → 字节串）注入，launch 后不可写——const 不在 generic 窗口模型内、无任何 store/atomic 指令可达（只读由构造保证）；`LDC` 越界（offset ≥ 注入 bank 大小或 bank 未注入）→ `memory`/`const_oob` trap。
    - snapshot：顶层 `const_memory` 键（bank → 确定序字节映射）；**不进** `ARCH_STATE_KEYS` 序无关比较子集（草稿 §1.3 条件裁定保留：只读内容对序无关门无判别力）；`memory` 子树维持 `{global, shared, local}` 三键，既有精确断言 0 改动。
  - Positive Tests (expected to PASS):
    - MC-16：注入 const bank 后 `LDC` 各 lane 读值正确（含 sub-word extend 与 `.64/.128/.256` 宽度）、参与 12 组合主门（const 读取本身序无关）。
    - spec.md/research-notes.md/plan-foundation FUT-2 scope-line 同步完成且 `python tools/check_structure.py` 与 test_spec_iss 全绿。
    - 未使用 `const_banks` 的程序 snapshot 不含（或含恒空）`const_memory` 键且现有测试 0 改动通过。
  - Negative Tests (expected to FAIL):
    - MC-N8：`LDC` 越界 / 未注入 bank → `const_oob`，state-determined、全部序组合同 trap。
    - `const_memory` 进入比较子集的实现被 VH-9 拒绝；ATM-4 的 const-atomic 禁令保持文档化（编码面不可达则注明）。

- AC-11: 独立无交互多 CTA grid 执行（用户裁定 DEC-5 纳入）
  - 范围：`nctaid` 定义 grid，`prod(nctaid)` 个 CTA **按 ctaid 字典序逐 CTA 顺序执行**（确定性执行序；CTA 间无任何同步原语，故顺序执行 sound 且最简）；每 CTA 独享 `block_state`（shared/local/barriers/warps 全 per-CTA 隔离，OWN-2 推广）；global 与 const 为 grid 级共享单例（OWN-1 不变）；首个 trap 中止整个 grid（确定性，后续 CTA 不执行）；**支持的程序类 = CTA 间无依赖**（跨 CTA 经 global 通信/自旋依赖 = 门外类：顺序执行下给确定结果或 budget_exhausted，不进任何序无关门——与 B-OI 边界同构的 grid 级 B-OI-5 边界，落 spec）；grid snapshot 形态 = `{ctas: [grid-normalized per-CTA 形态（= AC-5.5 CTA 形态去除 global——其 memory 仅 {shared, local}、global 不在各 CTA 内重复）], memory: {global}, const_memory, cta_barriers 并入各 CTA, trap, counters(聚合)}`，`prod(nctaid)==1` 退化为 AC-5.5 的 CTA 形态（嵌套退化链：grid→CTA→legacy 平铺，各级 0 改动兼容）。
  - Positive Tests (expected to PASS):
    - MC-15：N=2 CTA 各按 `SR_CTAID` 写不相交 global 区间 + CTA 内 barrier/shared 归约——grid 终态 global = 两 CTA 写并集、各 CTA shared 终态与单 CTA 运行 bit 一致；在 12 组合（per-CTA 调度轴）上 bit-identical。
    - `prod(nctaid)==1` 时全部既有测试与 AC-5.5 契约 0 改动通过（退化恒等）。
    - 每 CTA 的 `SR_CTAID/SR_NCTAID` 值正确（AC-5.4 协同）。
  - Negative Tests (expected to FAIL):
    - 跨 CTA 写同一 global 字节（无任何隔离）→ 双序/双 CTA 序 diff 标记为门外类（不进主门；`race_check=True` 时跨 CTA 同 epoch 冲突亦可被 AC-12 检出）。
    - CTA k trap 时 grid 确定性中止：终态 trap 即 CTA k 的 trap、k 之后 CTA 未执行（断言其 shared 全空）。

- AC-12: opt-in 运行时 data-race 检测（用户裁定 DEC-6 纳入；R-9 soundness 约束形态）
  - 范围：launch kwarg `race_check=False`（默认关）；开启时按草稿 §9.6 HOOK-RACE-1 的保守 epoch 模型实现 per-byte shadow——epoch 分界 = CTA barrier release / atomic 访问（barrier-epoch 保守版，单 CTA 粒度；跨 CTA epoch 推广留 FUT-6），同 epoch 内不同 thread 对 overlapping 字节的冲突访问（≥1 写、非全 atomic、无 happens-before，INV-RACE-1 字节判定）→ `memory`/`data_race` trap（detail 含 `address, space, racing_lanes, access_kinds`）；**默认关闭时行为与草稿 DEC-MS-RACE-1 完全一致**（racy 程序确定执行、不 hang、不 trap）——R-9：默认开启会把 PTX-weak 合法的 spin/flag 程序误杀（草稿 §4.1 关键修正『ISS 不可把 race 当 trap』）并打死 D 档 corpus，故检测语义定位为『CUDA C++ race=UB 透镜的 debug 判别工具』，默认语义保持『PTX-weak 确定执行』。
  - Positive Tests (expected to PASS):
    - MC-N3（构造为单一冲突字节对）在 `race_check=True` 下触发 `data_race`，且 kind/reason/address 在全部序组合下一致（单一冲突对 → 地址确定；trap pc 不作跨序断言）；同成员在默认 `race_check=False` 下确定执行完成、固定序两次运行 bit-exact。
    - barrier 隔离的跨 warp 通信（A 档全部成员）与全 atomic 同址访问（B/C 档成员）在 `race_check=True` 下零误报（epoch 分界与 atomic 豁免正确）。
    - D 档 MC-6/MC-7 在默认模式下照常通过 VH-14（检测器不影响默认语义）。
  - Negative Tests (expected to FAIL):
    - `race_check=True` 下无屏障跨 warp 同字节冲突写 → `data_race` trap（MC-N3 即此）；guard-false/inactive lane 的『访问』不参与冲突判定（MS-4 协同，负向：仅 guard-false 侧涉及同字节时不 trap）。
    - 检测器开启不改变任何 well-formed 成员的最终架构态（shadow 为旁路元数据、零架构效果——开/关两模式对 A/B 档成员终态 bit-identical 的负向看护）。

- AC-13: 原生核源码模块化拆分（用户裁定 DEC-10；R-10）
  - 范围：`iss/binding/native.cpp` 不再单文件承载本阶段全部新增子系统——按职责单元拆出独立 `.h`/`.cpp`（落位参照 `research-notes.md` §10/§11 蓝图），最小拆分集：memory_space 基类与实现（含 per-space 归属与序列化）、CtaBarrier 状态机、block_state/warp_state 状态聚合（含 grid 容器）、race 检测 shadow（AC-12）；pybind 边界保持单一 `_native` 模块、`launch/step/snapshot/state_diff` 入口与 step 循环驱动留在 `native.cpp`；`iss/CMakeLists.txt` 同步登记新源文件（`pybind11_add_module` 源列表）；拆分随各 Milestone 增量进行（每个子系统在其落地的 Milestone 内即以独立文件形态出现，不做收尾期一次性大搬迁），每次拆分遵循行为零变更纪律、以全量测试为回归网。
  - Positive Tests (expected to PASS):
    - 拆分后全量测试（含 VH-1 退化恒等、12 组合主门、wheel 构建安装测试）全绿；`cmake --build` 两目标（`currygpu_decode_smoke`、`_native`）正常。
    - 最小拆分集各文件存在且被 CMake 源列表引用；`native.cpp` 中不再包含 memory_space / CtaBarrier / block_state 的类型定义（仅 include 与使用）。
  - Negative Tests (expected to FAIL):
    - 把任一已拆出子系统的定义复制回 `native.cpp` 造成重复定义 → 构建期 ODR/重定义拒绝。
    - 任何拆分引入的行为差异 → 被既有门杀（VH-1 bit 等价 / metamorphic 主门 / mutation 全杀对拆分前后同样成立）。

## Path Boundaries

### Upper Bound (Maximum Acceptable Scope)
最全面的可接受实现 = AC-1..AC-13 全部判据，外加以下可选增强（每项实现都自带测试与文档）：
- `allow_misaligned` launch 开关（默认仍为严格 trap）；
- 64-bit 整数 atomic（AR-oracle 同构扩展）；
- HOOK-RACE-2 双序 diff 标记固化为 corpus 工具函数；
- lane_state/predicates 热路径 enum/bitmask 化（前提：snapshot 字符串表示与全部既有断言逐 bit 不变）；
- BAR.RED.POPC/AND/OR（前提：corpus 实测需要，且归约结果进比较子集前补齐序无关论证）。
到此为止即为上界；mbarrier/async 建模、tensor 字节、cluster_dim>1 / DSMEM remote / 跨 CTA 通信语义、litmus 工具、race 检测的跨 CTA epoch 推广与默认开启、以及一切 timing 维度属于 `## Future Work / Out of Scope` 的 FUT-* 条目。

### Lower Bound (Minimum Acceptable Scope)
最小可接受实现 = 恰好满足 AC-1..AC-13 的实现，其构成为：
- 源码按 AC-13 最小拆分集模块化落位（memory_space / CtaBarrier / block_state·warp_state / race shadow 各为独立 `.h`/`.cpp`，`native.cpp` 仅留 pybind 边界与 step 循环驱动）；
- 指令面恰为 AC-2 + `LDC`（AC-10）的集合（空间判定由直达指令 + generic 窗口推断 + `CVTA` + `LDC` bank 寻址完整承载；宽度集含 `.256`）；
- 内存面交付 global/shared/local/const 四空间 + global allocation 表 bounds check + 容量默认值 49152/16384（launch 可覆盖）；
- atomic 交付 32-bit 整数 10-op；float atomic 以『spec 接缝声明 + 不可编码』形态交付（R-6）；
- 多 CTA 交付『独立无交互 grid 顺序执行』（AC-11 的程序类边界与退化恒等链）；
- data-race 处理交付三层：static pre-screen + 双序 memory diff 标记（默认模式分类）+ opt-in 运行时检测器（AC-12，`race_check` 默认关）；
- 序无关验证交付 A/B 档 12 组合全叉积、C 档单序确定性基线、D 档 fair 序进度测试；
- 实现以正确性为唯一目标（性能形态保持现状即可）。

### Allowed Choices
- Can use: C++20 / pybind11 / 现有 CMake 工艺；`unordered_map` 或 `map` 任选（序列化时显式排序，INV-MEM-SER-1）；`NativeWarp` 内部函数级重构（提取 `step_one_group`）只要 VH-1 bit 等价；snapshot 双形态的实现机制（同一类按 `num_warps` 分支或拆分 CTA 容器类）自由；源码拆分粒度在 AC-13 最小拆分集之上自由加细（如 scheduler / trap 工具再独立成文件），但不得低于最小集、pybind 单模块边界不变；corpus emit 便捷方法自由设计。
- Cannot use（草稿 §8.5 红线有效；红线 10 经 R-8 修订为『`memory` 子树仍三键 + const 走顶层 `const_memory` 键、不进比较子集；tensor 仍不进 snapshot』）: 单一扁平内存（必须 per-space + 4KB 稀疏块）；per-warp 到达计数（必须 per-thread）；run-to-blocking-point 调度；IPDOM 栈；第 4 个 Bx snapshot 字段 / 第 5 个 lane_state 取值；relaxed/fence 可见性重排建模；counters 进序无关比较；伪造跨-CTA 远端态（独立 grid 的 CTA 间隔离不豁免此条）；默认开启的 race trap（R-9）；spec 以行号锚定代码。
- 固定设计（草稿钉死，无选择余地）: BLOCK=4096；16 barrier 槽（bar_id 0..15 全可用）；lane-id 升序单一定序律（INV-1，collective 与 atomic 共用）；调度量子 K=1；默认 `warp_round_robin`；小端唯一；sub-word extend-to-32 无 merge；SC-for-DRF + fence 功能 no-op；trap 两新 kind 命名 `memory`/`synchronization`。

## Feasibility Hints and Suggestions

> **Note**: This section is for reference and understanding only. These are conceptual suggestions, not prescriptive requirements.

### Conceptual Approach
1. 先做行为零变更重构：把 `NativeWarp::step` 循环体提取为 `step_one_group()`（一次 build/select/dispatch/commit），用全部既有测试做回归网；再在外层包 CTA 循环（草稿 §5.4 伪码为参考形态：三段恢复 → `select_warp` → `step_one_group`）。
2. `memory_space_impl` 以 `unordered_map<uint64, array<byte,4096>>` 落地；gather/scatter 为指令唯一访存入口；per-space 实例归属按草稿 §2.3 表（global 单例 / shared per-CTA / local per-lane[32]）。
3. `CtaBarrier` 按草稿 §6.3 struct；`try_fire_cta_barriers` 进三段恢复级联（fire 单调、级联 confluent，INV-SCH-7）；EXIT 路径复用现 `exec_exit` 的『清理 + 重检 fire』结构推广到 CTA 层。
4. atomic 在 commit 相位对参与 lane 按 pinned (warp, lane) 升序逐个 load-apply-store——单线程实现天然不可分，无需锁。
5. 12 组合主门直接在现四序断言循环外再套 `warp_sched_order` 循环；C/D 档分流用 corpus tags 驱动。
6. 源码落位（AC-13，参照 research-notes §10/§11）：`iss/binding/` 下新增 `memory_space.h/.cpp`、`cta_barrier.h/.cpp`、`block_state.h`（含 warp_state 聚合与 grid 容器）、`race_shadow.h/.cpp` 一类职责文件；`native.cpp` 收敛为 pybind 边界 + step 循环驱动 + 指令 dispatch；每个子系统在其落地 Milestone 内即以独立文件出现，避免收尾期大搬迁。

### Relevant References
- `iss/binding/native.cpp` — single-warp 内核、snapshot、`state_diff`、pybind 边界（本阶段全部填充点）。
- `iss/currygpu/iss/native.py` — Python wrapper（新 kwargs 需同步转发）。
- `isa/currygpu/isa/schema.py`、`isa/currygpu/isa/assembler.py`、`isa/currygpu/isa/layout/sample.py` — ISA 单一源三件套（新 operand kind 与指令落点）。
- `tests/iss/its_corpus.py`、`tests/iss/test_its_metamorphic.py`、`tests/iss/test_native.py`、`tests/structure/test_spec_iss.py`、`tests/isa/test_assembler.py`、`tests/isa/test_codegen.py` — 全部既有门与需扩展的基线。
- `docs/implement/ISS/spec.md`、`spec-iss.md`、`plan-foundation.md`、`plan-its.md`、`research-notes.md`、`plan-foundation-s2r.md` — 契约源与冻结约束。
- 本文末尾嵌入草稿 §10 — NV 一手证据与本地参考资料索引（PTX ISA 8.5 / Volta whitepaper / 专利 / cmodel / GPGPU-Sim 等）。

## Dependencies and Sequence

### Milestones
1. Milestone 1（契约基线）: AC-1——test_spec_iss 三 kind 扩展 + spec-iss.md 阶段③ 契约扩写；含 AC-10 的文档前置（spec.md §2/§5、research-notes §4 补 const、FUT-2 scope-line 重述）。无运行时行为变更，先行保证后续每个新 trap reason / 术语自始受门。
2. Milestone 2（ISA 扩展）: AC-2 全部（宽度集含 `.256`）+ AC-10 的 `LDC` schema——encode/decode 层，无执行语义。依赖：无硬依赖（与 Milestone 1 可并行，收口顺序在其后）。
3. Milestone 3（内存核心 + load/store + const，单 warp）: AC-3、AC-4（含 global allocation 表）、AC-10 实现部分，及 AC-8 的 fence 解码 + 空 handler。依赖 Milestone 2；AC-10 实现依赖 Milestone 1 的文档前置完成。
4. Milestone 4（multi-warp CTA 引擎）: AC-5 全部——`step_one_group` 重构、CTA 外层调度、launch 扩展、S2R 执行语义、snapshot 双形态 + SS-2 三处同步。依赖 Milestone 3（memory 归属挂接 block_state）。
5. Milestone 5（CTA barrier）: AC-6。依赖 Milestone 4（`cta_blocked`、三段恢复、SR_NTID）。
6. Milestone 6（atomic/RED）: AC-7。单 warp 语义可在 Milestone 3 后开发，跨 warp 验收依赖 Milestone 4。
7. Milestone 7（grid 多 CTA + race 检测器）: AC-11（依赖 Milestone 4 的 CTA 容器与 AC-5.4 几何）、AC-12（依赖 Milestone 5 的 barrier epoch 分界与 Milestone 6 的 atomic 豁免判定）。
8. Milestone 8（验证收口）: AC-9 全量 corpus/四档门/mutation + VH-16，及 AC-1.2 的 spec 最终一致性核对。依赖 Milestone 5、6、7。

依赖要点：AC-1 先于一切新 trap 落地收口；AC-10 实现以其文档前置（Milestone 1 内）为硬前提（草稿 §1.3 条件路径）；`cta_barriers` 进 `ARCH_STATE_KEYS` 必须与 snapshot 实际输出该键同一变更原子提交（否则 metamorphic 投影 KeyError）；S2R 执行语义依赖 launch 几何入参（AC-5.3 → AC-5.4）；VH-6a 依赖 SR_NTID（AC-2.4/AC-5.4 → AC-6）；AC-11 的 grid 形态以 AC-5.5 的 CTA 形态为内层退化锚；AC-12 的 epoch 分界依赖 AC-6 的 barrier release 事件；AC-13 模块化拆分贯穿 Milestone 3–7 增量进行（memory_space 文件随 Milestone 3、block_state/warp_state 随 Milestone 4、cta_barrier 随 Milestone 5、race shadow 随 Milestone 7），不设独立收尾里程碑。

## Task Breakdown

Each task must include exactly one routing tag:
- `coding`: implemented by Claude
- `analyze`: executed via Codex (`/humanize:ask-codex`)

| Task ID | Description | Target AC | Tag (`coding`/`analyze`) | Depends On |
|---------|-------------|-----------|----------------------------|------------|
| task1 | test_spec_iss 三 kind 提取器扩展 + required_reasons/terms 断言 + 提取器负向 fixture | AC-1.1 | coding | - |
| task2 | spec-iss.md 阶段③ 契约扩写（trap 表 / ATM-5 豁免标注 / 四档 / 键清单 / 命名并存 / 公平性契约） | AC-1.2 | coding | task1 |
| task3 | address operand kind + LD/ST 族 schema/layout/assembler + 测试 helper 扩展 | AC-2.1 | coding | - |
| task4 | ATOM/RED 族 + BAR/MEMBAR/FENCE/CVTA schema 与编码期拒绝 | AC-2.2, AC-2.3 | coding | task3 |
| task5 | S2R 选择子三处同步扩展 | AC-2.4 | coding | task3 |
| task6 | memory_space 基类/impl + per-space 归属 + 序列化 + mem_ops + 字节级 diff（以独立 memory_space 文件落位 + CMake 登记） | AC-3, AC-13 | coding | task3 |
| task7 | 直达/generic load/store + CVTA 执行 + CF-1..5 + memory trap 族 + fence 空 handler | AC-4, AC-8 | coding | task4, task6 |
| task8 | 对照 sm100a 资料核对 .E/.EL2 与 .128 对齐线索，细化对齐表并回写 AC-4 trap 判据 | AC-4 | analyze | task7 |
| task9 | step_one_group 行为零变更重构 + CTA 外层调度 + 三具名 warp 序 + 三态分类 + deadlock/livelock 分离（block_state/warp_state 聚合以独立文件落位） | AC-5.1, AC-5.2, AC-13 | coding | task6 |
| task10 | launch 扩展 + native.py 转发 + S2R 执行语义 + snapshot 双形态 + ARCH_STATE_KEYS/architectural_subset 同步 | AC-5.3, AC-5.4, AC-5.5 | coding | task9, task5 |
| task11 | CtaBarrier FSM + B-1..17 + EXIT 三路收口 + 混合阻塞 + barrier trap 族（cta_barrier 独立文件落位） | AC-6, AC-13 | coding | task10 |
| task12 | 整数 atomic/RED 执行 + AR-oracle + C 档基线 + atomic trap 族 | AC-7 | coding | task7, task10 |
| task13 | corpus 全量成员 + 12 组合主门 + C/D 档门 + 具名 mutant 集 + race 分流 + VH-16 | AC-9, AC-8 | coding | task11, task12, task17, task18, task19 |
| task14 | barrier FSM 与调度实现对 B-1..17 / INV-SCH-* / DEC-Q1 的对抗性审查 | AC-6, AC-5.2 | analyze | task11 |
| task15 | spec-iss / 计划 / 实现三方一致性与红线终检（含 R-1..R-10 修订点与用户裁定落实核对、AC-13 最小拆分集核验） | AC-1.2, AC-9, AC-13 | analyze | task13 |
| task16 | const 纳入的文档前置：spec.md §2/§5 + research-notes §4 补 const 只读空间 + plan-foundation FUT-2 scope-line 重述 | AC-10 | coding | task2 |
| task17 | `LDC` schema/exec + const memory_space 注入/只读/`const_oob` + 顶层 `const_memory` snapshot 键 | AC-10 | coding | task16, task6 |
| task18 | 独立多 CTA grid 执行：nctaid 驱动逐 CTA 顺序执行 + grid snapshot 形态 + 退化恒等链 + MC-15 | AC-11 | coding | task10 |
| task19 | opt-in race 检测器：race_check 开关 + barrier/atomic-epoch shadow + `data_race` trap + 双模式验证（race shadow 独立文件落位） | AC-12, AC-13 | coding | task11, task12 |

## Future Work / Out of Scope

Future, deferred, post-work, successor-loop, and out-of-scope items belong here, not under `## Acceptance Criteria`.

> 编号注：原 FUT-3（const）、FUT-7（`.256`）、FUT-9（global bounds）经用户评审裁定（DEC-1/DEC-8/DEC-7）提升为当前范围（分别由 AC-10、AC-2.1/AC-4、AC-4/AC-5.3 承载），编号保留空缺以维持引用稳定。

- FUT-1: float atomic 数值结果与 Tier-1/Tier-2 验证（correctly-rounded fadd 含 fp16/bf16、0-ULP 对 MPFR），随阶段④ 数值循环交付。
  - Source DEC: DEC-3
  - Current-loop handoff: AC-7（spec 接缝声明 + float 不可编码）与 AC-2.2
  - Promotion trigger: 阶段④ 数值 oracle（MPFR/fp64 参考）就绪。
- FUT-2: mbarrier / async transaction barrier（tx-count 双轨 AND、eager memcpy + complete_tx、`mbarrier_*` trap reasons），随 async-copy/TMA 在阶段④ 落地。
  - Source DEC: DEC-2
  - Current-loop handoff: AC-8（§7.2 语义基线文档化、本阶段不矛盾、无 mbarriers 键负向断言）
  - Promotion trigger: 阶段④ async-copy 数据通路开工，或用户确认北极星 FA-3 需要并同步修订 plan-foundation FUT-2 措辞。
- FUT-4: cluster_dim>1 / DSMEM remote-rank / 跨 CTA 通信与同步语义（含跨 CTA 序无关论证；独立无交互 grid 已由 AC-11 承载）。
  - Source DEC: DEC-4（DEC-5 已裁定纳入独立 grid，本条收窄为通信/cluster 残余）
  - Current-loop handoff: AC-5.1（rank≠self → `unsupported_space_access` trap）与 AC-11（grid 程序类边界 B-OI-5 文档化）
  - Promotion trigger: 北极星需要跨 CTA 协作（cooperative launch / DSMEM）。
- FUT-5: 形式化内存一致性 litmus 独立工具（weak outcome 枚举、message-passing/store-buffering/IRIW）。
  - Source DEC: 无新决策（承接 plan-foundation FUT-5 既定范围切分）
  - Current-loop handoff: AC-1.2（SC-for-DRF 保真口径文档化）
  - Promotion trigger: 独立工具立项。
- FUT-6: race 检测器的跨 CTA epoch 推广与『默认开启』评估（opt-in 单 CTA 保守版已由 AC-12 承载）。
  - Source DEC: DEC-6（已裁定本阶段 opt-in 实现，本条收窄为推广与默认化）
  - Current-loop handoff: AC-12（`race_check` 开关、单 CTA barrier-epoch 保守版、双模式语义文档化）
  - Promotion trigger: 跨 CTA 程序类成为常态，或误报率数据支持默认开启。
- FUT-8: BAR.RED.POPC/AND/OR（含其归约结果是否进比较子集的裁定）。
  - Source DEC: DEC-9
  - Current-loop handoff: AC-6（B-17 已含 BAR.RED 与 SYNC/ARV 混用 debug 断言边界）
  - Promotion trigger: corpus 实测需要 cross-warp 谓词归约且 `BAR.SYNC` + shared 归约不可表达。
- FUT-10: lane_state/predicates 热路径 enum/bitmask 性能优化。
  - Source DEC: 无（工程优化备忘，草稿 §5.1 热点注记）
  - Current-loop handoff: AC-5.1（snapshot 字符串表示冻结约束）
  - Promotion trigger: multi-warp / multi-CTA 规模化后的 profiling 证据。

## Claude-Codex Deliberation

### Codex 首轮分析（v1）要点
- CORE_RISKS: 阶段范围对单循环偏大；snapshot 兼容机制未定；INV-SCH-1 与 DEC-MS-MW-2 公平性措辞冲突；跨 warp ATOM 旧值即使交换 op 也序相关（INV-5a 过宽）；CTA barrier 全集状态多易错；race 术语（UB vs weak）口径不一。
- MISSING_REQUIREMENTS: launch/API 迁移契约、内存序列化精确格式、per-space 容量、最小指令子集、同 step 同字节 store 冲突确定行为、multi-warp local snapshot 键形、新 operand kind 负向 encode 测试。
- TECHNICAL_GAPS（已对代码核实）: 无内存基质、S2R 仅 SR_LANEID、无 address kind、test_spec_iss 只抓 convergence、`NativeWarp::step` 单体需先拆、`cta_barriers` 触碰冻结 9 键。
- ALTERNATIVE_DIRECTIONS: 按内存→multi-warp→barrier 切片；barrier 下界仅 BAR.SYNC；atomic 先 RED/ATOM(RZ)；fence decode-only；双 snapshot 投影。
- QUESTIONS_FOR_USER: barrier 下界、generic 归属、ATOM 旧值门、snapshot 兼容策略、data_race trap、容量默认值。

### Agreements
- 结构任务先行（test_spec_iss 三 kind + ARCH_STATE_KEYS 同步是首批任务）。
- `step_one_group` 提取作为独立的行为零变更重构，先于 CTA 语义。
- fence 解码保留 + 功能 no-op 为本阶段 AC；litmus/weak ordering 留 FUT。
- float atomic、mbarrier、const、tensor、多 CTA 全部留 FUT（注：此为用户评审前的 AI 双方共识——const、独立多 CTA、运行时 race 检测、global bounds、`.256` 后经用户评审轮提升为当前范围，见收敛轮次记录与 DEC-1/5/6/7/8）。
- data-race 处理 = pre-screen + 双序 diff，运行时 shadow 检测降级可选（DEC-6；后经用户裁定以 opt-in 形态进当前范围，AC-12/R-9）。

### Resolved Disagreements
- R-1（atomic 序相关与 B 档门，Codex 首轮提出、第二轮精确化 → Claude 采纳）: INV-5a 收窄为双条件——B 档跨 warp bit-equality 成员须 (1) op 属交换结合集 {ADD(整数),MIN,MAX,AND,OR,XOR} 且 (2) 不消费旧值（RED 或 Rd=RZ ATOM）；顺序敏感 op {EXCH,CAS,INC,DEC} 无论形态一律 C 档（`Rd=RZ` 不能拯救 EXCH——其终态内存本身序相关）；带旧值交换 ATOM 的跨序断言限单 warp。理由：INV-1 只钉 warp 内 lane 序，INV-5 明示跨 warp 顺序随调度变。
- R-2（公平性措辞冲突，Codex 提出 → Claude 采纳）: INV-SCH-1 的『每个具名序都是 fair permutation』收窄为『fair 序（warp_round_robin，默认）承载 unconditional weak fairness 与 D 档终止断言；min_id/max_id 为有意非 fair 的确定全序，仅入 A/B 档主门叉积』。与 DEC-MS-MW-2 修订版语义一致，消字面矛盾。
- R-3（同 step 同字节 store 冲突，Codex 提出 → Claude 采纳）: 新增 MS-5——scatter 同字节冲突按 lane-id 升序 commit、最高 lane 终值生效；确定执行、仍属 race 不进主门。
- R-4（race 术语，Codex 提出 → Claude 采纳）: 计划与 spec 统一口径——racy = 『序相关、排除出序无关主门、确定执行、默认不 trap』；草稿 §4.3 表的『UB』标签解释为门外排除类别（与 CUDA C++ 层约定方向一致），非 PTX 层 undefined；`data_race` trap 仅可选 hook（DEC-6）。
- R-5（snapshot 兼容机制，Codex 两轮追问 → Claude 以代码事实 + 投影口径解决）: test_native.py 不锁顶层键全集（已核实），故取双形态——num_warps==1 平铺 schema + 恒定新增 `cta_barriers` 顶层键，VH-1 的 bit-identical 锚定在『legacy 10 键投影』而非顶层全集（消解与新增键的字面矛盾）；num_warps>1 包裹 schema（per-warp 7 键 + CTA 级 memory/trap/counters/cta_barriers）；INV-AGG-4『per-warp 9-key』据此精确化；新增兼容性回归测试显式锁定该契约。
- R-6（float atomic 编码面，Claude 提出细化）: float type 本阶段不进可编码集（而非编码后运行期 trap），最小化死指令面；spec 接缝声明承载 DEC-MS-FATOM-1。
- R-7（mbarrier『可解码 + no-op』义务转化，Codex 第二轮指出字面冲突 → Claude 显式取代）: 草稿字面要求 mbarrier/async 指令『可解码且功能 no-op』，但这些 opcode 当前不在 ISA 中，预先发明编码会在 async-copy 数据通路设计前冻结操作数面（ARRIVES 双 modifier、mbarrier shared-地址、LDGSTS 双地址），与编码私有红线及最小面原则冲突。裁定：义务转化为『这些指令在任何后续阶段进入 ISA 时必须首先以 decode + 功能 no-op 形态落地』的接缝承诺，本阶段交付 §7.2 语义基线契约化 + 无 mbarrier 键/指令负向断言；红线 6 其余内容保持。
- 范围切片（Codex 提议拆分多循环 → Claude 折中）: 保持草稿单计划范围（草稿为人类收敛输入、FUT-2 锚定），以严格依赖序的 Milestone 链（收敛时 7 个，用户扩张后 8 个）与可独立验证任务化解循环失速风险；不削减 per-thread barrier / BAR.ARV / generic 窗口（均为草稿以 NV 一手证据收敛的裁定，MC-13/14 为 north_star、mutation 设计依赖之）。第二轮 Codex 认可 12 组合与 2000 步预算对该 corpus 规模合理。
- 测试预算与组合规模（草稿移交计划的两项裁决）: VH-14 步数预算 = 2000 macro-steps/成员（超预算 = budget_exhausted = 测试失败）；VH-2 取 12 组合全叉积。
- Path Boundaries 肯定式改写（Codex 第二轮 REQUIRED → Claude 采纳）: 上/下界改为肯定式描述，范围外事项指向 FUT-* 条目；Cannot-use 清单保留在 Allowed Choices（模板许可位置）。
- DEC 与 AC 的预决冲突（Codex 第二轮 REQUIRED → Claude 采纳）: AC-5.3 容量默认值与 AC-2.1 宽度集改为引用 DEC-7/DEC-8 的非预决表述（AC 锁语义与可配性、不锁待决数值/集合扩展），待用户裁定后回填（用户评审轮已回填）。
- R-8（const 纳入的集成设计，用户裁定 DEC-1 触发 → Claude 设计）: const = 第四个 `memory_space_impl` 实例（grid 级共享、只读、`LDC` bank 寻址、launch 注入），snapshot 取**顶层 `const_memory` 键**而非 memory 子树第四键——理由：(a) `ARCH_STATE_KEYS` 投影保持纯键清单（无嵌套剥除手术）；(b) `memory` 三键精确断言与 legacy 0-改动保持；(c) 草稿 §1.3 条件裁定『snapshot 可吐但不进序无关比较』被顶层非比较键自然满足。红线 10 据此修订；文档前置（spec.md/research-notes/FUT-2 scope-line）为实现硬前提。
- R-9（race 检测器 opt-in 形态，用户裁定 DEC-6 触发 → Claude 施加 soundness 约束）: 运行时检测以 `race_check=False` 默认关的 opt-in 开关交付。理由：草稿 §4.1 关键修正（Lustig ASPLOS'19 verbatim "PTX does not require data race freedom"）裁定『ISS 不可把 race 当 trap，否则拒绝合法 spin-lock / relaxed-flag 程序』，且默认 trap 会打死 D 档 MC-6/MC-7；故默认语义 = PTX-weak 确定执行（DEC-MS-RACE-1 不变），开启语义 = CUDA C++ race=UB 透镜的 debug 判别工具。检测器为旁路元数据、零架构效果（开/关对 well-formed 成员终态 bit-identical 受负向看护）。
- R-10（原生核模块化拆分升级为绑定要求，用户裁定 DEC-10 触发）: 计划原仅在实现自由度内允许拆分；用户裁定 `native.cpp` 不应单文件承载本阶段全部新增子系统，升级为 AC-13——最小拆分集（memory_space / CtaBarrier / block_state·warp_state / race shadow）+ pybind 单模块边界不变 + 随 Milestone 增量拆分 + 行为零变更纪律；落位参照 `research-notes.md` §10/§11 既有蓝图（该蓝图本就规划了 memory_space.h / block_state.h / warp_state.h 等文件），与项目分层惯例一致。

### 收敛轮次记录
- Round 1（第二 Codex 评审 candidate v1 → Claude 修订）: AGREE = R-2/R-3/R-4/R-5 方向、任务双向覆盖、里程碑原子性、12 组合与 2000 步预算；REQUIRED_CHANGES 6 项 = R-1 精确化（EXCH+RZ 漏洞）、VH-4 oracle 跨 warp 措辞、VH-1 与 cta_barriers 键矛盾、mbarrier 字面冲突（→R-7）、Path Boundaries 肯定式、DEC-7/8 预决冲突——全部采纳并已并入上文；OPTIONAL 4 项（sched_order 正交说明、兼容性回归测试、组合措辞、DEC 分区）全部采纳。无 needs_user_decision 新增（Codex 确认：若 mbarrier 与 snapshot 矛盾在计划内修复则无需新用户决策）。
- Round 2（第二 Codex 评审 candidate v2）: 裁定 converged——6 项修订逐项验证通过（fix 1..6 全部 accepted）、OPTIONAL 项落实确认、契约检查全过（AC 为当前循环门、FUT handoff 合法、无 Handoff 模板残留、AC/Task 双向覆盖、无行号引用、无时间估算）；REQUIRED_CHANGES 空、DISAGREE 空；仅 1 项措辞类 OPTIONAL（MC-N3 标记类与 trap 类负向断言分句，已采纳）。DEC-5/6/7/8 确认为正确限定的待决项、不阻塞任何 AC 可验性。
- 用户评审轮（PENDING 决策裁定 + 范围扩张）: 用户裁定 DEC-5 纳入独立多 CTA（→AC-11）、DEC-6 本阶段实现运行时 race 检测（→AC-12，R-9 opt-in 形态）、DEC-7 global 加 bounds 检查（→AC-4/AC-5.3）+ 容量默认 49152/16384 采纳、DEC-8 纳入 `.256`（→AC-2.1/AC-4），并显式推翻草稿 DEC-MS-SCOPE-1——const 第四内存空间本阶段实现（DEC-1 → AC-10，R-8，按草稿 §1.3 条件路径）。五项裁定已全部整合：新增 AC-10/11/12、AC-1.1/2.1/4/5.3/5.4/5.5/9 相应扩充、FUT-3/7/9 提升（编号留缺）、FUT-4/6 收窄、Milestone 扩至 8 个、task16..19 新增。
- Round 3（第二 Codex 对扩张整合的复核，candidate v3）: 裁定 converged——五项用户裁定的整合逐项 accepted（const 条件路径 + 只读构造 + 顶层键；grid 顺序执行对无交互类 sound + B-OI-5 + 退化链；race opt-in 为正确解、默认开会破坏 PTX-weak 修正与 D 档；global_allocations 向后兼容 + global_oob；`.256` 编码/对齐/CF-4）；全局一致性 accepted（AC-10/11/12↔task16..19 双向覆盖、8 Milestone 依赖、FUT 提升/收窄无悬挂引用、零 PENDING、肯定式边界、INV-RACE-1/MS-4 协同、AC-10/11/12 当前循环可验）；REQUIRED_CHANGES 空、DISAGREE 空、UNRESOLVED 空；1 项措辞 OPTIONAL（AC-11 grid-normalized per-CTA 形态防 global 重复嵌入，已采纳）。
- 用户评审补充裁定（收敛后追加）: 用户裁定 `native.cpp` 模块化拆分为绑定要求（DEC-10 → AC-13/R-10）——纯工程结构约束、行为零变更、不触碰任何已收敛的语义契约（各既有门对拆分前后同样成立），故不重开收敛轮；task6/9/11/19 增挂 AC-13 目标、task15 终检纳入最小拆分集核验。

### Convergence Status
- Final Status: `converged`（Claude/Codex 双方无遗留分歧；全部用户决策已裁定并整合，无 PENDING 项；扩张整合经第三轮 Codex 复核确认——结论见收敛轮次记录 Round 3）

## Pending User Decisions

当前无 PENDING 决策：全部决策已裁定（沿草稿或经用户评审），记录如下供追溯。

### 用户评审裁定（范围扩张五项 + 工程结构一项）

- DEC-1: const memory 阶段归属
  - Claude Position: 不进阶段③（沿草稿 DEC-MS-SCOPE-1），接缝预留。
  - Codex Position: 同意 Claude（const 归 FUT）。
  - Tradeoff Summary: 纳入则触发基线变更并越过 FUT-2 冻结 scope-line（草稿 §1.3 给出条件路径）；不纳入则 LDC/kernel 参数读取延后。
  - Decision Status: 用户裁定推翻草稿 DEC-MS-SCOPE-1——const 作为第四内存空间在本阶段实现（→ AC-10，R-8；按草稿 §1.3 条件路径执行：文档前置同步 + 只读 + 不进序无关比较子集）
- DEC-5: 独立无交互多 CTA 并行 launch（grid_dim 扩张提议）
  - Claude Position: 不纳入（严守单 CTA）。
  - Codex Position: N/A - open question（草稿明示待用户拍板）。
  - Tradeoff Summary: 纳入可早验 grid 几何 SR 值与独立 CTA 终态聚合，但引入 launch/snapshot 面扩张与 grid 级程序类边界论证；不纳入则验收聚焦单 CTA。
  - Decision Status: 用户裁定纳入（→ AC-11：nctaid 驱动、确定性逐 CTA 顺序执行、跨 CTA 通信属门外类 B-OI-5；cluster_dim>1 / DSMEM / 跨 CTA 通信仍留 FUT-4）
- DEC-6: `data_race` 运行时 trap 是否本阶段实现
  - Claude Position: 不 emit，降为可选 hook 留 FUT。
  - Codex Position: 倾向同方向（列为需用户确认项）。
  - Tradeoff Summary: emit 需 per-byte 影子内存 + epoch 时钟（重设施）；不 emit 则一般 race 仅靠测试侧分流捕获。
  - Decision Status: 用户裁定本阶段实现（→ AC-12；R-9 soundness 约束：以 `race_check=False` 默认关的 opt-in 形态落地——默认开启会违反草稿 §4.1『ISS 不可把 race 当 trap』的保真修正并打死 D 档 corpus；`data_race` 进必需 reason 集与反向校验）
- DEC-7: global 越界判别力与 per-space 容量默认值
  - Claude Position: global 稀疏无界不 trap（bounds 元数据留 FUT）；容量默认 49152/16384、launch 可配。
  - Codex Position: 要求显式确定容量默认值；global bounds 列为需确认。
  - Tradeoff Summary: global 加 bounds 需 launch 声明 allocation 表；容量默认值过小误 trap、过大丧失判别力（launch 可覆盖）。
  - Decision Status: 用户裁定 global 加 bounds 检查（→ AC-4/AC-5.3：launch `global_allocations` 区间表、越界 `global_oob` trap、未声明回退稀疏无界保持向后兼容）；shared/local 容量默认值采纳计划建议 49152/16384（用户未另指数值）
- DEC-8: `.256` 向量宽度
  - Claude Position: 留接缝不实现。
  - Codex Position: 同意延后。
  - Tradeoff Summary: 北极星 `.128` 足够；`.256` 为 Blackwell 常用宽度；寻址契约同构、仅寄存器组宽度不同。
  - Decision Status: 用户裁定本阶段纳入（→ AC-2.1/AC-4：宽度集扩至 `.256`，8-寄存器组、8-对齐编码期拒绝、32-byte 地址对齐运行期 trap、CF-4 扩展覆盖）
- DEC-10: `native.cpp` 源码组织形态
  - Claude Position: 拆分留在实现自由度内（Allowed Choices 允许、不强制）。
  - Codex Position: N/A - open question（首轮曾指出 `NativeWarp::step` 单体需先拆函数，未就文件组织表态）。
  - Tradeoff Summary: 单文件继续膨胀（现 1319 行 + 本阶段约七成改动落于此）则审查与回归定位成本上升；强制拆分则需 CMake 同步与拆分期回归纪律。
  - Decision Status: 用户裁定拆分为绑定要求（→ AC-13/R-10：最小拆分集 memory_space / CtaBarrier / block_state·warp_state / race shadow，pybind 单模块不变，随 Milestone 增量拆分、行为零变更）

### 沿草稿裁定（Claude/Codex 一致，无需用户介入）

- DEC-2: mbarrier 阶段归属
  - Claude Position: 阶段③ 不建模、不加 schema（DEC-MS-MB-1 最小读法）→ FUT-2。
  - Codex Position: 同意（mbarrier/async 归 FUT）。
  - Tradeoff Summary: 纳入需同步改 plan-foundation FUT-2 措辞且绑定 async-copy 数据通路；不纳入则 async producer-consumer 在阶段③ 不可表达。
  - Decision Status: 已裁定（沿草稿：不建模，留接缝）
- DEC-3: float atomic 阶段归属
  - Claude Position: 仅 spec 接缝声明 + 不可编码（DEC-MS-FATOM-1 + R-6）→ FUT-1。
  - Codex Position: 同意（float atomics → FUT）。
  - Tradeoff Summary: 阶段③ 无数值 oracle，提前实现无法 bit 验证。
  - Decision Status: 已裁定（沿草稿）
- DEC-4: cluster / 多 CTA 延后
  - Claude Position: dim=1 恒等薄壳 + rank≠self trap（DEC-MS-MW-4）→ FUT-4。
  - Codex Position: 同意。
  - Tradeoff Summary: 已由草稿以北极星依据裁定（FA-3 仅 ~2% 可选优化）。
  - Decision Status: 已裁定（沿草稿）
- DEC-9: BAR.RED 归属
  - Claude Position: 不进本阶段 Lower Bound（草稿 §6.2 已降级：cross-warp reduction 验收用 `BAR.SYNC` + 显式 shared 归约表达）→ FUT-8。
  - Codex Position: 同意（BAR.RED 列为 future/stretch）。
  - Tradeoff Summary: 实现则需谓词归约通路与比较子集裁定；不实现无验收缺口。
  - Decision Status: 已裁定（沿草稿降级，上界可选）

## Implementation Notes

### Code Style Requirements
- Implementation code and comments must NOT contain plan-specific terminology such as "AC-", "Milestone", "Step", "Phase", or similar workflow markers
- These terms are for plan documentation only, not for the resulting codebase
- Use descriptive, domain-appropriate naming in code instead
- 代码与注释一律英文；注释说明意图、约束与边界，不写开发过程叙述
- 运行时字符串（trap kind/reason、snapshot 键、调度序名）按 spec-iss.md 契约命名（如 `barrier_deadlock`、`cta_barriers`、`warp_round_robin`），与计划编号体系无关
- spec 与计划以符号/路径定位代码，不用行号
- 每个 Milestone 收口运行：原生模块重建 + `PYTHONPATH=isa:iss python -m pytest tests -q` + `python tools/check_structure.py` 全绿


--- Original Design Draft Start ---

# 阶段③ 内存与同步 — 调研要点（设计输入）

curryGPU 功能级 ISS 阶段③『内存与同步』的实现调研，收敛为可执行的实现契约。

> 本文是功能级 ISS 第③阶段（= `plan-foundation.md` FUT-2）内存与同步的实现调研，承接 `research-notes.md` §2（内存模型：per-space `memory_space_impl<4KB 块>`、`load<T>/store<T>` 包成 gather/scatter、atomic 单线程序列化）与 §4（`cluster_state → block_state → warp_state` 数据结构蓝图），向 `plan-memsync.md` 输出可执行实现契约。中文正文、技术术语 / 助记符 / 文件路径 / 标识符保留 English；量化结论附适用条件；与既定决策冲突处显式标注，被对抗评审驳回的设计选择反映其修正、不掩盖。本文只写方案、范围、依据与决策记录，**不写实现代码、不是 plan**。

---

## 1. 范围与目标

阶段③ = 把 foundation 留空的内存空间填入字节 + load/store 语义，引入 atomic、CTA named barrier，并把阶段② 的 single-warp 引擎升级为 multi-warp / CTA 执行模型，使序无关性主门从『warp 内组选择序无关』扩展到『warp 调度序无关』。

权威 scope 锚定 `plan-foundation.md` FUT-2（line 137，逐字）：

> `memory_space` 抽象基类 + `memory_space_impl`（4KB 块稀疏 hashmap）**覆盖 global/shared/local**、CTA barrier、atomics（单线程序列化 RMW）、以及 ordering/barrier 的负向误用测试。

交接接缝 = foundation AC-6（line 139）：内存空间在状态契约中**存在但为空**、`mem-ops==0`；阶段③ 为其填入字节与 load/store 语义、填充 `mem_ops` 计数、引入字节级内存 diff。

### 1.1 IN scope（阶段③ 实体交付）

- `memory_space` 抽象基类 + `memory_space_impl`（4KB 块稀疏 hashmap），覆盖 **global / shared / local** 三空间（**只此三空间**，见 §1.3 const 裁定）。
- general memory **load/store**（`LD/ST/LDG/STG/LDS/STS/LDL/STL`），32-地址 gather/scatter、byte-masked 写、sub-word extend-to-32、64/128-bit 寄存器对、packed word 搬运。
- **整数 atomic / RED**（ATOM/ATOMG/ATOMS/RED/REDG/REDS）单线程序列化、不可分 RMW（见 §3）。
- **CTA named barrier**（`BAR.SYNC`/`BAR.ARV`）到达计数状态机（见 §6），用 `block_state.barriers`。
- **multi-warp / CTA 执行模型**（`block_state` 持 `vector<warp_state>` + 共享 shared、warp 间确定调度序，见 §5）。
- **fence / MEMBAR** 解码 scope/order 操作数但功能 no-op（见 §4）。
- ordering / barrier 的**负向误用测试**（竞争 = UB 须可观测、确定、不 hang/silent-wrong）。
- 阶段③ **前置依赖**：S2R 的 special register 扩展（SR_TID/NTID/CTAID/NCTAID/WARPID/NWARPID）与 `launch(...)` 入参扩展，是 multi-warp 寻址与 CTA barrier 缺省 expected_count 的硬前提（见 §8.3）。

### 1.2 OUT scope（明确不在阶段③）

- **阶段④（FUT-3）**：tensor-mem 字节填充与 MMA 语义、leader-issue SINGLETON、FDA(F=25)、MX/NVFP4 block-scale、correctly-rounded `MUFU.EX2/LG2`、MPFR/fp64 精确参考 oracle 与 conformance 门。`block_state.tensor_memory` **空间字段须存在**（蓝图占位），但**不填字节、不进 snapshot**（见 §8.4）。**float atomic 的数值结果与 Tier-1/Tier-2 验证整体随阶段④ 数值循环交付**（见 §3.5）。
- **mbarrier / async transaction barrier**：`plan-foundation.md` FUT-2 **未点名** mbarrier，且 async-copy/TMA 数据通路属阶段④。阶段③ 只保证 mbarrier/async 指令**可解码且功能 no-op**，不实现 tx-count 握手、不实现 eager memcpy + complete_tx、不新增 mbarrier trap reason、不进 snapshot（见 §7）。
- **cluster / 多 CTA / DSMEM 远端语义**：`cluster_dim` 默认 1 恒等薄壳；跨-CTA 访问（rank≠self）走干净 `unsupported` trap，绝不伪造远端态 / 别名 self。真·多 CTA / grid 延后（见 §5.6）。
- **形式化内存一致性 / litmus**：relaxed/acquire/release 的可观测重排、weak-memory litmus outcome（message-passing / store-buffering / IRIW）留**独立 litmus 工具**（FUT-5）。功能 oracle 不枚举合法 outcome、不证明一致性模型（见 §4）。
- **timing**：coalescing、bank-conflict、L2 顺序、cache 命中、ordered-atomic 的 CAM/ordering-number/retry —— 一律不进功能态、不进 snapshot。

### 1.3 const memory 的 scope 裁定（修正 VERIFY [high]）

子主题调研一度把 const 作为第四个内存空间纳入 snapshot（`memory` 从 3 键扩为 4 键）。**这越过了 FUT-2 的冻结 scope-line**：`plan-foundation.md` FUT-2 与 `spec.md` §2/§5③ 均枚举恰好『global/shared/local』三空间；唯一含 const 的是 `research-notes.md` §1（line 13）的非规范散列式枚举。

**裁定（DEC-MS-SCOPE-1）**：阶段③ 的 `memory_space` 交付**只做 global/shared/local 三空间**。const（只读、`LDC`、bank-indexed）**不纳入阶段③ 内存空间实体、不进 snapshot `memory` 子树、不进 `ARCH_STATE_KEYS`**。const memory 的只读语义、`__constant__` 注入入口、`LDC` 解码作为**接缝预留**，与 const 数据所属阶段（建议与 kernel 参数 / Transformer 集成同期）一并实现。

- 依据：FUT-2 line 137（三空间）、`spec.md` §2 line 12 / §5③ line 56（三空间）。
- 收益：阶段③ 首步不触发 const-key 引起的 ARCH_STATE_KEYS / `test_native.py` 基线变更，验收聚焦 FUT-2 字面的 `shared-mem reduction / barrier` 类。
- 若后续确需 const：先在 `spec.md` §2/§5 与 `research-notes.md` §4 蓝图显式补 const 为只读空间并重述 FUT-2 scope-line，再落地；即便纳入，const 只读不变、snapshot 可吐但**不进** `ARCH_STATE_KEYS` 序无关比较（对序无关门无判别力）。

---

## 2. 内存空间模型与寻址

### 2.1 `memory_space` 抽象基类 + `memory_space_impl`（承接 M1/M2）

承接 `research-notes.md` §2/§10 与 §4 蓝图。设计契约：

```
class memory_space {                       // 抽象基类:空间无关的字节寻址 + gather/scatter
  virtual byte read_byte (uint64 addr) const = 0;
  virtual void write_byte(uint64 addr, byte v) = 0;
  template<class T> T    load (uint64 addr) const;   // 读 sizeof(T) 字节, 小端拼 T
  template<class T> void store(uint64 addr, T v);    // 写 sizeof(T) 字节, 小端拆 T
  void gather (const uint64 addr[32], const bool active[32],
               int width, bool sign_ext, uint32 out[32]) const;     // 每活跃 lane 取 width 字节, extend-to-32
  void scatter(const uint64 addr[32], const bool active[32],
               int width, const uint32 in[32], const uint8 byte_en[32]);  // byte-masked 写
};

class memory_space_impl : public memory_space {   // 稀疏 4KB 块 hashmap
  static constexpr uint64 BLOCK = 4096;
  std::unordered_map<uint64 /*block_id = addr>>12*/, std::array<byte,4096>> blocks_;
};
```

外部背书（GROUND D 已本机/外部核查）：GPGPU-Sim `cuda-sim/memory.h` 的 `memory_space_impl<BSIZE>`（`#define MEM_BLOCK_SIZE (4*1024)`、`hash_map<page_idx, mem_storage<BSIZE>>`、字节粒度 `read/write(addr,length,data)`）与本设计逐字同构 —— 这是 M2 的可核查外部背书。curryGPU **去掉** GPGPU-Sim `write` 的 `ptx_thread_info*`/`ptx_instruction*` 参数（纯功能、无 per-instruction 边界），并做 **per-space 实例化**（GPGPU-Sim 是单一 global + per-CTA shared，curryGPU 的 local 还要 per-lane[32]）。

不变式（编号供 spec 引用）：

- **MS-1（稀疏惰性 + 零初始化）**：块仅在首次写入时分配（`write_byte` 惰性 `blocks_[id]`）；读未分配块返回**全 0**。这是 ISS 的确定化选择（真 HW 是 UB），与 Lustig litmus『程序开始全内存初始化为 0』约定天然一致。snapshot 只序列化**已分配且非全零**的块，保证『写后清零 ↔ 从未写』snapshot 等价（序无关门可比）。
- **MS-2（空间无关核）**：`memory_space_impl` 不知道自己是 global/shared/local；空间语义（归属、大小窗、可见性）由**持有方**与**指令选择**决定。基类只提供『字节寻址的稀疏存储 + 小端拼装 + gather/scatter』。
- **MS-3（小端唯一）**：`load<T>/store<T>` 一律小端，对齐 GPR 寄存器对 / packed 小端约定（M6/M7），全模型零大端分支。
- **MS-4（byte-masked 写原子性）**：`scatter` 的 `byte_en[lane]` 是 per-lane per-byte 写使能（sub-word store `byte_en=0x1/0x3`、full-word `0xF`、`.128` 4 次 word 调用）。`active[lane]==false`（含 guard-false）**完全压制**：不分配块、不改字节、不计 mem_op。

### 2.2 gather/scatter 是 SIMT load/store 的唯一入口

一条 `LDG.E R_d, [R_a]` 在 ISS 中**不是 32 次独立 `load<T>`**，而是一次 `gather`：32 个 per-lane 地址（从 `R_a` lane 值 + UR + imm 算出）→ 一次批量取数 → 各 lane `extend-to-32` 写 `R_d`。这把『decode-once + masked-lane-loop』总则贯彻到访存。对照参考系统：GPGPU-Sim 功能模式按 thread 逐个 `mem->write`（无 coalesce），curryGPU 取其稀疏块但用统一 gather/scatter 接口（功能上无关，仅 mem_op 计数粒度与日后 trace 挂接点）。

### 2.3 Per-space 归属

承接 `research-notes.md` §4 蓝图，三空间归属对齐 NVIDIA 窗口语义（`nv_patent/sm/load_store_unit.md` US8271763B2）：

| Space | 实例归属 | 持有者 | 寻址来源 | 可见性 | snapshot key |
|---|---|---|---|---|---|
| **global** | 单全局实例（cluster/grid 级） | `cluster_state` 或全局单例 | 64-bit byte addr `[R.U32+UR+imm]`, `.E` | 全 lane / 全 CTA | `global` |
| **shared** | per-CTA 一份 | `block_state.shared_memory` | 32-bit CTA-相对 offset `[R+UR+imm]` | 同 CTA 内全 warp/lane | `shared` |
| **local** | per-lane private, 32 份/warp | `warp_state.local_mem[32]` | 32-bit thread-相对 offset `[R+UR+imm]` | 仅本 lane | `local`（按 lane 分组） |
| *(tensor-mem)* | per-CTA, 阶段④ 填字节 | `block_state.tensor_memory` | TMEM-addr | 阶段④ | *(不进 snapshot, §8.4)* |

归属不变式：

- **OWN-1（global 单例幂等）**：cluster_dim=1 下 global 是单一实例；跨-CTA 访问（rank≠self）走 `unsupported` trap、不别名（承接 S2）。
- **OWN-2（shared per-CTA 隔离）**：每个 `block_state` 持独立 `shared_memory`；两 CTA 写同一 offset **不互相可见**（除非 DSMEM remote-rank，本阶段不实现）。DSMEM 非新空间 = shared + remote-rank 选择子（承接 S1）：寻址时 remote-rank ≠self → trap，不路由到别的 `block_state.shared`。
- **OWN-3（local per-lane 私有）**：`local_mem[lane]` 各自独立 4KB-块 hashmap；lane i 的 local 写**永不**被 lane j 观察（这是 local 的定义性质，也使 local 天然序无关 —— 无跨 lane 通信）。外部印证：Ventus 的 private（per-thread）内存即此语义。

> **关键澄清**：shared 与 local 物理上都『按 thread/CTA 局部小』，但**语义相反**：shared = CTA 内**共享**（跨 lane 通信媒介，是阶段③ barrier/reduction 的载体），local = lane **私有**（无通信）。二者都用 `memory_space_impl`，仅归属与可见性不同（MS-2）。curryGPU **拒绝** Vortex 式单一扁平地址空间（同一数值地址在不同 space 指不同字节，扁平模型会丢失这一语义）。

### 2.4 Generic / 统一地址空间与窗口推断

NVIDIA 统一地址空间**不是平面**而是**带窗口**（`nv_patent/sm/mmu.md` US8271763B2 Fig.1/4）：generic 64-bit 地址里特定高位区间编码为 `Local Window`/`Shared Window`，落入窗口者映射到 Per-Thread Local / Per-CTA Shared，窗口外 fall-through 到 Global。这正是 PTX 的 generic pointer + `cvta`/`isspacep`。

curryGPU 取确定性窗口推断（functional, 无 timing）：

```
generic LD/ST 的空间选择:
  if  SHARED_WIN_BASE <= addr < SHARED_WIN_BASE + SHARED_WIN_SIZE:  → shared, off = addr - SHARED_WIN_BASE
  elif LOCAL_WIN_BASE <= addr < LOCAL_WIN_BASE + LOCAL_WIN_SIZE:    → local,  off = addr - LOCAL_WIN_BASE
  else:                                                            → global, off = addr (完整 64-bit)
```

指令 → 空间选择（寻址契约 ADDR，sm100a 实测形态）：

| 指令族 | 空间 | 寻址形态 | 推断方式 |
|---|---|---|---|
| `LDG/STG` | global（直达） | `[R.U32+UR+imm]`, `.E` 64-bit | decode 期锚定，**不推断** |
| `LDS/STS` | shared（直达） | `[R+UR+imm]` 32-bit offset | decode 期锚定 |
| `LDL/STL` | local（直达） | `[R+UR+imm]` 32-bit offset | decode 期锚定，隐含 per-lane |
| `LD/ST`（generic） | global/shared/local | `[R.U32+UR+imm]`, `.E` | **运行期窗口推断** |
| `CVTA` | 地址转换 | generic↔space-specific | 纯寄存器算术（加/减窗口基址） |

寻址不变式：

- **ADDR-1（直达优先）**：`LDG/STG/LDS/STS/LDL/STL` 的空间在 decode 时即确定（opcode/modifier 锚定），**不做运行期推断**，直接路由到对应 `memory_space_impl`。
- **ADDR-2（generic 推断纯函数）**：仅 `LD/ST`（无空间后缀）做窗口推断；推断结果是地址值的纯函数 → 确定、序无关。窗口用半开区间 `[base, base+size)`，边界归属确定（避免 off-by-one）。
- **ADDR-3（cvta 是纯地址算术）**：`CVTA.TO.GLOBAL`/`CVTA.GLOBAL` 是 generic↔space-specific 的纯地址加减（加/减窗口基址），**不访存、零开销**（对齐 bit-reinterpret 原样转译总则）；`isspacep` = 窗口范围判断写 Pd。
- **ADDR-4（local 窗口 per-lane 别名）**：generic 推断到 local 窗口时，**同一 offset 在不同 lane 映射到不同 `local_mem[lane]`**（per-lane 别名）。这是 local 窗口与 shared 窗口的本质区别（shared 同 offset 跨 lane 是同一字节）。
- **ADDR-5（窗口基址私有化）**：`SHARED_WIN_BASE/LOCAL_WIN_BASE` 等具体数值属 **layout 私有**（对齐『编码私有』红线），schema 只声明『存在窗口推断』，样例 layout 给可 build/CI 的占位窗口值。

### 2.5 64/128-bit 地址承载

global/generic 地址是 64-bit，承载在**偶对齐寄存器对** `R(2n):R(2n+1)` 小端（承接 M6）：low word = `R(2n)`、high word = `R(2n+1)`。shared/local offset 是 32-bit，单寄存器即可。

---

## 3. Atomic / RED 语义与定序

承接 `research-notes.md` §2 决策 M3（atomic 单线程序列化、不可分）。功能 oracle 的 atomic 价值**不在并发可见性时序**，而在『在确定的串行化顺序下，RMW 不可分地落到内存，产出确定的最终内存态与确定的旧值返回』。真实硬件的乱序/重试（`nv_patent/cache_coherence/ordered_atomics.md` US11016802B2 的 L2 slice + CAM + ordering number）是 timing 关注，功能层**拒绝建模其机制**，但其语义后果（同地址原子按确定顺序落地）由单线程串行 RMW 天然保证。

### 3.1 ATOM vs RED 与 op 集合

- **ATM-1（ATOM/RED 区分）**：`ATOM` = RMW + 返回旧值写 `Rd`（`Rd=RZ` 时仍 RMW 但丢弃旧值）；`RED` = RMW + **无返回**（fire-and-forget，不读旧值、不写任何 lane 寄存器）。二者共享同一 RMW 内核与同一定序律。依据：sm100a ATOMG/REDG 签名 + BR100 `procEuWred`。

- **ATM-2（op 集合，以 sm100a 实测为准 —— 修正 VERIFY [high]）**：整数 op 集 = **{ADD, MIN, MAX, INC, DEC, AND, OR, XOR, EXCH, CAS}**（10 个），浮点 op 子集 = **{ADD, MIN, MAX, EXCH, CAS}**。
  - 依据：sm100a `ATOM/ATOMG` Modifier Group 整数 op 枚举含 INC/DEC（北极星目标 ISA 的一等 modifier，**非可选**）。`cmodel_br100` `LSCCache.cpp:800` `doAtomic` 八路 switch `{ATADD,ATMIN,ATMAX,ATAND,ATOR,ATXOR,ATSWP,ATCAS}` 仅是 BR100 实现裁剪（无 INC/DEC），不能据此削减目标语义。
  - op 语义：`add=old+v`、`min/max`（有/无符号区分）、`and/or/xor`、`exch=v`（无视 old）、`cas=(old==cmp)?val:old`（三操作数）、`inc=(old>=v)?0:old+1`（CUDA atomicInc，仅无符号）、`dec=((old==0)||(old>v))?v:old-1`（CUDA atomicDec，仅无符号）。

- **ATM-3（op 交换性分类，决定序无关性）**：
  - **交换结合类** `{add(整数), min, max, and, or, xor}`：多 lane 命中同址时内存最终态**与串行化顺序无关**（INV-3）。
  - **顺序敏感类** `{exch, cas, inc, dec, add(浮点)}`：内存最终态与/或 per-lane 旧值**依赖串行化顺序**，**必须**用固定 lane-id 升序定序（INV-1）。整数 add 在 mod 2ⁿ 下结合，浮点 add 在 IEEE 下不结合 → 浮点 add 从交换结合类**降级**为顺序敏感类。

- **ATM-4（memory space）**：atomic/RED 仅对 **global**（ATOMG/REDG）与 **shared**（ATOMS/REDS）合法。**local**（per-lane 私有，跨线程原子无语义）、**const**（只读）上的 atomic 一律 clean trap，不伪造行为。

### 3.2 单线程序列化 RMW 与对 V2 三相的受控豁免

GROUND V2 规定一步内源操作数从 pre-step 态读、per-lane RHS 入私有临时、再 commit；**无 lane 观察另一 lane 的同步写**。普通 ALU/load 严格遵守。**但 atomic 是唯一的受控例外**：atomic 的定义要求读到『此前同址 atomic 已 commit 的值』，否则同址多 lane lost-update（违反不可分 RMW）。

- **ATM-5（atomic 的相位特例 —— 须落 spec 正文，修正 VERIFY [medium]）**：atomic/RED 指令在一个 warp-step 内对同一 group 的 32 lane **不是并行 read-from-pre-step**，而是**在 commit 相位内，按固定 lane-id 升序，逐 lane 串行执行完整 RMW**；第 k 个 lane 的 `old` 读到『pre-step 内存态 + 前 k−1 个同址 lane 已落的 RMW』。这是对 V2 的**受控、文档化的局部豁免，仅限 atomic/RED 指令**；普通 load/store 不享此豁免。
  - **须在 spec（及 spec-iss 风格契约）的 V2/三相条目旁显式标注**：『atomic/RED 例外：其 32 lane 的 RMW 在 commit 相位按 pinned lane-id 升序串行，普通 load/store 不享此豁免』，使三相律与不可分 RMW 形式自洽。
  - **不破坏序无关主门的论证**：豁免只发生在单条 atomic 指令内部的 32 lane 之间，且内部顺序被**固定为 lane-id 升序**（不随 `SchedOrder`/`warp_sched_order` 变）。故对同一程序，无论外层调度序如何，atomic 产出的内存态与旧值仍 bit 一致（INV-3b）。

- **ATM-6（不可分性）**：`load → apply_op → store` 三步对单个 (lane, addr) 是原子的、不可被其他 lane 的 RMW 插入。功能 oracle 用『串行 for 循环』天然保证（无并发），不需锁。CAS 的『比较 + 条件写』是一个不可分单元，不存在『比较成功但写入被插入』的窗口。

### 3.3 定序不变式

> 直接复用 `spec-iss.md` line 65 已确立的『Collective 归约使用固定 lane-id 升序定义……为后续非交换/浮点 collective 留契约接缝』——把同一条定序原则从 warp 内 collective 延伸到 atomic/RED 对内存的 RMW，保持项目内**单一定序律**，强一致、零冲突。

- **INV-1（warp 内固定串行序 = lane-id 升序）**：同一 warp 的同一 PC group 内，命中任意地址的多个参与 lane，其 RMW 按 lane-id 升序 `0,1,…,31` 串行执行；此顺序**不依赖** `SchedOrder`（min_pc/max_pc/round_robin/oldest_group）与外层 warp 调度。
- **INV-2（顺序敏感 op 的内存态 = 串行序求值）**：对 `{exch, cas, inc, dec, add(浮点)}`，同址多 lane 的最终内存态 = 按 INV-1 顺序逐个 apply 的结果；per-lane 旧值 = 各自执行时读到的中间态。例：lane 1,5,9 同址 `exch` 各写 a,b,c → 最终内存 = c，old_1=mem₀, old_5=a, old_9=b。
- **INV-3（交换结合 op 的序无关性）**：对 `{add(整数), min, max, and, or, xor}`，同址多 lane 的**最终内存态与串行序无关**。
  - **INV-3a**：`RED`（无旧值）在交换结合 op 下，内存最终态与 per-lane 行为皆序无关 → 真正可序无关验证的强对象。
  - **INV-3b**：`ATOM`（有旧值）即使整数 add，其 per-lane 旧值仍序敏感；但因 INV-1 把顺序固定为 lane-id 升序（不随调度序变），旧值在同一程序不同调度序下**仍 bit 一致** → 序无关主门（比较 `vgpr`）仍成立。这是『固定串行序』相对『任意合法序』的必要性所在，须写入 spec。
- **INV-4（地址互不相干 lane 完全独立）**：命中不同地址的 lane 之间 RMW 顺序不影响任何结果 → 退化为并行，bit-exact 等价于任意顺序。INV-1 仅在同址冲突时有可观测后果。
- **INV-5（跨 warp 定序与调度序）**：多 warp 命中同址时，warp 间 atomic 相对顺序由 **warp-step 调度顺序**决定（§5 的 `warp_sched_order`）。
  - 对**交换结合 op**：跨 warp 同址最终内存态**与 warp 调度序无关** → 这是阶段③『shared-mem reduction 验收』（`spec.md` §5③）的合法依据。
  - 对**顺序敏感 op**（exch/cas/inc/dec/float-add）：跨 warp 同址最终态**依赖 warp 调度序**。oracle 仍给**确定**结果（固定 warp 序），但**不声明**其序无关。
- **INV-5a（序无关门的 atomic 输入边界 —— 与 §5 P-OI、§9 corpus 分流统一）**：序无关 metamorphic 门**只纳入**：交换结合 op 的 RED + 整数 ATOM 全 op（靠 INV-1 固定序保旧值一致）。顺序敏感 op（exch/cas/inc/dec/float-add）跨 warp 归入**确定性差分基线**（固定单一序的回归基线），不归序无关门。
- **INV-6（counters 填充）**：`counters.mem_ops` 每条 atomic/RED 指令 `+= popcount(participants)`（承接 `native.cpp:47` 当前恒 0 的占位）；`mem_ops` **不进**序无关比较子集（counters 被 V3 排除），但 `memory` 内容进门。

### 3.4 atomic 与 fence/scope 的关系

atomic 的 scope/ordering 后缀（`.acquire/.release/.relaxed/.sc`、`.CTA/.GPU/.SYS`）**解码保留、功能忽略**：在确定性串行全可见模型下不产生功能态差异（见 §4）。atomic 的『coherence 公理』由单线程串行 RMW 满足。

### 3.5 float atomic 的阶段切分（修正 VERIFY [medium] / completeness）

float atomic（`ATOM.ADD.F32/.F16x2/.BF16x2`、float min/max）的 bit-exact 结果依赖 correctly-rounded fadd（含 fp16/bf16），而 correctly-rounded 数值是**阶段④ §9(a)** 的核心交付（IEEE-exact add/mul/fma 0 ULP vs MPFR + MUFU.EX2/LG2）。阶段③ 本身**无数值 oracle**（`spec.md` §5、`research-notes.md` §9：阶段③ 无数值 oracle，留阶段④）。

**裁定（DEC-MS-FATOM-1）**：
- 阶段③ **只落地并验证整数 atomic**（ADD/MIN/MAX/INC/DEC/AND/OR/XOR/EXCH/CAS）的串行化 RMW + 序无关门（整数确定、不依赖数值 oracle）。
- float atomic 在阶段③ **仅留语义接缝声明**：『float atomic 走逐次独立 IEEE RNE fadd（每次各自规格化 + 舍入，**不**折叠为 FDA fused 累加 —— FDA 仅属张量 MMA 域，与 SIMT atomic 不混用），序由 INV-1 lane-id 升序确定化』。**不在阶段③ 跑 0-ULP 差分、不实现 fp16/bf16 fadd**。
- float atomic 的数值结果 + Tier-1（对 MPFR 0 ULP）/ Tier-2（`atol+rtol·|b|`，误差模型 Higham γ_N）整体**随阶段④ 数值循环交付**。
- **作用域注（修正 VERIFY [medium] / 决策 #2 张力）**：`research-notes.md` line 89『curryGPU 不需 pin reduction 顺序，任意顺序 bit-exact 相同』的措辞**仅指 MMA-FDA（定点求和）**，不含 float atomic/RED。建议在阶段④ 数值契约补一句作用域限定，防止误读为覆盖所有归约。FDA（顺序无关）与 float atomic（顺序相关但固定）作用于不同语义对象，无实际矛盾。

---

## 4. 内存一致性的功能层建模与 UB 边界

### 4.1 功能内存语义 = 确定性串行推进 = SC-for-DRF

**DEC-MS-MEM-1（功能内存语义）**：curryGPU 功能 oracle 对内存采用『确定性串行推进 + 显式同步点全可见』，等价于对 data-race-free（DRF）程序的 sequential consistency（SC-for-DRF）：

1. warp 内一条访存遵 read→compute→commit 三相（V2）：源地址 / 源数据从 pre-step 态读、per-lane 效果入私有临时、再统一 commit。
2. 跨 warp 按确定性全序**逐 group 指令级交错**串行推进（调度原子粒度 = 一个 `step_one_group`，量子 K=1，见 §5.4 INV-SCH-3/INV-SCH-5）：一个 `step_one_group` 的一条指令全部访存 commit 后，下一个被调度 `(warp, group)` 在其起点才看见前序写 → 每条访存对**后续 step** 立即全可见。**此粒度是 SC 语义的地基**，与 INV-SCH-3 逐字一致；**不是 per-warp run-to-completion**（旧表述『一个 warp 全部访存 commit 后下一个被调度单元才执行』已废除：它与 INV-SCH-3 per-group 原子矛盾，且 spin-loop 在 per-warp run-to-completion 下结构性死锁）。
3. **显式同步点**（CTA barrier、收敛屏障 `BSYNC`、atomic）处所有先序写**已可见**（串行推进的自然推论，无需额外 fence 动作）。
4. 所有 scope/ordering/fence modifier **解码保留、功能忽略**。

**为何是 SC-for-DRF 而非 relaxed**：functional bit-exact oracle 必须给单一确定结果，与 relaxed 的非确定性（一个程序多个合法结果）**数学互斥**。建模 relaxed 重排会破坏序无关 bit-exact 门。对 DRF 程序，SC-for-DRF 保证『表现得像 SC』，与真硬件对 DRF 程序的保证 bit-exact 等价。

**保真口径（soundness / completeness 划分，承接保真契约 + §4.2 INV-FENCE-SOUND-1）**：curryGPU 在内存维度的 sound 含义 = ISS 对每个固定调度序产出**一条 sequentially-consistent 执行**，且该执行属于 PTX weak model 对该程序的合法执行集（SC 是 weak 允许的 outcome 之一，PTX §8 弱模型约束候选写集、SC 全序是其上的一个合法选择）。completeness 仅对 **DRF 子集**承诺（用足够 scope 的 strong atomic/fence 把所有冲突变 morally strong，PTX §8.10.5 得 strict SC，则 ISS 唯一 SC 执行 = NV 合法执行 bit-exact）；对 racy 程序 PTX 允许的合法 outcome 是一个集合（racy-but-defined-weak），ISS 只产出其中 SC 一个、不枚举其余（weak memory 重排留独立 litmus 工具）。**关键修正**：PTX 不强制 data-race freedom（Lustig et al. ASPLOS 2019 verbatim: "PTX does not require data race freedom"），racy 程序**非全局 UB**，故 ISS **不可把 race 当 trap**（否则会拒绝合法的 spin-lock / relaxed-atomic flag 程序）；ISS 把 racy 跨-warp 程序排除出**序无关主门**，仅因其结果序相关（保护门可证性），不是因为它是 UB——此排除与 CUDA C++ 层 race=UB 约定方向一致，但根因是『结果序相关』而非『未定义』。

**与 PTX 形式模型的对齐（修正 VERIFY [medium] / claims）**：需区分两层 ——
- **PTX ISA §8（Lustig et al. ASPLOS 2019，Scoped-RC11）对数据竞争给出的是 weakly-ordered（弱定序），不是 undefined**。
- **CUDA C++ 层（CUDA Programming Guide）才把 race 定为 UB**。

因此本文表述为：curryGPU 把 racy 程序排除出序无关门（因其结果序相关），**这与 CUDA C++ 的 race=UB 约定一致**，而非声称 PTX ISA 把 race 定为 UB。对 DRF 程序，curryGPU 满足 PTX 公理集的净效果（coherence / SC-per-location / causality / fence-SC / atomicity / no-thin-air，其中 causality 被串行全可见超额满足）—— 此 6-公理枚举**据 Lustig ASPLOS'19 / Scoped-RC11 重构，逐字公理名以论文正文为准**，不作为已核查 verbatim。SC-for-DRF 结论本身是标准结果、成立。

### 4.2 fence / MEMBAR：功能 no-op（解码操作数）

**DEC-MS-FENCE-1**：`MEMBAR.*`/`FENCE.*` 在功能 oracle 中**不改变任何架构态**；解码器**必须**提取 scope（`.CTA/.SM/.GPU/.SYS`，sm100a MEMBAR Modifier Group 2 四级）与 order 操作数（`.SC/.ALL/.VIEW.ASYNC` 等）以保持编码闭环与未来 timing 接口，但 handler 体为空（等价 NOP，仅推进 PC、消费 active mask）。

soundness 完整论证链（四环，引 INV-FENCE-SOUND-1）：
1. **(a) 指令级交错 + 每 step commit 即可见（SC）**：调度原子粒度 = 指令级交错（§5.4 INV-SCH-5，量子 K=1），每个 `step_one_group` 后写即时全局可见 → ISS 对每个固定调度序产出一条 sequentially-consistent 执行。这是整条链的粒度前提（旧表述『串行推进下无可重排窗口』方向对但不完整，须落到 per-group 指令级交错 + commit 即可见）。
2. **(b) DRF ⟹ SC（PTX §8.10.5）**：PTX §8.10.5 证明两两 morally-strong 重叠操作严格 sequentially-consistent；DRF 程序（用足够 scope 的 strong atomic/fence 把所有冲突变 morally strong，scope 须取到覆盖所有通信线程的级别——`.cta`/`.gpu`/`.sys`，由通信参与集决定）因此得 SC。
3. **(c) fence 唯一架构效果是 ordering，在 SC oracle 里被蕴含（PTX §9.7.12.4）**：fence/membar 唯一架构效果是 establish ordering（PTX 9.7.12.4 verbatim: "The fence instruction establishes an ordering between memory accesses requested by this thread"；"fence.sc is a slower fence that can restore sequential consistency"；"On sm_70 and higher membar is a synonym for fence.sc"），不搬数据、不改最终值；在已全局 totally-ordered 的 SC oracle 里任何 ordering fence 被 SC 蕴含 → 功能 no-op。
4. **(d) 关键 soundness：删 / 插 fence 不改 ISS 的 SC 执行**：ISS 的 SC 执行属于 PTX weak model 对该程序的合法执行集（§4.1 保真口径）。fence 只在 weak 重排窗口起作用，SC 全序无重排自由度，故删 / 插 fence 对**每个固定调度序下** ISS 的 SC 执行不产生任何变化 → no-op 对每序 sound，进而**全程序 sound**（含 racy 程序：见下方 completeness 补充）。
- **与已定 no-op 哲学同类**：`research-notes.md` §2『21-bit 控制段 no-op』、`spec.md` §3『async-copy/TMA eager』、S5『MMA.FENCE/COMMIT/WAIT no-op』；建模 fence 可见性会引入非确定性、破坏主门（见 §4.1）。
- **completeness 仅 DRF + 顺序敏感 atomic 的 soundness 厘清**：对 DRF 程序，ISS 唯一 SC 执行 = NV 合法执行 bit-exact。对顺序敏感 atomic（`exch/cas/inc/dec/float-add`）跨 warp，ISS **每个固定调度序产出一个 SC 执行**（非单一执行而是每序一个，见 §3 INV-5/INV-5a），该执行的每个 racy 读取值落在 PTX 候选写集合内 → 每序的 SC 执行 ∈ weak 合法集；fence 的删 / 插不改变任一固定序下 ISS 的 SC 执行（SC 无重排自由度），故 no-op 对每序 sound、对该类程序全程序 sound。这消除与 §3 INV-5 的口径割裂感（顺序敏感 atomic 不破坏 fence no-op soundness，只是其 SC 执行随序变）。
- **fence no-op 对无同步 spin 终止性无帮助**：spin 读不到新值是 visibility-across-step 问题（由公平调度 + 每 step commit 即可见解决，§5.4 INV-SCH-1），**不是** weak 重排问题，故插 fence 既不改架构态也不改终止性。

**例外澄清（关键，修正 VERIFY 一致性）**：`MEMBAR`/`FENCE` 独立指令整条 no-op；**但 CTA barrier `BAR.SYNC` 的 execution 部分必须真实建模**（到达计数、到齐前阻塞，见 §6），其 **memory-fence 部分**（屏障前写对屏障后全 CTA 可见）在串行推进下**自动成立**（到达 barrier 时该 warp 全部写已 commit），不是『忽略』而是『无需额外动作即满足』。即 `__syncthreads` 的『同步』真做、『fence』白送。**不要**把整条 `BAR.SYNC` 当 no-op（那会破坏跨-warp 通信语义），只有独立的 `MEMBAR`/`FENCE` 才整条 no-op。

### 4.3 data race 定义与 UB 边界

- **INV-RACE-1（data race 定义）**：两条内存操作构成 data race，当且仅当 (a) 访问 overlapping 字节（**按字节判定**，非按 word；byte-masked 写到同一 word 的不相交字节 = 非 race）、(b) 至少一条是 write、(c) 不被功能 oracle 的确定性同步顺序排序（无贯穿二者的 happens-before：同 warp 内未被 `BSYNC`/`__syncwarp` 隔离、或跨 warp 未被 CTA barrier / atomic 隔离）。对齐 PTX ISA §8 与 CUDA Programming Guide。

UB 边界总表：

| 访问模式 | 隔离机制 | 裁定 | 序无关门 | 依据 |
|---|---|---|---|---|
| warp 内同字节，无 `BSYNC`/`__syncwarp` | 无 | **UB** | 不进比较 | CUDA ITS UB；research-notes §5 |
| warp 内同字节，经 `BSYNC`/`__syncwarp` 隔离 | 收敛屏障 | 良定义 | 进比较 | `__syncwarp` = 重聚 + fence |
| 跨 warp 同 shared/global 字节，无 barrier | 无 | **UB** | 不进比较 | PTX §8（weak）/ CUDA race=UB |
| 跨 warp 同字节，经 `BAR.SYNC` 隔离 | CTA barrier | 良定义 | 进比较 | barrier=execution+auto-fence |
| 跨 warp 同字节，全 atomic 访问 | atomic coherence | 良定义 | 进比较 | M3 串行 RMW |
| 任意，访问不相交字节 | 地址不交 | 良定义（非 race） | 进比较 | byte-masked 写 |
| atomic-vs-plain 同字节，跨 warp 未额外同步 | — | **UB** | 不进比较 | coherence 仅对同址 atomic 间成立 |
| 跨-CTA（cluster）远端访问 | — | **`unsupported` trap**（非 UB） | N/A | S2:cluster 延后干净 trap |

- **裁定理由（边界 B：跨 warp race=UB 而非『确定结果即良定义』）**：虽然串行推进**会**给 racy 跨-warp 程序一个确定结果，但该结果**依赖调度序**（哪个 warp 先 commit），违反序无关主门。定为 UB = 把它**排除出主门比较前提**，保护序无关门的可证性。误把它当良定义则序无关门对 racy 程序的不同序产出不同态 → 门**静默破裂**。

- **DEC-MS-RACE-1（racy 程序行为）**：功能 oracle **不在运行时强制检测一般 data race**（一般 race 检测需影子内存 + happens-before 时钟，开销大、不应由功能核承担）。对 racy 程序按 read→compute→commit + 串行推进**正常执行**，产出确定但不可移植、可能序相关的结果；**不得 hang、不得 silent-wrong-without-determinism**（承接 V7）。与 ITS 结构可判定 UB（trap）的区别：memory race 是全局时序可判定，故留**可选 hook**（§9）不 trap。

### 4.4 序无关性保证的程序类

- **INV-MEM-OI-1（multi-warp 序无关充分条件）**：功能 oracle 保证最终架构态（含 `memory` 子集）与调度序无关，当且仅当程序 properly-synchronized：(1) warp 内跨 lane 内存通信被收敛屏障隔离；(2) 跨 warp 共享访问被 CTA barrier 或 atomic 隔离，或访问不相交字节；(3) 程序终止且每序 fair；(4) collective 被重聚其完整 membermask 的同步支配（m-converged）。满足者 `memory` 最终态对所有调度序 bit-identical → 通过主门；racy 程序前提排除。
- **INV-MEM-OI-2（atomic pinned 序硬接缝）**：atomic/RED 跨 lane 归约**必须**用 INV-1 的固定 (PC, lane-id) 升序，否则非交换/浮点 atomic（如 `RED.FADD`）在不同调度序给不同 bit → 主门静默破裂。这是 atomic 语义与内存一致性的硬接缝。

---

## 5. Multi-warp / CTA 执行模型升级

### 5.1 现状可复用性

`NativeWarp`（`native.cpp`）是单实例、无全局可变状态（唯一全局 `g_boundary_calls` 是边界计数器）。multi-warp 复用判定：

| 现状构件 | 复用判定 |
|---|---|
| warp 内 per-PC grouping `build_groups()` / 四具名序 `select_group()` | **原样复用**（降为 warp 内层） |
| warp 内收敛屏障 FSM `Bx`（bssy/bsync/break + try_fire_barrier） | **原样复用**，语义不变 |
| `step(max_steps)` 三段式恢复循环 | **拆分**：warp 体抽为 `warp.step_one_group()`，CTA 层包外循环 |
| snapshot schema | **包裹**：现 schema = 单 warp 视图，CTA 层吐 `warps:[...]` + CTA 级字段 |
| pybind `launch/step/snapshot/state_diff` | **保持粗粒度**（只在 launch/step/inspect 跨界） |
| `lane_state` 用 `std::string`、`predicates` 用 `std::map<string,...>` | **热点**（multi-warp × 32 lane N× 放大）；建议转 enum/bitmask（属性能优化，与功能正交，须保 snapshot 字符串表示不变） |

关键结论：状态隔离已天然就绪（所有 warp 态都是实例成员），可直接把 `NativeWarp` 当 per-warp 单元、由上层 `block_state` 持 `vector<warp_state>`。

### 5.2 三层状态聚合（承接 §4 蓝图）

**DEC-MS-MW-1**：阶段③ 新建 block 层、把现 `NativeWarp` 私有态降为 warp 层；cluster 层是 dim=1 恒等薄壳。

```
cluster_state         (默认 cluster_dim=1 → 恒等)
  ├─ blocks: vector<block_state>            // 阶段③ size==1
  └─ resolve_shared(rank=self, offset)      // dim=1 恒等;rank≠self → unsupported trap

block_state           (CTA;阶段③ 新建)
  ├─ shared_memory: memory_space_impl       // per-CTA;多 warp 共享
  ├─ tensor_memory: memory_space_impl       // 空间须存在,填充属阶段④(不进 snapshot)
  ├─ barriers:      map<bar_id, CtaBarrier> // CTA barrier 到达计数(§6);区别于 warp 内 Bx
  └─ warps:         vector<warp_state>

warp_state            (= 现 NativeWarp 私有态,语义冻结)
  ├─ vgpr / ureg / pred / pc[32] / lane_state[32] / Bx[16] / blocked_on[32] / barrier_phase[16]
  └─ local_mem: memory_space_impl[32]       // per-lane private
```

聚合不变式：

- **INV-AGG-1（warp 态语义冻结）**：warp_state 内所有阶段② 已定字段（`pc[32]`、`lane_state` 四态、`Bx` 3 字段 snapshot、`blocked_on`、`barrier_phase`）的语义、字段数、取值域**不变**；multi-warp 升级**只在外层包一层 warps 数组**，不得改 warp 内任何 transition table（承接 `spec-iss.md` Barrier State + `plan-its.md` line 115/116 冻结）。
- **INV-AGG-2（shared 由 CTA 持有）**：`shared_memory` 是 `block_state` 字段，同 CTA warp 经 `block_state` 引用同一实例；warp 不得各持一份 shared。`local_mem` 仍 per-lane。
- **INV-AGG-3（per-warp 隔离 + 唯一共享接缝）**：warp 之间**仅**经三类接缝交互 —— CTA barrier、atomic（对 shared/global 的 RMW）、shared-mem load/store；除此 warp 态完全私有。这是序无关性证明的结构前提。
- **INV-AGG-4（snapshot 向后兼容）**：阶段② 的 9-key 子集在 multi-warp 下成为每个 warp 的视图。CTA snapshot = `{warps:[per-warp-9-key…], cta_barriers, memory:{global,shared,local}, trap, counters}`。**单 warp、单 CTA 退化形态必须与现 snapshot bit 等价**（回归门 VH-1）。

### 5.3 warp_id 与寻址接缝

- warp 在 `block_state.warps` 中的下标 = `warp_id`（0-based，确定性来源）。`tid = warp_id*32 + lane`（线性 CTA 线程 id）。
- `SR_WARPID`/`SR_NWARPID` 读 `warp_id` 与 `warps.size()`；`SR_TID.*`/`SR_NTID.*`/`SR_CTAID.*`/`SR_NCTAID.*` 由 tid + CTA/grid 维度（launch 入参）推出。详见 §8.3（这是阶段③ 的显式前置依赖，须落定，不可默认 SR 已存在）。

### 5.4 两层调度：复用 ITS 调度器 + CTA 层加 warp 选择

把『每步选一个 PC-group 推进』嵌套进『每步选一个 warp 推进』：

```
cta.step(max_steps):
  while not cta_done() and trap.kind == "none":
    if issued >= max_steps: set_trap("max_steps",...); break
    runnable = [w for w in warps if w.has_runnable_group()]
    if runnable empty:
      progressed  = try_fire_cta_barriers()      # CTA barrier 到达即释放被阻 warp
      progressed |= each_warp.try_fire_bx()       # warp 内收敛屏障
      progressed |= each_warp.promote_yielded()
      runnable = recompute()
      if runnable empty:
        if any_warp_blocked_or_cta_blocked(): set_trap("synchronization","barrier_deadlock",...)
        break
    w = select_warp(runnable, warp_sched_order)   # ← 新增 CTA 层 warp 选择(确定序)
    w.step_one_group()                            # ← 复用阶段② warp 内 per-PC grouping
    ++issued
  return cta_snapshot()
```

- **DEC-MS-MW-2（warp 具名确定序，修订：默认改 `warp_round_robin`）**：引入 ≥3 个对 runnable warp 的具名确定全序：`warp_round_robin`（**默认，fair**）/ `warp_min_id_first` / `warp_max_id_first`（对齐 `plan-foundation.md` FUT-1 line 133『≥3 具名确定序』方法学）。`warp_sched_order`（CTA 层）与 `SchedOrder`（warp 内）是**两个正交自由参数**。逐序 fairness 论证：
  - `warp_round_robin` 是 **fair permutation**（游标轮转永不无限跳过任一 runnable warp，对应 GPGPU-Sim LRR equal-progress 工程惯例）→ 默认序满足 spin/lock 类程序的前向进度。
  - `warp_min_id_first` / `warp_max_id_first` 是**固定优先级序**：对 DRF / 终止程序，所有序产出同一终态（终态序无关）→ 三序均进 A/B 档主门；但对 spin / lock / producer-consumer，固定优先级可 starve flag 写者（若低优先 warp 持 flag 写、高优先 warp spin，则高优先永远先跑而 spin 不让出）。
  - **裁定**：三序均纳入主门用于 barrier-DRF / 交换 atomic 程序（A/B 档）；spin / mutex / producer-consumer 的前向进度**只用 fair 序 `warp_round_robin`** 在独立进度测试（VH-14）断言终止，不用固定优先级序断言终止。
- **INV-SCH-1（修订：全序 + unconditional weak fairness）**：每个 `warp_sched_order` 对 runnable warp 给出确定唯一选择，且是 runnable 集合上的 **fair permutation**——持续 runnable 的 `(warp, group)` 单元必在有限步内被推进至少一条指令，两次轮到同一 runnable 单元的间隔有界（不超过其余 runnable 单元各推进 O(1) 次）。**显式标注**：ISS 采用 **unconditional weak fairness**，强于 NV 在 Volta+ 承诺的 **conditional parallel-forward-progress**（ISO C++ intro.progress: once-scheduled 后 eventually-scheduled）。soundness 安全（更强公平只让更多程序终止，每个 ISS 执行仍是合法 NV 交错之一），且为序无关 metamorphic 门所必需（若不对从未执行的单元也排进 fair permutation，不同序下谁先执行会变 → 主门 false-fail）。**不得反向声称 NV 也给 unconditional 公平**；NV→ISO C++ parallel-forward-progress 的对接当前为二手（Olivier Giroux CppCon / NVIDIA Developer Forums），一手 verbatim 待补，但 ISS 取更强公平的 soundness 论证不依赖该对接（只需 ISS 执行集 ⊆ NV 执行集）。
- **INV-SCH-2（warp 内序与 warp 间序正交）**：metamorphic 门须在二者的笛卡尔积（或对角线 + 关键叉积）上断言最终态一致。
- **INV-SCH-3（修订：调度原子单位 = 一个 `step_one_group`，per-group 非 per-warp）**：调度原子单位 = 一个 `step_one_group`（一个 PC-group 的一条指令），**不是一个 warp 的全部访存**。对 shared-mem 的写在该 `step_one_group` 的 commit 阶段落地，下一个被调度 `(warp, group)` 在其起点才可见。这是『无同步的 shared-mem 竞争 = UB』判据来源，也是 fence no-op 与 spin 终止性论证共同依赖的粒度地基；**与 §4.1 DEC-MS-MEM-1 第 2 条修订后逐字一致**（统一为『一个 `step_one_group` 的一条指令 commit 后下一个被调度 `(warp,group)` 在其起点可见』，不残留『一个 warp 全部访存』表述）。
- **INV-SCH-4（单 warp 退化恒等）**：`warps.size()==1` 时任何 `warp_sched_order` 退化为透明包裹，行为与阶段② single-warp bit 等价。
- **INV-SCH-5（新增：调度粒度 = 指令级交错）**：macro-step = 选一个 runnable warp 的一个 runnable PC-group 推进恰一条指令后重选；量子 K=1 为默认且唯一规范粒度（与 single-warp step loop 现状一致——`native.cpp` single-warp step loop line 423-518 已是指令级交错：每 iteration 重 `build_groups`(line 619) + `select_group`，推进单条指令 `++issued`(line 518)；但 multi-warp `select_warp` / `warp_sched_order` 维度为本决策**全新待建外层**，须在 single-warp 循环外再包一层，不是现状）。量子 K>1 **已裁决阶段③ 不引入**（K=1 为唯一规范粒度；未来若作吞吐优化引入，须 K 有限且纳入 metamorphic 叉积或固定单一 K 做回归）。**明确禁止** run-to-blocking-point（跑到 `BAR`/`BSYNC`/`EXIT` 才切）作为公平性载体（违反 INV-SCH-1，且会让 spinner 永不让出、flag 写者永不被调度，拒绝合法可终止的 NV 程序）。run-to-block 仅可作已知无 busy-wait 的 barrier-DRF 程序性能快路且须带逃生阀（检测到无 runnable 单元前进但存在 non-blocked runnable 单元时强制切换）。
- **INV-SCH-6（新增：runnable / blocked / yielded 三态分类）**：(1) **runnable** = 该 warp 至少一 lane `active_mask` 且 `lane_state == active`（含 spinning lane：spin-loop 中 lane 始终 active 则始终 runnable）；(2) **blocked** = `lane_state == blocked` 且 `blocked_on` 属于 `Bx` 收敛屏障或 CTA barrier；(3) **yielded** = `lane_state == yielded`（无 runnable 时无条件提升回 active）。**spinning group 算 runnable 而非 blocked**；livelock（有 runnable 但无架构进展，如 spin 读不到 flag）不可一般检测，由 `max_steps` 兜底。与 §8.1 `cta_blocked` 协同：同 warp 内可有部分 lane 阻塞在 CTA barrier（`lane_state==blocked` + `cta_blocked` 标志）、其余 lane 仍 active runnable（与 §6.4 B-6 per-thread 到达咬合）。
- **INV-SCH-7（新增：deadlock vs livelock 检测分离）**：**deadlock（全 blocked）** = 三段恢复（`try_fire_cta_barriers` + `try_fire_bx` + `promote_yielded`）后无 runnable，且存在 `cta_blocked` warp 或存在 `blocked_on` 某 `Bx` 的 lane，而无任何 `bar_id` 可 fire（`Σarrived != expected`）、无任何 `Bx` 满足 `barrier_ready` → 确定性 `barrier_deadlock`（synchronization kind）。**纯快照函数论证**：『expected 永不可满足』在『三段恢复后无任何 runnable』不动点下退化为『当前快照 `Σarrived != expected` 且无 runnable 单元能再增 arrived』（arrived 已冻结），故为纯快照函数、满足 VP-4（全序同 trap）。三段恢复级联 confluent：deadlock 判定下三段均不 fire、顺序无影响；非 deadlock 下 fire 是单调的（fire 一个 barrier 只增不减其它单元的可运行性，释放的 warp 进 runnable 集后由 `warp_sched_order` 统一裁决），故终态由 `warp_sched_order` 唯一决定。**命名并存（对齐 spec-iss.md:50 阶段② 契约）**：阶段② single-warp 纯 `Bx` 死锁保留 `convergence`/`deadlock_no_progress`（向后兼容 VH-1 + 现 `test_spec_iss` convergence 抓取）；multi-warp / CTA 层混合死锁 / CTA-barrier 死锁用 `synchronization`/`barrier_deadlock`——**二者并存，不是替换**。**livelock（有 runnable 无架构进展）** = 依赖步数预算判定，**不 trap**（spin 合法），在独立进度测试（VH-14）设步数预算、超预算判测试失败而非架构 trap。livelock 谓词（依赖步数预算）**明确不进 state-determined trap 体系**（否则不同序步数不同破坏 VP-4）；`max_steps` `budget_exhausted` 因 issue 计数序相关亦不进序无关门（避免一序 200 步终止、另一序未终止导致 false-fail）。

#### 调度原子粒度与公平性契约（Q1 核心决策，填补整份文档隐含地基）

阶段③ multi-warp / CTA 调度的**原子粒度**与**公平性契约**是序无关门、fence no-op soundness、合法 NV 程序终止性三者的共同地基，须先于其余决策落定。

> **现状口径声明（防止行号锚被误读为已实现基线）**：本节决策基于 single-warp 已实现基线（`native.cpp` `NativeWarp` 类 + `spec-iss.md`）。`native.cpp` 全树**仅有 `NativeWarp` single-warp 类**，无 `select_warp` / `warp_sched_order` / `cta_blocked` / `CtaBarrier` / `arrived_thread_set` / `try_fire_cta_barriers`——这些**均为本阶段全新待建**，不是现有锚。下文凡引 `native.cpp` 行号锚定 single-warp 行为（如 step loop line 423-518 已是指令级交错、`build_groups` line 619、`barrier_ready` line 1108）均**已核实**；凡涉及 multi-warp `select_warp` 层均为**本决策新增、须在 single-warp 循环外再包一层**（『single-warp 内核不变、新增 multi-warp 外层』，非『零内核重构』）。

**DEC-Q1-GRANULARITY（调度原子粒度 = 指令级交错，量子 K=1）**：每个 macro-step 重新计算 runnable 集合并经 `warp_sched_order` 选恰一个 runnable warp，该 warp 经 `SchedOrder` 选恰一个 runnable PC-group 推进**恰一条指令**（= 一个 `step_one_group`），然后回到循环顶部重选。**明确禁止粒度 (c) run-to-blocking-point**（跑到 `BAR`/`BSYNC`/`EXIT` 才切）作为公平性载体。粒度 (b) 有界量子 K>1 **已裁决阶段③ 不引入**（若未来作吞吐优化引入，须 K 有限且把 K 纳入 metamorphic 叉积或固定单一 K 做回归）。

- **NV 证据（一手优先）**：
  1. **[Volta whitepaper p.27 'Per-Thread Program Counter' (已核实-PDF)]** "maintains execution state per thread, including a program counter"——Volta+ per-thread PC 是 ISS per-PC-group 调度模型的最直接 NV 一手描述（比 pre-Volta 的 Habermaier/Collange min-PC 更贴近 ISS 的 per-PC-group 调度）。
  2. **[Volta whitepaper p.29 Starvation-Free (已核实-PDF)]** verbatim: "another thread T1 in the same warp can successfully wait for the lock to become available without impeding the progress of thread T0"——spin-wait 须能 forward-progress，**直接否决粒度 (c)**（run-to-block 会让 spinner 永不让出）。作用域是 **same warp 内**，跨 warp liveness 经 ISO C++ parallel-forward-progress 对接。
  3. **[US11442795B2 (本地一手 verbatim)]** "The thread scheduler induces control transfer (e.g., to another shard in the warp) if the threads predicted to arrive ... have in fact not yet arrived"——阻塞即让出，支持指令级交错、否决粒度 (c)。
  4. **[NV BR100 cmodel `ModuleAggregate.cpp:148` run_all + `SQImpl_Obj.cpp:624` Exec_InstructionIssue (本地源码 verbatim)]** 用固定时间量子（100NS clamp 200，SystemC `sc_time` NS）lockstep advance 所有非 idle 模块 + 单发射 + `SwitchRRCredit` 轮转 = bounded-quantum round-robin。**注**：cmodel 是 cycle-approximate 时序模型，其『量子』是**时序步进单位**（SystemC NS），与功能 ISS 的『指令发射量子 K』不是同一抽象层；仅作『NV 参考模型用 bounded-quantum lockstep 轮询、无具名公平序作架构保证（age 仲裁是 `swc_warp.h:149-151` 死代码：注释自承 "This class is actually useless ... We should remove it"）』的旁证，**不作功能 ISS 量子 K 选择的直接依据**，barrier 语义不可对齐（pre-Volta per-warp）。
  - **工程惯例对照（非 NV 证据）**：GPGPU-Sim / Vortex / Ventus 三套主流 SIMT 模拟器对**调度粒度 = 指令级交错**是正面工程惯例佐证（每周期每 warp 发射 1 条、阻塞即跳过、无一用 run-to-block）；但对 **barrier 粒度 = per-warp** 是要拒绝的对照（Volta+ 须 per-thread，见 §6）——同组工具两维度分开陈述，避免口径漂移。
- **理由**：粒度 (c) 在 spin-lock 场景结构性死锁（spinner 循环无阻塞点则永不让出、flag 写者轮不到），而 Volta+ ITS 上该程序能终止，故 (c) 破坏 completeness 并误报 deadlock。粒度 (a) 使每个 ISS 执行是一个合法 NV 交错的 1:1 见证，soundness 最稳。single-warp step loop 已是 (a)，multi-warp 只在其外包 `select_warp` 一层。

**DEC-Q1-FAIRNESS（公平性契约 = unconditional weak fairness）**：见 INV-SCH-1（修订版）。ISS 对每个 runnable `(warp, group)` 执行单元施加 unconditional weak fairness；任何具名确定序必须是 runnable 集合上的 fair permutation。显式标注 ISS 强于 NV conditional parallel-forward-progress，不反向声称 NV 给 unconditional。NV 证据见 INV-SCH-1 锚点（ISO C++ intro.progress verbatim + Volta whitepaper starvation-free）。

**DEC-Q1-RUNNABLE（runnable 精确分类 + deadlock 谓词推广）**：见 INV-SCH-6 / INV-SCH-7。spinning group 算 runnable（否则 spin-lock 被误判 deadlock）；livelock 一般不可判定（停机问题）只能 `max_steps` 兜底；deadlock（全 blocked、无 runnable）是纯状态函数可确定 trap。NV 证据：`native.cpp` `has_blocked_lanes`(line 602) / `build_groups`(line 619-624 只收 active 则 spinning lane 始终入组) / deadlock trap(line 437-440 三段恢复后仍空且 `has_blocked_lanes`)（**single-warp 已核实**，multi-warp 推广为本决策新增）；NV cmodel 死锁检测原型 = 所有非 idle warp `WaitAllBar` 为真且 SRP 输出空、counter 未达阈值则全 block 死锁。

**DEC-Q1-NAMED-ORDERS（具名序集合 + 逐序 fairness）**：见 DEC-MS-MW-2（修订版）。关键厘清（修正文档张力）：`warp_min_id_first` 是 **warp 间固定优先级**，`min_pc_first` 是 **warp 内 group 选择**——两者层次不同。min-PC 调度的 unfairness 由 **Collange's lowest-program-counter scheduling policy**（Habermaier & Knapp ESOP 2012 §5 转引，/tmp/hk.txt:891 verbatim: "Collange's lowest program counter scheduling policy makes the overall mechanism unfair"）给出，**不是 H-K 自创术语**；H-K Program 2/3 的非终止机制是 **pre-Volta IPDOM / reconvergence-stack**（"the warp chooses the immediate post-dominator of the loop as the reconvergence point"），这是 Volta+ ITS 专门取代的 pre-Volta 栈机制，与 ISS 的 `min_pc_first` group 选择序**不是同一回事**（后者不涉及 IPDOM 栈）。故对 ISS `min_pc_first` 仅作『固定优先级序可饿死依赖被排后者先动的程序』的**类比论证**，不是 1:1 复现。结论（『ISS 须用 fair 序断言 spin 终止』）独立成立。
  - **min_pc_first 主门地位边界（与阶段② 咬合）**：`min_pc_first` 作为 warp 内 `SchedOrder` 四序成员在阶段② 主门中地位**不变**（`spec-iss.md`:13/15 不改），对 barrier-DRF(A) / 交换 atomic(B) 程序四序仍须 bit-identical 终态；min-PC unfair 警示**仅影响 D 档**（spin/lock/PC）的『终止性断言用哪个序』——D 档走独立进度测试 VH-14 且只用 fair 序，**不代表 `min_pc_first` 退出主门**。此为 B-OI-1 / INV-GATE-DOMAIN-1 的显式边界，防止跨 warp 公平性裁决反向削弱单 warp 四序留存。

**DEC-Q1-YIELD-CROSS-1（YIELD / SLEEP 跨 warp 层 = no-op-for-state）**：见 §5.5 末（YIELD 跨 warp 扩展）。

curryGPU 不抄任何硬件 warp scheduler（LRR/GTO/IPDOM 是 timing 工件）：具名确定序 + 序无关 metamorphic 门提供比『对单一硬件 trace』更强的正确性保证（覆盖一族合法调度而非一个点），依据 Habermaier & Knapp ESOP 2012（SIMT↔交错多线程 simulation）+ Dubey et al. confluence（arXiv:2511.12638，结构化 CTA 类下 sound+complete，可机械化形式）。

### 5.5 序无关性主门扩展 P-OI

**DEC-MS-MW-3（P-OI）**：对尊重 warp 内 + CTA 内同步契约的 CTA 程序，最终 CTA 架构态（每 warp 9-key + `cta_barriers` 稳定终态 + `memory`）与 `warp_sched_order × SchedOrder` 组合**无关**（任意两个 fair 序组合产出 bit-identical 终态）。single-warp 退化即阶段② 主门（向后兼容）。

P-OI 边界（B-OI，须落 spec 标注 —— 修正 VERIFY [medium]）：

- **B-OI-1（warp 内同步契约）**：阶段② 全部前提（warp 内跨 lane 交互经显式 `Bx` 重聚 / membermask 门控；无屏障 GPR/内存竞争 = UB）。
- **B-OI-2（CTA 内同步契约）**：warp 间对 shared/global 的通信必须经 CTA barrier 或 atomic 排序；无 barrier 隔开的非原子并发读写 = data race = UB（§4.3 边界 B）。
- **B-OI-3（终止 + fair，大幅扩写：序无关门四档适用域）**：序无关门**按程序类分四档**各设不同门（解决 §9.2 与本节历史矛盾，引 INV-GATE-DOMAIN-1）：
  - **A 档 barrier-DRF**（跨 warp 通信仅经 CTA barrier 隔离、终止）：门 = bit-equality across all fair `(warp_sched_order × SchedOrder)` schedules（P-OI 主门）。
  - **B 档 交换 atomic**（跨 warp 仅经交换结合 op 的 RED / 整数 ATOM 通信）：门 = bit-equality across schedules（交换律保终态序无关，INV-3a/INV-5）。
  - **C 档 非交换 atomic**（`exch/cas/inc/dec/float-add` 跨 warp）：门 = 确定性基线（固定单一序两次运行 bit-exact、不跨序断言，INV-5a/MC-N5）。
  - **D 档 lock-based / spin / producer-consumer-via-flag**：门 = 终止性 under fair schedule（`warp_round_robin`）+ 语义不变式（临界区互斥可观测、consumer 读到 producer 数据），**不做 bit-equality**（终止与否及最终交错序相关），走独立进度测试 VH-14。
  - **裁定**：MC-6（CAS spin-lock）/ MC-7（producer-consumer）归 **D 档**，§9.2 表序无关列从『是』改为『否（独立进度测试 VH-14）』，**与本条对齐、消除 §9.2 与本节矛盾**。
  - **理由依据**：Habermaier & Knapp 明确序无关仅对终止 / 无竞争程序成立。**[Volta whitepaper p.29 starvation-free (已核实-PDF)]** 是 **intra-warp 限定**（T0/T1 same warp），跨 warp 经 ISO C++ parallel-forward-progress 对接。**[CONCUR 2018 per-idiom (同行评审实验观测，OBE = Occupancy-Bound Execution，/tmp/concur.txt:151-152 原文 "While OBE is not officially supported")]** verbatim: "a barrier is not allowed, as all threads wait on all other threads regardless of whether they have been scheduled previously; a mutex is allowed, as a thread that has previously acquired a mutex will be fairly scheduled such that it eventually releases the mutex; PC is not allowed, as there is no guarantee that the producer will be scheduled relative to the consumer"——此表作 D 档裁定的 **supporting**（论证 mutex/PC 终止性确实序相关这一技术事实），**非 NV 产品承诺**（OBE 是实验观测模型，not officially supported）；NV 官方 liveness 承诺统一锚到 ISO C++ parallel-forward-progress + Volta whitepaper starvation-free。**livelock 谓词（依赖步数预算）不进 state-determined trap**（承接 INV-SCH-7）。D 档对齐 `plan-its.md` OD-5（spin 前向进度 deferred 独立进度测试）。
- **B-OI-4（交换 atomic）**：多 atomic 终值序无关**当且仅当 op 满足交换律**（add/min/max/and/or/xor 满足 → B 档进主门；exch/cas/inc/dec/float-add **不**满足、终值序相关 → C 档确定性基线、不在 P-OI 保证内）。串行化（M3）保证单 atomic 不可分，但**多 atomic 终值序无关额外要求 op 满足交换律** —— 这是对 V1『尊重同步契约』前提的精确化、与 V1 一致、与 INV-GATE-DOMAIN-1 B/C 档对应，须落 spec 而非默认。

- **INV-GATE-DOMAIN-1（序无关门四档适用域）**：**A barrier-DRF + B 交换 atomic** ⟹ bit-equality across all fair `(warp_sched_order × SchedOrder)` schedules（P-OI 主门 VH-2）；**C 非交换 atomic**（exch/cas/inc/dec/float-add）⟹ 确定性基线（固定单序 bit-exact、不跨序断言，VH-5）；**D lock/spin/producer-consumer** ⟹ 终止性 under fair schedule + 语义不变式（互斥 / 数据正确），不做 bit-equality、走独立进度测试 VH-14。

soundness 草证：对合规程序（A/B 档），warp 调度序只改变交错、不改变 (1) 每 warp 私有态演化（INV-AGG-3 + INV-SCH-3）、(2) CTA barrier release 集（纯计数谓词，序无关）、(3) 跨 barrier 的 shared 可见性（barrier 之间无竞争 → release 点 shared 态唯一）、(4) 交换 atomic 终值。⟹ 每个 barrier 释放点全 CTA 态被夹逼为唯一值，两次 barrier 之间各 warp 独立演化、终点汇合 → 终态序无关。严格证明引 Habermaier & Knapp simulation + confluence。

- **DEC-MS-YIELD-CROSS-1（YIELD / SLEEP 跨 warp 层 = no-op-for-state）**：`YIELD` / `SLEEP`（NANOSLEEP）在跨 warp 层是纯调度提示 = no-op-for-state：让出当前发射机会给其它 runnable 单元，零寄存器 / PC / barrier / 内存架构效果。在 unconditional weak fairness 下 YIELD 不改变最终架构态、不改变终止性（公平性已由调度器保证，YIELD 只影响被主门抹平的交错维度）。实现：`YIELD` 置 `lane_state=yielded`，调度器无 active group 时无条件提升回 active；跨 warp 层 YIELD 可触发 `select_warp` 切到别的 warp（鼓励切换），但序无关主门下对合规（A/B 档）程序 bit-不变。**单 warp 退化（`warps.size()==1`）**：无别的 runnable warp 可切，YIELD 跨 warp 切换退化为 no-op，行为与 `spec-iss.md`:30 阶段② 语义 bit-identical（承接 INV-SCH-4）。NANOSLEEP 计时部分在无时序 oracle 退化为 no-op-for-state。soundness：YIELD/SLEEP 只在调度交错维度起作用，而 D 档（依赖交错的 spin/PC）已移出序无关主门，故 YIELD 对 A/B 档 bit-不变则可安全实现为 no-op-for-state；对 D 档可加速 spinner 让出使 flag 写者更快被调度（独立进度测试中被尊重）。
  - **NV 证据**：**[NV cmodel `SQImpl_Obj.cpp:1177-1186` _EU_SLEEP 分支 (本地源码 verbatim)]** "if(opcode != _EU_SLEEP){ UpdateBraPC(...) }"——SLEEP 回送被 default 分支显式不更新 PC、不做任何事（功能 no-op）；YIELD/NANOSLEEP 在 cmodel 全树 0 命中（未建模、零架构效果）。**[US11442795B2 (本地一手)]** "induces control transfer ... if the threads predicted to arrive ... have in fact not yet arrived"——阻塞即让出提示机制。**[Volta whitepaper p.27 (已核实-PDF)]** "yield execution of any thread to allow one thread to wait for data to be produced by another"。`spec-iss.md` 既定 YIELD 纯调度提示零架构效果，`native.cpp` `exec_yield`(line 931，**single-warp 已核实**) 只置 yielded + 清 `blocked_on`。

### 5.6 cluster_dim=1 恒等与多 CTA 边界

- **DEC-MS-MW-4**：阶段③ 实现到 CTA 层；`cluster_state` 仅 dim=1 恒等薄壳（持 `vector<block_state>` size==1、`resolve_shared(rank=self)` 恒等），单-CTA 行为 bit 不变；rank≠self → `unsupported` trap、绝不伪造远端态。真·多 CTA / grid + 跨-CTA DSMEM **建议延后**（北极星单 / 独立 CTA 不需，FA-3 仅 ~2% 可选优化）。
- 『独立无交互 CTA 的并行 launch』（embarrassingly parallel，各 CTA 终态独立、无需跨-CTA 序无关证明）是一个**对延后边界的扩张提议**，列为 open question 待用户确认，**不默认纳入**阶段③ 范围。

---

## 6. CTA named barrier 状态机与不变式

### 6.1 两层屏障的本质区别（防混淆）

CTA named barrier（`BAR.SYNC`/`BAR.ARV`）与阶段② 已落地的 ITS warp 内收敛屏障 `Bx`（BSSY/BSYNC/BREAK）是**两套独立机制，不可复用同一 struct**：

| 维度 | ITS 收敛屏障 `Bx`（阶段②已存在） | CTA named barrier（阶段③新增） |
|---|---|---|
| 作用域 | warp 内 32 lane 子集（per-PC group） | 整 CTA 全部 warp 的参与线程 |
| 参与单位 | per-lane（`participation_mask` 32-bit） | **per-thread 计数，跨 warp 聚合**（与 `Bx` per-lane 区分；Volta+ sm_70+ 语义，见 B-6） |
| 身份载体 | `Bx` token `{participation_mask,reconv_pc,valid}` + `blocked_on[lane]` | `bar_id`(0..15) → `{phase, arrived_count, expected_count}` |
| 数据结构 | `warp_state.Bx[16]`（per-warp） | `block_state.barriers`（per-CTA，承接 research-notes §4） |
| 对应指令 | `BSSY/BSYNC/BREAK`（ITS） | `BAR.SYNC`/`BAR.ARV`（`__syncthreads` / arrive） |
| 恢复语义 | 统一 `reconv_pc+1`（全 lane 对齐重聚点） | **各 lane 各自 next PC（fallthrough），不强制统一 reconv_pc**（B-14） |
| snapshot | 3 字段冻结（不可改） | 新增独立子集 `cta_barriers`，**不复用** `bx` schema；quiescent 终态进序无关比较的只有 `{phase, arrived_count, expected_count, phase_parity}` 四纯量，`arrived_thread_set` 仅 debug/去重（B-11） |

核心论证：`Bx` 的 `participation_mask` 是 lane bitmask（32-bit 上限），而 CTA barrier 参与者可达 CTA 全部线程（典型 1024，32 warp），远超 32-bit；`Bx` 用 per-lane `blocked_on`，CTA barrier 用 **per-(warp,lane) 到达集**（`arrived_thread_set`，B-11）+ per-thread 到达计数（**到达粒度 per-thread，B-6**；存储按 warp 索引的 lane bitmask 数组只是编码形态，非 per-warp 到达语义）。混用会破坏 `bx` 3 字段冻结。`nv_patent/sm/async_barrier.md` 的三类等待对象 taxonomy（scoreboard / convergence / transaction barrier）印证 `Bx`（convergence）与 CTA barrier（execution/arrival）是不同对象。

### 6.2 指令与语义表面

sm100a `BAR.html` + `isa.json`（curryGPU 自定义编码，语义/modifier 对齐）：

| 形态 | SASS | PTX 等价 | 语义 |
|---|---|---|---|
| 全 barrier sync | `BAR.SYNC 0x0` | `barrier.cta.sync 0` | 全 CTA arrive + wait，expected = CTA 全线程 |
| partial barrier | `BAR.SYNC 0x0, 0x20` | `bar.sync 0, N` | arrive + wait，expected = 显式 N（32 倍数） |
| arrive-only | `BAR.ARV 0x0` | `barrier.cta.arrive 0` | arrive 不阻塞（split barrier 的 arrive 半） |
| reduction barrier | `BAR.RED.POPC/AND/OR` | `barrier.cta.red.*` | barrier + 跨线程谓词归约 |

- **named barrier 数量（修正 VERIFY [low]）**：**16 个 barrier 槽（bar_id 编码 0..15）**。PTX/编码层为 16 个逻辑 barrier（0..15）；`cmodel_br100` BR100 约定 bar_id 0 保留、实际可用 15 个（`SetBar`/`BarPreIssue` 均 `CMOD_ASSERT(barID > 0 && barID < 16)`）。curryGPU 自定义编码可选 0..15 全可用，但需文档化与 cmodel 的差异，**不宣称『cmodel 三重一致』**。
- **partial barrier**：第二操作数 = 期望到达线程数 N（必须 32 的倍数，warp 整组参与）；缺省 expected = CTA 全线程数（依赖 SR_NTID，见 §8.3）。**count 单位 = thread 数（warp 对齐）、非命名线程集合、无 membership check**（只要任意 N 个 warp 对齐的 thread 到达即释放，不校验是哪些 thread；DEC-Q2-COUNT-1）。
- **`.aligned` vs non-aligned 裁定（DEC-Q2-ALIGNED-1）**：SASS-like `BAR.SYNC` = PTX `bar.sync ≡ .aligned` 变体的计数行为（PTX A8: "bar{.cta}.sync is equivalent to barrier{.cta}.sync.aligned"）。curryGPU **取 non-aligned per-thread 语义**（per-thread 计数模型对 divergent 到达本就良定义、各 lane 各自计）；`.aligned` 误用（条件代码中各线程对条件求值不一致却用 aligned barrier）的 UB **属编译器 / 程序员契约层、不引入运行期 trap、不做 aligned 一致性检查**，仅文档化为非目标。依据 **[PTX ISA 8.5 §9.7.12.1 (已核实-PDF)]** verbatim: "When specified, it indicates that all threads in CTA will execute the same barrier{.cta} instruction. In conditionally executed code, an aligned barrier{.cta} instruction should only be used if it is known that all threads in CTA evaluate the condition identically, otherwise behavior is undefined."——`.aligned` 是契约性声明，UB 仅在程序员违约时触发、非硬件运行期可廉价检测的 state-determined 谓词；功能 oracle 的 per-thread 计数对 non-aligned 到达良定义，引入 aligned-trap 会 false-reject 合法 divergent-barrier 程序（与 §4.3 "不可把一切偏离当 UB/trap" 一致）。
- **BAR.ARV 非零 count 要求**：`barrier{.cta}.arrive` 要求非零 thread count（PTX A1: "a non-zero thread count is required for barrier{.cta}.arrive"）。
- **BAR.RED.POPC/AND/OR**：barrier + 跨 warp 谓词归约。**降为阶段③ 可选 / 可推迟**（修正 VERIFY [low]）：cross-warp reduction 验收（spec §5③）可用 `BAR.SYNC` + 显式 shared-mem 归约表达；BAR.RED 不进 Lower Bound，除非 corpus 实测需要。BAR.RED 不得与 BAR.SYNC/BAR.ARV 在同一 active barrier 混用（PTX A6 → B-17 debug 断言）。

### 6.3 到达计数数据结构与三态 FSM

```
struct CtaBarrier {              // 每 bar_id 一个槽
  uint32 phase;                  // {Idle, Gathering, Released}
  uint32 arrived_count;          // 已 arrive 的线程数(跨 warp 累加)
  uint32 expected_count;         // 期望到达(block-wide=动态非退出线程数,B-12;partial=静态 N,B-13)
  bool   expected_pinned;        // expected 是否被首个到达者锁定(显式 count 形态)
  std::vector<uint32> arrived_thread_set;  // per-warp 32-bit lane bitmask 数组,长度 num_warps;哪些 (warp,lane) 已贡献到达(去重,B-11)
  uint64 phase_parity;           // phase 翻转位,split-barrier wait 用
};
```

> `research-notes.md` §4 写 `map<bar_id, arrived_warp_set>`，本节细化为『到达集 + 到达计数 + 期望计数』，与该蓝图一致、不引入新决策。`arrived_thread_set` 取 per-warp 的 32-bit lane bitmask 数组（`vector<uint32>` 长度 `num_warps`）或等价稀疏结构——单 `uint64` **不足**容纳典型 CTA 1024 thread（= 32 warp）的到达标记（原 `uint64 arrived_warp_mask` 字段类型 / 命名作废，B-11）。`expected_count` 区分 block-wide（动态非退出，B-12）与显式 count（静态 N，B-13）；跨 call site 同 `bar_id` 累加（B-14）。

**到达计数 FSM 一手参考（修正 VERIFY [medium] 引用定位错误）**：真正的到达计数实现在 `cmodel_br100/model/spc/cu/srp/SRPImpl.cpp`（`barCounter[gsmID][barID]++`、release 条件 ALL 模式 `>= RetrieveTotal(gsmID)` / COUNTER 模式 `>= get_tg_bar_cnt()`、KICK 时 reset=0）与 `SRPImpl.h`（`barCounter[_CU_TG_NUM][_CU_TG_BAR_NUM]`）。`swc_warp.h` 的 `BarSlot`/`Bar_Group` 是 **debug-only 声明、未实例化**（注释自承『Technically we don't have to record BarType and BarMode. Here record it for debug.』、无 .cpp 方法定义），**仅作枚举命名参考**（SYNC/PASS/CSM、ALL/COUNTER），不作为到达计数 FSM 的一手实现依据。

三态 FSM：

```
[Idle] ─first arrive─▶ [Gathering] ─arrived==expected─▶ [Released]
   ▲                       │  ▲                              │
   │                       └─ more arrivals(arrived<exp) ───┘
   └──── 所有等待 warp 消费 Released, parity 翻转, 槽回 Idle ────┘
```

- **Idle**：`arrived_count=0`，expected 未锁定。首个 `BAR.SYNC`/`BAR.ARV` 到达 → 锁定 `expected_count`，转 Gathering。
- **Gathering**：`0 < arrived < expected`。`BAR.SYNC` 的 warp 进 `cta_blocked`（warp 级阻塞标志，见 §8.1）；`BAR.ARV` 的 warp 累加后**立即 fallthrough**。
- **Released**：`arrived == expected`。所有阻塞 warp 一次性恢复 runnable，phase parity 翻转，槽回收 Idle 准备下一 phase。

### 6.4 不变式

- **B-1（16 槽边界）**：`bar_id ∈ [0,15]`；越界 → `barrier_id_out_of_range` trap（synchronization kind；原 `cta_barrier_id_out_of_range` 统一重命名，§9.4 表同步改）。
- **B-2（单 phase 单次到达）**：同一 (warp,lane) 对同一 (bar_id, phase) 至多累加一次（`arrived_thread_set` 去重）；重复到达 → debug 断言 `cta_barrier_double_arrive`。
- **B-3（expected 锁定一致 + count 单位）**：显式 count 形态同 phase 内 `expected_count` 一旦被首个到达者锁定不可改（block-wide 动态形态见 B-12）；count N **必须是 warp size(32) 整数倍**（PTX A1/A7 verbatim "the value must be a multiple of the warp size"），count 单位 = thread 数（warp 对齐）。trap reason 拆分：编码期可检者（字面常量非 32 倍数）在 assemble/encode 期拒绝；运行期非 32 倍数 / 同 phase 不同 expected → `barrier_count_not_warp_multiple`（synchronization kind；替换原 `cta_barrier_count_mismatch`，§9.4 表同步改）。
- **B-4（arrived ≤ expected 单调）**：Gathering 阶段 `arrived` 单调不减；`arrived==expected` 即刻且唯一触发 Released；`arrived > expected` 不可达（curryGPU per-thread 用 `==` 配合 B-12 动态 expected；本地 cmodel 用 `>=` 因 per-warp，是 pre-Volta 差异）。
- **B-5（EXIT 调减 expected，反死锁——仅 block-wide）**：**仅适用 block-wide barrier（缺省 expected，B-12）**：lane `EXIT` → 从所有 Gathering block-wide barrier 的参与集移除、`expected_count` 相应递减；递减后若 `arrived==expected` 立即 Released（对齐 PTX A2 "non-exited" + US9442755B2 "withdrawn thread ... does not participate"）。**显式 count barrier 不调减**（见 B-13，有意例外）。对称 `Bx` 的 EXIT 清 participation（research-notes §4 starvation-free），复用 `exec_exit` 结构 + EXIT 后 `try_fire_cta_barriers()`。本地 cmodel 静态计数（`SRPImpl.cpp:167-176` RetrieveTotal 返回 launch 期静态 totalWarps、不随 exit 下调）是反例、拒绝。
- **B-6（per-thread arrival，不要求 warp 收敛；删除自相矛盾末句）**：CTA barrier 到达是 **per-thread / per-active-lane（Volta+ sm_70+ 语义）**；divergent warp 的 lane 可在不同 PC 分批到达，每个 guard-true 且 active 且 non-exited 的 lane 各算一次；exited lane 不计、未执行 `BAR` 的 lane 不计（留待其自己执行到 `BAR` 时再计）。barrier fire **不改变任何 `Bx` 收敛状态**（两层正交）。【**删除原末句『到达粒度是 warp』** —— 该 per-warp 残留是 pre-Volta 语义、与首句直接矛盾、与本地 cmodel per-warp 同属须拒绝的做法】。
  - NV 依据锚：**[PTX ISA 8.5 §9.7.12.1 (已核实-PDF)]** verbatim: "barrier{.cta} instruction causes executing thread to wait for all non-exited threads from its warp and marks warps' arrival at barrier. In addition to signaling its arrival at the barrier, the barrier{.cta}.red and barrier{.cta}.sync instructions causes executing thread to wait for non-exited threads of all other warps participating in the barrier to arrive."；**[同 PDF §9.7.12.1 sm_6x note 第2点 (已核实-PDF)]** verbatim: "For .target sm_6x or below ... All threads in warp (except for those have exited) must execute barrier{.cta} instruction in convergence."——这条 per-warp-convergence **限定到 pre-Volta（sm_6x 及更低）**，是『Volta+ 解除 per-warp、改为 per-thread』的最强一手 NV 证据。**[CUDA C++ Programming Guide CC 7.x (多源搜索, 未本地 PDF 核实——主依据挂已核实的 PTX A2 "non-exited")]** "all non-exited threads reach the barrier"。反面证据（须拒绝）：本地 cmodel `EUAlu.cpp:1389` `_BAR` 不读 active mask、`SRPImpl.cpp:192` `barCounter++` 每 warp 仅 +1 = per-warp = pre-Volta。
  - **与 Q1 INV-SCH-6 咬合**：per-thread 到达意味着同 warp 内可有部分 lane 阻塞在 CTA barrier（`lane_state==blocked` + `cta_blocked` 标志，§8.1）、其余 lane 仍 active runnable（INV-SCH-6 三态分类）——删除本条末句是该咬合的硬前提。
- **B-7（到达集去重，warp→thread）**：去重单位从 warp 收紧为 **thread/lane**。`arrived_thread_set`（原 `arrived_warp_mask` 重命名，见 B-11）记录哪些 (warp,lane) 已贡献到达，用于 B-2 单 phase 去重与 EXIT 时判定该 lane 是否已计入。
- **B-8（phase parity）**：每次 Released → parity 翻转、槽回收 Idle；split-barrier wait 半通过比对 parity 判定等的是哪个 phase；同一 bar_id 在循环中可复用（漏复位 → 第二轮假 release）。
- **B-9（序无关 fire）**：Released 触发是当前 CTA 聚合态 `Σ arrived == expected` 的纯函数，与 warp 调度序无关（承接 V1/V2/V3）。**注**：B-9 依据来自 curryGPU 自身纯计数谓词论证（per-thread 计数是聚合态纯函数 → 序无关，VP-4 state-determined），非外部对照（本地开源对照无序无关概念、单一确定调度）。只取 quiescent 终态进比较（SS-2），中间 Gathering 计数排除。
- **B-10（谓词化 CTA barrier，升级为裁定）**：从 open question **升级为裁定** —— CTA barrier per-thread 语义下**接受** guard-true 子集到达（per-thread arrival 的自然延伸，Volta+ 下 divergent 分支各 `__syncthreads` 合法完成的前提，PTX A6 "Different warps may execute different forms ... using the same barrier name"），**不 trap** `predicated_cta_barrier_unsupported`。与 `Bx` 的 `predicated_barrier_unsupported`（拒谓词化）是**有意差异**：`Bx` 是 warp 内 per-lane-token 重聚机制（谓词化破坏 token 一致性）；CTA barrier 是 per-thread 到达计数（谓词化 = 部分线程参与，良定义）。partial-of-partial（谓词化 + 显式 count）仍 debug 断言。【同步删除 §11 对应 open question 条目】。

新增不变式（B-11..B-17，接续编号不重排 B-1..B-10）：

- **B-11（arrived 集编码）**：`CtaBarrier.arrived_thread_set` 必须能容纳 `num_warps × 32` 个 thread 的到达标记（典型 CTA 1024 thread = 32 warp，单 `uint64` 不足）。实现取 per-warp 的 32-bit lane bitmask 数组（`vector<uint32>` 长度 `num_warps`）或等价稀疏结构；snapshot 序列化按 (warp_id 升序, lane 升序) 确定输出。原 §6.3 的 `uint64 arrived_warp_mask` 字段类型 / 命名作废。
- **B-12（block-wide 动态非退出 expected）**：block-wide barrier（缺省 expected）的 `expected_count` 是**动态量 = 当前 CTA 内非退出线程数**。lane `EXIT` 时对所有 Gathering 态 block-wide barrier 的 `expected_count` 扣减 1 并从参与集移除，扣减后立即重检 fire（对齐 PTX/CUDA "all non-exited threads"）。NV 依据：**[PTX ISA 8.5 §9.7.12.1 (已核实-PDF)]** "wait for all non-exited threads"；**[US9442755B2 (本地一手)]** "A thread that has withdrawn ... remains 'awake' ... does not participate"。【显式 thread-count barrier 例外：B-13】。**可落地性硬依赖 SR_NTID（DEC-MS-PRE-1）算 CTA 全线程数**：已裁决阶段③ 补全 SR_NTID（DEC-MS-PRE-1 全量落地），block-wide 缺省形态与 B-12 动态扣减可用、VH-6a 可建；不取『显式 partial 形态回避 SR_NTID』路线。
- **B-13（显式 count barrier 静态不调减）**：显式 thread-count partial barrier（`BAR.SYNC id, N`）的 `expected_count = N` 固定（首个到达者锁定后不可改，承接 B-3），`EXIT` 不调减 N。被 N 计入的线程退出而不到达 → `arrived` 永不到 N → 由 deadlock 检测（B-15）兜底，非 membership 报错。这是有意的语义差异：缺省 barrier 动态非退出（B-12），显式 count barrier 静态固定。理由：count N 是程序语义（期望恰 N 个到达），自动扣减会改变程序语义；退到不齐 → 死锁是 faithful（NV 显式 count barrier 同样要求 N 个到达、退出线程不会神奇补上）。
- **B-14（跨 call site 同 bar_id 互相计数）**：同一 (bar_id, phase) 的到达计数累加所有执行到该 bar_id 的 thread，与到达发生在哪条 `BAR` 指令 / 哪个 call site 无关。Volta+ 下 divergent if/else 两支各有 `BAR.SYNC` 用同 bar_id 时，两支到达汇入同一 `arrived_count`。各到达 lane fire 后从各自 next PC（fallthrough）恢复，**不强制统一 reconv_pc**（区别于 `Bx`）。NV 依据：**[PTX ISA 8.5 §9.7.12.1 (已核实-PDF)]** "Different warps may execute different forms of the barrier{.cta} instruction using the same barrier name and thread count."；**[US9442755B2 (本地一手)]** "the program counter of the top barrier instruction is appended to a barrier identifier as a tag to allow the same barrier identifier to be used in multiple places"——**注**：PC-tag 是硬件实现机制（专利仅证明 NV 硬件早有按 thread 数计数能力），PTX 编程模型层按到达计数累加、与 call site 无关；per-thread 代际归属由 PTX A2 "non-exited threads"（已 PDF 核实）拍板，专利不作代际证据。
- **B-15（混合阻塞死锁谓词，统一 §6.5）**：`deadlock_no_progress` 推广（纯快照函数，归 synchronization / `barrier_deadlock`）：三段恢复（`try_fire_cta_barriers` + `try_fire_bx` + `promote_yielded`）后无任何 runnable group，且（存在 lane 阻塞在某 `Bx` 或存在 warp `cta_blocked`），且无任何 `bar_id` 满足 `Σarrived == expected`、无任何 `Bx` 满足 `barrier_ready` → `barrier_deadlock`。**纯快照函数论证**（与 INV-SCH-7 逐字一致）：『expected 永不可满足』在『三段恢复后无任何 runnable』不动点下等价于『当前快照 `Σarrived != expected` 且无 runnable 单元能再增 arrived』（arrived 已冻结），故为纯快照函数、满足 VP-4（全序同 trap）。覆盖交叉死锁四类：
  - **D-a（Bx 等 bar）**：同 warp 内一部分 lane 阻塞在 `Bx`、其余 lane 阻塞在 CTA barrier b，而 b 的其他参与 warp 也都互等 → `Bx` 永等不到卡在 b 的 lane、b 永等不到卡在 `Bx` 的 thread。
  - **D-b（bar 等 Bx）**：对称。
  - **D-c（单支 barrier）**：divergent branch 只一支有 barrier（部分线程走无 barrier 路径不 EXIT 而停在别处）→ `arrived` 永不到 expected（B-5 EXIT 调减未能补救）。
  - **D-d（显式 count 不足）**：显式 count barrier 期望 N 但实际到达 thread < N（B-13）。
  - **NV-faithful 论证**：这些场景在 per-thread 等待语义下**结构性死锁**，与 NV Volta+ 文档化的 "a barrier will not be reached by some non-exited thread ... must be modified"（PTX A2 / CUDA B3，即非良构程序）一致，故 trap 是 **NV-faithful 的推断**（区分：已核实的 per-thread 等待语义 vs 由其外推的真机死锁断言）。**[Habermaier & Knapp ESOP 2012 (/tmp/hk.txt:822)]** "threads at the synchronization point might be waiting for threads that do not even exist yet ... resulting in a deadlock" 佐证 "等不到的线程 → deadlock" 真实可能。混合阻塞 D-a/D-b 与 arrive-overuse 的 unpredictable 行为**无 NV 直接证据**（cmodel 不建模 `Bx`），以 per-thread 等待语义 + `barrier_ready` 代码锚（`native.cpp:1108-1124`，已核实）+ US11442795B2 收敛点等待语义作工程推断。
- **B-16（混合阻塞 fire 谓词正交）**：`Bx` 的 fire predicate（`barrier_ready`：participation 内每 lane `blocked_on==Bx` 或 exited）与 CTA barrier 的 fire predicate（`Σarrived==expected`）各自只认自己的阻塞 / 到达集。lane 阻塞在 CTA barrier b 时，对 `Bx` 既非 `blocked_on==Bx` 也非 exited → `Bx` 等待（NV-faithful：该 lane 须被外层屏障释放、跑到 `BSYNC` 才算 `Bx` 到达）；lane 阻塞在 `Bx` 时不计入任何 CTA barrier 到达。承接 `native.cpp:1117` `barrier_ready` 的精确 `blocked_on==index` 匹配（**single-warp 已核实**）+ spec-iss "不能把阻塞在其他 barrier 上的 lane 当作当前 barrier 到达者"。代码锚 verbatim: `const bool lane_blocked_here = active_mask_[lane] && lane_state_[lane] == "blocked" && blocked_on_[lane] == index;`——面对 blocked-on-CTA-bar lane 自然返回 false=等待，无需新逻辑，仅须把 CTA-bar-blocked lane 用 `cta_blocked` 标志区分（不写 `blocked_on`）。
- **B-17（BAR.ARV 复用 + producer-consumer + 混用断言）**：`BAR.ARV`（= PTX `barrier.cta.arrive`）累加 `arrived` 后立即 fallthrough、不阻塞（PTX A2: "barrier{.cta}.arrive does not cause executing thread to wait for threads of other participating warps."）；barrier 完成（`arrived==expected`）后自动 reinit（`arrived` 清零、`phase_parity` 翻转、回 Idle）立即可复用同 bar_id（PTX A3: "When a barrier completes, the waiting threads are restarted without delay, and the barrier is reinitialized so that it can be immediately reused."）。producer-consumer = producer `BAR.ARV` + consumer `BAR.SYNC`（PTX A6）。同 warp 在 reset 前对同 bar_id 发多于预期 `BAR.ARV` 后跟任何 `BAR` → debug 断言 `cta_barrier_arrive_overuse`（unpredictable，PTX A6: "Care must be taken to keep a warp from executing more barrier{.cta} instructions than intended ... Execution in this case is unpredictable."）；`BAR.RED` 与 `BAR.SYNC`/`BAR.ARV` 同 active barrier 混用 → debug 断言。显式 `phase_parity` 字段是工程选择（NV 不强制暴露 parity，但 split-barrier wait 需区分 phase、防漏复位假 release）；本地 cmodel 无独立 phase bit、靠 counter 清零复用，curryGPU 取显式 parity 更利于 snapshot 序无关比较的 quiescent 终态判定。

### 6.5 deadlock 检测

deadlock 检测**统一由 B-15 谓词承载**（normative source = B-15；§5.4 伪码 line 325 的 `set_trap("synchronization","barrier_deadlock",...)` 与本节、§9.4 表均引用 B-15）：三段恢复（`try_fire_cta_barriers` + `try_fire_bx` + `promote_yielded`）后无 runnable group，且（存在 lane 阻塞在某 `Bx` 或存在 warp `cta_blocked`），且无任何 `bar_id` 可 fire（`Σarrived != expected`）、无任何 `Bx` 满足 `barrier_ready` → `barrier_deadlock`（synchronization kind）。纯快照函数（『expected 永不可满足』在 no-runnable 不动点下退化为当前快照 `Σarrived != expected` 且 arrived 已冻结，B-15）保证所有序到同一 trap（VP-4）。覆盖交叉死锁 D-a（Bx 等 bar）/ D-b（bar 等 Bx）/ D-c（单支 barrier，early-exit before barrier 或 divergent branch 只一支有 barrier 且 B-5 未能补救）/ D-d（显式 count 不足）。

**deadlock（本节）vs livelock（Q1 域）边界**：deadlock = 全 blocked 无 runnable（本节，确定性 trap）；livelock = 有 runnable 但无架构进展（spin 读旧值，属 Q1 域 INV-SCH-7，**不在此 trap**，靠 `max_steps` / 独立进度测试 VH-14 兜底）。**命名并存**：阶段② single-warp 纯 `Bx` 死锁保留 `convergence`/`deadlock_no_progress`（向后兼容 VH-1 + 现 `test_spec_iss` convergence 抓取）；multi-warp / CTA 层混合 / CTA-barrier 死锁用 `synchronization`/`barrier_deadlock`——二者并存、非替换（承接 INV-SCH-7、spec-iss.md:50）。

---

## 7. mbarrier / async 接缝

### 7.1 阶段归属裁定（修正 VERIFY [high] boundary + completeness）

mbarrier（async / transaction barrier）的价值完全绑定 async-copy/TMA：它是 `cp.async.bulk`/TMA 完成事件汇入点（complete_tx）、tx-count 单位是字节（TMA 风格）、`SYNCS.TRANS64`/`ARRIVES.LDGSTSBAR` 是 cp.async 配套指令。而 `plan-foundation.md` FUT-2（line 137）枚举阶段③ 范围为 `memory_space + CTA barrier + atomics + 负向测试`，**完全没有点名 mbarrier/async barrier/transaction barrier**；`spec.md` §2 把 async-copy/TMA(eager) 与 §7 S5 把 MMA.FENCE/COMMIT/WAIT 一并列为同类 timing 工件，GROUND §C 把 async-copy/TMA descriptor 归阶段④（FUT-3）。

**裁定（DEC-MS-MB-1）**：阶段③ **不**实质性建模 mbarrier。按 FUT-2 字面收窄：

- 阶段③ CTA 同步**只做** `BAR.SYNC`/`BAR.ARV` 计数型 named barrier（有 SASS + cmodel 双重背书且 FUT-2 点名，见 §6）。
- mbarrier / async-transaction-barrier **整体随 async-copy/TMA 推迟到阶段④**（与 MMA.COMMIT/WAIT 同期，`research-notes.md` §6 已把 MMA fence/commit/wait 同 async-copy 处理）。
- 阶段③ **只保留 mbarrier / async 指令『可解码且功能 no-op』的接缝**（与 `cp.async.commit_group`/`wait_group`/`DEPBAR`/`ACQBULK` 处理同级）：**不实现 tx-count 记账、不实现 eager memcpy + complete_tx、不新增 mbarrier trap reason、不进 snapshot、不进序无关门**。
- 若用户确认北极星 FA-3 必须在阶段③ 就跑通 async producer-consumer，再把 mbarrier 显式提升为阶段③ 范围**并同步改 `plan-foundation.md` FUT-2 措辞** —— 这是需用户拍板的范围扩张，不默认纳入。

### 7.2 阶段④ 落地时的语义基线（接缝预留，供后续阶段承接）

下列是 mbarrier 真正落地（阶段④）时的语义基线，本阶段只须保证不与之矛盾、且解码器能提取操作数：

- mbarrier 是驻 shared memory 的 64-bit 不透明对象（8-byte 对齐），跟踪 4 分量：current phase（parity）、pending arrival count、expected arrival count、pending transaction count（tx-count，字节为单位）。
- **双轨 AND 完成**：phase 完成条件 = pending-arrival==0 **AND** pending-tx==0（CUDA Programming Guide 逐字：『until all the producer threads have performed an arrive AND the sum of all the transaction counts reaches an expected value』）；完成时原子 reset（pending-arrival ← expected、pending-tx ← 0、parity 翻转）。SASS `ARRIVES` 的 `ARVCNT`/`TRANSCNT` 双 modifier 佐证双轨。
- 落地时 mbarrier 驻 `block_state.shared_memory`（**自动被 `memory.shared` 子树覆盖、无需新增 `mbarriers` 顶层键** —— 这是降低对冻结 9 键扰动的合理路线）；async-copy 数据 eager memcpy + 同步 `complete_tx` 回调，timing 丢弃、语义正确。
- 落地时新增 trap reasons（`mbarrier_uninitialized` / `mbarrier_arrive_overflow` / `mbarrier_tx_underflow` / `mbarrier_phase_violation`）归 `synchronization` kind（见 §9.4）。

### 7.3 与 scoreboard / Bx 的非冲突边界

mbarrier（transaction barrier）**不是**控制位段的 `read_barrier`/`write_barrier`/`wait_mask`（scoreboard 依赖管理），**也不是** operand kind `"barrier"`（B0–B15 ITS 收敛屏障槽）。mbarrier 的操作数是 **shared 地址**（`[R+UR+imm]`），落地时 operand kind 应是 `address/memory`（阶段③ 要新增的 kind，见 §8.2），而非复用 `_parse_barrier`。

---

## 8. 与现有架构 / 已定决策的衔接

### 8.1 数据结构 block_state / cluster_state 与冻结约束

承接 `research-notes.md` §4 蓝图，阶段③ 新建 `block_state` 层、`cluster_state` dim=1 薄壳（§5.2）。冻结约束（不可矛盾）：

- **warp_state.Bx snapshot 为 3 字段** `{participation_mask, reconv_pc, valid}`，**不可加第 4 字段**（`spec-iss.md` Barrier State；`plan-its.md` line 115/116）。
- **lane_state 四态枚举** `{active, blocked, yielded, exited}` **冻结**，不得新增。
- **warp 级 CTA 阻塞态**（warp 阻塞在某 CTA barrier）**不侵入** lane_state 四态：用 warp 级独立标志承载。**统一命名为 `cta_blocked`**（warp_state 的 warp 级标志 + CTA 层维护『每 warp 阻塞在哪个 cta_bar_id』），区别于 warp 内 lane 级 `blocked_on[32]`（不同层级的不同字段）。lane 阻塞在 CTA barrier 时 `lane_state` 仍是 `blocked`，用 `cta_blocked` 区分阻塞对象。**须在阶段③ spec 收口为单一术语**（修正 VERIFY Missing：子主题间曾用 `cta_blocked`/`blocked_on_cta_bar` 不一致），否则 snapshot schema 与 test_spec_iss 反向校验漂移。
- **混合阻塞处理（DEC-Q2-MIXED-1，承接 B-15/B-16 + Q1 INV-SCH-6）**：同 warp 内 `{active, blocked_on Bx, blocked-on-CTA-bar b, yielded, exited}` lane 共存时：(1) `build_groups` 仍只收集 active 且 `active_mask==true` 的 lane 按裸 PC 分组——blocked（无论阻塞对象是 `Bx` 还是 CTA bar）、yielded、exited lane 一律排除出可调度集（阶段② 既有正确行为，无需改）；(2) lane 阻塞在 CTA barrier 时 `lane_state==blocked` 但用 warp 级 `cta_blocked` 标志 + 记录 bar_id 区分阻塞对象（**不侵入** lane_state 四态、**不写** `blocked_on[lane]` 这个 `Bx` 专用字段）；(3) 两层 fire 谓词各认自己阻塞集（B-16）：`Bx` 的 `barrier_ready` 面对 blocked-on-CTA-bar lane 返回 false=等待（NV-faithful），CTA barrier fire 只数到达 CTA barrier 的 thread。一个 lane 同一时刻只能阻塞在一个同步点（它执行到哪条指令就阻塞在哪），不可能同时是 `Bx` 到达者和 CTA barrier 到达者，等待是唯一正确语义。
- **EXIT 双重清理收口条（统一三路，承接 B-5/B-12/B-13 + Q1 INV-SCH-7 + spec-iss.md:31）**：`EXIT`（guard-true lane）同时 (a) 对 `Bx`——清 `participation_mask` 对应 bit，清空则 `phase=dissolved`（承接 `spec-iss.md`:31，阶段② 语义不变）；(b) 对 **block-wide CTA barrier**——从参与集移除并 `expected_count` 调减 1，调减后立即 `try_fire`（B-12）；(c) 对**显式 count barrier**——不调减（B-13，有意例外，到不齐则 `barrier_deadlock` 兜底）。三者共用『退出即从同步集移除并重检 fire』模式，但 CTA 侧分动态（block-wide）/ 静态（显式 count）两路。复用 `exec_exit`（`native.cpp` single-warp 已有 "EXIT 清 participation + `try_fire_barriers()`" 结构）自然推广到 CTA 层。Q1 INV-SCH-7 三段恢复中的 `try_fire_cta_barriers` 与本条交叉引用，避免 EXIT 语义在 Bx 侧（dissolved）与 CTA 侧（expected 调减 vs 固定）的不对称漂移。
- **CTA barrier 用 `block_state.barriers`**（到达计数），区别于 warp 内 `Bx`（per-PC mask）—— 两套不同机制（§6.1）。

### 8.2 ISA 单一源衔接（schema / 编码层）

现有 ISA（全量 12 指令 + 1 alias，`schema.py` `INSTRUCTIONS`/`ALIASES`）**无任何访存 / atomic / CTA-barrier / fence 指令**；`assembler.py` `_parse_operand` 支持的 operand kind 仅 `register/predicate/sreg/barrier/membermask/immediate`，**缺 `address`/`memory` kind**。阶段③ schema 扩展按 Accel-Sim `ISA_Def` 的 opcode→{memory-space, op-class} 分类粒度组织：

- 新增 memory/atomic/barrier/fence `InstructionSchema`，沿用 `_base_fields`（guard_pred/guard_neg）。
- 新增 **`address`/`memory` operand kind**（`_parse_operand` + `_symbolize_operand` 各加一臂，承载 `base-reg + uniform-reg + imm-offset` 寻址）；新增 memory-space modifier / 字段（沿用 `ModifierSchema`/`ModifierLayout` 机制 + `FieldSchema(kind=...)`），承载 (op, width, sign, space, scope) 多维 modifier。
- sample layout 新增 opcode 须过 `_validate_no_overlap`/`_validate_full_instruction_coverage` 门；`native.cpp instruction_from_word` 须新增 dispatch 臂填操作数。
- atomic 的 op modifier 取 §3 的 10-op 整数集 + 5-op 浮点子集；memory-space 字段区分 global/shared/local。

> **核查缺位说明**：上述 schema 扩展可行性（`address` kind 缺失、多维 modifier 承载）以现有 `schema.py`/`assembler.py` 现状为据；具体新增字段宽度 / opcode 分配 / 多维 modifier 编码形态属阶段③ 实现细节，须在落地时过 codegen 完备 / 无重叠门验证，本调研不预设具体编码（编码私有红线）。

### 8.3 阶段③ 前置依赖：S2R 扩展 + launch 入参扩展（修正 VERIFY [high] Missing）

multi-warp 寻址、CTA barrier 缺省 expected_count、cross-warp reduction corpus **共同硬依赖** special register，但当前 S2R 仅 `choices=("SR_LANEID",)`（`schema.py:183`，已核查）。这是阶段③ 必需的显式前置，**不能继续作为无主的『GROUND A 盘点项』**。

**DEC-MS-PRE-1（S2R 扩展）**：阶段③ S2R 的 `choices` 从 `{SR_LANEID}` 扩到至少 `{SR_LANEID, SR_TID.X/Y/Z, SR_NTID.X/Y/Z, SR_CTAID.X/Y/Z, SR_NCTAID.X/Y/Z, SR_WARPID, SR_NWARPID}`（同步扩 schema `choices` + `SREG_VALUES` 映射 + `S2R.sr` operand `choices` 三处）。值来源：`warp_id = block_state.warps` 下标、`tid = warp_id*32 + lane`、`ntid`/`nctaid` 来自 launch 配置入参。

- **更窄回避接缝**：若阶段③ 暂不补 SR_NTID，CTA barrier `BAR.SYNC` 可**始终用显式 partial 形态 `BAR.SYNC id, N`**（N 为显式线程数），回避缺省全线程数对 SR_NTID 的依赖。但 tid / warp_id 寻址需求无法回避，故 SR_WARPID/SR_NWARPID 仍须补。须在阶段③ 范围确认时与 S2R 协同拍板。

**DEC-MS-PRE-2（launch 入参扩展，统一收口）**：阶段③ 须把以下 launch 接口扩展收拢为单一契约（避免各子主题各扩 launch 导致 pybind 粗粒度边界漂移）：
- `num_warps`（默认 1）+ `warp_sched_order`（**默认 `warp_round_robin`**，fair 序；修订自原 `warp_min_id_first`，与 §5.4 DEC-MS-MW-2 默认值统一，确保 launch 契约与调度契约自洽、且 D 档独立进度测试 VH-14 默认走 fair 序）；
- CTA / grid 维度（`ntid`/`nctaid`，承载 SR 值来源与 CTA barrier 缺省 expected）；
- （const 注入入口归 const 所属阶段，本阶段不做 —— §1.3）。
- 边界调用数仍 O(launch+step)（不随指令 / warp 数增长，VH-10）。

### 8.4 tensor-mem 接缝最小契约（修正 VERIFY Missing）

`block_state.tensor_memory` 字段**存在但本阶段不填字节**（阶段④）。**裁定（DEC-MS-TENSOR-1）**：`tensor` 键**阶段③ 不加入 snapshot `memory` 子树**（与 tensor_memory 不填字节一致），留阶段④ 随 MMA 累加器一并加入。这与 §1.3 const 不进 snapshot 一致，使 `ARCH_STATE_KEYS` 基线变更点清晰：**阶段③ snapshot `memory` 子树仍为 `{global, shared, local}` 三键**，只是从恒空填入字节内容。

### 8.5 红线汇总（违反即与已定决策冲突）

1. 内存**必须** per-space + 4KB 稀疏块 hashmap，**不得**退回单一扁平内存（M1/M2）。
2. atomic RMW **必须**单线程序列化、不可分（M3）；ATM-5 三相豁免须落 spec 正文。
3. sub-word 写**必须** extend-to-32、**无** partial-register merge（M5）。
4. DSMEM **不得**作为新内存空间，**必须** = shared + remote-rank 选择子（S1）。
5. cluster_dim=1 **必须** bit 恒等；跨-CTA **必须** trap、**绝不**伪造远端态（S2）。
6. async-copy/TMA / mbarrier 在阶段③ **只解码 + 功能 no-op**（不实现 tx-count 握手，S3/S5 + §7）。
7. **不得**改 3 字段 Bx snapshot、**不得**扩 4 态 lane 枚举；warp 级 CTA 阻塞用 `cta_blocked`（§8.1）。
8. 内存读写**必须**遵 read→compute→commit 三相；序无关 metamorphic 门（含 `memory` 子集、排除 counters）**必须**继续成立（V1/V2/V3）。
9. 形式化内存一致性**留** litmus 独立工具，**不**进 ISS（V4）；relaxed/fence 不建可见性重排。
10. const / tensor **不**进阶段③ snapshot `memory` 子树（§1.3 / §8.4）；snapshot `memory` 仍三键。

---

## 9. 验证与差分策略

> 承接阶段② 序无关性 metamorphic 框架（`tests/iss/its_corpus.py` + `tests/iss/test_its_metamorphic.py` + `spec-iss.md`）。设计原则：复用 > 扩展 > 新建；任何新机制必须可还原为阶段② 的『四序最终态子集 bit-identical + canonical trap + 具名 mutant 全杀』三件套；不引入 timing 维度。

### 9.1 验证哲学（框架不变量）

- **VP-1（相位保持）**：一条访存指令在一个 step 内遵 read→compute→commit；**禁止同 step 内 lane A 的写被 lane B 在同 step 读到**（否则序无关门因 group 合并粒度差异静默破裂）。atomic/RED 例外见 ATM-5。
- **VP-2（UB = 非序无关豁免）**：无保护内存竞争 = UB，最终态豁免序无关；序无关主门只对 race-free corpus 成立，data-race 成员被 negative control 单独捕获、不混入序无关比较集。
- **VP-3（门的最小扩展面）**：`memory` 与 `uniform_registers` 已在 `ARCH_STATE_KEYS`（its_corpus.py:9-19），但 foundation 下恒空 / 恒零占位。阶段③ 填字节后该门**自动开始对内存态做序无关校验，无需改 `architectural_subset()` 投影逻辑**（foundation AC-6 预留接缝）。本节据此**不新增比较键**，只扩 `memory` 子树判别力。
- **VP-4（state-determined trap → 全序同 trap）**：阶段③ 新增的 `misaligned`/`oob`/`atomic_*`/`barrier_deadlock` 必须是 state-determined（由地址/对齐/参与集纯函数决定），从而四序 trap 一致（例外：collective-placement 须 pre-screen 前置）。`barrier_deadlock` 是纯快照函数（『expected 永不可满足』在 no-runnable 不动点下退化为当前快照 `Σarrived != expected` 且 arrived 已冻结，B-15/INV-SCH-7）→ 进 state-determined trap、全序同 trap。**明确排除出 state-determined trap 体系**：`budget_exhausted`（`max_steps`）与 livelock 检测谓词因 issue 计数 / 步数随调度序不同（一序 200 步终止、另一序未终止），不进序无关门比较（避免 false-fail）；livelock 走独立进度测试 VH-14、超预算判测试失败而非架构 trap。

### 9.2 测试 corpus 清单

corpus 仍由 `KernelBuilder`（its_corpus.py）确定性发 word-list、不入发布包。阶段③ 为其新增 `LDS/STS/LDG/STG/LDL/STL/ATOM*/RED*/BAR` 的 emit 路径。「序无关」列指是否进主门四序比较（race-free + 交换 atomic 才进）。

| ID | corpus 成员 | 覆盖目标 | 序无关 | 关键 oracle |
|---|---|---|---|---|
| MC-1 | warp 内 shared-mem reduction（每 lane 写 `sm[laneid]`，BSYNC 后 lane0 累加） | shared LDS/STS + 同步可见性 | 是 | 终态 `sm` bit-identical;累加 = Σ laneid |
| MC-2 | cross-warp shared-mem reduction（多 warp 写 shared，BAR.SYNC 后归约） | CTA barrier + multi-warp 序无关 | 是 | 终态 shared + 归约 GPR bit-identical |
| MC-3 | CTA barrier 同步（BAR.SYNC arrive-wait 全 warp 到达计数） | `block_state.barriers` 状态机 | 是 | `cta_barriers` 终态 + 越障后 GPR 一致;per-thread 到达、divergent 分批 |
| MC-4 | 整数 atomic 计数器（N lane ATOM.ADD 1 同 global 地址） | atomic RMW 串行化 | 是 | 终值 == 参与 lane 数 |
| MC-5 | 整数 atomic reduction（ATOM.MIN/MAX/AND/OR/XOR + RED） | 非 ADD 原子 + RED 无返回 | 是 | 终值 == 单线程序列化 fold |
| MC-6 | CAS spin-lock / 互斥临界区 | CAS + 前向进度 | **否（D 档独立进度测试 VH-14）** | fair 序下有限步终止 + 临界区互斥可观测;无 false-deadlock |
| MC-7 | producer-consumer via shared mem | warp 间 flag 通信 + barrier | **否（D 档独立进度测试 VH-14）** | fair 序下有限步终止 + consumer 读到 producer 数据 |
| MC-8 | local memory 私有性（每 lane LDL/STL 自己 slot） | per-lane local | 是 | lane i 的 local 只含 lane i 写入 |
| MC-9 | generic addressing（同寄存器地址经 generic→shared/global/local） | 窗口推断 + cvta | 是 | 命中预期 space;无跨 space 别名 |
| MC-10 | sub-word LD/ST（LD.U8/.S8/.U16/.S16 extend, ST.U8/.U16 写低位） | sub-word extend + byte-mask | 是(conformance) | extend 语义 bit 精确 |
| MC-11 | byte-masked / 部分 lane 谓词化 store | byte-mask 压制写 | 是(conformance) | mask=0 字节保持原值 |
| MC-12 | gather/scatter（每 lane 异地址 LDG/STG） | 32-地址 gather/scatter | 是(conformance) | 终态稀疏块逐字节一致 |
| MC-N1 | **negative**: misaligned（addr % width ≠ 0） | misaligned trap | 否(trap) | `memory`/`misaligned`(全序一致) |
| MC-N2 | **negative**: OOB / 跨-CTA shared | oob / unsupported trap | 否(trap) | `oob` 或 `unsupported`(S2) |
| MC-N3 | **negative**: data-race（无屏障 lane 间冲突写） | race 检测 | 否(滤出/标记) | pre-screen 拒 或 双序 diff 标记 |
| MC-N4 | **negative**: barrier deadlock（部分 warp 未到 barrier 即走开） | barrier_deadlock trap | 否(trap) | `synchronization`/`barrier_deadlock` |
| MC-N5 | **order-sensitive 基线**: 多 warp ATOM.EXCH/CAS 同址 | 顺序敏感 op | 否(单序基线) | 固定序两次运行 bit-exact;不跨序断言 |
| MC-13 | divergent if/else 两支同 bar_id BAR.SYNC（per-thread 跨 call site 计数） | per-thread 到达 + 跨 call site 累加（B-14） | 是 | 两支到达汇入同一 arrived_count;全员到齐后各自从 next PC 恢复 |
| MC-14 | BAR.ARV producer + BAR.SYNC consumer split-barrier | arrive/wait 双模式（B-17） | 是 | split-barrier 复用正确;consumer 等到 producer arrive |
| MC-N6 | **negative**: 单支 barrier（divergent 只一支有 BAR,另一支不到达不退出） | barrier_deadlock（D-c） | 否(trap) | `synchronization`/`barrier_deadlock` |
| MC-N7 | **negative**: 显式 count barrier 期望 N 但实际到达 < N | barrier_deadlock（D-d） | 否(trap) | `synchronization`/`barrier_deadlock` |

corpus 分层标签沿用阶段② `tags` 并新增 `progress_test`（与 `base`/`north_star`/`negative_control`/`order_sensitive` 并列）：MC-1..3 标 `base`，**MC-4/MC-5 标 `north_star`，MC-6/MC-7 改标 `progress_test`**（从原 north_star 迁出，归 D 档独立进度测试 VH-14，与 §5.5 B-OI-3 对齐、消除 §9.2 与 §5.5 矛盾），MC-13/MC-14 标 `north_star`，MC-8..12 标 `base`/`conformance`，MC-N1..4 + MC-N6/MC-N7 标 `negative_control`，MC-N5 标 `order_sensitive`。

**corpus → 四档映射（INV-GATE-DOMAIN-1 准入依据，统一各成员归属）**：

| 档 | 程序类 | 成员 | 门 |
|---|---|---|---|
| **A** | barrier-DRF | MC-1, MC-2, MC-3, MC-13, MC-14 | bit-equality 主门 VH-2（含 MC-13/MC-14 经 VH-3 divergent 子断言） |
| **B** | 交换 atomic | MC-4, MC-5 | bit-equality 主门 VH-2 |
| **C** | 非交换 atomic | MC-N5 | 确定性基线 VH-5（固定单序 bit-exact、不跨序断言） |
| **D** | lock/spin/producer-consumer | MC-6, MC-7 | 终止性 under fair 序 + 语义不变式，独立进度测试 VH-14（不做 bit-equality） |
| **conformance** | sub-word/byte-mask/gather | MC-8..12 | 单序 bit 精确差分 CF-1..5（与序无关门正交） |
| **negative** | UB/死锁 | MC-N1..4, MC-N6, MC-N7 | state-determined trap，四序同 trap |

### 9.3 架构状态子集（承接 ARCH_STATE_KEYS）

阶段② 比较子集 = 9 键（`active_mask,pc,lane_state,vgpr,predicates,uniform_registers,memory,bx,trap`），`counters` 排除。阶段③：

- **SS-1（memory 各 space）**：`memory` 子树由三键恒空（`{global,shared,local}`）**扩为含字节内容**（每 space `{block_id(hex): [byte,...]}` 稀疏映射）。**snapshot `memory` 子树仍三键**（const/tensor 不加入，§1.3/§8.4）。`state_diff`（`native.cpp` 递归 JSON-path）已对 dict key-union 递归 + 缺键填 `none`，**稀疏内存差分开箱即用**，无需改 diff 工具。
  - **INV-MEM-SER-1（确定序列化）**：同一 space 的块按 `block_id` 升序、块内按字节偏移升序输出，避免 hashmap 迭代序污染 bit-identical 判定。
  - **INV-MEM-SER-2（稀疏）**：只输出被写过且非全零的块；untouched / 写后清零区天然一致（MS-1 序列化对应）。
- **SS-2（CTA barrier 终态 —— 冻结契约面变更点，修正 VERIFY [medium]）**：新增顶层键 `cta_barriers`（16 槽 × `{phase, arrived_count, expected_count, phase_parity}` 四纯量）进序无关比较子集。**这触碰 spec-iss.md 冻结的 9 键面**，须把『新增顶层键进比较子集』标记为变更点，要求阶段③ **首步同步三处**：`spec-iss.md` Grouping And Scheduling 键清单、`its_corpus.py ARCH_STATE_KEYS`、`tests/structure/test_spec_iss.py` 反向校验。
  - **`arrived_thread_set` 不进 `ARCH_STATE_KEYS`**：`arrived_thread_set`（B-11，per-warp 32-bit lane bitmask 数组）仅 debug / 去重用，quiescent 终态全空（`arrived_count` 全 0）、无判别力，**不进**序无关比较子集（同 counters 排除逻辑）；只 `cta_barriers` 的 `{phase, arrived_count, expected_count, phase_parity}` 四纯量进比较子集。若需验证『去重正确』须单序 1:1 diff。
  - **比较点取 quiescent 终态**（所有 warp 退出或 trap 时，`arrived_count` 全 0、`phase_parity` 收敛）；**中间到达计数明确排除**（不同 warp 序下 Gathering 计数瞬时不同，纳入会主门 false-fail）。snapshot 序列化 `arrived_thread_set`（debug 用）按 (warp_id 升序, lane 升序) 确定输出，避免迭代序污染 bit-identical 判定。
  - **阶段③ spec-iss 主门子集最终键清单（统一锚点，承接 Q1/Q2 落地）**：每 warp 9 键（`active_mask,pc,lane_state,vgpr,predicates,uniform_registers,memory,bx,trap`）+ 顶层 `cta_barriers`（四纯量）；`counters`（含 `mem_ops`）与 `arrived_thread_set` 排除。同步落入 spec-iss 的新调度 / 同步术语见 §9.4 反向校验项清单。
- **SS-3（mbarrier 不进，承接 §7）**：阶段③ mbarrier 不建模 → 无 `mbarriers` 键。落地（阶段④）时若驻 shared 则被 `memory.shared` 自动覆盖、无需新顶层键。
- **SS-X（counters 含 mem_ops 排除）**：继续排除全部 counters，含新激活的 `mem_ops`（偶然 group/warp 交错合法改变访存次数）；`mem_ops` 仍做**单序 1:1 diff**（foundation AC-6 计数器导出契约），用于回归而非序无关门。timing/coalescing 派生量一律不进 snapshot。

### 9.4 trap 分类：统一新 kind（修正 VERIFY [high] completeness）

现有 trap kinds：`decode`/`execute`/`convergence`/`max_steps`。多个子主题对新 trap kind 各自命名冲突，且现有 `test_spec_iss.py` 反向校验门只 grep `set_convergence_trap` 与 `set_trap("convergence",...)` 两种 pattern —— 任何 `memory`/`synchronization` kind 的新 reason **都不会被该门捕获 → 新 reason 可不文档化而 CI 仍绿**，直接破坏反向校验契约。

**DEC-MS-TR-1（统一 trap kind 集 + 反向校验扩展）**：阶段③ 裁定统一两个新 kind：

- **`memory` kind**：承载访存类 UB。
- **`synchronization` kind**：承载 CTA barrier 类（`barrier_deadlock` 归 `synchronization` 覆盖 multi-warp/CTA 层；**单 warp 纯 `Bx` 死锁保留 `convergence`/`deadlock_no_progress`**，二者**并存**——非替换，向后兼容 VH-1 + 现 `test_spec_iss` convergence 抓取，承接 INV-SCH-7 / spec-iss.md:50），以及阶段④ mbarrier reasons。

并把『扩展 `test_spec_iss.py` 正则同时抓 `set_trap("memory",...)`、`set_trap("synchronization",...)` 与既有 `set_convergence_trap`/`set_trap("convergence",...)`（三 kind 全覆盖）』列为阶段③ **首批结构任务**（与 ARCH_STATE_KEYS 基线更新、SS-2 三处同步并列），否则反向校验门对阶段③ 全部新 reason 失效。每个新 reason 须在 spec-iss 风格契约以反引号文档化、被 test_spec_iss 反向校验。`detail` 沿用 `{trap_reason, pc, ...}` schema（适用时加 `address, space, width, thread_id, bar_id`）。

- **test_spec_iss 反向校验项统一清单（合并 Q1/Q2 去重，单一 required 集）**：
  - **required_reasons**（synchronization kind 新增 / 重命名）：`barrier_deadlock`、`barrier_count_not_warp_multiple`（替换 `cta_barrier_count_mismatch`）、`barrier_id_out_of_range`（替换 `cta_barrier_id_out_of_range`）、`cta_barrier_arrive_overuse`（若 emit）；memory kind 见 §9.4 表；convergence kind 保留 `deadlock_no_progress`（单 warp 退化）。
  - **required_terms**（spec-iss 风格契约须文档化的新调度 / 同步术语）：`warp_round_robin`、`warp_min_id_first`、`warp_max_id_first`、`weak fairness`（unconditional）、`livelock`、`forward progress`、`run-to-block`、`per-thread arrival`、`cta_blocked`、`arrived_thread_set`。
  - **当前缺口**：`test_spec_iss.py:14-15` 只抓 `convergence`，须扩到同抓三 kind 并断言上述 required_reasons + required_terms。

阶段③ trap reason 表（state-determined）：

| kind | reason | 触发条件 | detail | 依据 |
|---|---|---|---|---|
| `memory` | `misaligned_address` | addr % width ≠ 0（width∈{2,4,8,16}；width=1 不约束） | address,width,space,thread_id | M6;PTX 自然对齐 |
| `memory` | `shared_oob` | shared off ≥ per-CTA 容量 | address,space,bound,thread_id | space 边界 |
| `memory` | `local_oob` | local off ≥ per-thread 容量 | address,space,bound,thread_id | space 边界 |
| `memory` | `data_race` | 同步 epoch 内多 lane 同字节冲突且无 atomic/barrier（运行时兜底，§9.6 可选） | address,space,racing_lanes,access_kinds | VP-2/V7 |
| `memory` | `unsupported_space_access` | 跨-CTA shared（rank≠self）/ 未实现 space | address,requested_rank,space | S2 红线 |
| `memory` | `generic_resolve_failure` | generic 地址无法解析到合法 backing space | address,thread_id | ADDR-2 |
| `memory` | `atomic_on_local_unsupported` | atomic/RED on local（per-lane 私有无语义） | address,op | ATM-4 |
| `memory` | `atomic_on_readonly_space` | atomic/RED on const | address,op | ATM-4 |
| `memory` | `atomic_misaligned` | atomic 地址未按 op 宽度对齐 | address,width,op | M6 |
| `memory` | `atomic_unsupported_op` | op 不在 §3 支持集 / float 用 and/or/xor/inc/dec | op,address,space | ATM-2 |
| `memory` | `red_has_destination` | RED 编码出现非 RZ dst（RED 不写寄存器） | op,address | ATM-1 |
| `synchronization` | `barrier_deadlock` | 三段恢复后无 runnable 且无 bar_id 可 fire（快照 `Σarrived != expected` 且 arrived 已冻结，B-15）；覆盖 D-a/D-b/D-c/D-d | bar_id,arrived_thread_set,expected_count | B-15;multi-warp/CTA 层（单 warp 纯 Bx 死锁仍 emit convergence/deadlock_no_progress，并存） |
| `synchronization` | `barrier_count_not_warp_multiple` | partial count 非 32 倍数 / 同 phase 不同 expected（运行期；编码期可检者 encode 期拒绝） | bar_id,arrived,expected | B-3（替换原 `cta_barrier_count_mismatch`） |
| `synchronization` | `barrier_id_out_of_range` | bar_id 越界 | bar_id | B-1（替换原 `cta_barrier_id_out_of_range`） |
| `synchronization` | `cta_barrier_arrive_overuse`（可选 debug） | 同 warp reset 前对同 bar_id 发多于预期 BAR.ARV 后跟任何 BAR（unpredictable，PTX A6） | bar_id,warp_id | B-17（若 emit） |

> 编码期可检的（奇数寄存器对、非 4-对齐 .128、越界 imm offset）**必须在 assemble/encode 期拒绝**（非运行期 trap），承接 foundation `plan-foundation.md:44`。

### 9.5 conformance 差分（单序 bit 精确，与序无关门正交）

用独立差分 oracle 逐字节比对：

- **CF-1（sub-word extend）**：`LD.U8/.S8/.U16/.S16` extend-to-32（sign/zero 按签名）、**无 partial-register merge**。oracle = `int32_t(int8_t(byte))`(S8) / `uint32_t(uint8_t(byte))`(U8)。**关键负向**：先写 GPR 满 1 再 LD.U8，断言高 24 位清零（拒 x86 AL 合并 hazard）。
- **CF-2（byte-masked store）**：`ST.U8/.U16` 只写低位；mask=0 lane 不写。**关键负向**：mask=0 lane 对应字节终态 == 写前值。
- **CF-3（gather/scatter）**：32 lane 异地址 LDG/STG 逐 lane 地址→块→字节比对；同址 scatter 冲突无 atomic → 落入 data_race，有保护 → atomic oracle。利用 `state_diff` 稀疏 dict 做最小局部化报告（对称 foundation AC-6『篡改一字节 → 一处最小 diff』）。
- **CF-4（64/128-bit 对齐对）**：`.64` 偶对齐对小端、`.128` 4-对齐组。**关键负向**：非对齐寄存器编号 / 非对齐地址 → `misaligned_address`/`atomic_misaligned` trap。
- **CF-5（packed SIMD-within-word）**：packed load 入 GPR 后是 4×INT8/2×INT16/2×f16 小端 lane；内存搬运视为普通 32-bit word copy（SIMD 解释在 ALU、不在访存层）。仅比对 word 字节布局，零额外 oracle。
- **AR-oracle（atomic 序列化 oracle）**：整数 atomic 终值 = 按 INV-1 pinned (warp,lane) 升序逐个 fold 的参考值（对照 `cmodel_br100 LSCCache::doAtomic` 的 op 分派）；交换结合 op 任意串行序同值 → oracle 唯一；CAS 成功/失败两路径返回值差分。

### 9.6 data-race 检测 soundness 边界（修正 VERIFY Missing，统一两子主题）

统一为：阶段③ 对 data-race 的处理 =

1. **竞争 = UB、确定执行不 hang/不 silent-wrong**（功能态，必做）。
2. **结构显然的 static pre-screen 拒**（同一字面地址、同一 kernel、无任何 barrier 指令介入）—— 复用阶段② `pre_screen` 单边滤器，**单边 sound（不漏杀 well-formed）**；一般 memory race 不可判定（需别名分析），故只对结构显然者保守 pre-screen。
3. **双序 memory diff 标记**（HOOK-RACE-2，零新增基础设施）：对疑似 racy 成员跑两个调度序、diff `memory` 子集；不同 = 确证非序无关，排除出 well-formed corpus 或标 negative-control。直接复用四序门 + `state_diff`。

**『运行时 per-byte epoch shadow + `data_race` trap』降为可选 debug hook**（HOOK-RACE-1，非功能态、非 Lower Bound 必需）：一般 race 的运行时检测需 per-byte 影子 + happens-before/epoch 时钟（较重设施），不作阶段③ 必需，避免把重型 race detector 前置。`memory`/`data_race` trap reason 因此是**可选**（若降为可选 hook 则不进 native 必需 reason 集 / test_spec_iss 反向校验必查项）；spec 须明确其是否 emit。`epoch` 粒度（一个 step vs 一次 barrier 区间）：先以 **barrier/atomic 为 epoch 分界**（single-CTA、barrier-epoch 保守版），multi-CTA 留 FUT。

### 9.7 验证点与 mutation 全杀

- **VH-1（单 warp 退化恒等）**：`warps.size()==1` 的 CTA snapshot 其 `warps[0]` 视图与阶段② single-warp snapshot **bit-identical**，现有 foundation 测试在 multi-warp 引擎下 0 改动通过（回归基线）。
- **VH-2（warp 调度序无关主门，收紧适用域）**：**仅 A 档（barrier-DRF）+ B 档（交换 atomic）** corpus 成员在 `warp_sched_order ∈ {round_robin(默认),min_id,max_id} × SchedOrder ∈ {四序}` 组合（至少对角线 + 关键叉积）上最终 CTA 架构态子集（含 `memory`、`cta_barriers` 终态）bit-identical（P-OI 工程门）。默认 `warp_sched_order` = `warp_round_robin`（fair 序）；`min_id`/`max_id` 仍进主门叉积（DRF/终止程序同终态）。**C 档（非交换 atomic）、D 档（lock/spin/PC）不进 VH-2**（C→VH-5、D→VH-14）。
- **VH-3（CTA barrier 跨 warp 重聚，扩展 per-thread divergent 到达）**：两 warp 经 `BAR.SYNC` 会合，到齐前任一 warp 不越障、到齐后同步推进、release 后 `cta_barriers` 复位。**新增子断言（判别 Volta+ per-thread vs pre-Volta per-warp）**：divergent warp 内部分 lane 走 if 支 `BAR.SYNC`、部分走 else 支 `BAR.SYNC`（同 bar_id），验证两支到达汇入同一 `arrived_count` 且全员到齐后各自从 next PC 恢复（B-14）——per-warp 模型会在一支到达时即整 warp 算到达 → 提前 fire 或误判，被此子断言杀。
- **VH-4（atomic 序列化差分）**：整数 atomic corpus 实测终态 memory/GPR 与 AR-oracle 逐字节比对；交换结合 op 四序 + 多 warp 序 bit-identical（INV-3a/INV-5）；ATOM 旧值四序一致（INV-3b）。
- **VH-5（顺序敏感 op 确定性基线）**：MC-N5（多 warp ATOM.EXCH/CAS）对固定序两次运行 bit-exact（确定性），**不跨序断言**（INV-5a）；验证该类被正确标注排除出 P-OI 主门、避免 false-fail。
- **VH-6（EXIT × CTA barrier 负向门，拆两子门）**：
  - **VH-6a（block-wide 动态扣减）**：一 warp 提前 EXIT（从未到 barrier）其余到 barrier → `expected` 动态扣减（B-12）→ 不死锁。**可落地性注**：VH-6a 硬依赖 SR_NTID 算 block-wide 缺省全线程数（B-12）；已裁决阶段③ 补全 SR_NTID（DEC-MS-PRE-1 全量落地），VH-6a 可建。
  - **VH-6b（显式 count 静态固定）**：被 N 计入的线程 EXIT 不到达 → `expected` 固定 N（B-13）→ `arrived` 永不到 N → 确定性 `barrier_deadlock` trap（mem/sync 负向误用门，FUT-2 line 137 + V7）。
- **VH-7（fence no-op 不变性）**：任意 well-formed corpus 任意位置插入 `MEMBAR`/`FENCE`，断言最终架构态逐 bit 不变 + 解码器正确提取 scope/order 操作数 round-trip；插入/删除 fence 仅可改 counters（排除）。
- **VH-8（局部化 diff）**：`state_diff` 对篡改单字节内存 → 报一处最小局部化差异（path=`memory/<space>/<addr>`），承接 foundation AC-6 推迟至 FUT-2 的字节级 diff。
- **VH-9（序无关比较子集精确性）**：`counters`（含 `mem_ops`）排除；`memory` + `cta_barriers` 终态进子集（承接 spec-iss + V3）。
- **VH-10（边界调用数不随 warp 数增长）**：`boundary_calls()` 在 `num_warps` 变化下仍 O(launch+step)。
- **VH-11（spec 反向校验）**：`test_spec_iss.py` 扩展正则抓 `set_trap("memory"/"synchronization",...)`，断言每新 reason 在 spec-iss 反引号文档化 + 术语（`memory_space`/`CTA barrier`/`atomic`/`data_race`/`misaligned`）出现。
- **VH-12（mutation kill）**：沿用阶段② named-mutant；新增 `drop_cta_barrier_arrive`（漏到达）、`wrong_expected_set`（退休 warp 未移出 expected → 假死锁）、`non_atomic_rmw`（atomic 退化非原子 LD-ADD-ST → 序相关终值 / lost-update）、`wrong_extend`（sub-word merge 而非 extend）、`byte_mask_ignored`（mask=0 也写）、`warp_sched_bias`（某序偏置某 warp）。**Q2 barrier 粒度具名 mutant**：`per_warp_arrival`（per-thread 计数退化为 per-warp 整 warp 算一次 → divergent 到达提前 fire，被 MC-13/VH-3 杀）、`static_expected_no_exit_decrement`（block-wide expected 不随 EXIT 扣减 → VH-6a 假死锁）、`cross_callsite_not_counted`（同 bar_id 不同 call site 各起独立计数 → MC-13 提前/永不 fire）、`bx_fires_on_cta_blocked_lane`（`barrier_ready` 错误把 blocked-on-CTA-bar lane 当 Bx 到达 → VH-13 错误 fire）。**Q1 调度公平性具名 mutant**：`warp_sched_unfair`（某序固定偏置某 warp 永不让出 → 破坏 fair permutation，在 D 档 spin corpus 上该序死锁而 fair 序终止，被 VH-14 杀）、`skip_yielded_promotion`（无 runnable 时不提升 yielded → 假死锁）。门有效性以**具名 mutant 全杀**量化（非百分比）。

- **VH-13（混合阻塞死锁矩阵门，Q2）**：构造 corpus 成员覆盖交叉死锁 D-a（Bx 等 bar）/ D-b（bar 等 Bx），验证 (1) `Bx` 面对 blocked-on-CTA-bar lane 时不误 fire（`barrier_ready` 返回 false，B-16），(2) 全 blocked 无 runnable 时确定性 `barrier_deadlock` trap（B-15），(3) 四序（`warp_sched_order × SchedOrder`）到同一 trap（纯快照函数，VP-4）。这是 Q2 混合阻塞矩阵回归门。
- **VH-14（D 档独立进度测试，Q1；非序无关门）**：对 D 档（MC-6 CAS spin-lock、MC-7 producer-consumer）在 fair 序（`warp_round_robin`）下设步数预算，断言 (a) 有限步内终止、(b) 临界区互斥可观测 / consumer 读到 producer 数据等语义不变式；**超预算判测试失败而非架构 trap**。这是 deadlock vs livelock 的区分载体——livelock 谓词（依赖步数预算）不进 state-determined trap 体系（VP-4）。对齐 `plan-its.md` OD-5。具体步数预算值与超限报告形式（测试失败的呈现 / 可选诊断 hook）已裁决转 `plan-memsync.md` 定。
- **VH-15（调度粒度回归，Q1）**：断言 multi-warp step 是指令级交错（每 macro-step 推进恰一个 `step_one_group`），即 `warp_instructions/step` 与 group 数对齐；负向 mutant `warp_run_to_block`（跑到阻塞才切）须被杀（在含 spin 的 D 档 corpus 上该 mutant 死锁或超步数预算）。
- **VH-16（fence no-op 跨 warp 层不变性，Q1；扩展 VH-7 到 multi-warp）**：在 A/B 档 well-formed corpus 任意位置插入 `MEMBAR`/`FENCE`，断言所有 fair schedule 最终架构态逐 bit 不变（验证 fence soundness 链 §4.2 的工程后果：SC 执行 ⊆ weak 合法执行集 → 删/插 fence 不改 SC 执行）。

---

## 10. 关键参考

> 本地文件 + 找到的最佳 web 源；标注适用条件与不确定度。

**本地（契约 / 代码 / ground-truth）**：
- `docs/implement/ISS/research-notes.md` §2（内存模型 M1/M2、atomic 串行化 M3、async-copy eager + 控制段 no-op、功能/timing 分离）、§4（`cluster_state/block_state/warp_state` 数据结构蓝图、GPR 无类型 word + sub-word extend-to-32 + 64b 偶对齐对/128b 4-对齐组/packed M5/M6/M7）、§6（MMA FDA + fence/commit/wait no-op）、§9（数值契约 Tier-1/Tier-2、计数器 1:1 diff）、§10/§11（`memory_space.h`/`reg_cell.h`/`block_state.h`/`warp_state.h` 落位）—— 本文承接的内存/同步/数值契约源。
- `docs/implement/ISS/research-its.md` §4/§5/§7（warp 内 `Bx` FSM、read→compute→commit 三相、序无关 metamorphic 主门、≥3 具名确定序、mutation-kill 方法学）—— 本文 multi-warp 调度与验证门的同构模板。
- `docs/implement/ISS/spec.md` §2/§5/§7（三空间 global/shared/local、阶段②③ 主门、GPR 数据类型决策、cluster/DSMEM 延后 S1/S2、Tier-1/Tier-2 容差 V5）—— scope 与红线源。
- `docs/implement/ISS/plan-foundation.md` FUT-2（line 137 阶段③ 范围 = memory_space + CTA barrier + atomics 单线程序列化 + 负向测试）、AC-6（line 139/165 内存空间存在但空、`mem-ops==0`、字节级 diff 推迟至 FUT-2）、FUT-5（line 149 形式化内存一致性 litmus 独立工具）、line 278（cluster_dim=1 恒等不实现跨-CTA）—— 阶段③ 定义与接缝。
- `docs/implement/ISS/spec-iss.md`（9-key 序无关比较子集、`counters` 排除、Barrier State 3 字段冻结、lane_state 四态冻结、Convergence Traps reason 表 + detail schema、Collectives lane-id 升序定序 line 65、Static Pre-Screen）—— 序无关门契约与 trap 框架基准，不可矛盾。
- `docs/implement/ISS/plan-its.md`（read→compute→commit line 147/394、counters 非序无关排除 line 218、3 字段/4 态冻结 line 115/116、具名 mutant 全杀 line 58、结构化 trap 原则 line 449、OD-5 spin 前向进度 deferred line 500）—— 阶段② 框架不变量。
- `iss/binding/native.cpp`（snapshot `memory={global,shared,local}` 三键恒空 :577-581、`counters.mem_ops` 恒 0 :47/:540、`read_word`/`write_gpr` 裸 uint32 :724-760、`set_trap`/`set_convergence_trap` 框架 :1195-1214、`state_diff` 稀疏 dict key-union 递归 :1235-1293、pybind 边界 :1297-1319）—— foundation 现状填充点。
- `tests/iss/its_corpus.py:9-21`（`ARCH_STATE_KEYS` 9 键 + `SCHED_ORDERS` 四序 + `pre_screen` 单边滤器）、`tests/iss/test_its_metamorphic.py:29-39/98-107`（四序断言 + 具名 mutant 全杀）、`tests/structure/test_spec_iss.py:12-23/26-47`（trap reason / 术语反向校验门）、`tests/iss/test_native.py:117,520`（snapshot memory 三键硬断言基线）、`isa/currygpu/isa/schema.py:183`（S2R `choices=("SR_LANEID",)`）、`assembler.py:223-242`（`_parse_operand` kind 集，缺 address/memory）—— 须同步更新的测试 / schema 基线。

**本地参考资料（语义 / 一手实现 / 编码，标注核查状态）**：
- `nv_patent/sm/mmu.md`（US8271763B2：统一 per-thread 地址空间非平面而带窗口，Local/Shared Window → fall-through Global）—— generic/cvta 窗口推断模型权威依据（适用：功能语义，窗口基址数值属 layout 私有）。
- `nv_patent/sm/load_store_unit.md`（US8271763B2：LSU 前段判定 local/shared/global/constant 地址空间）；`nv_patent/sm/shared_memory_local.md`（shared per-CTA 共享 vs local per-lane 私有）；`nv_patent/sm/memory_barrier.md`（memory barrier=visibility vs convergence/execution barrier=线程到齐；cp.async completion≠visibility；memory sync domains 留 timing）；`nv_patent/sm/async_barrier.md`（三类等待对象 scoreboard/convergence/transaction；US20230289242A1 transaction barrier = thread + transaction arrival、expectation 顺序宽松）；`nv_patent/cache_coherence/ordered_atomics.md`（US11016802B2 L2 ordered-atomic = microarchitecture，curryGPU 拒建机制、借确定序语义后果）—— 语义边界依据。
- `cmodel_br100/model/spc/cu/lsc/LSCCache.cpp:800-835`（`doAtomic` 8-op switch ATADD/ATMIN/ATMAX/ATAND/ATOR/ATXOR/ATSWP/ATCAS）+ `:837+`（`doGsmAtomic` 按 width/dataType 分派）—— atomic RMW 内核与整数 op 骨架参考（适用：BR100 子集，INC/DEC 缺失是实现裁剪、目标 op 集以 sm100a 为准；shared 64-bit 限制 mem_dem<3 是 BR100 特性非 sm100a 约束）。
- `cmodel_br100/model/spc/cu/srp/SRPImpl.cpp` + `SRPImpl.h`（`barCounter[gsmID][barID]++`、ALL/COUNTER release 判据、KICK reset）—— **CTA barrier 到达计数 FSM 一手实现参考**（`swc_warp.h` BarSlot/Bar_Group 为 debug-only 声明、未实例化，仅作枚举命名参考）。
- `sm100a/output/{BAR,MEMBAR,FENCE,ATOM,ATOMG,ATOMS,REDG,REDUX,LDG,STG,LDS,STS,LDL,STL,SYNCS,ARRIVES,LDGSTS,DEPBAR,ACQBULK}.html` + `isa.json`（BAR.SYNC/ARV + thread-count、BAR.RED.POPC/AND/OR、MEMBAR scope 四级、ATOM/RED 整数 op 含 INC/DEC、ATOMS 支持 64/128、寻址 `[R+UR+imm]`/`desc[]`、宽度 .U8/.S8/.U16/.S16/.64/.128/.256、SYNCS.TRANS64/ARRIVES ARVCNT/TRANSCNT）—— 编码 modifier / 寻址形态实测（适用：curryGPU 自定义编码，语义/modifier 表面对齐）。

**Web（标注适用条件 / 不确定度）**：
- GPGPU-Sim `cuda-sim/memory.h`（`MEM_BLOCK_SIZE (4*1024)`、`memory_space_impl<BSIZE>` 稀疏 page map）—— M2 的逐字外部背书（仅 cuda-sim 功能层；timing 层完全不适用）。https://gpgpu-sim.org/manual/
- PTX ISA §8 Memory Consistency Model（scope {cta,cluster,gpu,sys}、morally strong、data-race 给出 weak-ordering 而非 UB、Sequential Consistency Per Location）—— 一致性公理与 race 语义层次（适用：PTX ISA 层 race=weak；CUDA C++ 层才 race=UB）。https://docs.nvidia.com/cuda/parallel-thread-execution/
- Lustig, Sahasrabuddhe, Giroux, "A Formal Analysis of the NVIDIA PTX Memory Consistency Model", ASPLOS 2019（Scoped-RC11、SC-for-scoped-DRF、litmus 全内存初始化为 0）—— V1 序无关门形式化背书 + V4 形式化留 litmus（适用：6-公理逐字名以论文正文为准，本文据 Scoped-RC11 重构、非已核查 verbatim）。
- M. Habermaier & A. Knapp, "On the Correctness of the SIMT Execution Model of GPUs", ESOP 2012, LNCS 7211（SIMT↔交错多线程 simulation、min-PC unfair、序无关仅对终止/无竞争程序成立）—— P-OI 主门理论主依据（warp 调度 = 选定交错，合规程序 confluent；B-OI-3 边界采信）。本地转录 /tmp/hk.txt:781,810,822,891（核查：同行评审文献，证据优先级 3，非 NV 官方）。**注**：§5.1 lowest-PC unfairness 原文归给 **Collange**（/tmp/hk.txt:891 "Collange's lowest program counter scheduling policy makes the overall mechanism unfair"，非 H-K 自创）；Program 2/3 非终止机制是 pre-Volta IPDOM/reconvergence-stack（Volta+ ITS 已取代），对 ISS `min_pc_first` 仅作类比论证、非 1:1 复现。
- Dubey et al., "Equivalence Checking of ML GPU Kernels", arXiv:2511.12638（2025-11，结构化 CTA 类下 sound+complete，核心 confluence）—— P-OI 可机械化形式佐证（非硬依赖）。

**本次新增引用（Q1 调度粒度与公平性 / Q2 CTA barrier，标注核查状态）**：
- **[PTX ISA 8.5 §9.7.12.1（印刷页274-275），本地 PDF（`/home/yanggl/.claude/projects/-home-yanggl-code-curryGPU/.../webfetch-1781051608115-peal23.pdf`；源 docs.nvidia.com/cuda/pdf/ptx_isa_8.5.pdf），已核实-PDF]**：barrier{.cta} per-thread/non-exited 到达（A2 "wait for all non-exited threads"）、arrive 不阻塞（A2）、reinit 复用（A3）、count warp 倍数（A1/A7）、`.aligned` 契约（A4）、bar.sync≡.aligned（A8）、sm_6x per-warp-convergence 限定（A5 第2点）、same barrier name 多 call site（A6）—— DEC-Q2-ARRIVAL-1/DIVERGENT-1/ALIGNED-1/COUNT-1/ARV-1、B-6/B-13/B-14/B-17 最强一手依据。
- **[PTX ISA 8.5 §8.10.5（印刷页90）+ §9.7.12.4（印刷页279-280），本地 PDF 同上，已核实-PDF]**：§8.10.5 "each program slice of overlapping pairwise morally strong operations is strictly sequentially-consistent"（DRF⟹SC axiom）；§9.7.12.4 "The fence instruction establishes an ordering ... fence.sc is a slower fence that can restore sequential consistency ... On sm_70 and higher membar is a synonym for fence.sc"（fence 唯一架构效果是 ordering）—— DEC-Q1-FENCE-SOUNDNESS / INV-FENCE-SOUND-1 链条 (b)(c) 环、completeness-on-DRF 依据。
- **[Volta Architecture Whitepaper WP-08608-001_v1.1, p.27/p.29，本地 PDF（webfetch-1780797781429-e7beuu.pdf / webfetch-1781050958557-vwd9uz.pdf；源 images.nvidia.com/content/volta-architecture/pdf/volta-architecture-whitepaper.pdf），已核实-PDF]**：p.27 "maintains execution state per thread, including a program counter"（Volta+ per-thread PC，DEC-Q1-GRANULARITY 证据1）；p.27 "yield execution of any thread to allow one thread to wait for data to be produced by another"（YIELD，DEC-MS-YIELD-CROSS-1）；p.29 Starvation-Free "another thread T1 in the same warp can successfully wait for the lock to become available without impeding the progress of thread T0"（否决粒度 c，作用域 intra-warp 限定）—— 适用 Volta GV100 sm_70+。
- **[ISO C++ intro.progress / P0299（eel.is/c++draft/intro.progress），ISO 标准 verbatim 已核实；NV 对接二手（Olivier Giroux CppCon / NVIDIA Developer Forums）待补一手]**："once this thread has executed a step, it provides concurrent forward progress guarantees"—— NV Volta+ conditional parallel-forward-progress 精确形式（once-scheduled 后 eventually-scheduled）；ISS 取更强 unconditional weak fairness、标注强于此。
- **[Lustig, Sahasrabuddhe, Giroux, "A Formal Analysis of the NVIDIA PTX Memory Consistency Model", ASPLOS 2019 (DOI 10.1145/3297858.3304043)，论文 verbatim（多源搜索）+ PTX §8 PDF 互证]**："PTX does not require data race freedom"—— 重大修正：racy 程序非全局 UB，ISS 不可把 race 当 trap；ISS soundness = SC outcome ∈ NV 合法集、completeness 仅 DRF。
- **[Sorensen, Evrard, Donaldson, "GPU Schedulers: How Fair Is Fair Enough", CONCUR 2018 §1.1，本地转录 /tmp/concur.txt:151-152,155]**：per-idiom 终止性表（barrier 不行 / mutex 行 / PC 不行）—— **同行评审实验观测（OBE = Occupancy-Bound Execution，原文 "While OBE is not officially supported"）**，作 D 档裁定 supporting（mutex/PC 终止性序相关这一技术事实），**非 NV 产品承诺**；NV 官方 liveness 锚 ISO C++ + Volta whitepaper。
- **[US11442795B2，本地 PDF `/home/yanggl/code/nv_patent/file/sm/us11442795b2.pdf`（有文本层，一手 verbatim）]**："The thread scheduler induces control transfer ... if the threads predicted to arrive ... have in fact not yet arrived"；"Convergence barriers are for performance, not correctness"—— 阻塞即让出（否决粒度 c，支持指令级交错）+ BSSY/BSYNC 层不承诺确定调度序（序无关主门须建在架构态语义而非屏障调度时序）。
- **[US9442755B2，本地 PDF `/home/yanggl/code/nv_patent/file/sm/us9442755b2.pdf`（有文本层，一手 verbatim）— Hardware scheduling of indexed barriers]**："It is not necessary for all threads of a CTA to participate ... using an instruction predicate"；"The reference value ... indicates the number of threads that are expected to arrive at the barrier"；"the program counter of the top barrier instruction is appended to a barrier identifier as a tag ... used in multiple places"—— **机制佐证（非粒度裁定）**：partial-via-predicate（B-10）、reference count（DEC-Q2-COUNT-1）、PC-tag 多 call site（B-14）；专利是 pre-Volta 风格硬件，仅证明 NV 硬件早有按 thread 数计数能力，per-thread **代际归属由 PTX A2 "non-exited"（已 PDF 核实）拍板**，专利不作代际证据。
- **[NV BR100 cmodel（本地源码 verbatim）]**：`ModuleAggregate.cpp:148` run_all 固定时间量子 lockstep（bounded-quantum round-robin，时序步进 SystemC NS，**非功能量子 K 直接依据**）；`SQImpl_Obj.cpp:624` Exec_InstructionIssue 单发射 + SwitchRRCredit；`SQImpl_Obj.cpp:1177-1186` _EU_SLEEP 分支功能 no-op（YIELD/NANOSLEEP grep 0 命中）；`swc_warp.h:149-151` CWarpScheduler 死注释（age 仲裁 HW 不支持）；`EUAlu.cpp:1389` `_BAR` 不读 active mask + `SRPImpl.cpp:167-176,192` per-warp 静态计数 = pre-Volta（**反面对照、须拒绝**，barrier 语义不可对齐，调度循环结构可借鉴）。
- **[`native.cpp` single-warp 已核实锚点]**：step loop line 423-518 指令级交错、`build_groups` line 619、deadlock trap line 437-440、`has_blocked_lanes` line 602、`barrier_ready` line 1108-1124（`blocked_on==index` 精确匹配）、`exec_yield` line 931—— 均为 **single-warp `NativeWarp` 已实现**；`select_warp`/`warp_sched_order`/`cta_blocked`/`CtaBarrier`/`arrived_thread_set`/`try_fire_cta_barriers` **全树不存在、为本阶段全新待建**（single-warp 内核不变、新增 multi-warp 外层）。
- CUDA C++ Programming Guide（atomicAdd/Inc/Dec wrap 语义、float atomicAdd no-order-guarantee、Asynchronous Barriers 双轨 AND 完成 + atomic reset、Asynchronous Data Copies cp.async/cp.async.bulk tx-count、"race condition leads to undefined behavior"）—— inc/dec wrap 与 race=UB(C++ 层) 与 mbarrier 阶段④ 语义基线（适用：通用领域知识，PTX/CUDA 语义面）。
- 未本机核查（领域知识级对照，不改 curryGPU 已定红线）：GPGPU-Sim `barrier_set_t` / atomic memory-partition RMW（src/gpgpu-sim/shader.cc，以仓库为准）；Vortex `vx_barrier(id,nwarp)` + 扁平地址空间 + RISC-V A 扩展（以 Vortex 仓库为准）；Ventus global/local/private 三空间（private=per-thread 印证 local_mem[32]，以 Ventus ISA 手册为准）；Accel-Sim `ISA_Def/*.h` opcode→{space,op-class} 分类表（schema 扩展粒度参照，以仓库为准）。

---

## 11. open questions / 需用户拍板

- **const memory 阶段归属**：本文裁定 const 不进阶段③（DEC-MS-SCOPE-1）。若北极星需要 `LDC` / `c[bank]` 读 kernel 参数在阶段③ 就跑通，须先在 spec.md §2/§5 + research-notes §4 补 const 为只读空间并重述 FUT-2 scope-line，再纳入（即便纳入也不进序无关比较子集）。
- **mbarrier 阶段归属**：本文裁定阶段③ 只解码 + no-op（DEC-MS-MB-1）。若北极星 FA-3 必须在阶段③ 跑通 async producer-consumer，须把 mbarrier 提升为阶段③ 并同步改 plan-foundation FUT-2 措辞。
- **独立多 CTA 并行 launch（grid_dim）**：是否在阶段③ 接受 `grid_dim` 跑独立无交互 CTA（embarrassingly parallel）作为延后 cluster 下的廉价折中，还是严守单 CTA？倾向严守（REC-CLUSTER 默认延后），待用户拍板。
- **~~P-OI 序组合规模~~（已裁决：转 plan）**：`warp_sched_order`(≥3) × `SchedOrder`(4) = 12 组合取全叉积还是对角线 + 关键叉积，属测试预算问题；裁决 = 由 `plan-memsync.md` 按 corpus 规模定（INV-SCH-2 两种形态均许可，契约不受影响）。
- **顺序敏感 atomic 分流机制**：EXCH/CAS/float-add 跨 warp 是 corpus 标注 `order_sensitive` + pre-screen 不拒但主门跳过 + 单序基线（本文倾向，DEC 已表态），还是 static pre-screen 排除 / 要求显式 barrier？须与 data-race negative-control 分流合并为一套『哪些成员不进序无关比较子集』规则。
- **~~谓词化 CTA barrier 语义（B-10）~~（已裁决，关闭）**：裁决 = 接受 guard-true 子集 per-thread 到达、不 trap（B-10 升级版，对齐 Volta+ divergent 分支各 `__syncthreads` 合法 + PTX A6 same barrier name 多 call site）。与 `Bx` 拒谓词化是有意差异（`Bx` per-lane-token vs CTA per-thread-count）。partial-of-partial 仍 debug 断言。
- **~~CTA barrier 缺省 expected vs SR_NTID~~（已裁决，关闭）**：裁决 = 阶段③ 补全 SR_NTID（DEC-MS-PRE-1 全量落地），启用 block-wide 缺省 + B-12 动态非退出，VH-6a 可建；不取『显式 partial 形态回避』路线（北极星跑真 kernel 本就需要 tid/ntid，回避只是把同一依赖推后）。B-12 尾注已同步。
- **`data_race` trap 是否 emit**：运行时 per-byte epoch shadow 检测降为可选 debug hook（本文 §9.6 倾向）；若降为可选则 `data_race` 不进 native 必需 reason 集 / test_spec_iss 反向校验必查项，须 spec 明确。
- **global OOB bounds 元数据**：global 稀疏无界不 trap（本文 DEC，北极星不需），意味着对 global 越界 bug 无判别力。是否引入 launch-time allocation 表做 bounds check（代价：元数据 + 跨 launch 状态）留 FUT，待确认。
- **misaligned 默认严 trap 是否过紧**：某些向量 load 的真实对齐要求（.128 是否仅需 4-对齐而非 16）须核对 sm100a HTML 的 .E/.EL2 modifier 对齐线索，阶段③ 细化对齐表；本文默认严（可配 `allow_misaligned` 松绑）。
- **256-bit 向量 LD/ST**：北极星 `.128` 足够，但 Blackwell SASS 常用 `.256`。是否阶段③ 落实还是留接缝（寻址契约同构，仅寄存器组宽度不同）？本文倾向留接缝。

新增残留 OQ（Q1 调度粒度与公平性 / Q2 CTA barrier）：

- **warp 间调度原子粒度与公平性契约（Q1 核心，已裁定；遗留低风险复核项）**：裁定 = 指令级交错（粒度 a，量子 K=1）+ unconditional weak fairness + 默认 `warp_round_robin`（DEC-Q1-GRANULARITY/FAIRNESS/NAMED-ORDERS，§5.4）。已拍板：(1) 有界量子 K>1 **不引入**，K=1 为阶段③ 唯一规范粒度（INV-SCH-5 / DEC-Q1-GRANULARITY 已同步）；(2) D 档独立进度测试（VH-14）的步数预算值、与 `max_steps` 的关系、超限报告形式（测试失败 vs 可选诊断 hook）**转 `plan-memsync.md` 定**（VH-14 已注；spin/PC 终止预算是经验值、须与 corpus 规模协同）。残留：(3) `warp_min_id_first`/`warp_max_id_first` 是否仍作主门序（裁定保留——DRF/终止程序同终态，但默认改 `round_robin`；若后续发现某些 barrier-DRF 程序在固定优先级序下有非预期交错效应——理论上不应有——须复核，属低风险工程推断）。
- **NV→ISO C++ parallel-forward-progress 对接 verbatim**：ISS 取更强 unconditional weak fairness 的论证不依赖该对接（只需 ISS 执行集 ⊆ NV 执行集），但若要在 spec 把『NV 承诺 conditional parallel-forward-progress』写为已核实硬事实，须直连 Olivier Giroux CppCon / NVIDIA Developer Forums 一手 verbatim（当前为二手转引，诚实标注）。同理 US10067768B2 "no thread can indefinitely block the execution of any other thread" 当前为本地 patent note 二手转引，写进 spec verbatim 前须直连专利原文复核。
- **CUDA C++ Programming Guide CC 7.x "all non-exited threads reach the barrier" 一手核实**：该句经多源搜索 verbatim、本地 webfetch PDF grep 0 命中（未本地 PDF 核实），当前主依据已改挂已 PDF 核实的 PTX §9.7.12.1 "non-exited threads"。若要把 CUDA Guide verbatim 入 spec，须下载 CUDA C++ Programming Guide PDF 复核 CC 7.x 附录一次。
- **单 warp 退化 deadlock 命名并存**：裁定 = 单 warp 纯 `Bx` 死锁保留 `convergence`/`deadlock_no_progress`（向后兼容 VH-1 + 现 `test_spec_iss`），CTA 层混合 / CTA-barrier 死锁用 `synchronization`/`barrier_deadlock`，二者并存（INV-SCH-7 / B-15 / spec-iss.md:50）。`test_spec_iss` 反向校验须同抓 convergence 与 synchronization 两 kind。属已收口裁定，列此备查。
- **`arrived_thread_set` 是否进 ARCH_STATE_KEYS**：倾向否（quiescent 终态全空、无判别力，同 counters 排除逻辑，SS-2）；只 `cta_barriers` 四纯量进比较子集。若需验证『去重正确』须单序 1:1 diff。
- **~~显式 thread-count partial barrier 无 membership check（DEC-Q2-COUNT-1）~~（已裁决，关闭）**：裁决 = 保持 NV-faithful，**不加** membership check（可选 debug 警告也不加）：NV 硬件按到达数释放、不校验是哪些线程，非预期线程凑够 N 导致的『错误』释放与真机行为一致；到达数不足的误用表现为死锁，由 B-15 谓词 / VH-14 步数预算检测。
- **BAR.RED.POPC/AND/OR 到达计数与跨线程谓词归约**：已降为阶段③ 可选 / 可推迟（§6.2，cross-warp reduction 可用 `BAR.SYNC` + 显式 shared 归约表达）。若 corpus 实测需要，BAR.RED 的归约结果是否进序无关比较子集（POPC 计数序无关；AND/OR 布尔序无关）须拍板。BAR.RED 不得与 sync/arrive 同 active barrier 混用已落 B-17 debug 断言。

--- Original Design Draft End ---
