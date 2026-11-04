#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pack_manifests.py — 把一批 run 的 manifest 打成"CI 用的瘦包"（M6.4）

CI 闸门（`perfctl diff … --significance-only`）只读 manifest 里的 p50，不需要 trace。
trace 动辄几十上百 MB 且 runs/ 是 gitignore 的，所以 CI 里传的是这个瘦包：每个 run 只有
`<bundle>/<run_id>/manifest.json`（几 KB），解压后直接 `-a <bundle>/r1 … -b <bundle>/r2 …` 即可。

用法：
  python tools/pack_manifests.py --out bundle/ --root runs --runs A1 A2 A3 A4 A5 B1 B2 B3 B4 B5
  python tools/pack_manifests.py --out bundle/ --manifest runs/A1/manifest.json --manifest runs/A2/manifest.json
"""
import argparse
import io
import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def copy_manifest(manifest_path, bundle, run_id=None):
    src = os.path.abspath(manifest_path)
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    with io.open(src, encoding="utf-8") as f:
        manifest = json.load(f)
    name = run_id or manifest.get("run_id") or os.path.basename(os.path.dirname(src))
    dest_dir = os.path.join(bundle, name)
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copy2(src, os.path.join(dest_dir, "manifest.json"))
    return name, os.path.getsize(src)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="把 run 的 manifest 打成 CI 用的瘦包（M6.4）")
    ap.add_argument("--out", required=True, help="瘦包输出目录")
    ap.add_argument("--root", default=os.path.join(REPO, "runs"), help="runs 根目录")
    ap.add_argument("--runs", nargs="*", default=[], help="run 目录名（相对 --root）")
    ap.add_argument("--manifest", action="append", default=[], help="直接给 manifest.json 路径（可重复）")
    a = ap.parse_args(argv)

    if not a.runs and not a.manifest:
        print("[error] 至少要给 --runs 或 --manifest")
        return 2
    os.makedirs(a.out, exist_ok=True)
    total = 0
    copied = []
    try:
        for name in a.runs:
            run_dir = a.runs and os.path.join(a.root, name)
            path = os.path.join(run_dir, "manifest.json")
            run_id, size = copy_manifest(path, a.out, name)
            copied.append(run_id)
            total += size
        for path in a.manifest:
            run_id, size = copy_manifest(path, a.out)
            copied.append(run_id)
            total += size
    except FileNotFoundError as exc:
        print("[error] manifest not found: %s" % exc)
        return 1

    index = os.path.join(a.out, "bundle.json")
    with io.open(index, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"schema": "perf-manifest-bundle/0", "runs": copied}, f, ensure_ascii=False, indent=1)
    print("[pack] %d 个 manifest → %s（合计 %.1f KB，无 trace）"
          % (len(copied), os.path.abspath(a.out), total / 1024.0))
    print("[ok] %s" % index)
    return 0


if __name__ == "__main__":
    sys.exit(main())
