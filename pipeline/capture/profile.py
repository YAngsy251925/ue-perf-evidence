#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""profile.py — 采集 profile：加载、校验、生成启动命令（PLAN §7.2 / §9-M2.1）

profile（harness/profiles/*.json）声明"一次 run 怎么启动"：地图、RHI、窗口、trace 通道与 tail、
固定采集条件（cvar）。prepare 把它落成 run 目录里的两份文件：
  launch.bat           交给 harness/coexist_run.sh 启动（P1–P3 的进程纪律在 harness 里）
  capture_profile.json 声明快照；run 结束后 manifest 拿它和引擎日志逐条对账（caliber/conditions.py）

命令行设计依据（源码核实，PLAN 附录 A）：
  - cvar 与 r.SetRes 一律放进 -ExecCmds：命令在第 0 帧作为延迟命令执行（UnrealEngine.cpp:2552），
    优先级是 ECVF_SetByConsole = 0x10000000，为最高档（IConsoleManager.h:187）；GameUserSettings 用
    ECVF_SetByCode（0x0E）写 r.GTSyncType（GameUserSettings.cpp:423），盖不过它。每条设置都会在日志里
    回显 `<cvar> = "<value>"`，这是对账的证据
  - -ExecCmds 只在非 Shipping 构建解析（UnrealEngine.cpp 那段代码在 !UE_BUILD_SHIPPING 里）
  - -ExecCmds 按逗号分割命令（ParseExecCommands.cpp:28，单引号内除外），所以命令里不允许逗号和单引号
  - r.SetRes 是注册过的 cvar（UnrealEngine.cpp:504）；不开 CSV Profiler 时，日志里只有它的回显能证明分辨率
  - -trace 里的 "file" 是平台文件 I/O 通道（PlatformFileTrace.cpp:19），不是输出目标；输出路径用 -tracefile
  - 退出：引擎没有通用的"运行 N 秒后退出"；唯一的干净退出是 -ExitAfterCsvProfiling（CsvProfiler.cpp:4309），
    要连带开 CSV Profiler。M2.1 先用 harness 的时长模式，M2.2 再定
  - 时长从"引擎就绪"算起，不从启动算起：harness 等日志出现 "Engine is initialized"（LaunchEngineLoop.cpp:4974，
    B2 实测之后 45ms 内开始出帧）再计时；冷启动实测从 12 秒到 100 秒以上（着色器类型初始化、Zen 冷启动、
    UBT AutoSDK），startup_budget_s 只是等就绪的上限
"""
import argparse
import hashlib
import json
import os
import sys

SCHEMA = "perf-profile/0"
SNAPSHOT_SCHEMA = "perf-capture/0"
CLASSES = ("smoke", "measure")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROFILE_DIR = os.path.join(REPO, "harness", "profiles")
DEFAULT_UPROJECT = os.path.join(REPO, "host", "TraceDigestProbe", "TraceDigestProbe.uproject")
DEFAULT_UE_ROOT = r"D:\Epic\Games\UE_5.8"
MEASURE_REQUIRED_CVARS = ("t.MaxFPS", "r.VSync", "r.GTSyncType", "t.IdleWhenNotForeground")  # §7.2
REQUIRED_CHANNELS = ("cpu", "frame")  # R4：没有 frame 通道就没有帧轨道（example.utrace 的教训）
MEASURE_REQUIRED_CHANNELS = ("gpu",)  # M4.0 的 GPU 视图要 GPU 时间线
QUIT_WORDS = ("quit", "exit")
BAT_SPECIAL = '%"&|<>^'  # 生成的 bat 里有特殊含义的字符；值里一律不允许


def profile_path(name_or_path: str) -> str:
    if os.sep in name_or_path or "/" in name_or_path or name_or_path.endswith(".json"):
        return os.path.abspath(name_or_path)
    return os.path.join(PROFILE_DIR, name_or_path + ".json")


def canonical_sha256(profile: dict) -> str:
    """对规范化 JSON 取 sha256：与换行风格、缩进无关。"""
    text = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


COVERAGE_EVENT_START = "PerfEvidence CoverageStart"   # 宿主模块发的 CSV_EVENT / TRACE_BOOKMARK
COVERAGE_EVENT_STOP = "PerfEvidence CaptureStop"


def exec_at_config(profile: dict):
    """归一化 exec_at 块（hitch-hunt 的定时命令注入，M2.3）；没有则 None。
    命令经 PerfEvidence.ExecAt=<秒>:<命令> 由宿主模块在运行该秒数后排进延迟命令队列。"""
    at = profile.get("exec_at")
    if not at:
        return None
    return {"at_s": float(at["at_s"]), "command": str(at["command"]),
            # in_probe=true：这个定时命令**故意**放进 coverage_probe 段（M6.1 的 memreport），
            # 目的是让重操作远离统计窗口，而不是像 hitch-hunt 那样要在干净段里制造 hitch。
            "in_probe": bool(at.get("in_probe", False))}


def csv_config(profile: dict) -> dict:
    """归一化 csv 块；没有 csv 块返回 None。coverage 在运行尾段（P15）：
    就绪后 coverage_start_s 秒开 CSV（含写文件尖峰的探针段），stop_at_s 秒发停止事件，
    ExitAfterCsvProfiling 写完 CSV 干净退出。"""
    csv = profile.get("csv")
    if not csv:
        return None
    return {
        "coverage_start_s": float(csv["coverage_start_s"]),
        "stop_at_s": float(csv["stop_at_s"]),
        "stop_event": COVERAGE_EVENT_STOP,
        "exit": csv.get("exit", "exit_after_csv"),
    }


def exec_commands(profile: dict) -> list:
    """cvar（按声明顺序）→ r.SetRes（若 set_res_cvar 且有窗口）→ 其余 exec_cmds。"""
    cmds = ["%s %s" % (name, value) for name, value in (profile.get("cvars") or {}).items()]
    window = profile.get("window")
    if profile.get("set_res_cvar") and window:
        cmds.append("r.SetRes %dx%dw" % (window["resx"], window["resy"]))
    cmds += list(profile.get("exec_cmds") or [])
    return cmds


def _unsafe(value) -> str:
    """值不安全时返回原因，否则返回空串（E2 / E3：生成的 bat 与路径只用 ASCII）。"""
    text = str(value)
    if any(ord(c) > 127 for c in text):
        return "non-ASCII"
    bad = sorted(set(c for c in text if c in BAT_SPECIAL))
    return "contains %s" % "".join(bad) if bad else ""


def validate(p: dict) -> list:
    """返回问题列表；空列表表示合法。"""
    problems = []
    if p.get("schema") != SCHEMA:
        problems.append("schema must be %s (got %r)" % (SCHEMA, p.get("schema")))
    if not p.get("name"):
        problems.append("name is required")
    cls = p.get("class")
    if cls not in CLASSES:
        problems.append("class must be one of %s (got %r)" % (list(CLASSES), cls))
    rhi = p.get("rhi")
    if rhi not in ("null", "default"):
        problems.append("rhi must be 'null' or 'default' (got %r)" % rhi)
    if cls == "smoke" and rhi != "null":
        problems.append("smoke profile must use rhi=null (R11: smoke only checks the pipeline)")
    if cls == "measure" and rhi != "default":
        problems.append("measure profile must use a real RHI (rhi=default)")
    if not str(p.get("map", "")).startswith("/"):
        problems.append("map must be a package path such as /Engine/Maps/Entry")

    for flag in p.get("flags") or []:
        if not str(flag).startswith("-"):
            problems.append("flag %r must start with '-'" % flag)
        if str(flag).lower().startswith(("-execcmds", "-trace", "-abslog")):
            problems.append("flag %r is generated from the profile; do not set it in flags" % flag)

    window = p.get("window")
    if window is not None:
        if not window.get("windowed"):
            problems.append("window.windowed must be true")
        if not (isinstance(window.get("resx"), int) and isinstance(window.get("resy"), int)):
            problems.append("window.resx / window.resy must be integers")
    if cls == "measure" and window is None:
        problems.append("measure profile needs a fixed window resolution (PLAN §7.2)")

    trace = p.get("trace")
    if trace is None:
        problems.append("trace is required (the pipeline reads the trace for every run)")
    else:
        channels = list(trace.get("channels") or [])
        for ch in REQUIRED_CHANNELS:
            if ch not in channels:
                problems.append("trace.channels must include %r (R4)" % ch)
        if cls == "measure":
            for ch in MEASURE_REQUIRED_CHANNELS:
                if ch not in channels:
                    problems.append("measure trace.channels must include %r (GPU timeline)" % ch)
        if "file" in channels:
            problems.append("'file' is the platform file I/O channel (PlatformFileTrace.cpp:19), "
                            "not an output switch; drop it (the output path is -tracefile)")
        if not (isinstance(trace.get("tail_mb"), int) and trace["tail_mb"] > 0):
            problems.append("trace.tail_mb must be a positive integer")

    cvars = p.get("cvars") or {}
    if cls == "measure":
        for name in MEASURE_REQUIRED_CVARS:
            if name not in cvars:
                problems.append("measure profile must pin %s (PLAN §7.2)" % name)
    try:
        cmds = exec_commands(p)
    except (KeyError, TypeError, ValueError):
        cmds = []  # window 字段不合法，上面已经报过
    for cmd in cmds:
        words = cmd.split()
        if "," in cmd or "'" in cmd:
            problems.append("exec command %r contains ',' or \"'\" (ExecCmds splits on commas)" % cmd)
        if words and words[0].lower() in QUIT_WORDS:
            problems.append("exec command %r would quit in the same tick (P1)" % cmd)

    at = exec_at_config(p)
    if at is not None:
        if at["at_s"] <= 0:
            problems.append("exec_at.at_s must be > 0 (seconds after engine ready)")
        if "," in at["command"] or "'" in at["command"]:
            problems.append("exec_at.command %r contains ',' or \"'\" (command line splits on commas)" % at["command"])
        if at["command"].split()[0].lower() in QUIT_WORDS:
            problems.append("exec_at.command %r would quit in the same tick (P1)" % at["command"])

    cond = p.get("conditions") or {}
    if not (isinstance(cond.get("warmup_frames"), int) and cond["warmup_frames"] >= 0):
        problems.append("conditions.warmup_frames must be an integer >= 0")
    if not (isinstance(cond.get("duration_s"), int) and cond["duration_s"] > 0):
        problems.append("conditions.duration_s must be a positive integer")
    stop = p.get("stop") or {}
    csv = csv_config(p)
    if stop.get("mode") not in ("harness_duration", "clean_exit"):
        problems.append("stop.mode must be 'harness_duration' or 'clean_exit' (P15)")
    if stop.get("mode") == "clean_exit":
        if cls != "measure":
            problems.append("clean_exit is for measure runs; smoke uses harness_duration")
        if csv is None:
            problems.append("clean_exit requires a csv block (-ExitAfterCsvProfiling only fires when a "
                            "CSV capture runs and ends)")
        elif csv["exit"] != "exit_after_csv":
            problems.append("csv.exit must be 'exit_after_csv' for clean_exit")
    if not (isinstance(stop.get("startup_budget_s"), int) and stop["startup_budget_s"] > 0):
        problems.append("stop.startup_budget_s must be a positive integer (upper bound for reaching the ready marker)")
    if stop.get("mode") == "clean_exit":
        if not (isinstance(stop.get("exit_budget_s"), int) and stop["exit_budget_s"] > 0):
            problems.append("clean_exit needs stop.exit_budget_s > 0 (kill fallback after the CSV stop point)")
    else:
        if not (isinstance(stop.get("settle_s"), int) and stop["settle_s"] >= 0):
            problems.append("stop.settle_s must be an integer >= 0 (extra seconds so the steady window covers duration_s)")

    if csv is not None:
        if csv["coverage_start_s"] <= 0:
            problems.append("csv.coverage_start_s must be > 0 (seconds after engine ready)")
        if csv["stop_at_s"] <= csv["coverage_start_s"]:
            problems.append("csv.stop_at_s must be > csv.coverage_start_s")
        if csv["exit"] != "exit_after_csv":
            problems.append("csv.exit must be 'exit_after_csv' (P15)")
        if cls == "measure" and csv["coverage_start_s"] < cond.get("duration_s", 0):
            problems.append("csv.coverage_start_s must be >= conditions.duration_s (the CSV-free segment "
                            "must cover the declared duration; P15 coverage probe lives at the end)")
        at = exec_at_config(p)
        if at and at.get("in_probe"):
            if not (csv["coverage_start_s"] <= at["at_s"] < csv["stop_at_s"]):
                problems.append("exec_at.in_probe=true requires coverage_start_s <= at_s < stop_at_s "
                                "(重操作必须整段落在探针窗口内，M6.1)")
        elif at and at["at_s"] >= csv["coverage_start_s"]:
            problems.append("exec_at.at_s must be < csv.coverage_start_s (the injected hitch belongs in the "
                            "CSV-free measurement segment); 内存快照请用 in_probe=true")

    for label, value in [("map", p.get("map", ""))] + [("flag", f) for f in p.get("flags") or []] \
            + [("exec command", c) for c in cmds]:
        reason = _unsafe(value)
        if reason:
            problems.append("%s %r is not bat-safe (%s)" % (label, value, reason))
    return problems


def load_profile(name_or_path: str):
    path = profile_path(name_or_path)
    if not os.path.isfile(path):
        raise ValueError("profile not found: %s" % path)
    with open(path, encoding="utf-8") as f:
        profile = json.load(f)
    problems = validate(profile)
    if problems:
        raise ValueError("invalid profile %s:\n  - %s" % (path, "\n  - ".join(problems)))
    return profile, path


def build_args(profile: dict, run_dir: str, uproject: str, map_override=None) -> list:
    """profile → UnrealEditor-Cmd 的参数（不含 exe）。路径和 ExecCmds 已加双引号。"""
    args = ['"%s"' % uproject, map_override or profile["map"], "-game"]
    if profile["rhi"] == "null":
        args.append("-nullrhi")
    window = profile.get("window")
    if window:
        args += ["-windowed", "-resx=%d" % window["resx"], "-resy=%d" % window["resy"]]
    args += list(profile.get("flags") or [])
    if profile.get("statnamedevents"):
        args.append("-statnamedevents")
    trace = profile["trace"]
    args += ["-trace=%s" % ",".join(trace["channels"]),
             '-tracefile="%s"' % os.path.join(run_dir, "trace.utrace"),
             "-tracetailmb=%d" % trace["tail_mb"],
             "-stdout",
             '-ABSLOG="%s"' % os.path.join(run_dir, "engine.log")]
    csv = csv_config(profile)
    if csv:
        # coverage_probe（P15）：尾段探针。CSV 用事件对开停：CoverageStart 事件启动采集
        #（-csvStartOnEvent，帧数上限给足，真正结束靠 CaptureStop 上的 -csvStopOnEvent），
        # EndCapture 后 -ExitAfterCsvProfiling 写完 CSV 干净退出。CSV 关闭段没有任何开销。
        args += ["PerfEvidence.CoverageStart=%g" % csv["coverage_start_s"],
                 "PerfEvidence.CaptureStop=%g" % csv["stop_at_s"],
                 '-csvStartOnEvent="%s"' % COVERAGE_EVENT_START,
                 "-csvCaptureOnEventFrameCount=200000",
                 '-csvStopOnEvent="%s"' % csv["stop_event"],
                 "-ExitAfterCsvProfiling"]
    at = exec_at_config(profile)
    if at:
        args.append("PerfEvidence.ExecAt=%g:%s" % (at["at_s"], at["command"]))
    cmds = exec_commands(profile)
    if cmds:
        args.append('-ExecCmds="%s"' % ",".join(cmds))
    return args


def render_bat(exe: str, args: list, profile: dict, sha: str) -> str:
    lines = ["@echo off",
             "rem generated by perfctl profile prepare: profile=%s sha256=%s" % (profile["name"], sha[:16]),
             "rem do not edit; re-run prepare instead",
             '"%s" %s' % (exe, " ".join(args)),
             "echo EXITCODE=%ERRORLEVEL%"]
    return "\r\n".join(lines) + "\r\n"  # bat 用 CRLF（.gitattributes 同一约定）


def harness_timing(profile: dict):
    """(启动预算, 就绪后运行秒数)。clean_exit：就绪后等 stop_at_s + exit_budget_s（正常情况引擎
    在 stop_at_s 后自己写完 CSV 退出；超过预算才强杀兜底）；harness_duration：duration_s + settle_s。"""
    stop = profile["stop"]
    if stop["mode"] == "clean_exit":
        return stop["startup_budget_s"], profile["csv"]["stop_at_s"] + stop["exit_budget_s"]
    return stop["startup_budget_s"], profile["conditions"]["duration_s"] + stop["settle_s"]


def prepare(name_or_path: str, run_dir: str, uproject=None, ue_root=None, map_override=None) -> dict:
    """写 <run_dir>/launch.bat 与 <run_dir>/capture_profile.json；返回快照。"""
    profile, path = load_profile(name_or_path)
    run_dir = os.path.abspath(run_dir)
    uproject = os.path.abspath(uproject or DEFAULT_UPROJECT)
    ue_root = ue_root or os.environ.get("UE_ROOT") or DEFAULT_UE_ROOT
    exe = os.path.join(ue_root, "Engine", "Binaries", "Win64", "UnrealEditor-Cmd.exe")
    for label, value in (("run_dir", run_dir), ("uproject", uproject), ("ue_root", ue_root),
                         ("map", map_override or "")):
        reason = _unsafe(value)
        if reason:
            raise ValueError("%s is not bat-safe (%s): %s (PLAN E3)" % (label, reason, value))
    if not os.path.isfile(exe):
        raise ValueError("UnrealEditor-Cmd.exe not found: %s (use --ue-root or UE_ROOT)" % exe)
    if not os.path.isfile(uproject):
        raise ValueError("uproject not found: %s" % uproject)
    for leftover in ("engine.log", "trace.utrace"):
        if os.path.exists(os.path.join(run_dir, leftover)):
            raise ValueError("run_dir already holds %s; one run per directory, pick a new run_dir" % leftover)
    os.makedirs(run_dir, exist_ok=True)

    sha = canonical_sha256(profile)
    args = build_args(profile, run_dir, uproject, map_override)
    bat = render_bat(exe, args, profile, sha)
    bat_path = os.path.join(run_dir, "launch.bat")
    with open(bat_path, "w", encoding="ascii", newline="") as f:
        f.write(bat)
    startup_budget, run_after_ready = harness_timing(profile)
    snapshot = {
        "schema": SNAPSHOT_SCHEMA,
        "profile_name": profile["name"],
        "profile_class": profile["class"],
        "profile_sha256": sha,
        "profile_file": os.path.relpath(path, REPO).replace("\\", "/") if path.startswith(REPO) else path,
        "profile": profile,
        "map": map_override or profile["map"],
        "uproject": uproject,
        "ue_root": ue_root,
        "exec_commands": exec_commands(profile),
        "args": args,
        "exec_at": exec_at_config(profile),
        "csv": csv_config(profile),
        "harness": {
            "startup_budget_s": startup_budget,
            "run_after_ready_s": run_after_ready,
            "command": "bash harness/coexist_run.sh %s %s %d - - %d" % (
                bat_path.replace("\\", "/"), run_dir.replace("\\", "/"), startup_budget, run_after_ready),
        },
    }
    with open(os.path.join(run_dir, "capture_profile.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)
    return snapshot


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # PLAN 7.4 E4: UTF-8 output without relying on PYTHONUTF8
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="采集 profile（PLAN §9-M2.1）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出 harness/profiles 下的 profile")
    p = sub.add_parser("show", help="校验并打印 profile 与它生成的启动参数")
    p.add_argument("profile", help="profile 名或 json 路径")
    p = sub.add_parser("prepare", help="在 run 目录写 launch.bat 与 capture_profile.json")
    p.add_argument("profile", help="profile 名或 json 路径")
    p.add_argument("run_dir", help="新的 run 目录（每个 run 一个目录）")
    p.add_argument("--map", default=None, help="覆盖 profile 的地图")
    p.add_argument("--uproject", default=None, help="宿主工程（默认 host/TraceDigestProbe）")
    p.add_argument("--ue-root", default=None, help="UE 根目录（默认读 UE_ROOT）")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "list":
            for fn in sorted(os.listdir(PROFILE_DIR)):
                if fn.endswith(".json"):
                    prof, _ = load_profile(os.path.join(PROFILE_DIR, fn))
                    print("%-10s %-8s %s" % (prof["name"], prof["class"], prof.get("description", "")))
            return 0
        if a.cmd == "show":
            prof, path = load_profile(a.profile)
            print("[ok] %s (sha256 %s)" % (path, canonical_sha256(prof)[:16]))
            print("exec commands: %s" % exec_commands(prof))
            print("args: %s" % " ".join(build_args(prof, r"<run_dir>", r"<uproject>")))
            print("harness: startup budget %ds, run %ds after ready" % harness_timing(prof))
            return 0
        snap = prepare(a.profile, a.run_dir, a.uproject, a.ue_root, a.map)
    except ValueError as e:
        print("[error] %s" % e)
        return 1
    print("[ok] prepared %s run in %s" % (snap["profile_name"], os.path.abspath(a.run_dir)))
    print("  launch.bat + capture_profile.json written (profile sha256 %s)" % snap["profile_sha256"][:16])
    print("  next: %s" % snap["harness"]["command"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
