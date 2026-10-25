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

        # Preserve the initial system/user events (keep_first) which contain Chronos context
        kept_head = events[:self.keep_first]
        remaining_budget = self.max_events - self.keep_first

        # Prioritize keeping critical security rejections or lease alerts from middle history
        middle_events = events[self.keep_first:-remaining_budget] if len(events) > (self.keep_first + remaining_budget) else []
        critical_middle = []
        for ev in middle_events:
            text_repr = str(getattr(ev, "content", "")) + str(getattr(ev, "observation", ""))
            if any(marker in text_repr for marker in ("Chronos", "Veto", "blocked", "lease", "locked")):
                critical_middle.append(ev)

        tail_events = events[-remaining_budget:] if remaining_budget > 0 else []

        # Return a new immutable view with pinned head, critical governance events, and recent tail
        pruned_events = list(kept_head) + critical_middle[:3] + list(tail_events)
        return View(events=pruned_events)

    async def acondense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        return self.condense(view, agent_llm=agent_llm)
