#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dump_umg.py — 把 UMG 控件树 / 绑定 / 蓝图节点导成 JSON（M10.2）

为什么需要它：trace 只记运行时耗时，没有"控件树结构、绑定函数、K2 节点图"。要给
"这个控件为什么每帧重绘、布局/逻辑哪里不对"这种建议，必须读资产。

读资产要绕一道：`UWidgetBlueprint::WidgetTree` / `Bindings`、`UBlueprint::UbergraphPages`
在 Python 里是 **protected**（实测报 "Property ... is protected and cannot be read"），
所以由 C++ 库 `UPerfEvidenceUILibrary`（PerfEvidenceToolset 插件）暴露导出函数，这个脚本只负责
遍历资产、调用它、落 JSON。

产物（每个 WidgetBlueprint）：
  widgets[]  控件树：depth / name / class / visibility
  bindings[] 属性绑定：object / property / function（"每帧重绘"最常见的原因）
  graph{}   Ubergraph 节点直方图 + 事件入口名（逻辑复杂度与热点）

用法（在编辑器进程里跑）：
  # A) 编辑器菜单 Tools → Execute Python Script…，选本文件
  # B) 命令行：
  UnrealEditor-Cmd.exe <uproject> -run=pythonscript -script="<repo>/tools/dump_umg.py" \
      -unattended -nosplash -nullrhi -- --prefix /Game/UI --limit 20 --out D:/out/umg.json

  参数也可用环境变量：UMG_PREFIX / UMG_LIMIT / UMG_OUT
"""
import json
import os
import sys

import unreal

DEFAULTS = {"prefix": "/Game", "limit": 20, "out": os.path.join(os.getcwd(), "umg_dump.json")}


def parse_args():
    args = dict(DEFAULTS)
    for key, env in (("prefix", "UMG_PREFIX"), ("limit", "UMG_LIMIT"), ("out", "UMG_OUT")):
        if os.environ.get(env):
            args[key] = os.environ[env]
    argv = sys.argv
    if "--" in argv:
        rest = argv[argv.index("--") + 1:]
        i = 0
        while i < len(rest):
            key = rest[i].lstrip("-")
            if key in ("prefix", "limit", "out") and i + 1 < len(rest):
                args[key] = rest[i + 1]
                i += 2
                continue
            i += 1
    args["limit"] = int(args["limit"])
    return args


def main():
    args = parse_args()
    try:
        paths = unreal.PerfEvidenceUILibrary.list_widget_blueprints(args["prefix"], args["limit"])
    except Exception as exc:
        print("[dump_umg] 找不到 UPerfEvidenceUILibrary（PerfEvidenceToolset 插件没启用？）: %s" % exc)
        return 1
    print("[dump_umg] %s 下取 %d 个 WidgetBlueprint（上限 %d）"
          % (args["prefix"], len(paths), args["limit"]))

    assets = []
    for path in paths:
        try:
            payload = unreal.PerfEvidenceUILibrary.dump_widget_blueprint(path)
            rec = json.loads(payload)
        except Exception as exc:
            rec = {"path": path, "error": "%s: %s" % (type(exc).__name__, exc)}
        assets.append(rec)
        print("  %-62s widgets=%3s bindings=%2s nodes=%3s%s"
              % (str(rec.get("path"))[:62], len(rec.get("widgets") or []),
                 len(rec.get("bindings") or []), (rec.get("graph") or {}).get("node_count", 0),
                 ("  ERR " + str(rec.get("error"))[:36]) if rec.get("error") else ""))

    out = {"schema": "perf-umg-dump/0", "prefix": args["prefix"],
           "asset_count": len(assets), "assets": assets}
    out_path = args["out"]
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("[dump_umg] 写出 %s" % out_path)
    return 0


sys.exit(main())
