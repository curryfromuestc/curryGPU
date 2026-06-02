# 行为级 SIMT 功能 ISS — 调研要点（设计输入）

> curryGPU C++ 功能级 ISS 的设计参考。综合 GPGPU-Sim、Vortex/SimX、Ventus、Simty、Barra、Hanoi、MMA-Sim、Accel-Sim 及 QEMU decodetree / Sail / riscv-opcodes 等系统的核查结论，按主题内联引用。技术术语保留 English。量化结论附适用条件。

## 1. 为什么 GPU 功能 ISS ≠ 串行 CPU ISS（Spike）

Spike 持一份标量状态、核心循环逐指令推进，复杂度全在指令语义。SIMT 把图景反转：标量语义平凡，复杂度在“一条指令跨 32 lane 复制执行 + 决定哪些 lane 活跃”。五个结构性差异：

1. **执行单元 = 32-lane 向量**：warp 是 fetch/decode 单位，lane 是架构状态单位；每条指令带 active mask，mask=0 的 lane 被压制（不写寄存器/内存）。
2. **寄存器堆规模**：Spike `XPR[32]` → `[warp×lane×reg]` 三维（单 SM ~256 KB）；且分 scalar(uniform) / vector。
3. **分歧/重聚**：CPU 一个 PC 改写即可；SIMT 的核心问题是“next-PC-per-lane + lane 如何重新分组”，Spike 无对应物。
4. **warp 调度**有真同步层级（warp / CTA / cluster / grid）；但调度策略（RR/GTO）是 timing 关注点，功能 oracle 只需一个尊重同步契约的确定性顺序。
5. **内存按 space 分离实例化**（global/shared/local/const），非单一扁平内存。

→ **functional-only 本身是最大速度杠杆**：GPGPU-Sim 纯功能模式（`-gpgpu_ptx_sim_mode 1`）比 timing 模式快约 5–10×。

## 2. 已定结论（强佐证 → 架构骨架，不再争论）

- **decode-once / execute-masked-lane-loop**：一次 decode，循环 lane、`active(t)` 跳过非活跃 lane。GPGPU-Sim `execute_warp_inst_t`、Vortex `execute.cpp`、Simty 一致。
- **SoA per-lane 寄存器堆 `[regID][32]`**（Simty 式）；拒绝 GPGPU-Sim 的 per-thread hash-map AoS（已知热点，`get_reg` ~3.9% wall-clock）。
- **内存** = 抽象 `memory_space` 基类 + `memory_space_impl<4KB 块 hashmap>`（稀疏），per-space；`load<T>/store<T>` 包成 32-地址 gather/scatter，支持 byte-masked 写；atomic RMW 单线程序列化、不可分。
- **async-copy/TMA 与 21-bit 控制段在功能上 no-op**：数据急切 memcpy，但 credit mbarrier tx-count 让握手解析；控制段解码（提取屏障操作数）但不影响结果。
- **功能/timing 严格分离**（GPGPU-Sim cuda-sim vs gpgpu-sim；Vortex Emulator vs Core）：现在只建功能核，留干净回调边界供日后包 timing。

## 3. curryGPU 必须自定义（无开源功能-ISS 先例 → spec 显式定义 + 配验证）

- **ITS**：per-thread PC[32] + 收敛屏障，**非单 IPDOM 栈**（Vortex/Ventus/GPGPU-Sim 全是栈式）。机制依据 NVIDIA 专利 US10067768B2 + Hanoi（arXiv:2407.02944，匹配真实 Turing 到 1.03%）。这是 net-new 工程：先做 per-PC grouping 简化版，再用 trace 验证。
- **MMA bit-exact 数值（最高风险）**：warp-collective；**不抄 GPGPU-Sim 的 FP16-accumulate 捷径**；选并文档化一个 FDA(fused-dot-add) 式累加 + rounding/subnormal 策略。参考 MMA-Sim（arXiv:2511.10909）。属 curryGPU 自己的规范决策，不能照抄任何现有硬件。
- **SFU exp / 8-bit GPR**：GPU SFU 只 faithfully-rounded（非 IEEE 正确舍入，Oberman & Siu ARITH'05）；exp（softmax 用）选 spec-faithful（自定义近似 + 文档 ULP，推荐）或 HW-matching（贵）。8-bit GPR 语义无先例，全自定义。

## 4. 数据结构蓝图

```
block_state_t (CTA;可扩展为 cluster)
  ├─ shared_memory: memory_space_impl          // per-CTA;cluster 下为 distributed shared
  ├─ barriers:      map<bar_id, arrived_warp_set>   // 到达计数
  └─ warps:         vector<warp_state_t>

warp_state_t
  ├─ vgpr:  reg_cell[NUM_VREG][32]   // SoA;reg_cell = tagged union(8/16/32/64b·f16·f32·packed·MX·NVFP4)
  ├─ ureg:  reg_cell[NUM_UREG]       // per-warp 标量 uniform 寄存器
  ├─ pred:  uint32_t Pn[NUM_PRED]    // per-warp bitmask predicate(1 bit/lane)
  ├─ pc:    uint32_t pc[32]          // ITS:per-thread PC(关键)
  ├─ lane_state: enum{active,blocked,yielded,exited} [32]
  ├─ barriers:   {uint32 participation_mask; uint32 reconv_pc; bool valid} Bx[NUM_BARRIER]
  └─ local_mem:  memory_space_impl [32]   // per-lane private
```

三类操作数：32-wide 向量 GPR（tagged cell 覆盖 8-bit / MX / NVFP4）、per-warp 标量 uniform 寄存器、per-warp bitmask predicate + 全局 active mask。Operand collector / banking / 端口 = “文档化但功能省略”的 timing 工件。固定规范 lane/寄存器索引约定，避免 SASS-vs-PTX 式 off-by-one。

## 5. ITS 实现（per-PC grouping）

每步：① 按相同 PC 对 active lane 分组；② 取一组，decode 一次，跨组 mask 执行；③ handler 算 per-thread next-PC，自然分裂/推进。收敛屏障状态机（按 US10067768B2 / Hanoi 语义）：

- `BSSY Bx, target`：`Bx.participation_mask = 当前组 mask; Bx.reconv_pc = target; valid = true`
- `BSYNC Bx`：lane → blocked；当 Bx mask 内 lane 全 ∈ {blocked, yielded, exited} 时，在 reconv_pc 重激活
- `BREAK Bx`：从 `participation_mask` 清位（loop break / 早退；防 BSYNC 永等）
- `YIELD`：标记 lane yielded，强制调度器推进另一 PC-group（让自旋 / 生产者-消费者终止）

不硬接 IPDOM。21-bit 控制段功能 no-op，但解码器必须提取 BSSY/BSYNC 的屏障索引操作数。若日后要镜像 HW 微行为，再升级到 Hanoi 双栈（~432 B/warp）。

## 6. MMA + 低精度数值

作单指令 **warp-collective 宏-op**，单次 bit-exact tiled matmul，**不模拟 systolic dataflow**。fragment↔lane 寄存器映射参考 GPGPU-Sim 的 threadgroup/octet offset 表。数值是 first-class bit-exact 关注点：

- 显式 dtype-correct 乘 + 定义 reduction 顺序的累加（FDA 式，文档化 subnormal/rounding）。
- **MX** = per-32 元素块共享 8-bit E8M0 scale；**NVFP4** = per-16 块 FP8 E4M3 scale + per-tensor FP32 scale（两级）。
- structured-sparsity 在 dot-add 前功能性施加 2:4 metadata 选择。
- shape(884/1688/16816) + dtype 编码为解码表字段，生成 per-variant handler（Accel-Sim ISA_Def 风格）。

参考：OCP “Microscaling Formats (MX) v1.0”、microsoft/microxcaling、MMA-Sim（arXiv:2511.10909）。

## 7. 解码器生成（Python 表 → C++）

声明式 bit 编码表为单一源，**双向**（像 Sail `encdec`，同时驱动 C++ 解码器与 Python `emit()` 汇编器）。128-bit 定长 → FixedLenDecoderEmitter 式 bit 区分树 + APInt 宽提取器；`DecoderMethod` / `trans_*` 式 hook 接手写 C++ 语义（MMA/TMA/sparsity/MX/NVFP4/8-bit GPR/控制段必需）。`.`（字段/匹配位）vs `-`（忽略位）区分 reserved 与控制段。emit JSON IR 同步 asm/disasm/docs。生成时做完备性 + 无重叠检查（强正确性保证）。每指令一个语义文件（Spike 式），`RS1/WRITE_RD` 宏展开为 masked 32-lane 循环。

参考：QEMU `decodetree.py`、LLVM TableGen、Sail、riscv-opcodes。

## 8. 速度技术（按价值排序）

立即采用：① **PC-keyed decode 记忆化**（最高价值，一次 decode 服务所有 lane 与 warp；Spike 软件 icache 4096 项可借鉴）；② **SoA 寄存器堆**（为自动向量化铺路）；③ **handler 返回 per-thread next-PC**（ITS 必需）；④ **dual-path step(n)**（fast 路径与 instrumented/tracing 路径分离）。
中期：uniform/affine scalarization（Barra）、跨 warp/CTA 多线程（per-warp 隔离状态 + 确定性 merge）、SIMD lane 向量化（per-PC group 后映射 AVX-512 k-mask）。
后期可选：JIT warp body。

## 9. 验证与数值契约

按指令类显式契约：
- **(a) IEEE-exact**（add/mul/fma/sqrt/rcp-rn/cvt）：实现全四舍入模式，验到 0 ULP vs correctly-rounded（MPFR）。
- **(b) SFU**（exp/log2/sin/cos/rcp/rsqrt）：spec-faithful（自定义近似 + 文档 ULP）或 HW-matching，二选一并文档化（两者不可同时成立）。
- **(c) 低精度/MX**：chop 式 round-after-fp32-compute + OCP/NVFP4 block-scale；stochastic rounding 需可 seed PRNG（保可复现/可差分）。

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
  reg_cell.h          # tagged union(8/16/32/64b·f16·f32·packed·MX·NVFP4)
  memory_space.h      # 抽象基类 + memory_space_impl<4KB 块>
  warp_state.h        # SoA VGPR + uniform + pred + pc[32] + lane_state[32] + Bx[]
  block_state.h       # shared mem + barriers + warps
  core.h              # step(n) batched;per-PC grouping 调度;barrier 处理
  insns/<name>.h      # 每指令语义片段(RS1/WRITE_RD 宏 = masked 32-lane 循环)
  mma.h               # warp-collective bit-exact tiled matmul + MX/NVFP4 + 2:4 sparsity
  numeric.h           # IEEE 全舍入 + SFU 近似(文档 ULP) + chop 低精度

binding:
  pybind.cpp          # 暴露 launch/step/state-diff(粗粒度边界,非 per-instruction)
```

pybind11 只在 kernel-launch / step / state-inspection 粒度跨边界，避免 Python 调用开销主导。

## 可信度小结

- **强佐证、可直接落地**：执行核结构、functional/timing 分离、SoA 布局、解码器生成、内存空间模型、no-op 控制段处理（多源一致 + 可核查源码/规范）。
- **需 curryGPU 自定义并配验证**：ITS 功能实现策略（per-PC grouping 简化是逻辑推导，无开源功能 ISS 直接先例）、MMA bit-exact 数值（自定义硬件的规范决策）、SFU/低精度/8-bit GPR 语义。

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
