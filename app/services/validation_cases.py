"""验证用例检查与覆盖判定。"""

from __future__ import annotations

from app.contracts.reference import ReferenceBundle, ValidationCase
from app.contracts.schema import SchemaBinding
from app.contracts.semantic import SemanticContract


def choose_case(
    bundle: ReferenceBundle,
    case_id: str | None = None,
) -> ValidationCase:
    if case_id is not None:
        for item in bundle.validation_cases:
            if item.case_id == case_id:
                return item
        raise ValueError(f"不存在校验用例 {case_id}")
    for item in bundle.validation_cases:
        if item.kind == "coverage":
            return item
    raise ValueError("ReferenceBundle 没有 coverage 用例")


def coverage_is_effective(
    expected_counts: list[int],
    *,
    composed_expected_row_count: int = 0,
) -> bool:
    """来源全命中或最终事实组合有结果，均构成有效业务覆盖。"""
    all_sources_hit = (
        bool(expected_counts) and all(value > 0 for value in expected_counts)
    )
    return all_sources_hit or composed_expected_row_count > 0


def discover_validation_cases(
    contract: SemanticContract,
    binding: SchemaBinding,
    executor,
) -> list[ValidationCase]:
    """生成数据驱动覆盖用例及契约要求的边界/空结果用例。"""
    parameters, probe_evidence = executor.discover_parameter_values(
        contract, binding
    )
    cases = [
        ValidationCase(
            case_id="coverage_probe",
            kind="coverage",
            parameters=parameters,
            selection_evidence=probe_evidence,
        )
    ]
    parameter_by_id = {item.id: item for item in contract.parameters}
    for filter_item in contract.filters:
        if (
            filter_item.operator == "full_day_range"
            and len(filter_item.parameter_ids) == 2
        ):
            start_id, end_id = filter_item.parameter_ids
            end_value = parameters.get(end_id)
            if end_value is not None:
                same_day = dict(parameters)
                same_day[start_id] = end_value
                same_day[end_id] = end_value
                cases.append(
                    ValidationCase(
                        case_id=f"boundary_{filter_item.id}_same_day",
                        kind="boundary",
                        parameters=same_day,
                        selection_evidence={
                            "source": "coverage_probe",
                            "purpose": "same_day_and_full_day_boundary",
                        },
                    )
                )
    for parameter_id, parameter in parameter_by_id.items():
        if parameter.required:
            continue
        null_case = dict(parameters)
        null_case[parameter_id] = None
        cases.append(
            ValidationCase(
                case_id=f"boundary_{parameter_id}_null",
                kind="boundary",
                parameters=null_case,
                selection_evidence={
                    "source": "contract_optional_parameter",
                },
            )
        )
    if contract.allow_empty:
        empty_parameters = dict(parameters)
        date_parameters = [
            item for item in contract.parameters
            if item.logical_type in {"date", "datetime"}
        ]
        if len(date_parameters) >= 2:
            empty_parameters[date_parameters[0].id] = "1900-01-01"
            empty_parameters[date_parameters[1].id] = "1900-01-01"
            cases.append(
                ValidationCase(
                    case_id="empty_legal_period",
                    kind="empty",
                    parameters=empty_parameters,
                    selection_evidence={
                        "source": "contract_allow_empty",
                        "purpose": "legal_empty_period",
                    },
                )
            )
    return cases
