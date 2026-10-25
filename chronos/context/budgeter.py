"""Context ranking and token budgeting for agent prompt injection."""

from typing import Any
from chronos.context.graph import GraphStore

# Approximate token estimator (~4 characters per token)
def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def assemble_context(
    symbols: list[str],
    graph_store: GraphStore,
    token_budget: int = 4000,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Retrieve graph-aware repository context and rank within a token budget."""
    sections: list[str] = [
        "### ⏳ Chronos Repository Context (Bi-Temporal AST Graph)",
        f"Context assembled as-of: {as_of or 'latest'}\n",
    ]
    current_tokens = estimate_tokens("\n".join(sections))

    included_symbols: list[str] = []

    for sym in symbols:
        callers = graph_store.as_of_callers(sym, as_of=as_of)
        callees = graph_store.as_of_callees(sym, as_of=as_of)
        impact = graph_store.as_of_impact(sym, as_of=as_of, max_depth=2)

        sym_lines = [
            f"#### Symbol: `{sym}`",
            f"- **Callers ({len(callers)}):** {', '.join([c['name'] for c in callers[:10]]) or 'None'}",
            f"- **Callees ({len(callees)}):** {', '.join([c['name'] for c in callees[:10]]) or 'None'}",
            f"- **Blast Radius / Impact ({len(impact)} downstream symbols):** {', '.join([i['name'] for i in impact[:10]]) or 'Local only'}",
            "",
        ]
        sym_block = "\n".join(sym_lines)
        block_tokens = estimate_tokens(sym_block)

        if current_tokens + block_tokens > token_budget:
            sections.append("*(Remaining context truncated to respect token budget)*")
            break

        sections.append(sym_block)
        current_tokens += block_tokens
        included_symbols.append(sym)

    final_text = "\n".join(sections)
    return {
        "text": final_text,
        "token_count": estimate_tokens(final_text),
        "included_symbols": included_symbols,
        "truncated": len(included_symbols) < len(symbols),
    }
