"""Compatibility alias: this module moved to `chronos.agent.gateway`."""

import sys as _sys

from chronos.agent.gateway import *  # noqa: F401,F403
import chronos.agent.gateway as _target

_sys.modules[__name__] = _target
