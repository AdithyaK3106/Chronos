# Auto-loads ChronosCapturePlugin for any pytest run inside this repo.
#
# This lives at the repo root, not in chronos/, because pytest only auto-loads
# conftest.py from rootdir and test directories — one inside the package would
# never be read.
#
# For partner repos: they either copy this conftest.py or install chronos as a
# package, which registers the plugin via entry_points automatically.
pytest_plugins = ["chronos.pytest_plugin"]
