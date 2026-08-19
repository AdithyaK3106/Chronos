.PHONY: demo demo-fast

demo:
	python demo/run_demo.py

# Re-run the comparison + enforcement without re-indexing the full commit
# history (the graph is already built) -- fast iteration on the demo output.
demo-fast:
	python demo/run_demo.py --skip-index
