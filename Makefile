.PHONY: demo demo-fast demo-fixture demo-start demo-scenario-1 demo-scenario-2 demo-scenario-3 demo-reset

demo:
	python demo/run_demo.py

# One-time (re)build of the committed novapay/.chronos/ fixture: graph,
# rules, and 3 seeded ledger sessions. `demo` and `demo-fast` don't need
# this on every run -- only after changing novapay's history or the rules.
demo-fixture:
	python demo/build_graph.py
	python demo/seed_rules.py
	python demo/seed_ledger.py

# Re-run the comparison + enforcement without re-indexing the full commit
# history (the graph is already built) -- fast iteration on the demo output.
demo-fast:
	python demo/run_demo.py --skip-index

demo-start:
	python demo/demo_start.py

demo-scenario-1:
	python demo/demo_scenario_1.py

demo-scenario-2:
	python demo/scenario_2_time_travel.py

demo-scenario-3:
	python demo/run_comparison.py

demo-reset:
	python demo/demo_reset.py
