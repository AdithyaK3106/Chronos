"""Compatibility alias: this module moved to `chronos.governance.firewall`.

The alias keeps `runtime.hooks` importable (including private names) for existing code.
"""

import sys as _sys

from chronos.governance.firewall import *  # noqa: F401,F403
import chronos.governance.firewall as _target

_sys.modules[__name__] = _target
