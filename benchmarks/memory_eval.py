"""Memory recall quality: does the prompt hook inject the right memory, and nothing when none applies?

Seeds a fresh store with realistic memories for one repository (what the post-task distiller writes
plus notes an agent saves with `chronos_remember`), then runs prompts that should recall a specific
memory and prompts that should recall none. No model calls.

    python -m benchmarks.memory_eval            # table
    python -m benchmarks.memory_eval --json out.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

NS = "shopapp"

# (key, tier, content)
MEMORIES = [
    ("pay_retry", "learned", "Learned resolution: Stripe webhook retries caused duplicate refunds; fixed by checking the idempotency key in payments/webhooks.py before calling refund()."),
    ("tz_tests", "procedural", "Repository invariant: tests that touch order timestamps must freeze time with freezegun, otherwise test_order_expiry fails around midnight UTC."),
    ("migrations", "procedural", "Repository invariant: never edit an applied Alembic migration; create a new revision with `alembic revision --autogenerate` instead."),
    ("cache_keys", "learned", "Learned resolution: product page showed stale prices because the Redis cache key omitted the currency; key is now product:{id}:{currency}."),
    ("auth_rate", "learned", "Learned resolution: login brute-force protection lives in auth/ratelimit.py and uses a sliding window of 5 attempts per 15 minutes per IP."),
    ("email_tpl", "core", "Order confirmation emails are rendered from templates/email/order_confirmed.html with Jinja; the plain-text version must be updated too."),
    ("search_idx", "learned", "Failed approach: rebuilding the Elasticsearch index inside the request handler timed out; reindexing must run in the Celery task search.tasks.reindex_products."),
    ("money", "procedural", "Repository invariant: all money amounts are integer cents (Decimal is not used); format only at the template layer with format_money()."),
    ("flaky_ci", "learned", "test_checkout_concurrency is flaky on CI because it depends on Postgres advisory locks; run it with -p no:randomly."),
    ("feature_flags", "core", "Feature flags are read through flags.is_enabled(name, user); never read LaunchDarkly directly from views."),
    ("inventory", "learned", "Learned resolution: overselling happened because stock was decremented after payment capture; it now reserves stock with SELECT ... FOR UPDATE in inventory/reserve.py."),
    ("i18n", "procedural", "Repository invariant: user-facing strings go through gettext _() and new strings require running make messages."),
    ("ep1", "episodic", "Task [t-101] 'Add VAT breakdown to invoice PDF': Outcome=success. Touched: invoices/pdf.py, invoices/tax.py."),
    ("ep2", "episodic", "Task [t-102] 'Fix coupon stacking allowing two percentage discounts': Outcome=success. Touched: cart/discounts.py, tests/test_discounts.py."),
    ("ep3", "episodic", "Task [t-103] 'Export orders to CSV for accounting': Outcome=success. Touched: admin/exports.py."),
    ("ep4", "episodic", "Task [t-104] 'Shipping estimate wrong for Alaska and Hawaii': Outcome=failure. Touched: shipping/rates.py."),
    ("ep5", "episodic", "Task [t-105] 'Speed up the product listing query': Outcome=success. Touched: catalog/queries.py, catalog/models.py."),
    ("ep6", "episodic", "Task [t-106] 'Add dark mode toggle to account settings page': Outcome=success. Touched: web/static/settings.js, web/templates/settings.html."),
]

# (prompt, expected memory key or None)
QUERIES = [
    ("customers are getting refunded twice when stripe resends the webhook", "pay_retry"),
    ("the order expiry test fails at night, fix it", "tz_tests"),
    ("add a column to the orders table", "migrations"),
    ("prices on product pages are wrong after switching currency", "cache_keys"),
    ("tighten the limit on failed login attempts", "auth_rate"),
    ("change the wording of the order confirmation email", "email_tpl"),
    ("search results don't include newly added products, reindex them", "search_idx"),
    ("show order totals with two decimal places in the admin", "money"),
    ("checkout concurrency test keeps failing in CI", "flaky_ci"),
    ("only show the new checkout to beta users", "feature_flags"),
    ("we sold more units than we had in stock during the flash sale", "inventory"),
    ("translate the new banner text into German", "i18n"),
    ("invoice PDF should show the tax rate per line", "ep1"),
    ("customers can combine two discount codes, they shouldn't", "ep2"),
    ("shipping costs to Hawaii are still wrong", "ep4"),
    # Nothing in memory applies to these.
    ("rename the variable foo to bar in utils", None),
    ("add a README section about local setup", None),
    ("upgrade pytest to the latest version", None),
    ("write a docstring for the main function", None),
    ("fix the typo in the about page", None),
    ("add type hints to the logging helpers", None),
    ("make the header logo bigger on mobile", None),
    ("remove the unused import warnings", None),
    ("add a health check endpoint for kubernetes", None),
    ("bump the copyright year in the footer", None),
    ("the order list page should paginate 50 items", None),
    ("add a test for the user model", None),
    ("refactor the settings module into smaller files", None),
    ("support uploading a profile picture", None),
    ("log the request id in every error message", None),
]


def _store(con):
    from chronos.memory.store import ChronosMemoryStore

    return ChronosMemoryStore(con=con)


def seed(con) -> dict[int, str]:
    st = _store(con)
    ids = {}
    for key, tier, content in MEMORIES:
        mid = st.remember(content=content, repo_namespace=NS, tier=tier, summary=content[:60], source="eval")
        ids[mid] = key
    return ids


def run(recall_kwargs: dict | None = None, inject: callable = None) -> dict:
    """`inject(store, prompt)` -> list of recalled records the prompt hook would inject."""
    from chronos.core.db import get_connection

    tmp = Path(tempfile.mkdtemp(prefix="chronos-memeval-"))
    con = get_connection(tmp / "chronos.db")
    ids = seed(con)
    st = _store(con)
    inject = inject or (lambda store, q: store.recall(query=q, repo_namespace=NS, limit=3, current_only=True, **(recall_kwargs or {})))
    rows = []
    for q, gold in QUERIES:
        got = [ids.get(r.id, "?") for r in inject(st, q)]
        rows.append({"prompt": q, "gold": gold, "injected": got, "hit": (gold in got) if gold else None, "noise": len([g for g in got if g != gold])})
    con.close()
    rel = [r for r in rows if r["gold"]]
    unrel = [r for r in rows if not r["gold"]]
    return {
        "relevant_hit_rate": sum(r["hit"] for r in rel) / len(rel),
        "relevant_top1": sum(1 for r in rel if r["injected"][:1] == [r["gold"]]) / len(rel),
        "unrelated_prompts_with_injection": sum(1 for r in unrel if r["injected"]) / len(unrel),
        "avg_irrelevant_memories_per_prompt": sum(r["noise"] for r in rows) / len(rows),
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    res = run()
    for r in res["rows"]:
        print(f"{'+' if r['hit'] else ('-' if r['gold'] else ' ')} {r['prompt'][:60]:62} gold={r['gold'] or '':14} got={r['injected']}")
    for k, v in res.items():
        if k != "rows":
            print(f"{k:40} {v:.2f}")
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()


# Written before the relevance filter was tuned and never used to tune it.
HELD_OUT = [
    ("a refund was issued two times for the same order after a webhook retry", "pay_retry"),
    ("freeze the clock in the expiry tests so they stop failing", "tz_tests"),
    ("we need a new alembic migration for the shipments table", "migrations"),
    ("redis cache returns euro prices to dollar customers", "cache_keys"),
    ("too many login attempts should lock the account for longer", "auth_rate"),
    ("elasticsearch reindex is too slow when done in the view", "search_idx"),
    ("flash sale oversold inventory again", "inventory"),
    ("gettext strings for the checkout page are missing", "i18n"),
    ("the coupon discount stacking bug is back", "ep2"),
    ("product listing page query is slow again", "ep5"),
    ("add a favicon", None),
    ("delete the old docker-compose file", None),
    ("make the 404 page friendlier", None),
    ("add pre-commit with black", None),
    ("document the API authentication header in the README", None),
    ("rename the users table column fullname to full_name", None),
    ("add an index on the email column", None),
    ("increase the gunicorn worker count", None),
    ("the contact form should validate phone numbers", None),
    ("add sentry error reporting", None),
]
