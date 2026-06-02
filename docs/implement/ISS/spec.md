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

要点(依据见 `research-notes.md`):decode-once / masked-lane-loop;SoA per-lane 寄存器堆;ITS = per-thread PC + 收敛屏障(非 IPDOM 栈);21-bit 控制段解码但功能 no-op;MMA = warp-collective bit-exact tiled matmul;功能/timing 严格分离(留接口供日后包 timing)。

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
- **② ITS 分歧**:per-thread PC + per-PC grouping + 收敛屏障(BSSY/BSYNC/BREAK/YIELD)。
  验收:if/else、循环、subwarp 分歧测试结果对;active-mask trace 合理。
- **③ 内存与同步**:memory_space(global/shared/local)+ barrier + atomics。
  验收:shared-mem reduction、barrier 类测试结果对。
- **④ 张量与数值**:MMA(bit-exact tiled)、SFU exp、归约、低精度(MX/NVFP4)量化。
  验收:GEMM tile 对 `numpy.matmul`(文档容差);exp conformance ULP 达标。
- **⑤ 组装 Transformer block**:attention(QKᵀ / softmax / ×V)+ FFN + LayerNorm + residual,用 kernel-builder。
  验收:整块输出对 numpy / PyTorch 参考(文档容差)。

## 6. 整体验收标准(可运行 / 可验证 / 可回滚)

编码表单一源驱动 decoder + assembler、round-trip 零差异、完备性检查过;各阶段单测 + 差分测试过;小 Transformer block 端到端跑通且数值在文档容差内;架构状态计数器可导出(留 trace / timing / 对硬件 diff 接口)。

## 7. 开放决策(暂留,后续确定;先记提议默认)

- **ITS 实现**:默认 per-PC grouping 简化版(不上 Hanoi 双栈)。— 阶段 ② 定
- **MMA 累加方案**:默认一种 FDA 式 + 文档化 rounding / subnormal。— 阶段 ④ 定
- **SFU exp**:默认 spec-faithful(自定义多项式 + 文档 ULP)。— 阶段 ④ 定
- **cluster / DSMEM**:默认留接口、后置(Transformer 块不强依赖)。— 阶段 ③ 评估
- **数值容差**:IEEE add/mul/fma = 0 ULP;端到端相对误差容差 — 阶段 ④/⑤ 定具体数。
- **代码落位**:`isa-spec/`(Python 表 + codegen)、`model/`(C++ 核)、`tests/` vs 单一顶层目录 — 实现起步时定。

## 8. 风险

- **ITS 功能实现无开源先例**:per-PC grouping 简化是逻辑推导,需用 trace-equivalence 验证;阶段 ② 首要降风险点。
- **MMA / 低精度 bit-exact 数值**:自定义硬件的规范决策,最高风险;必须显式定义并配 conformance 验证。
- **128-bit 定长解码器生成**:主流工具(decodetree)对 >32-bit 支持弱,需 APInt 式宽提取,可能要自写生成器核心。
- **Python / C++ 边界开销**:必须保持粗粒度(launch / step 级),避免 per-instruction 过界拖慢热循环。

## 9. 参考

深度技术依据、对照系统与引用见同目录 `research-notes.md`。
