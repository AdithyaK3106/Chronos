# Chronos capture is registered by the `pytest11` entry point in pyproject.toml
# (see chronos/pytest_plugin.py), so this file no longer loads it.
#
# It used to say `pytest_plugins = ["chronos.pytest_plugin"]`. That was a
# workaround from before the entry point existed, and it hid a real bug: with
# the entry point missing from an installed dist-info, capture worked in THIS
# repo and was silently dead in every other one. Once the entry point was
# registered, loading it here too made pytest abort the whole run with
# "Plugin already registered under a different name".
#
# pytest_plugin.pytest_configure() also refuses to register a second capture
# plugin, so a partner repo that copied the old conftest keeps working.
