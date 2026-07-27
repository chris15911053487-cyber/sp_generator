import pytest

from app.routes.chat import _parse_assumption_response


def test_parse_assumption_response_preserves_confirmed_and_modified():
    payload = _parse_assumption_response(
        '{"confirmed":["exclude_cancelled"],'
        '"modified":{"customer_filter":"不按客户过滤"}}',
        {"exclude_cancelled", "customer_filter"},
    )

    assert payload == {
        "confirmed": ["exclude_cancelled"],
        "modified": {"customer_filter": "不按客户过滤"},
    }


@pytest.mark.parametrize(
    "message",
    [
        "全部确认",
        "[]",
        '{"confirmed":"exclude_cancelled","modified":{}}',
        '{"confirmed":["customer_filter"],'
        '"modified":{"customer_filter":"不按客户过滤"}}',
    ],
)
def test_parse_assumption_response_rejects_ambiguous_or_invalid_input(message):
    with pytest.raises(ValueError):
        _parse_assumption_response(message)


def test_parse_assumption_response_rejects_missing_or_unknown_keys():
    with pytest.raises(ValueError, match="缺少：customer_filter"):
        _parse_assumption_response(
            '{"confirmed":["exclude_cancelled"],"modified":{}}',
            {"exclude_cancelled", "customer_filter"},
        )

    with pytest.raises(ValueError, match="未知：old_key"):
        _parse_assumption_response(
            '{"confirmed":["exclude_cancelled","old_key"],"modified":{}}',
            {"exclude_cancelled"},
        )
