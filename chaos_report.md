# Chronos MCP Chaos Test Report

| Component Tested | Tool | Result | MCP Status | Response Snippet |
|---|---|---|---|---|
| Chaos Target | `what_changed` | TIMEOUT | **TIMEOUT** | `Request timed out` |
| Chaos Target | `what_changed` | TIMEOUT | **TIMEOUT** | `Request timed out` |
| Chaos Target | `as_of_callers` | OK | **OK** | `Error executing tool as_of_callers: 1 validation error for as_of_callersArguments symbol   Field required [type=missing, input_value={'node_id': 'http` |
| Chaos Target | `chronos_acquire_lock` | OK | **OK** | `{   "acquired": true,   "renewed": false,   "node_id": "../../../C:/Windows/System32/fake_0.dll",   "expires_at": "2058-05-05T07:58:58.452799+00:00" }` |
| Chaos Target | `chronos_acquire_lock` | OK | **OK** | `{   "acquired": true,   "renewed": false,   "node_id": "../../../C:/Windows/System32/fake_1.dll",   "expires_at": "2058-05-05T07:58:58.464735+00:00" }` |
| Chaos Target | `chronos_acquire_lock` | OK | **OK** | `{   "acquired": true,   "renewed": false,   "node_id": "../../../C:/Windows/System32/fake_2.dll",   "expires_at": "2058-05-05T07:58:58.472008+00:00" }` |
| Chaos Target | `chronos_acquire_lock` | OK | **OK** | `{   "acquired": true,   "renewed": false,   "node_id": "../../../C:/Windows/System32/fake_3.dll",   "expires_at": "2058-05-05T07:58:58.482451+00:00" }` |
| Chaos Target | `chronos_acquire_lock` | OK | **OK** | `{   "acquired": true,   "renewed": false,   "node_id": "../../../C:/Windows/System32/fake_4.dll",   "expires_at": "2058-05-05T07:58:58.491150+00:00" }` |
| Chaos Target | `chronos_acquire_lock (x50)` | STORM_COMPLETE | **STORM_COMPLETE** | `Fired 50 concurrent locks. Errors: 0` |
| Chaos Target | `chronos_capture_lesson` | TIMEOUT | **TIMEOUT** | `Request timed out` |
| Chaos Target | `chronos_enforce` | OK | **OK** | `Error executing tool chronos_enforce: 2 validation errors for chronos_enforceArguments file_path   Field required [type=missing, input_value={'diff_te` |
| Chaos Target | `chronos_generate_rule` | OK | **OK** | `Error executing tool chronos_generate_rule: 3 validation errors for chronos_generate_ruleArguments rule_text   Field required [type=missing, input_val` |
| Chaos Target | `chronos_promote_rule` | OK | **OK** | `Error executing tool chronos_promote_rule: 1 validation error for chronos_promote_ruleArguments promoted_by   Field required [type=missing, input_valu` |
| Chaos Target | `chronos_check_gate_status` | OK | **OK** | `{   "error": "no such gate",   "gate_id": "not-a-uuid" }` |
| Chaos Target | `chronos_trigger_baseline_recompute` | OK | **OK** | `{   "agents_updated": 0 }` |
| Chaos Target | `chronos_sensitive_reads` | OK | **OK** | `{   "reads": [],   "total": 0,   "high_severity_count": 0 }` |
| Chaos Target | `chronos_anomaly_report` | OK | **OK** | `{   "anomalies": [],   "learning_mode_agents": [],   "total_agents": 0 }` |
| Chaos Target | `chronos_mcp_status` | OK | **OK** | `{   "busy": true,   "in_flight": [     {       "tool": "what_changed",       "agent_id": "(unnamed caller)",       "started_at": "2026-08-27T06:12:09.` |
