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
