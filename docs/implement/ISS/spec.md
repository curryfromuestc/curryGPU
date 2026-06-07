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
- **编码私有化(开源工程 / 私有编码)**(已定):工程开源,但**精确 bit 编码(opcode 数值 / 字段偏移 / `fsel` 映射)私有**——延续「借模型、自定义编码」哲学(语义公开、编码私有)。`isa/` 拆两层:**`schema` 公开**(指令 / 操作数 / 语义 / 字段名 + 宽度)+ **`layout` 私有**(bit 偏移 / opcode 值 / fsel,置私有 submodule)。生成器 = `schema + layout → decoder / emit / IR`,**引擎与编码无关**(语义 handler 吃已解码字段、不碰 raw bit);公库带**样例 layout**(够 build / CI / demo)、私库注入**生产 layout**,公共 CI 跑样例 conformance、私有 CI 跑真实全量。三红线:① 生成的 decoder / emit **不入库**(内嵌区分树可反推编码);② 真实编码 `.bin`(kernel 二进制)**不公开**——kernel 以 `kernel_builder` 源码(symbolic)公开、私有 build 现场编码;③ JSON IR 为 post-decode 语义、**可公开**。
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
