"""chronos_task_04: export the active (blocking) security rules as a JSON Schema document."""
import json

import pytest

from chronos.core.db import get_connection
from chronos.verification.rule_store import export_rules_json_schema, promote_rule, upsert_rule

FIELDS = {"rule_id", "language", "rule_text", "yaml_pattern", "status", "promoted_at", "promoted_by"}


@pytest.fixture
def con():
    c = get_connection(":memory:")
    upsert_rule(c, "no-eval", "python", "Do not call eval()", "pattern: eval(...)", detectability_passed=True)
    upsert_rule(c, "no-shell-true", "python", "Avoid shell=True", "pattern: subprocess.run(..., shell=True)", detectability_passed=True)
    upsert_rule(c, "draft-rule", "python", "Still being validated", None)
    promote_rule(c, "no-shell-true", promoted_by="alice")
    promote_rule(c, "no-eval", promoted_by="bob")
    yield c
    c.close()


def test_schema_document_shape(con):
    doc = export_rules_json_schema(con)
    json.dumps(doc)
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["type"] == "array"
    item = doc["items"]
    assert item["type"] == "object"
    assert FIELDS <= set(item["properties"])
    assert {"rule_id", "language", "status"} <= set(item["required"])
    assert item["properties"]["rule_id"]["type"] == "string"


def test_only_active_rules_are_exported_sorted_by_id(con):
    examples = export_rules_json_schema(con)["examples"]
    assert [r["rule_id"] for r in examples] == ["no-eval", "no-shell-true"]
    assert all(r["status"] == "blocking" for r in examples)
    assert examples[1]["promoted_by"] == "alice"
    assert all(set(r) <= FIELDS for r in examples)


def test_writes_the_document_when_given_a_path(con, tmp_path):
    out = tmp_path / "rules.schema.json"
    doc = export_rules_json_schema(con, path=out)
    assert json.loads(out.read_text(encoding="utf-8")) == doc


def test_no_active_rules_gives_an_empty_example_list():
    c = get_connection(":memory:")
    assert export_rules_json_schema(c)["examples"] == []
    c.close()
