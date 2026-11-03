"""Replay events through Chronos and Cupcake, interleaved (alternating order) so both see the same load."""
import json, os, statistics, subprocess, sys, time

events = json.load(open(sys.argv[1]))
repo = sys.argv[2]; chronos_exe = sys.argv[3]; cupcake_exe = sys.argv[4]; policy_dir = sys.argv[5]; out_path = sys.argv[6]
env = {**os.environ, "CHRONOS_DATA_DIR": os.path.join(os.path.dirname(out_path), "chronos-data")}


def decision_from(stdout: str, code: int) -> str:
    s = stdout.strip()
    if s:
        try:
            d = json.loads(s.splitlines()[-1])
        except json.JSONDecodeError:
            return f"unparsed:{s[:40]}"
        h = d.get("hookSpecificOutput") or {}
        if h.get("permissionDecision"):
            return h["permissionDecision"]
        if d.get("decision") == "block" or d.get("continue") is False:
            return "deny"
    if code == 2:
        return "deny"
    return "allow" if code == 0 else f"error:{code}"


def run(argv, payload):
    t = time.perf_counter()
    r = subprocess.run(argv, input=payload, capture_output=True, text=True, env=env, cwd=repo)
    return decision_from(r.stdout, r.returncode), (time.perf_counter() - t) * 1000, r.stderr[-200:]


engines = {
    "chronos": [chronos_exe, "hook", "claude", "pre"],
    "cupcake": [cupcake_exe, "eval", "--harness", "claude"],
}
rows = []
for i, e in enumerate(events):
    payload = json.dumps(e["event"])
    order = ["chronos", "cupcake"] if i % 2 == 0 else ["cupcake", "chronos"]
    row = {"kind": e["kind"], "tool": e["event"]["tool_name"],
           "path": e["event"]["tool_input"].get("file_path") or e["event"]["tool_input"].get("glob")}
    for name in order:
        d, ms, err = run(engines[name], payload)
        row[name] = d; row[name + "_ms"] = round(ms, 1)
        if d.startswith(("error", "unparsed")):
            row[name + "_err"] = err
    rows.append(row)
json.dump(rows, open(out_path, "w"), indent=0)
for name in engines:
    ms = sorted(r[name + "_ms"] for r in rows)
    print(f"{name:8} p50 {statistics.median(ms):6.0f} ms  p95 {ms[int(.95 * len(ms))]:6.0f} ms")
