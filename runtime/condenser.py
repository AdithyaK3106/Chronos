"""Chronos context condenser integrating with OpenHands SDK CondenserBase."""

from openhands.sdk.context.condenser.base import CondenserBase
from openhands.sdk.context.view import View
from openhands.sdk.event.condenser import Condensation
from openhands.sdk.llm import LLM
from openhands.sdk.event import Event, MessageEvent

class ChronosContextCondenser(CondenserBase):
    """Prunes conversation event history to fit within token budgets while preserving critical context."""

    max_events: int = 20
    keep_first: int = 2

    def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        events = view.events
        if len(events) <= self.max_events:
            return view

        # Preserve the initial system/user events (keep_first) and the most recent events
        kept_head = events[:self.keep_first]
        tail_count = self.max_events - self.keep_first
        kept_tail = events[-tail_count:] if tail_count > 0 else []

        # Return a new immutable view with pruned events
        pruned_events = list(kept_head) + list(kept_tail)
        return View(events=pruned_events)

    async def acondense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        return self.condense(view, agent_llm=agent_llm)
