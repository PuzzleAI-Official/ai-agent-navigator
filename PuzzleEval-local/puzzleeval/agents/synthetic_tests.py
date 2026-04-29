"""Back-compat shim — moved to ``puzzleeval.agents.agent3``.

Phase 7: this module re-exports the FULL surface from the new
``agent3`` package, including private (underscore-prefixed)
helpers that test files reach into directly. Existing callers
using ``from puzzleeval.agents.synthetic_tests import X`` keep working unchanged
for both public and private names.

New code should prefer the canonical path
``puzzleeval.agents.agent3.core``.
"""

from puzzleeval.agents.agent3 import core as _core

# Mirror every module attribute (public + private) from core into
# this shim module. Tests that do `from <legacy> import _helper`
# need private names; mock.patch on legacy attributes works because
# they share identity with core attributes (same function objects).
_skip = {"__name__", "__file__", "__doc__", "__loader__", "__spec__",
         "__package__", "__builtins__", "__cached__"}
for _name in dir(_core):
    if _name not in _skip:
        globals()[_name] = getattr(_core, _name)
del _core, _name, _skip
