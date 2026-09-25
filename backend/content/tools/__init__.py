"""The agent's tool catalogue, one module per feature area.

Each module declares a ``TOOLS`` tuple of tool entries; ``agent.py`` imports
the aggregation below and binds it to the loop. A new feature area is a new
module plus one line in ``_MODULES`` — the guard, the approval flow and the
prompt need no changes.

Every entry is:

- ``security``  — the level the guard enforces (``read`` / ``write`` / …).
  Server-side only: it lives on the outer dict, never inside ``tool_dict``,
  so the LLM never sees it. A tool without one is treated as ``admin``.
- ``function``  — the implementation, whose first parameter is the ``pid``
  of the project the run answers in (``prepare_tools`` binds it), and whose
  second is the calling ``user`` when ``wants_user`` is set — the identity
  every write is authored as.
- ``tool_dict`` — the schema the LLM sees: name, description, parameters.
- ``available`` — optional async ``fn(pid) -> bool``: a per-project module
  gate. False keeps the tool out of the run's schema and its calls are
  refused as unknown (e.g. the learning tools behind ``allow_quiz``).
"""

from .library import TOOLS as _LIBRARY
from .notes import TOOLS as _NOTES
from .learning import TOOLS as _LEARNING
from .board import TOOLS as _BOARD
from .transfer import TOOLS as _TRANSFER

_MODULES = (_LIBRARY, _NOTES, _LEARNING, _BOARD, _TRANSFER)

TOOLS = tuple(t for module in _MODULES for t in module)
