# 优化建议层（Advisor）——怎么让建议不老化、可证伪、能自增长

> 代码：`pipeline/advice/advisor.py`（`perfctl advise`）· 杠杆目录：`tools/scan_cvars.py` → `catalog/cvars.json`
> 相关：PLAN §9（口径规则）、§10（验收）、`docs/COST.md`（成本）

## 1. 问题的本质

性能建议最容易出的两种病：

| 病 | 表现 | 根因 |
|---|---|---|
| **套路化** | "关阴影、降分辨率、砍后处理" | 建议来自记忆/博客，不来自这份 trace 的数据 |
| **过时** | 建议的 cvar 已被删、默认值变了、新版本有更好的开关 | 知识写在人脑或文档里，没有跟引擎版本绑定 |

结论：**建议必须"由证据生成 + 由本机引擎解析 + 由实验判决"**，否则一定会腐化。

## 2. 三条硬规则（代码里就是这么写的）

### 2.1 证据锚定：没有数字就没有建议

`detect_*` 只读 digest 的 JSON：GPU pass 份额、四线 occupancy、fence 等待、GT 自耗时分布。
每条假设必须带 `evidence` 字段（哪一项、多少、来源文件），否则不输出。

例：`gpu_effect_share:bloom` 的 evidence 是
`share_of_gpu_work_pct=5.3 / ms_effective=1.51 / graphics_busy_ms=28.313`。

### 2.2 杠杆来自本机引擎，不写死

`tools/scan_cvars.py` 扫 `<UE root>/Engine/Source/**` 里的
`TAutoConsoleVariable<T>` / `FAutoConsoleVariableRef`，产出 `catalog/cvars.json`：

```json
{"name": "r.Bloom.ScreenPercentage", "default": "100.0f",
 "help": "Controles the axis resolution of the FFT convolution", "file": ".../Bloom.cpp", "line": 30}
```

于是建议里每个开关都带 **默认值 + 帮助 + 源码位置**。引擎升级 → 重新扫一次 → 清单自然更新
（删掉的开关自动消失，新增的自动出现）。这就是"不靠记忆"的部分。

### 2.3 可证伪 + 可复验：每条建议自带判决条件

每条假设包含：

| 字段 | 用处 |
|---|---|
| `expected_saving_ms` | 预期收益区间（份额 × 队列 busy × 0.3–0.6 的保守折算） |
| `verification` | 用我们自己的 R9 流程验证（`capture` A/B 各 5 次 + `diff --significance-only --fail-on regression`） |
| `invalidation` | **什么情况下这条假设是错的**（例如"Δ 落在噪声内"、"关掉 UI 后帧时间不变"） |

`invalidation` 是这套东西的关键：没有它，建议无法被推翻，就会变成玄学。

## 3. 检测器清单（只看形态，不预设引擎缺陷）

| 检测器 | 触发条件 | 产出 |
|---|---|---|
| `gpu_effect_share:*` | 单个 GPU 效果 ≥3% GPU 工作份额 | 砍该效果的质量/分辨率，附该子系统的 cvar |
| `bound_saturated_multi` | ≥2 条线 occupancy ≥95% | 提醒 busy 无分辨力，改用自耗时排名 |
| `fence_cross_queue` | fence 等待 ≥30% 帧间隔 | 查队列依赖 / 异步开关（`r.RDG.AsyncCompute` 等） |
| `cpu_ui_dominant` | UI 相关自耗时 ≥30% | 先确认是否"在编辑器里 PIE"（编辑器 Slate 不该算游戏帧） |
| `cpu_single_hotspot` | 单个 timer ≥15% | 直接给 `b2 callers` 下钻命令 |
| `cpu_dispersion` | Top1 <15% | 提示做前缀聚合 + 逐次分布，区分"广度"与"抖动" |

新增检测器 = 新增一种"数据形态 → 假设"的映射，**不需要动配置或知识库**。

## 4. 怎么防止建议腐化（三层机制）

### 第一层：目录随版本重建

```bash
python tools/scan_cvars.py --ue-root <UE> --prefix r.Lumen --prefix r.Bloom ... --out catalog/cvars.json
```

把 `catalog/cvars.json` 的 `engine_root` 与 manifest 的引擎版本对齐；换引擎版本必须重扫。
`advise` 在目录缺失时会明确提示，不会静默给"记忆里的开关"。

### 第二层：预测 vs 实测的账本（自增长知识）

每条假设跑完 A/B 后，把 `预测区间 / 实测 Δ / 是否显著 / 版本 / 场景` 记进 `advice/ledger.json`。
规则：

- 同一 `id` 在 ≥3 个场景里预测高估 >2 倍 → 该检测器的 `expected_saving_ms` 系数下调；
- 同一 `id` 实测 consistently `within_noise` → 降级为"待淘汰"，不再默认输出；
- 实测**显著但方向相反**（比如关掉后更慢）→ 这是一条**新发现**，提升为该场景的"反直觉条目"。

这就是"自己探索出来"的部分：知识不是我们写死的，而是**由我们自己的实验账本长出来的**。

### 第三层：主动发现（不被清单束缚）

三个来源，都是"从数据出发"而不是"从套路出发"：

1. **未解释时间**：`busy − self` 的空隙、fence stall、`overlap` 等待 → 都是"我们还不知道原因的时间"，
   本身就是待探索的入口；
2. **版本差异**：把两个引擎版本的 `catalog/cvars.json` 做 diff，新增在热区附近的开关就是新杠杆候选；
3. **跨 run 对比**：同一场景 A/B 之外，再做"同一套 cvar 在不同内容上的效果"，
   找出"这条建议只在这类内容成立"的边界条件。

## 5. 验证成本（很重要）

一切都走我们已有的 R9 流程，所以验证本身很便宜：

| 步骤 | 成本 |
|---|---|
| 采集一次 A/B（各 5 run，measure profile） | ~30 分钟机器时间（可后台） |
| R9 判定 | 秒级（只读 manifest，`--significance-only`）+ 退出码给 CI |
| 记录账本 | 一行 JSON |

**没有"跑一条命令就说优化了 X%"这种事**——那正是老套路的来源。

## 6. 当前实现的能力边界（诚实清单）

- 检测器目前 6 个，覆盖 GPU 效果份额 / 饱和 / fence / CPU UI / 单热点 / 分散；
  还没做：内存与加载侧的建议、跨 run 的自动对比、账本自动写入（现在是产出 JSON 由人/agent 填）。
- `expected_saving_ms` 是**保守先验**（份额 × busy × 0.3–0.6），不是模型预测；
  它的作用是把假设排序，不是承诺收益。
- 目录扫描是文本级正则，覆盖 `TAutoConsoleVariable`/`FAutoConsoleVariableRef` 两种写法；
  用 `IConsoleManager` 动态注册的 cvar 扫不到（后续可从运行中的引擎 dump 补齐）。
