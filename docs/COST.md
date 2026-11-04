# 一次分析的成本（token 实测）

数据来源：DSH 会话日志里每步的 `data.usage`（`~/.dsh/sessions/--<cwd>--/session-*/session.v4.jsonl.zstd`）。
用 `python tools/session_cost.py` 可随时重测。

## 口径

| 字段 | 含义 | 说明 |
|---|---|---|
| `input` | 未命中缓存的新增输入 | 真正的"新信息"进模型 |
| `cacheRead` | 本次请求重发的历史上下文（prompt cache 命中） | 随对话长度线性增长，单价远低于 input |
| `output` | 模型输出 | 结论 + 工具调用参数 |

**看单次分析的增量成本，用 `input + output`；`cacheRead` 反映的是"对话有多长"，不是"这次干了多少活"。**

## 实测（2026-11-05，本仓库 40 轮长会话）

| 回合 | 步数 | input | cacheRead | output | 说明 |
|---|---|---|---|---|---|
| 34 | 31 | 8,932 | 12,908,288 | 15,777 | M6.3 追查（含多次改代码/跑测试） |
| 35 | 32 | 8,269 | 13,918,080 | 12,682 | 同上 |
| 37 | 20 | 6,674 | 9,438,976 | 9,532 | 口径收紧（WAIT_SCOPES）|
| 38 | 18 | 5,196 | 8,762,880 | 8,284 | 文档收尾 |
| **40** | **26** | **7,159** | **13,036,032** | **12,993** | **分析最新 utrace + 定位自录 trace + 成本测量** |
| 合计 | 1,314 | 1,327,385 | 522,134,912 | 697,424 | 全会话累计 |

## 这条数据怎么读

- **一次"分析最新 trace"的增量 ≈ 7k input + 13k output ≈ 20k token**（回合 40 含若干次实现/验证，纯分析只占其中一部分）。
- `cacheRead` 13M 不是"多花了 13M"：它是每步重发上下文的记账值，命中缓存后单价通常只有 input 的 1/10 甚至更低；
  真正推高它的是**会话长度**（工具输出堆积），所以长会话越到后面每步越贵。
- 想压低成本：**新开会话做一次分析**（上下文短 → cacheRead 小），并且只让 agent 读 `summary.md`（<1KB），
  不要一次把三份 digest 全读进来（约 6.5KB ≈ 2–3k token）。

## 产物大小（一次分析的"必读"部分）

| 文件 | 大小 | 约 token |
|---|---|---|
| `auto/summary.md` | 926 B | ~0.4k |
| `auto/cpu/perf_digest.md` | 3.8 KB | ~1.5k |
| `auto/bound/perf_digest.md` | 1.8 KB | ~0.7k |
| CLI 摘要行 | ~1 KB | ~0.4k |

即：**最小读取 ≈ 3k token**，只读 summary 更省（<0.5k）。

## 复测

```bash
python tools/session_cost.py                       # 最近会话，按回合
python tools/session_cost.py --turn 40             # 只看某一回合
python tools/session_cost.py --json                # 给脚本用
```

---

## 实测二：指定 trace 的完整分析（2026-11-05 01:48–01:51）

**输入**：用户指定 `20261105_012713.utrace`（542 MB / 439.6 s / 6353 帧）。
**步骤**（4 步工具调用，无返工）：

```bash
perfctl ingest --trace <012713> --run runs/m8_9/t012713 --log <Lyra.log> --mode packaged
perfctl digest cpu   runs/m8_9/t012713 --out runs/m8_9/cpu
perfctl digest bound runs/m8_9/t012713 --out runs/m8_9/bound --b2
# （读结论 + 记录成本）
```

| 指标 | 值 |
|---|---|
| **input（未命中缓存）** | **+1,126 token** |
| **output** | **+878 token** |
| cacheRead | +2,081,920 token（4 步 × ≈52 万上下文） |
| 墙钟耗时 | ≈ 3 分（ingest 含 Insights 导出 ~90 s，cpu ~20 s，bound+b2 ~70 s） |
| 必读产物 | `cpu/perf_digest.md` 3.8 KB + `bound/perf_digest.md` 2.2 KB ≈ **6 KB ≈ 2.5k token** |

**结论**：一次"给定 trace 的完整分析"的**真实增量约 2k token**（input+output）；
同一条分析在**长会话**里要额外背着 4 × 52 万的 cacheRead（单价低但会累积）。
若在新会话里做同样的事，cacheRead 会降一到两个数量级。

### 判定结果（顺带留档）

- 帧：439.6 s / 6353 帧，p50 26.39 ms，p99 105.8 ms，max 992 ms。
- bound：GT/RT/RHI 三线 busy 都 ≈ 31.9 ms（100%），GPU 28.31 ms（88.8%）；
  按自耗时 **GT 22.21 ms/帧**（次名 RT 11.67 的 1.9 倍）→ 工作量在 GameThread；
  fence 每帧等待 19.2 ms（占帧间隔 60%）→ GPU 队列内部有跨队列等待。
- cpu：`WinPumpMessages` 19.0%、`Slate::Prepass` 14.7%、`Slate_PaintSlowPath` 13.1%、
  `FEngineLoop_UpdateTimeAndHandleMaxTickRate` 5.8%、`Mass…Runner Task` 5.3%
  → Slate（UI）合计约 47%，无单一热点。
