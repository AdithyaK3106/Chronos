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
    is_targeted: bool = False,
    target_files: list[str] | None = None,
) -> dict[str, Any]:
    """Retrieve graph-aware repository context and rank within a token budget.
    
    If is_targeted is True (specific file/function task):
    - Prioritizes in-file definitions, signatures, and immediate local contracts.
    - Prunes transitive callers to max depth 1, top 2 callers.
    - Explicitly labels upstream callers as 'Reference Only — Do Not Modify'.
    - Omits expansive blast-radius listings that trigger exploratory multi-file edits.
    """
    if is_targeted:
        sections: list[str] = [
            "### ⏳ Chronos Repository Context (Focused Task Scope)",
            "Context assembled for targeted task. Upstream callers are reference-only; focus modifications strictly on the target file.",
            f"As-of snapshot: {as_of or 'latest'}\n",
        ]
    else:
        sections: list[str] = [
            "### ⏳ Chronos Repository Context (Bi-Temporal AST Graph)",
            f"Context assembled as-of: {as_of or 'latest'}\n",
        ]

    current_tokens = estimate_tokens("\n".join(sections))
    included_symbols: list[str] = []

    for sym in symbols:
        # Retrieve node metadata if available on graph store
        node_info = None
        if hasattr(graph_store, "get_nodes_by_name"):
            nodes = graph_store.get_nodes_by_name(sym)
            if nodes:
                node_info = nodes[0]

        callers = graph_store.as_of_callers(sym, as_of=as_of)
        callees = graph_store.as_of_callees(sym, as_of=as_of)

        if is_targeted:
            # P1: Targeted Task Scoping & Caller Pruning
            top_callers = callers[:2]
            top_callees = callees[:5]

            caller_str = ', '.join([c['name'] for c in top_callers]) if top_callers else 'None'
            callee_str = ', '.join([c['name'] for c in top_callees]) if top_callees else 'None'

            sym_lines = [f"#### Symbol: `{sym}`"]
            if node_info:
                loc = f"`{node_info.get('path', '')}` ({node_info.get('kind', 'Symbol')})"
                sym_lines.append(f"- **Target Location:** {loc}")
                sig = node_info.get("signature")
                if sig:
                    sym_lines.append(f"- **Signature:** `{sig}`")
                doc = node_info.get("docstring")
                if doc:
                    short_doc = doc.strip().split("\n")[0][:120]
                    sym_lines.append(f"- **Docstring:** *{short_doc}*")

            sym_lines.append(f"- **Local Contracts & Callees:** {callee_str}")
            sym_lines.append(f"- **Upstream Callers (Reference Only — Do Not Modify):** {caller_str}")
            sym_lines.append("")
        else:
            # Exploratory Zero-Path Context
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
