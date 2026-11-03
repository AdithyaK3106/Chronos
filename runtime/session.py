"""Compatibility alias: this module moved to `chronos.context.session`.

The alias keeps `runtime.session` importable (including private names) for existing code.
"""

import sys as _sys

from chronos.context.session import *  # noqa: F401,F403
import chronos.context.session as _target

_sys.modules[__name__] = _target
