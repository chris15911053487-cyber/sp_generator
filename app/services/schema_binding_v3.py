"""把业务语义提案绑定到目录中的真实对象身份。"""

from __future__ import annotations

from app.contracts.schema import (
    CatalogObject,
    CatalogSnapshot,
    EntityBinding,
    FieldBinding,
    JoinBinding,
    SchemaBinding,
    SchemaBindingProposal,
)
from app.contracts.semantic import SemanticContract
from app.services.catalog_v3 import catalog_fingerprint


class SchemaBindingError(ValueError):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def _key(value: str) -> str:
    return value.casefold()


def _validate_fact_entity_connectivity(
    contract: SemanticContract,
    joins: list[JoinBinding],
) -> None:
    join_edges = [
        {item.left_entity, item.right_entity}
        for item in joins
    ]
    for fact in contract.facts:
        required = set(fact.entity_ids)
        if len(required) < 2:
            continue
        reachable = {next(iter(required))}
        changed = True
        while changed:
            changed = False
            for edge in join_edges:
                if not edge.issubset(required) or not (edge & reachable):
                    continue
                expanded = reachable | edge
                if expanded != reachable:
                    reachable = expanded
                    changed = True
        if reachable != required:
            raise SchemaBindingError(
                "SCHEMA_FACT_ENTITY_GRAPH_DISCONNECTED",
                f"事实 {fact.id} 所需实体没有被 SchemaBinding 关联成连通图",
                evidence={
                    "fact_id": fact.id,
                    "required_entities": sorted(required),
                    "reachable_entities": sorted(reachable),
                    "missing_entities": sorted(required - reachable),
                },
            )


def _find_object(
    catalog: CatalogSnapshot,
    schema_name: str,
    object_name: str,
) -> CatalogObject:
    matches = [
        item for item in catalog.objects
        if _key(item.schema) == _key(schema_name)
        and _key(item.name) == _key(object_name)
    ]
    if len(matches) != 1:
        candidates = [
            f"{item.schema}.{item.name}" for item in catalog.objects
            if _key(item.name) == _key(object_name)
        ]
        code = "SCHEMA_OBJECT_NOT_FOUND" if not matches else "SCHEMA_OBJECT_AMBIGUOUS"
        raise SchemaBindingError(
            code,
            f"目录中无法唯一绑定对象 [{schema_name}].[{object_name}]",
            evidence={"candidates": candidates},
        )
    return matches[0]


def _currency_family(
    physical: CatalogObject,
    column_name: str,
) -> tuple[str, list[str]]:
    """识别 SAP 风格 base/FC/Sy/SC/Sys 金额列族。"""
    name = column_name.casefold()
    suffix = ""
    root = name
    for candidate in ("sys", "fc", "sy", "sc"):
        if name.endswith(candidate):
            root = name[:-len(candidate)]
            suffix = candidate
            break
    family_names = {
        root,
        root + "fc",
        root + "sy",
        root + "sc",
        root + "sys",
    }
    family = [
        item.name for item in physical.columns
        if item.name.casefold() in family_names
    ]
    return suffix, family


def _validate_currency_scope(
    contract: SemanticContract,
    physical: CatalogObject,
    semantic_id: str,
    column_name: str,
) -> None:
    output = next(
        (
            item for item in contract.outputs + contract.source_fields
            if item.id == semantic_id
        ),
        None,
    )
    if output is None or output.logical_type not in {"money", "decimal"}:
        return
    suffix, family = _currency_family(physical, column_name)
    if len(family) < 2:
        return
    meaning = output.meaning.casefold()
    display_name = getattr(output, "name", output.id)
    expected = None
    if any(token in meaning for token in ("原始币种", "单据币种")):
        raise SchemaBindingError(
            "SCHEMA_CURRENCY_SCOPE_AMBIGUOUS",
            f"{display_name} 要求按单据币种，但单一物理金额列不能覆盖混合币种单据",
            evidence={
                "semantic_id": semantic_id,
                "meaning": output.meaning,
                "candidates": family,
                "required": "需同时绑定币种判别字段和对应金额列",
            },
        )
    if any(token in meaning for token in ("系统币", "系统货币")):
        expected = {"sy", "sc", "sys"}
    elif any(token in meaning for token in ("本位币", "本币", "当地货币")):
        expected = {""}
    elif "外币" in meaning:
        expected = {"fc"}
    if expected is not None and suffix not in expected:
        raise SchemaBindingError(
            "SCHEMA_CURRENCY_SCOPE_MISMATCH",
            f"{display_name} 的币种口径与物理字段 {column_name} 不一致",
            evidence={
                "semantic_id": semantic_id,
                "meaning": output.meaning,
                "selected": column_name,
                "candidates": family,
            },
        )


def build_schema_binding(
    contract: SemanticContract,
    catalog: CatalogSnapshot,
    proposal: SchemaBindingProposal,
) -> SchemaBinding:
    """校验提案并返回只含真实 object_id/column_id 的冻结绑定。"""
    if not catalog.can_read_catalog:
        raise SchemaBindingError(
            "ENV_CATALOG_PERMISSION_DENIED",
            "当前连接没有读取数据库定义的权限，无法安全生成 SQL",
        )

    contract_entities = {item.id for item in contract.entities}
    proposed_entities = {item.entity_id for item in proposal.entities}
    if proposed_entities != contract_entities:
        raise SchemaBindingError(
            "SCHEMA_ENTITY_SET_MISMATCH",
            "实体绑定必须与语义合同声明的实体完全一致",
            evidence={
                "missing": sorted(contract_entities - proposed_entities),
                "extra": sorted(proposed_entities - contract_entities),
            },
        )
    aliases = [item.alias.casefold() for item in proposal.entities]
    if len(aliases) != len(set(aliases)):
        raise SchemaBindingError(
            "SCHEMA_ALIAS_DUPLICATE",
            "实体别名不能重复",
        )

    entities: list[EntityBinding] = []
    object_by_entity: dict[str, CatalogObject] = {}
    for item in proposal.entities:
        if _key(item.database) != _key(catalog.database_name):
            raise SchemaBindingError(
                "ENV_DATABASE_IDENTITY_MISMATCH",
                f"绑定数据库 {item.database} 与当前数据库 {catalog.database_name} 不一致",
            )
        physical = _find_object(catalog, item.schema_name, item.object_name)
        object_by_entity[item.entity_id] = physical
        entities.append(
            EntityBinding(
                entity_id=item.entity_id,
                database=catalog.database_name,
                schema=physical.schema,
                object=physical.name,
                object_id=physical.object_id,
                alias=item.alias,
            )
        )

    fields: list[FieldBinding] = []
    for item in proposal.fields:
        physical = object_by_entity.get(item.entity_id)
        if physical is None:
            raise SchemaBindingError(
                "SCHEMA_FIELD_ENTITY_UNKNOWN",
                f"字段绑定 {item.binding_id} 引用了未绑定实体 {item.entity_id}",
            )
        matches = [
            column for column in physical.columns
            if _key(column.name) == _key(item.column_name)
        ]
        if len(matches) != 1:
            raise SchemaBindingError(
                "SCHEMA_COLUMN_NOT_FOUND",
                f"[{physical.schema}].[{physical.name}] 中不存在列 [{item.column_name}]",
                evidence={"available_columns": [column.name for column in physical.columns]},
            )
        column = matches[0]
        _validate_currency_scope(
            contract,
            physical,
            item.semantic_id,
            column.name,
        )
        fields.append(
            FieldBinding(
                binding_id=item.binding_id,
                semantic_id=item.semantic_id,
                entity_id=item.entity_id,
                column=column.name,
                column_id=column.column_id,
                sql_type=column.sql_type,
                nullable=column.nullable,
                collation=column.collation,
                literal_map=item.literal_map,
            )
        )

    field_by_id = {item.binding_id: item for item in fields}
    derived_outputs = {item.output_id for item in contract.derived_fields}
    required_semantics = (
        {item.id for item in contract.source_fields}
        if contract.facts else {
            item.id for item in contract.outputs
            if item.id not in derived_outputs
        }
    ) | {
        field_id for item in contract.filters for field_id in item.field_ids
    }
    bound_semantics = {item.semantic_id for item in fields}
    if not required_semantics.issubset(bound_semantics):
        raise SchemaBindingError(
            "SCHEMA_SEMANTIC_FIELD_MISSING",
            "SchemaBinding 没有覆盖全部业务输出和过滤字段",
            evidence={
                "missing": sorted(required_semantics - bound_semantics),
            },
        )
    fields_by_semantic: dict[str, list[FieldBinding]] = {}
    for item in fields:
        fields_by_semantic.setdefault(item.semantic_id, []).append(item)
    missing_literal_maps = []
    for semantic_filter in contract.filters:
        for literal in semantic_filter.literal_values:
            key = str(literal)
            if not any(
                key in field.literal_map
                for semantic_id in semantic_filter.field_ids
                for field in fields_by_semantic.get(semantic_id, [])
            ):
                missing_literal_maps.append({
                    "filter_id": semantic_filter.id,
                    "semantic_literal": literal,
                    "field_ids": semantic_filter.field_ids,
                })
    if missing_literal_maps:
        raise SchemaBindingError(
            "SCHEMA_LITERAL_MAPPING_MISSING",
            "SchemaBinding 没有把业务枚举值映射到物理存储值",
            evidence={"missing_literal_maps": missing_literal_maps},
        )
    joins: list[JoinBinding] = []
    for item in proposal.joins:
        left = field_by_id.get(item.left_field_binding_id)
        right = field_by_id.get(item.right_field_binding_id)
        if left is None or right is None:
            raise SchemaBindingError(
                "SCHEMA_JOIN_FIELD_UNKNOWN",
                f"关联 {item.id} 引用了未绑定字段",
            )
        if left.entity_id != item.left_entity or right.entity_id != item.right_entity:
            raise SchemaBindingError(
                "SCHEMA_JOIN_ENTITY_MISMATCH",
                f"关联 {item.id} 的字段与实体不一致",
            )
        if item.evidence == "foreign_key":
            left_object = object_by_entity[left.entity_id]
            right_object = object_by_entity[right.entity_id]
            has_fk = any(
                (
                    fk.parent_object_id == left_object.object_id
                    and fk.parent_column_id == left.column_id
                    and fk.referenced_object_id == right_object.object_id
                    and fk.referenced_column_id == right.column_id
                )
                or (
                    fk.parent_object_id == right_object.object_id
                    and fk.parent_column_id == right.column_id
                    and fk.referenced_object_id == left_object.object_id
                    and fk.referenced_column_id == left.column_id
                )
                for fk in catalog.foreign_keys
            )
            if not has_fk:
                raise SchemaBindingError(
                    "SCHEMA_JOIN_EVIDENCE_INVALID",
                    f"关联 {item.id} 声明为外键，但目录中没有对应外键",
                )
        joins.append(JoinBinding(**item.model_dump()))

    _validate_fact_entity_connectivity(contract, joins)

    return SchemaBinding(
        contract_hash=contract.content_hash,
        catalog_fingerprint=catalog_fingerprint(catalog),
        entities=entities,
        fields=fields,
        joins=joins,
    )


def validate_binding_against_catalog(
    contract: SemanticContract,
    catalog: CatalogSnapshot,
    binding: SchemaBinding,
) -> None:
    """重新核对冻结 ID，防止名称相同但对象已替换或绑定被伪造。"""
    if binding.contract_hash != contract.content_hash:
        raise SchemaBindingError(
            "SCHEMA_CONTRACT_HASH_MISMATCH",
            "SchemaBinding 不属于当前 SemanticContract",
        )
    if binding.catalog_fingerprint != catalog_fingerprint(catalog):
        raise SchemaBindingError(
            "SCHEMA_CATALOG_FINGERPRINT_MISMATCH",
            "数据库目录结构已发生变化",
        )
    contract_entities = {item.id for item in contract.entities}
    if {item.entity_id for item in binding.entities} != contract_entities:
        raise SchemaBindingError(
            "SCHEMA_ENTITY_SET_MISMATCH",
            "SchemaBinding 的实体集合与语义合同不一致",
        )
    entity_objects: dict[str, CatalogObject] = {}
    for entity in binding.entities:
        if _key(entity.database) != _key(catalog.database_name):
            raise SchemaBindingError(
                "ENV_DATABASE_IDENTITY_MISMATCH",
                f"实体 {entity.entity_id} 绑定到了其他数据库",
            )
        try:
            physical = catalog.object_by_id(entity.object_id)
        except KeyError as exc:
            raise SchemaBindingError(
                "SCHEMA_OBJECT_ID_NOT_FOUND",
                f"对象 ID {entity.object_id} 已不存在",
            ) from exc
        if (
            _key(physical.schema) != _key(entity.schema)
            or _key(physical.name) != _key(entity.object_name)
        ):
            raise SchemaBindingError(
                "SCHEMA_OBJECT_IDENTITY_CHANGED",
                f"对象 ID {entity.object_id} 的名称身份已变化",
            )
        entity_objects[entity.entity_id] = physical
    for field in binding.fields:
        physical = entity_objects.get(field.entity_id)
        if physical is None:
            raise SchemaBindingError(
                "SCHEMA_FIELD_ENTITY_UNKNOWN",
                f"字段 {field.binding_id} 引用未知实体",
            )
        matches = [
            item for item in physical.columns
            if item.column_id == field.column_id
        ]
        if len(matches) != 1:
            raise SchemaBindingError(
                "SCHEMA_COLUMN_ID_NOT_FOUND",
                f"列 ID {field.column_id} 已不存在",
            )
        column = matches[0]
        if (
            _key(column.name) != _key(field.column)
            or _key(column.sql_type) != _key(field.sql_type)
            or column.nullable != field.nullable
            or column.collation != field.collation
        ):
            raise SchemaBindingError(
                "SCHEMA_COLUMN_IDENTITY_CHANGED",
                f"字段 {field.binding_id} 的物理定义已变化",
            )
    derived_outputs = {item.output_id for item in contract.derived_fields}
    required_semantics = (
        {item.id for item in contract.source_fields}
        if contract.facts else {
            item.id for item in contract.outputs
            if item.id not in derived_outputs
        }
    ) | {
        field_id for item in contract.filters for field_id in item.field_ids
    }
    bound_semantics = {item.semantic_id for item in binding.fields}
    if not required_semantics.issubset(bound_semantics):
        raise SchemaBindingError(
            "SCHEMA_SEMANTIC_FIELD_MISSING",
            "SchemaBinding 没有覆盖全部业务输出和过滤字段",
        )
    fields = {item.binding_id: item for item in binding.fields}
    for join in binding.joins:
        left = fields[join.left_field_binding_id]
        right = fields[join.right_field_binding_id]
        if join.evidence != "foreign_key":
            continue
        left_object = entity_objects[left.entity_id]
        right_object = entity_objects[right.entity_id]
        if not any(
            (
                fk.parent_object_id == left_object.object_id
                and fk.parent_column_id == left.column_id
                and fk.referenced_object_id == right_object.object_id
                and fk.referenced_column_id == right.column_id
            )
            or (
                fk.parent_object_id == right_object.object_id
                and fk.parent_column_id == right.column_id
                and fk.referenced_object_id == left_object.object_id
                and fk.referenced_column_id == left.column_id
            )
            for fk in catalog.foreign_keys
        ):
            raise SchemaBindingError(
                "SCHEMA_JOIN_EVIDENCE_INVALID",
                f"关联 {join.id} 的外键证据已失效",
            )
