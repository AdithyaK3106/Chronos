"""Compatibility alias: this module moved to `chronos.agent.openhands_adapter`."""

import sys as _sys

from chronos.agent.openhands_adapter import *  # noqa: F401,F403
import chronos.agent.openhands_adapter as _target

_sys.modules[__name__] = _target
