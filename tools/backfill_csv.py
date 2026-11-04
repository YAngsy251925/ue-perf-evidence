#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性回填：把历史 run 对应的 CSV Profiler 文件拷进 <run_dir>/csv/（M5.2 验证用）。

新 run 由 perfctl capture 自动归档；这个脚本只处理归档机制上线之前采集的 run。
映射依据：文件名里的时间 = 该 run 结束、CSV 落盘的时刻。
"""
import io
import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pipeline.capture import capture as C  # noqa: E402

UPROJECT = "C:/UEProject/LyraStarterGame/LyraStarterGame.uproject"
MAPPING = {
    "runs/m3_4L2/A5": "Profile(20261103_211239).csv",
    "runs/m3_4L2/B5": "Profile(20261103_211415).csv",
    "runs/m3_4L2/C5": "Profile(20261103_211544).csv",
    "runs/m4_2/chprobe": "Profile(20261103_224539).csv",
}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    src = C.project_csv_dir(UPROJECT)
    if not os.path.isdir(src):
        print("[error] csv dir not found: %s" % src)
        return 2
    for run_rel, name in MAPPING.items():
        path = os.path.join(src, name)
        if not os.path.isfile(path):
            print("[skip] missing %s" % path)
            continue
        dest = os.path.join(REPO, run_rel, "csv")
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(path, os.path.join(dest, name))
        index = {"schema": "perf-csv-index/0", "files": [name],
                 "source": src.replace(os.sep, "/")}
        with io.open(os.path.join(dest, "index.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(index, f, ensure_ascii=False, indent=1)
        print("[ok] %s <- %s (%d bytes)" % (run_rel, name, os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
