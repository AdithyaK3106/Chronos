"""Compatibility alias: this module moved to `chronos.agent.condenser`."""

import sys as _sys

from chronos.agent.condenser import *  # noqa: F401,F403
import chronos.agent.condenser as _target

_sys.modules[__name__] = _target
