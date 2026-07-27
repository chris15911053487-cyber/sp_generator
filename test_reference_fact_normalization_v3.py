import json
from types import SimpleNamespace

from app.agent.nodes import (
    _canonicalize_reference_fact_projections,
    _generate_reference_fact_designs_v3,
)
from v3_test_helpers import contract


def test_reference_projection_semantic_ids_are_compiled_to_output_names():
    semantic = contract()
    payload = {
        "facts": [{
            "fact_id": "invoice_fact",
            "meaning": "发票事实",
            "actual_projection": ["invoice_id", "InvoiceAmount"],
        }],
    }

    _canonicalize_reference_fact_projections(payload, semantic)

    assert payload["facts"][0]["actual_projection"] == [
        "InvoiceId", "InvoiceAmount",
    ]


def test_unknown_reference_projection_is_not_silently_rewritten():
    semantic = contract()
    payload = {
        "facts": [{
            "fact_id": "invoice_fact",
            "meaning": "发票事实",
            "actual_projection": ["unknown_output"],
        }],
    }

    _canonicalize_reference_fact_projections(payload, semantic)

    assert payload["facts"][0]["actual_projection"] == ["unknown_output"]


def test_reference_fact_design_repairs_missing_grain_and_output_coverage():
    responses = [
        {
            "facts": [{
                "fact_id": "amount_fact",
                "meaning": "金额事实",
                "actual_projection": ["Amount"],
            }],
        },
        {
            "facts": [{
                "fact_id": "invoice_fact",
                "meaning": "按发票主键核对发票金额",
                "actual_projection": ["InvoiceId", "Amount"],
            }],
        },
    ]

    class FakeLlm:
        def invoke(self, _messages):
            return SimpleNamespace(
                content=json.dumps(responses.pop(0), ensure_ascii=False)
            )

    events = []
    facts = _generate_reference_fact_designs_v3(
        FakeLlm(), contract(), events,
    )

    assert facts[0].actual_projection == ["InvoiceId", "Amount"]
    assert events[0]["role"] == "reference_fact_design"
    assert events[0]["status"] == "repaired"
    assert events[0]["evidence"]["missing"] == ["invoiceid"]
