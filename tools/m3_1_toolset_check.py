# M3.1 verification: drive the registered PerfEvidence toolset through the ToolsetRegistry
# exactly the way an AI assistant would (execute_tool), using the host control channel to
# launch this script inside the running editor.
import json
import time

import unreal

TOOLSET = "PerfEvidenceToolset.PerfEvidenceToolset"
TRACE = "D:/dsh-output/ue-perf-evidence/runs/m3_1/tool_capture.utrace"


def wait_value(result, label):
    for _ in range(100):
        try:
            value = result.get_editor_property("Value")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            return "ERR reading Value: %s" % exc
        if value:
            unreal.log("[m3_1_check] %s -> %s" % (label, value))
            return value
        time.sleep(0.1)
    unreal.log_error("[m3_1_check] %s -> timed out waiting for async value" % label)
    return "TIMEOUT"


schemas = unreal.ToolsetRegistry.get_all_toolset_json_schemas()
unreal.log("[m3_1_check] schemas len=%d" % len(schemas))
unreal.log("[m3_1_check] has PerfEvidenceToolset: %s"
           % ("PerfEvidenceToolset" in schemas))
unreal.log("[m3_1_check] schema head: %s" % schemas[:600])

start = unreal.ToolsetRegistry.execute_tool(
    TOOLSET, "PerfStartCapture", json.dumps({"TracePath": TRACE}))
wait_value(start, "PerfStartCapture")

time.sleep(3)

stop = unreal.ToolsetRegistry.execute_tool(TOOLSET, "PerfStopCapture", "{}")
wait_value(stop, "PerfStopCapture")

unreal.log("[m3_1_check] done")
