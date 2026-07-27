"""Shared strict and hashable contract helpers."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict


class StrictContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True, exclude_none=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
