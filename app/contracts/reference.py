"""Frozen independent reference definitions and deterministic comparators."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract
from app.contracts.relational_plan import RelationalPlan, ResultColumn


class ComparatorSpec(StrictContract):
    type: Literal[
        "keyed_rows_equal", "multiset_rows_equal", "scalar_metrics_equal",
    ]
    key_columns: list[str] = Field(default_factory=list)
    compare_columns: list[str] = Field(min_length=1)
    tolerance: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_comparator(self):
        if self.type == "keyed_rows_equal" and not self.key_columns:
            raise ValueError("keyed_rows_equal 必须声明 key_columns")
        if self.type != "keyed_rows_equal" and self.key_columns:
            raise ValueError(f"{self.type} 不允许 key_columns")
        unknown = set(self.tolerance) - set(self.compare_columns)
        if unknown:
            raise ValueError(
                "容差引用未比较列: " + ", ".join(sorted(unknown))
            )
        if any(value < 0 for value in self.tolerance.values()):
            raise ValueError("比较容差不能为负数")
        return self


class ValidationCase(StrictContract):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: Literal["coverage", "boundary", "empty"]
    parameters: dict[str, object | None]
    selection_evidence: dict = Field(default_factory=dict)


class ReferenceFactDesign(StrictContract):
    """独立可证明的最小业务事实，不包含 SQL 或关系计划。"""

    fact_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    actual_projection: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_monolithic_placeholder(self):
        if self.fact_id in {"final_result", "sp_result", "procedure_result"}:
            raise ValueError("Reference Fact 必须描述独立业务事实，不能复制最终 SP")
        return self


class ReferenceFact(StrictContract):
    fact_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    meaning: str = Field(min_length=1)
    comparison_role: Literal["direct_actual", "source_fact"] = "direct_actual"
    actual_projection: list[str] = Field(default_factory=list)
    reference_plan: RelationalPlan
    expected_sql: str = Field(min_length=1)
    coverage_sql: str | None = None
    expected_schema: list[ResultColumn] = Field(min_length=1)
    comparator: ComparatorSpec | None = None
    allowed_object_ids: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_projection(self):
        if self.expected_schema != self.reference_plan.result_schema:
            raise ValueError(
                f"{self.fact_id} Expected Schema 与 Reference Plan 不一致"
            )
        schema_names = {item.name.casefold() for item in self.expected_schema}
        if self.comparison_role == "source_fact":
            if self.actual_projection or self.comparator is not None:
                raise ValueError(
                    f"{self.fact_id} source_fact 不能直接投影或比较 SP 结果"
                )
            if len(self.allowed_object_ids) != len(set(self.allowed_object_ids)):
                raise ValueError(f"{self.fact_id} allowed_object_ids 重复")
            return self
        if not self.actual_projection or self.comparator is None:
            raise ValueError(
                f"{self.fact_id} direct_actual 必须声明投影和比较器"
            )
        if len(self.actual_projection) != len(
            {item.casefold() for item in self.actual_projection}
        ):
            raise ValueError(f"{self.fact_id} Actual 投影列重复")
        missing = [
            item for item in self.actual_projection
            if item.casefold() not in schema_names
        ]
        if missing:
            raise ValueError(
                f"{self.fact_id} Actual 投影不在 Expected Schema: "
                + ", ".join(missing)
            )
        comparison_columns = (
            self.comparator.key_columns + self.comparator.compare_columns
        )
        unknown_comparison = [
            item for item in comparison_columns
            if item.casefold() not in schema_names
        ]
        if unknown_comparison:
            raise ValueError(
                f"{self.fact_id} 比较列不在 Expected Schema: "
                + ", ".join(unknown_comparison)
            )
        projection_names = {
            item.casefold() for item in self.actual_projection
        }
        missing_actual = [
            item for item in comparison_columns
            if item.casefold() not in projection_names
        ]
        if missing_actual:
            raise ValueError(
                f"{self.fact_id} Actual 投影缺少比较列: "
                + ", ".join(missing_actual)
            )
        if len(self.allowed_object_ids) != len(set(self.allowed_object_ids)):
            raise ValueError(f"{self.fact_id} allowed_object_ids 重复")
        return self


class ReferenceBundle(StrictContract):
    version: Literal[3] = 3
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_version: str = Field(min_length=1)
    facts: list[ReferenceFact] = Field(min_length=1)
    result_comparator: ComparatorSpec | None = None
    validation_cases: list[ValidationCase] = Field(min_length=1)
    compile_evidence: dict = Field(default_factory=dict)
    preflight_evidence: dict = Field(default_factory=dict)
    status: Literal["draft", "reference_ready"]

    @model_validator(mode="after")
    def validate_facts(self):
        ids = [item.fact_id for item in self.facts]
        roles = {item.comparison_role for item in self.facts}
        if len(roles) != 1:
            raise ValueError(
                "ReferenceBundle 不能混用 direct_actual 和 source_fact"
            )
        if roles == {"source_fact"} and self.result_comparator is None:
            raise ValueError(
                "source_fact ReferenceBundle 必须声明最终结果比较器"
            )
        if roles == {"direct_actual"} and self.result_comparator is not None:
            raise ValueError(
                "direct_actual ReferenceBundle 不应声明最终结果比较器"
            )
        if len(ids) != len(set(ids)):
            raise ValueError("ReferenceBundle 存在重复 fact_id")
        if not any(item.kind == "coverage" for item in self.validation_cases):
            raise ValueError("ReferenceBundle 至少需要一个 coverage 用例")
        return self
