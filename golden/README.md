# Golden 回归集（PLAN §10.2，M1.7）

`golden.json` 固化了四条实测 run 的口径结论与 cpu digest / diff 的关键数字（无机器路径、无时间戳，
进 git）。每次跑测试套件时 `tests/test_golden.py` 用当前代码重算并逐字段对比——引擎升级或导出器
口径变化会首先在这里失败，这就是它的职责。

## 覆盖

| 类别 | run | 固化内容 |
|---|---|---|
| 口径类 | example（桥接未生效、按 trace 判 real） | manifest 全部口径字段 |
| 口径类 | B2_smoke_bridge（桥接生效、nullrhi、不可测量） | 同上 + not_measurable digest |
| 口径类 | C_smoke_hitch（nullrhi、游戏帧轨道编号 0–16200） | manifest |
| 形态类 | coexist_D（真实 RHI，Game 737 / Rendering 736） | manifest + digest（moderate）|
| — | example vs d3d12 | diff（Top 差值 / 等待类 / 新增 162 / 独有 70）|
| 性能类 | 待 M2.5 在 Lyra 建立（固定相机 + 已知 hitch + 期望 Top-N） | — |

## 用法

```bash
# 重放（跑测试套件即含）：约 4 秒，窗口导出走 prepare_rows 缓存
python -X utf8 -m unittest discover -s tests -t .

# 重新生成（确定性重算；需要本机有 tests/acceptance_data.py 指向的实验数据）
python -X utf8 -m tests.make_golden
```

**重新生成是刻意动作，不是随手刷新**：先看 test_golden 失败的具体字段，确认变化是预期
（引擎升级 / 导出器变化 / 口径规则修改）再覆盖，并把 golden.json 的变更写进提交说明。

## 前提

本机存在 `tests/acceptance_data.py` 指向的实验目录数据（`tests/acceptance_data.py` 里 `LAB` 指向的实验目录，以及
`runs/m1_1`）；缺失时相关测试自动跳过。trace 本体不进 git（体积原因），路径见
`tests/acceptance_data.py`。
