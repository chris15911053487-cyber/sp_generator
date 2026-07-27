"""SQL Server catalog snapshots and semantic-to-physical bindings."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract


class CatalogColumn(StrictContract):
    column_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    sql_type: str = Field(min_length=1)
    max_length: int | None
    precision: int | None
    scale: int | None
    nullable: bool
    collation: str | None


class CatalogObject(StrictContract):
    schema_name: str = Field(alias="schema", min_length=1)
    name: str = Field(min_length=1)
    object_id: int = Field(gt=0)
    object_type: Literal["table", "view"]
    columns: list[CatalogColumn]
    primary_key: list[int] = Field(default_factory=list)
    unique_keys: list[list[int]] = Field(default_factory=list)

    @property
    def schema(self) -> str:
        return self.schema_name


class CatalogForeignKey(StrictContract):
    name: str = Field(min_length=1)
    parent_object_id: int = Field(gt=0)
    parent_column_id: int = Field(gt=0)
    referenced_object_id: int = Field(gt=0)
    referenced_column_id: int = Field(gt=0)


class CatalogSnapshot(StrictContract):
    version: Literal[3] = 3
    server_identity: str = Field(min_length=1)
    database_name: str = Field(min_length=1)
    database_id: int = Field(gt=0)
    compatibility_level: int = Field(gt=0)
    database_collation: str = Field(min_length=1)
    default_schema: str = Field(min_length=1)
    current_user: str = Field(min_length=1)
    can_read_catalog: bool
    captured_at: datetime
    objects: list[CatalogObject]
    foreign_keys: list[CatalogForeignKey] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_catalog_identity(self):
        object_ids = [item.object_id for item in self.objects]
        object_names = [
            (item.schema.casefold(), item.name.casefold())
            for item in self.objects
        ]
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("CatalogSnapshot 存在重复 object_id")
        if len(object_names) != len(set(object_names)):
            raise ValueError("CatalogSnapshot 存在重复对象名称")
        columns_by_object = {}
        for item in self.objects:
            column_ids = [column.column_id for column in item.columns]
            column_names = [column.name.casefold() for column in item.columns]
            if len(column_ids) != len(set(column_ids)):
                raise ValueError(f"{item.schema}.{item.name} 存在重复 column_id")
            if len(column_names) != len(set(column_names)):
                raise ValueError(f"{item.schema}.{item.name} 存在重复列名")
            known = set(column_ids)
            if not set(item.primary_key).issubset(known):
                raise ValueError(f"{item.schema}.{item.name} 主键引用未知列")
            if any(not set(key).issubset(known) for key in item.unique_keys):
                raise ValueError(f"{item.schema}.{item.name} 唯一键引用未知列")
            columns_by_object[item.object_id] = known
        for item in self.foreign_keys:
            if (
                item.parent_column_id
                not in columns_by_object.get(item.parent_object_id, set())
                or item.referenced_column_id
                not in columns_by_object.get(item.referenced_object_id, set())
            ):
                raise ValueError(f"外键 {item.name} 引用未知对象或列")
        return self

    def object_by_id(self, object_id: int) -> CatalogObject:
        for item in self.objects:
            if item.object_id == object_id:
                return item
        raise KeyError(f"Catalog 中不存在 object_id={object_id}")


class EntityBinding(StrictContract):
    entity_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    database: str = Field(min_length=1)
    schema_name: str = Field(alias="schema", min_length=1)
    object_name: str = Field(alias="object", min_length=1)
    object_id: int = Field(gt=0)
    alias: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @property
    def schema(self) -> str:
        return self.schema_name


class FieldBinding(StrictContract):
    binding_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    semantic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    entity_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    column_name: str = Field(alias="column", min_length=1)
    column_id: int = Field(gt=0)
    sql_type: str = Field(min_length=1)
    nullable: bool
    collation: str | None
    literal_map: dict[str, Any] = Field(default_factory=dict)

    @property
    def column(self) -> str:
        return self.column_name


class JoinBinding(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    left_entity: str
    left_field_binding_id: str
    right_entity: str
    right_field_binding_id: str
    join_type: Literal["inner", "left", "full"]
    evidence: Literal[
        "foreign_key", "unique_key", "sap_b1_business_relation", "user_confirmed",
    ]
    meaning: str = Field(min_length=1)


class EntityBindingProposal(StrictContract):
    entity_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    database: str = Field(min_length=1)
    schema_name: str = Field(alias="schema", min_length=1)
    object_name: str = Field(alias="object", min_length=1)
    alias: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class FieldBindingProposal(StrictContract):
    binding_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    semantic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    entity_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    column_name: str = Field(alias="column", min_length=1)
    literal_map: dict[str, Any] = Field(default_factory=dict)


class JoinBindingProposal(StrictContract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    left_entity: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    left_field_binding_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    right_entity: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    right_field_binding_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    join_type: Literal["inner", "left", "full"]
    evidence: Literal[
        "foreign_key", "unique_key", "sap_b1_business_relation", "user_confirmed",
    ]
    meaning: str = Field(min_length=1)


class SchemaBindingProposal(StrictContract):
    entities: list[EntityBindingProposal] = Field(min_length=1)
    fields: list[FieldBindingProposal] = Field(min_length=1)
    joins: list[JoinBindingProposal] = Field(default_factory=list)


class SchemaBindingAmbiguity(StrictContract):
    semantic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    candidates: list[str] = Field(min_length=2)
    reason: str = Field(min_length=1)
    required_semantic_shape: Literal[
        "user_choice_required",
        "derived_expression",
        "multi_entity_fact",
        "missing_join",
        "literal_mapping",
    ] = "user_choice_required"


class SchemaBindingDraft(StrictContract):
    proposal: SchemaBindingProposal | None = None
    ambiguities: list[SchemaBindingAmbiguity] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_resolution(self):
        if self.proposal is None and not self.ambiguities:
            raise ValueError("Schema 绑定必须二选一：唯一提案或歧义候选")
        return self


class SchemaBinding(StrictContract):
    version: Literal[3] = 3
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    entities: list[EntityBinding] = Field(min_length=1)
    fields: list[FieldBinding] = Field(min_length=1)
    joins: list[JoinBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_binding_graph(self):
        entity_ids = {item.entity_id for item in self.entities}
        binding_ids = {item.binding_id for item in self.fields}
        if len(entity_ids) != len(self.entities):
            raise ValueError("SchemaBinding 存在重复实体绑定")
        if len(binding_ids) != len(self.fields):
            raise ValueError("SchemaBinding 存在重复字段绑定")
        aliases = [item.alias.casefold() for item in self.entities]
        if len(aliases) != len(set(aliases)):
            raise ValueError("SchemaBinding 存在重复实体别名")
        field_by_id = {item.binding_id: item for item in self.fields}
        for item in self.fields:
            if item.entity_id not in entity_ids:
                raise ValueError(
                    f"字段绑定 {item.binding_id} 引用未知实体 {item.entity_id}"
                )
        for item in self.joins:
            if item.left_entity not in entity_ids or item.right_entity not in entity_ids:
                raise ValueError(f"关联 {item.id} 引用未知实体")
            if (
                item.left_field_binding_id not in binding_ids
                or item.right_field_binding_id not in binding_ids
            ):
                raise ValueError(f"关联 {item.id} 引用未知字段")
            if (
                field_by_id[item.left_field_binding_id].entity_id
                != item.left_entity
                or field_by_id[item.right_field_binding_id].entity_id
                != item.right_entity
            ):
                raise ValueError(f"关联 {item.id} 的字段与实体不一致")
        return self

    def entity(self, entity_id: str) -> EntityBinding:
        for item in self.entities:
            if item.entity_id == entity_id:
                return item
        raise KeyError(f"未知实体绑定: {entity_id}")

    def field(self, binding_id: str) -> FieldBinding:
        for item in self.fields:
            if item.binding_id == binding_id:
                return item
        raise KeyError(f"未知字段绑定: {binding_id}")
