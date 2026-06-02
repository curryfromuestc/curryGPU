# curryGPU

端侧 GPGPU —— 借鉴 NVIDIA Blackwell 的先进机制，目标在能效与峰值性能上超越开源 RISC-V GPGPU。

**在线文档（GitHub Pages）**：https://curryfromuestc.github.io/curryGPU/

## 内容

- **架构总览**（`docs/design/index.html`）：地基级取舍、数据流、SM 尺寸初步提案。
- **ISA 与编码**（`docs/design/isa.html`）：128-bit 定长指令字、21-bit 静态调度控制段、独立线程调度（ITS）、张量数据流（脉动阵列 + 张量近存）、thread-block cluster、结构化稀疏、低精度 MX / NVFP4、编码定稿。

## 状态

架构骨架与 ISA 已定稿、自洽；RTL 实现与 uarch 标定为后续工作（FPGA → 学术流片）。
