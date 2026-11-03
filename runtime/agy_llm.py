"""Compatibility alias: this module moved to `chronos.agent.agy_llm`."""

import sys as _sys

from chronos.agent.agy_llm import *  # noqa: F401,F403
import chronos.agent.agy_llm as _target

_sys.modules[__name__] = _target
