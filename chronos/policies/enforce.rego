# Chronos Wedge 4 — enforcement decision policy.
#
# This file is read by `opa eval` at runtime. It is deliberately a real file,
# not a Python string, so it can be audited, diffed, and edited without
# touching Chronos. Input shape (one violation per evaluation):
#
#   {"rule_id": str, "rule_status": "blocking"|"warn-only-validated"|
#    "warn-only-unvalidated", "deprecated_in_graph": bool,
#    "deprecated_since": str|null, "matched_node": str}
#
# Output: {"verdict": "block"|"warn", "reason": str}
#
# Rego v1 syntax (`if` required before rule bodies). OPA >= 1.0 rejects the v0
# form. Verified against OPA 1.19.0.
#
# The blocking branch requires BOTH an explicitly promoted rule AND temporal
# confirmation from Wedge 1. A pattern match alone never blocks a merge --
# that is the whole point of wiring enforcement to the graph rather than to a
# static list.

package chronos

enforce := result if {
	input.rule_status == "blocking"
	input.deprecated_in_graph == true
	result := {
		"verdict": "block",
		"reason": "pattern matches deprecated node confirmed by temporal graph",
	}
}

enforce := result if {
	input.rule_status != "blocking"
	result := {"verdict": "warn", "reason": "rule in warn-only mode"}
}

enforce := result if {
	input.rule_status == "blocking"
	input.deprecated_in_graph == false
	result := {
		"verdict": "warn",
		"reason": "pattern matched but graph does not confirm deprecation",
	}
}
