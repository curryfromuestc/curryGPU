# 行为级 SIMT 功能 ISS — 调研要点（设计输入）

> curryGPU C++ 功能级 ISS 的设计参考。综合 GPGPU-Sim、Vortex/SimX、Ventus、Simty、Barra、Hanoi、MMA-Sim、Accel-Sim 及 QEMU decodetree / Sail / riscv-opcodes 等系统的核查结论，按主题内联引用。技术术语保留 English。量化结论附适用条件。

## 1. 为什么 GPU 功能 ISS ≠ 串行 CPU ISS（Spike）

Spike 持一份标量状态、核心循环逐指令推进，复杂度全在指令语义。SIMT 把图景反转：标量语义平凡，复杂度在“一条指令跨 32 lane 复制执行 + 决定哪些 lane 活跃”。五个结构性差异：

1. **执行单元 = 32-lane 向量**：warp 是 fetch/decode 单位，lane 是架构状态单位；每条指令带 active mask，mask=0 的 lane 被压制（不写寄存器/内存）。
2. **寄存器堆规模**：Spike `XPR[32]` → `[warp×lane×reg]` 三维（单 SM ~256 KB）；且分 scalar(uniform) / vector。
3. **分歧/重聚**：CPU 一个 PC 改写即可；SIMT 的核心问题是“next-PC-per-lane + lane 如何重新分组”，Spike 无对应物。
4. **warp 调度**有真同步层级（warp / CTA / cluster / grid）；但调度策略（RR/GTO）是 timing 关注点，功能 oracle 只需一个尊重同步契约的确定性顺序。
5. **内存按 space 分离实例化**（global/shared/local/const + 张量近存 **tensor-mem**），非单一扁平内存。

→ **functional-only 本身是最大速度杠杆**：GPGPU-Sim 纯功能模式（`-gpgpu_ptx_sim_mode 1`）比 timing 模式快约 5–10×。

## 2. 已定结论（强佐证 → 架构骨架，不再争论）

- **decode-once / execute-masked-lane-loop**：一次 decode，循环 lane、`active(t)` 跳过非活跃 lane。GPGPU-Sim `execute_warp_inst_t`、Vortex `execute.cpp`、Simty 一致。
- **SoA per-lane 寄存器堆 `[regID][32]`**（Simty 式）；拒绝 GPGPU-Sim 的 per-thread hash-map AoS（已知热点，`get_reg` ~3.9% wall-clock）。
- **内存** = 抽象 `memory_space` 基类 + `memory_space_impl<4KB 块 hashmap>`（稀疏），per-space；`load<T>/store<T>` 包成 32-地址 gather/scatter，支持 byte-masked 写；atomic RMW 单线程序列化、不可分。
- **async-copy/TMA 与 21-bit 控制段在功能上 no-op**：数据急切 memcpy，但 credit mbarrier tx-count 让握手解析；控制段解码（提取屏障操作数）但不影响结果。
- **功能/timing 严格分离**（GPGPU-Sim cuda-sim vs gpgpu-sim；Vortex Emulator vs Core）：现在只建功能核，留干净回调边界供日后包 timing。

## 3. curryGPU 必须自定义（无开源功能-ISS 先例 → spec 显式定义 + 配验证）

- **ITS（已定）**：per-thread PC[32] + 收敛屏障，**非单 IPDOM 栈**（Vortex/Ventus/GPGPU-Sim 全是栈式）。机制依据 NVIDIA 专利 US10067768B2 + Hanoi（arXiv:2407.02944）。**per-PC grouping 即 post-Volta ITS 架构语义本身、非简化**（Hanoi §IV：同 PC lane 任意周期可同调、重聚可早/晚/省略，唯一约束是 warp 内同步），功能 oracle 取最廉价的合规设计即可。规范见 §5；Hanoi 的 1.03% 是 timing trace 拟合，与功能态无关。
- **MMA bit-exact 数值（规范已定）**：**leader-issue SINGLETON**（ELECT 选唯一 leader lane 发描述符；A/B 经 shared descriptor、C/D 驻 tensor-mem，**无 GPR fragment / 非 warp-collective**，对齐已定 ISA §09 / Blackwell tcgen05）；累加**已定为统一 FDA(F=25)** fused-dot-add（**不抄 GPGPU-Sim 的 FP16-accumulate 捷径**，规范与依据见 §6），依据 MMA-Sim（arXiv:2511.10909）实测。属 curryGPU 自己的规范决策，不照抄任何现有硬件。
- **SFU exp（已定）/ GPR 宽度语义（已定）**：真 GPU SFU 只 faithfully-rounded（Oberman & Siu ARITH'05），但 curryGPU 自验证、不复刻 HW——`MUFU.EX2`/`LG2` 取 **correctly-rounded**（`RN(2^x)`，≤0.5 ULP vs MPFR），并入 §9(a)；`exp = exp2(x·log2e)`。CR f32 exp2 已由 LLVM-libc/CORE-MATH/RLIBM 穷举可证、无 Table-Maker's-Dilemma。sin/cos/tanh 北极星用不到、暂留 faithful。**GPR 数据类型/宽度**：GPR = 无类型 32-bit word（8-bit 仅寄存器索引编码、非数据宽度），类型随指令 modifier；sub-word 结果 **extend-to-32**（按签名 sign/zero，**不做 partial-register merge**——循 ARM AArch64 / RISC-V、拒 x86 AL/AX 合并 hazard）；64-bit = 偶对齐寄存器对、128-bit = 4-对齐组；packed = SIMD-within-word（小端 lane）。详见 §4。

## 4. 数据结构蓝图

```
cluster_state_t (默认 cluster_dim=1 → 恒等;DSMEM 非新空间 = shared + remote-rank 选择子,ISA §02)
  ├─ blocks:        block_state_t[cluster_dim]
  └─ resolve_shared(cta_rank=self, offset)   // dim=1 恒等;rank≠self 延后期干净 unsupported trap,绝不别名/伪造远端态

block_state_t (CTA;由 cluster_state 持有)
  ├─ shared_memory: memory_space_impl          // per-CTA;cluster 下为 distributed shared
  ├─ tensor_memory: memory_space_impl          // tensor-mem(TMEM):MMA 累加器 C/D + block-scale 矩阵;TMALLOC/TMFREE 分配,LDT/STT/CPYT 访问
  ├─ barriers:      map<bar_id, arrived_warp_set>   // 到达计数
  └─ warps:         vector<warp_state_t>

warp_state_t
  ├─ vgpr:  uint32[NUM_VREG][32]     // SoA;GPR = 无类型 32-bit word(类型随指令);64-bit=偶对齐对,128-bit=4-对齐组;packed=SIMD-within-word;f16/低精度按 op 解释,MX/NVFP4 块在 tensor-mem
  ├─ ureg:  uint32[NUM_UREG]         // per-warp 标量 uniform 寄存器(无类型 32-bit word)
  ├─ pred:  uint32_t Pn[NUM_PRED]    // per-warp bitmask predicate(1 bit/lane)
  ├─ pc:    uint32_t pc[32]          // ITS:per-thread PC(关键)
  ├─ lane_state: enum{active,blocked,yielded,exited} [32]
  ├─ barriers:   {uint32 participation_mask; uint32 reconv_pc; bool valid} Bx[NUM_BARRIER]  // NUM_BARRIER ≥ 16(ISA CBR);EXIT/KILL/RET 立即从所有 valid mask 清位
  └─ local_mem:  memory_space_impl [32]   // per-lane private
```

操作数分四路：① 32-wide 向量 GPR（**无类型 32-bit word**，类型随指令 modifier——packed 即 F2FP/I2IP/F2FP.RS 产出、IDP4A/IDP2A 消费的 SIMT 低精度路径，4×INT8 / 2×INT16 / 2×f16 小端 lane；FP8/FP6/FP4 乘加只走张量域）；② per-warp 标量 uniform 寄存器（兼作 MMA/TMA descriptor 载体，warp 内 uniform）；③ per-warp bitmask predicate + 全局 active mask；④ 张量路径操作数不进 GPR——A/B 经 shared descriptor、C/D + block-scale 矩阵驻 tensor-mem（TMEM-addr）。**数据类型 / 宽度语义**：寄存器无类型、由指令解释；sub-word(8/16-bit) 写 **extend-to-32**（签名定 sign/zero，**无 partial-register merge**——循 ARM AArch64 W-zeroing / RISC-V，拒 x86 AL/AX 合并 hazard），`LD.U8/.S8/.U16/.S16` 同样扩展、`ST.U8/.U16` 只写低位；64-bit(FP64/INT64/地址)=偶对齐对 `R(2n):R(2n+1)` 小端、128-bit(`.128` 向量)=4-对齐组；bit-reinterpret(`MOV`、i32↔f32)原样转译、零开销。Operand collector / banking / 端口 = "文档化但功能省略"的 timing 工件。固定规范 lane/寄存器索引约定，避免 SASS-vs-PTX 式 off-by-one。

## 5. ITS 实现（per-PC grouping，已定）

per-PC grouping 不是简化，而是 post-Volta ITS 架构语义本身（Hanoi §IV）。每步：① 按相同 PC 对 active lane 分组；② **最小-PC 优先**选一组（确定序，仅为 trace 可复现），decode 一次，跨组 mask 执行；③ handler 算 per-thread next-PC，自然分裂/推进。warp 退休 = 全 lane exited。收敛屏障状态机（按 US10067768B2 / Hanoi 语义）：

- `BSSY Bx, target`：`Bx.participation_mask = 当前组 mask; Bx.reconv_pc = target; valid = true`
- `BSYNC Bx`：lane → blocked；当 Bx mask 内 lane 全 ∈ {blocked, yielded, exited} 时，存活子集在 reconv_pc 重激活、`valid = false`
- `BREAK Bx`：从 `participation_mask` 清位（loop break / 早退；防 BSYNC 永等）
- `CONT`：分支到 loop header（重聚由该 loop 自己的 Bx 管）
- `YIELD`：标记 lane yielded，强制调度器推进另一 PC-group（让自旋 / 生产者-消费者终止）
- `EXIT/KILL/RET`：lane → exited，并**立即从所有 valid `Bx.participation_mask` 清位**（ISA §10 starvation-free 硬前提，退休 lane 不死锁待决屏障）

**核心性质**：对尊重同步契约的程序，**最终架构态与组选择序无关**——每 lane 私有架构态 + 所有跨-lane 交互经显式重聚 / `membermask ⊆ active` 门控 + warp 内无屏障的内存竞争 = UB。这是 Hanoi §IV 的闭包论证，也是功能 oracle 唯一需要的性质 ⇒ 序无关性 metamorphic 测试（≥3 种确定序，断言最终态 bit 一致）即主验收门，替代「对真实硬件 trace」。不硬接 IPDOM；21-bit 控制段功能 no-op，但解码器必须提取 BSSY/BSYNC 屏障索引操作数。日后镜像 HW 微行为再升级 Hanoi 双栈（~432 B/warp，纯 timing）。

## 6. MMA + 低精度数值

作单指令 **leader-issue 宏-op**：ELECT 在 membermask 内选**唯一** leader lane（`@elected` guard，exactly-one 否则 illegal）发描述符，非 leader lane 在该 PC guard 假、跳过，ITS 收敛屏障保证 warp 在 MMA 处收敛、同步前进。功能上一次算完整 bit-exact tiled matmul，**不模拟 systolic dataflow**、**无 GPR fragment / 非 warp-collective**（对齐已定 ISA §09 / Blackwell tcgen05）。数据通路：A/B 经 shared descriptor（驻 UR，载 shape/stride/tile/swizzle）取数 → C/D 累加器与 block-scale 矩阵驻 tensor-mem（TMEM-addr，经 LDT/STT/CPYT 与 RF 往来；LDT.RED 在累加器域做 row/col 归约）。MMA.FENCE/COMMIT/WAIT 与 group scoreboard 功能上 no-op（急切计算，同 async-copy/TMA 处理），但解码器须提取 .kind/.shape/idesc/.block/membermask/enable-acc 等操作数。数值是 first-class bit-exact 关注点：

- 单 MMA opcode + 结构化字段：`.kind`(F16/BF16/F8F6F4/I8/I4·MX/NVFP4，决定累加精度) + `idesc`(M/N/K/dtype/transpose/sparse **唯一真源**) + `.shape`(仅选 tile-class，**不**携 M/N/K) + `.block`(B16/B32)，生成 per-variant handler（Accel-Sim ISA_Def 风格）。
- **累加 = 统一 FDA(F=25) fused-dot-add（决策 #2，已定）**：dtype-correct 精确乘 → 对齐 e_max 截 25 fraction bits → 定点求和（**顺序无关**）→ RNE 规格化到 FP32。详见下「FDA(F=25) 累加规范」。
- **MX** = per-32 元素块共享 UE8M0 scale；**NVFP4** = per-16 块 FP8 E4M3 scale + per-tensor FP32 scale（两级）；scale 矩阵驻 tensor-mem，`.block` 粒度须与 amax 归约窗口（REDUX.FMAX.ABS，∈{16,32}）对齐。量化链：amax → F2SF 生成因子 → F2FP pack → 张量 MMA。
- `MMA.SP`（2:4 / 4:8 结构化稀疏）：dot-add 前功能性施加 metadata 选择，metadata 选择子绑 `reuse[4]` 第 4 槽（稀疏下专用、不作 reuse）。

参考：OCP “Microscaling Formats (MX) v1.0”、microsoft/microxcaling、MMA-Sim（arXiv:2511.10909）。

### FDA(F=25) 累加规范（决策 #2，已定）

依据 MMA-Sim（arXiv:2511.10909，Microsoft Research；CLFP 黑盒探测，在 Volta–RTX Blackwell / CDNA2–3 共 10 架构上经 >10⁶ 随机输入/指令验到 bitwise 等价）。该工作把张量核累加归纳为四种 summation order（sequential / group-pairwise / **fused** / chain-of-fused）；**FDA = fused-dot-add**，是 NVIDIA Ampere–Blackwell 主用算法。curryGPU 取**单一统一 FDA(F=25)**，对所有 `.kind`（F16/BF16/TF32/FP8/FP6/FP4/MX/NVFP4）一致——**不**抄 per-format 算法动物园（GDFS group-of-16 / CoFDA K/2 链 / GFDRDA even-odd），后者正是论文记录的跨架构数值不一致来源。

单条 MMA（一个 K-tile）算 `d = c + Σ_k a_k·b_k`，五步：

1. **特殊值**：任一操作数 NaN 或 `0×∞` → `d = NaN`（canonical quiet NaN）；±∞ 混合 → NaN；单一 ∞ → `d =` 该 ∞。
2. **dequant + 精确乘积**：按 `.kind` 解码 `a_k,b_k` 为 (sign, significand, exponent)，折入 block-scale——MX 的 UE8M0 为纯 2 次幂，仅加指数（精确）；NVFP4 两级（per-16 E4M3 + per-tensor FP32）的 significand 乘入、指数相加。乘积 `p_k` 以**精确定点**存（不规格化、无精度损失、无 over/underflow）；subnormal 如实表示，**不 FTZ**。
3. **对齐 e_max**：`e_max = max{e_c, e_0, …}`；各 significand 右移 `e_max − e_k`，保留 **F=25 fraction bits**，移出位**截断（RZ）**。这是累加内**唯一**的中间舍入，即「累加精度」。
4. **定点求和**：`c` + 全部对齐乘积在宽定点累加器内相加。**精确且与求和顺序无关**（整数加法可结合；累加器够宽 → 无溢出）。⇒ curryGPU **不需 pin reduction 顺序**，任意顺序 bit-exact 相同（这消解了早前「定义 reduction 顺序」的顾虑）。
5. **规格化 + 一次舍入到 FP32**：`s_sum × 2^{e_max}` 规格化到 FP32，第 23 fraction bit 用 **RNE**（对称、无偏）舍入；`≥ 2¹²⁸` → ±∞。输出累加器 D 为 FP32，驻 tensor-mem。

跨指令（GEMM K-loop）：每条 MMA 是一次 FDA、写 FP32 D 回 tensor-mem；跨 K-tile 累加 = FP32 累加器自然复用，顺序由 kernel K-loop 固定（**唯一**顺序相关处，且非 MMA 自身语义）。

**参数与依据**（全部对应 MMA-Sim §VI「future MMA」设计建议）：

- **F=25**（所有 `.kind` 统一）：≥ FP32 的 24-bit significand；对齐 Blackwell / RTX Blackwell（本机 RTX PRO 6000 Blackwell 即论文验证件，HMMA/QMMA 均 F=25）；**避开 Hopper/Ada FP8 的 F=13 瓶颈**（DeepSeek-V3 训练不稳定根因）。论文建议「≥23 bit，越多越好」。
- **全 FP32 动态范围 + 保留 subnormal**（输入/乘积/累加器/输出皆不 FTZ）；定点累加器**无中间溢出**。避开 CDNA2 subnormal-flush 范围瓶颈。
- **仅对称舍入**：对齐截断 RZ（fused 设计固有）+ 末次规格化 RNE。**不用 RD**——避开 CDNA3 round-down 的系统性负偏。
- **单 FDA / 不链**：每条 MMA 对其完整 instruction-K 做一次 fused 累加（无 CoFDA 链、无分组）。链/分组是 HW 面积微优化，会引入顺序相关与不一致，**不进规范**（HW 若必须链，作文档化偏差）。

## 7. 解码器生成（Python 表 → C++）

声明式 bit 编码表为单一源，**双向**（像 Sail `encdec`，同时驱动 C++ 解码器与 Python `emit()` 汇编器）。128-bit 定长 → FixedLenDecoderEmitter 式 bit 区分树 + APInt 宽提取器；`DecoderMethod` / `trans_*` 式 hook 接手写 C++ 语义（MMA/TMA/sparsity/MX/NVFP4/sub-word·packed·寄存器对/控制段必需）。`.`（字段/匹配位）vs `-`（忽略位）区分 reserved 与控制段。emit JSON IR 同步 asm/disasm/docs。生成时做完备性 + 无重叠检查（强正确性保证）。每指令一个语义文件（Spike 式），`RS1/WRITE_RD` 宏展开为 masked 32-lane 循环。

参考：QEMU `decodetree.py`、LLVM TableGen、Sail、riscv-opcodes。

## 8. 速度技术（按价值排序）

立即采用：① **PC-keyed decode 记忆化**（最高价值，一次 decode 服务所有 lane 与 warp；Spike 软件 icache 4096 项可借鉴）；② **SoA 寄存器堆**（为自动向量化铺路）；③ **handler 返回 per-thread next-PC**（ITS 必需）；④ **dual-path step(n)**（fast 路径与 instrumented/tracing 路径分离）。
中期：uniform/affine scalarization（Barra）、跨 warp/CTA 多线程（per-warp 隔离状态 + 确定性 merge）、SIMD lane 向量化（per-PC group 后映射 AVX-512 k-mask）。
后期可选：JIT warp body。

## 9. 验证与数值契约

按指令类显式契约：
- **(a) IEEE-exact**（add/mul/fma/sqrt/rcp-rn/cvt）：实现全四舍入模式，验到 0 ULP vs correctly-rounded（MPFR）。
- **(b) SFU**：**`MUFU.EX2`/`LG2` = correctly-rounded**（`RN`，≤0.5 ULP vs MPFR）——并入 (a) 同一契约类，clean-slate 自验证不复刻 HW faithful（Oberman & Siu ARITH'05；CR f32 exp2 已被 LLVM-libc/CORE-MATH/RLIBM 穷举可证）；`exp = exp2(x·log2e)`（CR EX2 ∘ CR FMUL，组合本身非声明 CR）。`rcp/rsqrt/sqrt` 由 MUFU 种子 + Newton 合成到 IEEE 级（见 (a)）。`sin/cos/tanh` 北极星不需、暂留 faithful（自定义近似 + 文档 ULP），需要时同样可升 CR。
- **(c) 低精度（张量 vs SIMT 两路分开）**：张量 MMA 走 §6 **FDA(F=25)** 规范，conformance 参考 oracle = FDA(F=25) 精确实现（**非 `numpy.matmul`**，其累加方式不同），每 kind/shape ≥10⁴–10⁶ 随机 tile bit-exact 差分，另以 FDA(F=∞)（MPFR 精确 round-once）作精度上界；SIMT pack/cvt（F2FP `.SATFINITE` / I2IP / F2SF）= chop 式 round-after-fp32-compute + OCP MX(UE8M0)/NVFP4 block-scale；stochastic rounding（F2FP.RS）需可 seed PRNG（保可复现/可差分）。
- **(d) 端到端容差（两层）**：**Tier-1 GOLD**（conformance）——C++ 核对自身 MPFR/fp64 精确规范（FDA(F=25) + CR EX2 + IEEE-RNE）**逐基元与整块 0 ULP**；**Tier-2 SANITY**（validation）——整块对**独立 fp64 参考**（numpy 主、PyTorch CPU/TF32-off/deterministic 交叉）用组合判据 `|a−b| ≤ atol + rtol·|b|`（报 max + RMS-relative + 分段分解），**非 bit-exact**（累加序 / exp 都不同，且 PyTorch 跨架构本就非 bit-reproducible）。误差模型：GEMM Higham γ_K（内积）+ FDA(F=25)-vs-∞ 间隙 ≤ K·2⁻²⁵ + softmax/LayerNorm 归约 γ（或概率 √n·u，Higham & Mary）+ CR EX2 ≤0.5 ULP。暂定 `rtol = 2⁻¹³(~1.2e-4)`、`RMS ≤ 2⁻¹⁶`、`atol = 2⁻²⁰`（解析最坏界，标注 provisional）；阶段 ④/⑤ 按实测 max × 安全系数(2–4) 收紧，下限不低于解析界。Tier-1 须先 0-ULP 通过，Tier-2 校准才可信。

Day-one 起：Varity 式随机差分测试 + LLVM-libc 式 fp16/fp32 单变量穷举 conformance gate（softmax-exp）；暴露架构状态计数器（指令数 / warp 指令 / divergence 事件 / mem-op）做 1:1 diff；Hanoi 式 trace-equivalence harness（比 warp active-mask/PC trace）验分歧引擎。
CUDA 单精度 ULP 参考（extensive but not guaranteed）：expf/exp2f 2、sinf/cosf 2、tanf 4、logf/log2f 1、sqrtf 0（IEEE）。

## 10. 最小骨架

```
Python 层:
  encoding_table.py   # 声明式 bit 编码表(单一源,双向)
  gen_decoder.py      # 生成 C++ 解码器(bit 区分树) + JSON IR + emit() 钩子
  kernel_builder.py   # emit() API 构 Transformer block kernel
  test_harness.py     # 差分测试 + conformance gate(pybind11 调 C++ 核)

C++ 功能核:
  decoded_inst.h      # 生成的解码指令 struct(含 21-bit 控制段字段,no-op)
  reg_cell.h          # 无类型 32-bit word + 按 dtype 解释的访问器(sub-word extend-to-32,64b=偶对齐对,packed=SIMD-within-word);MX/NVFP4 块在 tensor-mem
  memory_space.h      # 抽象基类 + memory_space_impl<4KB 块>
  warp_state.h        # SoA VGPR + uniform + pred + pc[32] + lane_state[32] + Bx[]
  block_state.h       # shared mem + tensor-mem(TMEM:MMA 累加器 + block-scale) + barriers + warps
  core.h              # step(n) batched;per-PC grouping 调度;barrier 处理
  insns/<name>.h      # 每指令语义片段(RS1/WRITE_RD 宏 = masked 32-lane 循环)
  mma.h               # leader-issue bit-exact tiled matmul:shared descriptor 取 A/B → tensor-mem 累加 C/D + MX/NVFP4 block-scale + 2:4 sparsity
  numeric.h           # IEEE 全舍入 + SFU correctly-rounded EX2/LG2(vs MPFR) + chop 低精度;Tier-1 用其 MPFR/fp64 精确版作 GOLD

binding:
  pybind.cpp          # 暴露 launch/step/state-diff(粗粒度边界,非 per-instruction)
```

pybind11 只在 kernel-launch / step / state-inspection 粒度跨边界，避免 Python 调用开销主导。

## 11. 代码落位

仓库根 = **整个 curryGPU 工程**（ISS 现建，RTL / 验证 / 软件后续）。`isa/`（ISA 单一源，**顶层共享**：ISS 现用、未来 RTL 解码器 / 汇编器 / 文档共用，对标 RISC-V `riscv-opcodes` 独立于实现）+ `iss/`（功能 ISS **独立子树**，命名同 Spike / `riscv-isa-sim`、与 docs 路径一致）。构建**按组件自治**——build 文件在 `iss/`，**不在仓库根放 Python 包文件**（仓库根是 HW 工程根、非 Python 包根）。

```
curryGPU/
├── docs/                   # 现有：design/(isa.html/index.html)+ implement/ISS/(spec.md & research-notes.md)
├── isa/                    # ISA 单一源(顶层共享:ISS / 未来 RTL / 汇编器 / 文档)
│   ├── encoding_table.py   # 声明式 128-bit 定长编码表(真源)
│   ├── gen_decoder.py      # 表 → C++ 解码器(bit 区分树 + APInt 宽提取)+ decoded_inst + JSON IR;完备/无重叠门
│   ├── emit.py             # 表驱动 emit() 汇编器(导入同一表 → Sail-encdec 双向)
│   └── kernel_builder.py   # 构 Transformer-block kernel → 128-bit 指令流
├── iss/                    # 功能 ISS(独立子树 = 当前构建目标)
│   ├── pyproject.toml      # scikit-build-core(本组件 pip 包;currygpu namespace 横跨 isa+iss)
│   ├── CMakeLists.txt      # codegen(custom_command DEPENDS ../isa)→ C++ 核 → pybind → ctest
│   ├── cmake/              # FindMPFR、codegen 依赖宏、pybind11/scikit glue
│   ├── include/currygpu/   # decoded_inst/reg_cell/memory_space/warp_state/block_state/core/mma/numeric.h
│   ├── src/ + src/insns/<name>.h
│   ├── binding/pybind.cpp  # 仅 launch/step/state-diff(粗边界)
│   ├── tests/              # cpp/ unit/<family>/ roundtrip/ differential/ conformance/ trace_equiv/ e2e/
│   └── build/ (gitignored) # 生成的 decode.gen.cpp / decoded_inst.gen.h / isa_ir.json
└── (rtl/  dv/  sw/  tools/  —— 未来按需新增；现在不建空目录)
```

要点：① **依赖单向 `isa → iss`**（iss build 时调 isa 生成器，**严禁反向**，防循环）；② `currygpu` 作 namespace package 横跨 `isa/`（纯 Python emit/kernel_builder）+ `iss/`（C++ 扩展），构建文件按组件自治、不 root-anchor（为 RTL/验证留出并列空间）；③ 生成物入 `iss/build/generated/currygpu/`、CI `git diff --exit-code` regen-clean 门禁手改 / 陈旧；④ 粗 pybind 边界（调用数 O(launch+step)，不随指令数增长）；⑤ 生成器内建完备性（128 bit 全覆盖）+ 无重叠（无 bit 重定义）检查（riscv-opcodes / decodetree 式）；⑥ RTL 用时加 `rtl/<module>/`、验证加 `dv/`（RTL↔ISS 联合仿真 + formal/）、软件加 `sw/`——现在不建空目录。

## 可信度小结

- **强佐证、可直接落地**：执行核结构、functional/timing 分离、SoA 布局、解码器生成、内存空间模型、no-op 控制段处理（多源一致 + 可核查源码/规范）。
- **规范已定、待落实 + 验证**：ITS（per-PC grouping = post-Volta 语义，US10067768B2 / Hanoi §IV，序无关性可证）、MMA（FDA(F=25)，MMA-Sim 实测）、SFU exp（CR EX2）、端到端容差（两层）、cluster（延后接缝）、代码落位、GPR 宽度语义（无类型 word + extend-to-32 + 寄存器对/packed，循 ARM/RISC-V）——均已定稿并附依据；剩 conformance / 差分 / 序无关 / 穷举 gate 的实现落地。

## 关键参考

- GPGPU-Sim manual & cuda-sim：https://gpgpu-sim.org/manual/index.php/Main_Page
- Accel-Sim：https://mkhairy.github.io/Docs/Accel-Sim.pdf
- Control Flow Management in Modern GPUs (Hanoi)：https://arxiv.org/html/2407.02944v1
- NVArchSim “Need for Speed” (HPCA 2021)：https://d1qx31qr3h6wln.cloudfront.net/publications/HPCA_2021_NVArchSim.pdf
- Spike / riscv-isa-sim：https://github.com/riscv-software-src/riscv-isa-sim
- QEMU decodetree：https://www.qemu.org/docs/master/devel/decodetree.html
- Dynamic Warp Formation (MICRO 2007)：https://people.ece.ubc.ca/aamodt/publications/papers/wwlfung.micro2007.pdf
- NVIDIA convergence-barrier patent：https://patents.google.com/patent/US20160019066A1
- MMA-Sim (bit-accurate tensor core)：arXiv:2511.10909
- Microscaling (MX) Formats：OCP MX v1.0；arXiv:2310.10537；github.com/microsoft/microxcaling
- NVIDIA ITS / convergence-barrier patent（granted）：US10067768B2（≈ application US20160019066A1）
- SFU 插值（faithful，非 CR）：Oberman & Siu, "A High-Performance Area-Efficient Multifunction Interpolator", ARITH-17, 2005
- Correctly-rounded f32 exp2（穷举可证）：LLVM-libc math（libc.llvm.org/headers/math）、CORE-MATH（core-math.gitlabpages.inria.fr）、RLIBM（CGO 2023）
- 浮点误差界（容差 #5）：N. J. Higham, *Accuracy and Stability of Numerical Algorithms*, 2nd ed., SIAM 2002（§3 内积 γ_n）；Higham & Mary, SIAM J. Sci. Comput. 41(5):A2815, 2019（概率 √n·u）
- cluster / DSMEM：NVIDIA Hopper Architecture In-Depth（DSMEM）；CUDA C++ Programming Guide（Thread Block Clusters / cluster.map_shared_rank → PTX `mapa`）；FlashAttention-3 arXiv:2407.08608（cluster 仅 ~2% 可选优化）
- 单一源 codegen / 布局对标：sail-riscv（model/ + c_emulator/，Sail `encdec`）、riscv-opcodes（声明表 + 完备/无重叠检查）、LLVM TableGen（FixedLenDecoderEmitter）、QEMU decodetree
- GPR 数据类型/宽度语义（partial-register）：x86 partial-register stall（sub-word 合并 hazard、假依赖，避之）vs ARM AArch64（W 写零扩展 X、LDRB 清整寄存器）/ RISC-V RV64I（sub-word load 符号/零扩展到 XLEN，无 partial 状态）
