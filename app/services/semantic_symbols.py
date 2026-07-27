"""Stable identifiers and symbol resolution for staged semantic design."""

from __future__ import annotations

import re


class SymbolResolutionError(ValueError):
    def __init__(self, code: str, symbol: str):
        super().__init__(f"{code}: {symbol}")
        self.code = code
        self.symbol = symbol


def stable_semantic_id(symbol: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", str(symbol).strip().casefold())
    value = re.sub(r"_+", "_", value).strip("_")
    if not value or not value[0].isalpha():
        value = "semantic_" + value
    return value


class SemanticSymbolTable:
    def __init__(self):
        self._values: dict[str, dict[str, str]] = {
            "parameter": {},
            "output": {},
            "entity": {},
            "source": {},
            "fact": {},
            "filter": {},
        }
        self._fact_values: dict[tuple[str, str], tuple[str, str]] = {}

    def register(self, namespace: str, symbol: str) -> str:
        key = str(symbol).casefold()
        values = self._values[namespace]
        if key in values:
            raise SymbolResolutionError("SEMANTIC_SYMBOL_DUPLICATE", symbol)
        value = stable_semantic_id(symbol)
        if value in values.values():
            raise SymbolResolutionError("SEMANTIC_ID_COLLISION", symbol)
        values[key] = value
        return value

    def resolve(self, namespace: str, symbol: str) -> str:
        try:
            return self._values[namespace][str(symbol).casefold()]
        except KeyError as exc:
            raise SymbolResolutionError(
                "EXPRESSION_SYMBOL_UNKNOWN", symbol,
            ) from exc

    def register_fact_value(
        self,
        fact_symbol: str,
        value_symbol: str,
    ) -> tuple[str, str]:
        key = (fact_symbol.casefold(), value_symbol.casefold())
        if key in self._fact_values:
            raise SymbolResolutionError(
                "SEMANTIC_SYMBOL_DUPLICATE",
                f"{fact_symbol}.{value_symbol}",
            )
        result = (
            self.resolve("fact", fact_symbol),
            stable_semantic_id(value_symbol),
        )
        self._fact_values[key] = result
        return result

    def resolve_fact_value(
        self,
        fact_symbol: str,
        value_symbol: str,
    ) -> tuple[str, str]:
        try:
            return self._fact_values[
                (fact_symbol.casefold(), value_symbol.casefold())
            ]
        except KeyError as exc:
            raise SymbolResolutionError(
                "EXPRESSION_SYMBOL_UNKNOWN",
                f"{fact_symbol}.{value_symbol}",
            ) from exc

    def model_dump(self) -> dict:
        return {
            **{key: dict(value) for key, value in self._values.items()},
            "fact_value": {
                f"{fact}.{value}": list(target)
                for (fact, value), target in self._fact_values.items()
            },
        }

