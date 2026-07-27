"""澄清与关键项阶段共享的结构化业务决策契约。"""
import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.semantic_guard import assert_semantic_text


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class DecisionOption(_StrictModel):
    id: str = Field(pattern=r"^[A-Z]$")
    value: str = Field(min_length=1)


class BusinessDecision(_StrictModel):
    decision_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    decision_type: Literal["blocking", "defaultable"]
    question: str = Field(min_length=1)
    options: list[DecisionOption] = Field(min_length=2)
    reason: str = ""
    recommended_option_id: str | None = None
    contract_relevant: bool = True
    status: Literal["pending", "confirmed"] = "pending"
    selected_option_id: str | None = None
    value: str | None = None
    source: Literal["user", "default", ""] = ""

    @model_validator(mode="after")
    def validate_options(self):
        option_ids = [item.id for item in self.options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("决策选项 id 重复")
        if (
            self.recommended_option_id is not None
            and self.recommended_option_id not in option_ids
        ):
            raise ValueError("recommended_option_id 不属于 options")
        if self.decision_type == "defaultable" and self.recommended_option_id is None:
            self.recommended_option_id = self.options[0].id
        if self.status == "confirmed" and not self.value:
            raise ValueError("已确认决策必须包含 value")
        return self


class DecisionPlan(_StrictModel):
    requirements_summary: str = Field(min_length=1)
    decisions: list[BusinessDecision]

    @model_validator(mode="after")
    def validate_keys(self):
        assert_semantic_text(self.requirements_summary, "需求摘要")
        keys = [item.decision_key for item in self.decisions]
        if len(keys) != len(set(keys)):
            raise ValueError("DecisionPlan 包含重复 decision_key")
        for index, left in enumerate(self.decisions):
            assert_semantic_text(left.question, f"决策 {left.decision_key} 的问题")
            assert_semantic_text(left.reason, f"决策 {left.decision_key} 的原因")
            for option in left.options:
                assert_semantic_text(
                    option.value,
                    f"决策 {left.decision_key} 的选项 {option.id}",
                )
            if left.value:
                assert_semantic_text(
                    left.value, f"决策 {left.decision_key} 的确认值",
                )
            for right in self.decisions[index + 1:]:
                question_similarity = SequenceMatcher(
                    None,
                    _normalized(left.question),
                    _normalized(right.question),
                ).ratio()
                left_options = "|".join(sorted(
                    _normalized(item.value) for item in left.options
                ))
                right_options = "|".join(sorted(
                    _normalized(item.value) for item in right.options
                ))
                option_similarity = SequenceMatcher(
                    None, left_options, right_options,
                ).ratio()
                if (
                    question_similarity >= 0.78
                    or question_similarity >= 0.55 and option_similarity >= 0.9
                ):
                    raise ValueError(
                        "DecisionPlan 包含语义重复决策: "
                        f"{left.decision_key}, {right.decision_key}"
                    )
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class ConfirmedDecisionSet(_StrictModel):
    summary: str = Field(min_length=1)
    decisions: list[dict]
    decision_hash: str = Field(min_length=1)


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def parse_decision_plan(content: str) -> DecisionPlan:
    text = content.strip()
    match = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE,
    )
    if match:
        text = match.group(1).strip()
    data = json.loads(text)
    if data.get("action") == "plan":
        data = {
            "requirements_summary": data.get("requirements_summary"),
            "decisions": data.get("decisions", []),
        }
    return DecisionPlan.model_validate(data)


def confirm_decision(
    plan: DecisionPlan,
    decision_key: str,
    *,
    value: str,
    selected_option_id: str | None,
    source: Literal["user", "default"],
) -> DecisionPlan:
    data = plan.model_dump(mode="json")
    found = False
    for item in data["decisions"]:
        if item["decision_key"] != decision_key:
            continue
        item.update({
            "status": "confirmed",
            "selected_option_id": selected_option_id,
            "value": value,
            "source": source,
        })
        found = True
        break
    if not found:
        raise ValueError(f"DecisionPlan 中不存在决策: {decision_key}")
    return DecisionPlan.model_validate(data)


def freeze_decisions(plan: DecisionPlan) -> ConfirmedDecisionSet:
    pending = [item.decision_key for item in plan.decisions if item.status != "confirmed"]
    if pending:
        raise ValueError("仍有未确认决策: " + ", ".join(pending))
    decisions = [
        {
            "key": item.decision_key,
            "value": item.value,
            "contract_relevant": item.contract_relevant,
            "source": item.source,
        }
        for item in plan.decisions
    ]
    payload = json.dumps(
        {
            "summary": plan.requirements_summary,
            "decisions": decisions,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return ConfirmedDecisionSet(
        summary=plan.requirements_summary,
        decisions=decisions,
        decision_hash=hashlib.sha256(payload).hexdigest(),
    )
