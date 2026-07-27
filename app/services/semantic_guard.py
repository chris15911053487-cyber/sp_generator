"""Guards that keep pre-binding contracts free of physical database choices."""

import re


_QUALIFIED_IDENTIFIER = re.compile(
    r"(?<!@)\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*[A-Za-z_][A-Za-z0-9_]*\b"
)
_SQL_TOKEN = re.compile(
    r"(?i)\b(?:select|from|where|join|group\s+by|order\s+by|"
    r"create|alter|procedure|object_id|column_id)\b"
)
_PHYSICAL_STYLE_TOKEN = re.compile(
    r"(?<!@)\b(?:[A-Z]{3,}[0-9]*|[A-Z][a-z]+[A-Z][A-Za-z0-9_]*)\b"
)
_ALLOWED_BUSINESS_ACRONYMS = {
    "API", "BP", "ERP", "ID", "SAP", "SKU",
}
_KNOWN_SAP_PHYSICAL_IDENTIFIERS = {
    "acctcode",
    "acctname",
    "canceled",
    "cardcode",
    "cardname",
    "docdate",
    "docentry",
    "docnum",
    "docstatus",
    "doctotal",
    "taxdate",
    "transid",
    "vatsum",
}
_SAP_OUTPUT_BUSINESS_NAMES = {
    "acctcode": "AccountCode",
    "acctname": "AccountName",
    "canceled": "CancellationStatus",
    "cardcode": "CustomerCode",
    "cardname": "CustomerName",
    "docdate": "DocumentDate",
    "docentry": "DocumentId",
    "docnum": "DocumentNumber",
    "docstatus": "DocumentStatus",
    "doctotal": "DocumentAmount",
    "taxdate": "TaxDate",
    "vatsum": "TaxAmount",
}
_SAP_PHYSICAL_TERM_MEANINGS = {
    "acctcode": "科目代码",
    "acctname": "科目名称",
    "cardcode": "客户代码",
    "cardname": "客户名称",
    "docdate": "单据日期",
    "docentry": "单据内部标识",
    "docnum": "单据编号",
    "docstatus": "单据状态",
    "doctotal": "单据金额",
    "taxdate": "税务日期",
    "transid": "内部会计交易标识",
    "vatsum": "税额",
}


def strip_redundant_physical_annotations(value: str) -> str:
    """Remove only parenthesized physical-name annotations, not semantics."""

    def replace(match: re.Match) -> str:
        token = match.group("token")
        if (
            _PHYSICAL_STYLE_TOKEN.fullmatch(token)
            and token not in _ALLOWED_BUSINESS_ACRONYMS
        ):
            return ""
        if token.casefold() in _KNOWN_SAP_PHYSICAL_IDENTIFIERS:
            return ""
        return match.group(0)

    normalized = re.sub(
        r"[\(（]\s*(?P<token>[A-Za-z_][A-Za-z0-9_]*)\s*[\)）]",
        replace,
        str(value or ""),
    )
    for physical, business in _SAP_PHYSICAL_TERM_MEANINGS.items():
        normalized = re.sub(
            rf"(?i)\b{re.escape(physical)}\b",
            business,
            normalized,
        )
    return normalized.strip()


def assert_semantic_text(value: str, context: str) -> None:
    """Reject physical identifiers and SQL syntax from business-only text."""
    text = str(value or "")
    if not text:
        return
    if "[" in text or "]" in text:
        raise ValueError(f"{context} 包含物理标识符或 SQL 方括号")
    match = _QUALIFIED_IDENTIFIER.search(text)
    if match:
        raise ValueError(f"{context} 包含物理对象引用: {match.group(0)}")
    match = _SQL_TOKEN.search(text)
    if match:
        raise ValueError(f"{context} 包含 SQL 语法: {match.group(0)}")
    for match in _PHYSICAL_STYLE_TOKEN.finditer(text):
        token = match.group(0)
        if token not in _ALLOWED_BUSINESS_ACRONYMS:
            raise ValueError(f"{context} 包含疑似物理字段或表名: {token}")


def assert_business_output_name(value: str) -> None:
    if str(value).casefold() in _KNOWN_SAP_PHYSICAL_IDENTIFIERS:
        raise ValueError(f"输出名称使用了 SAP B1 物理字段名: {value}")


def canonical_business_output_name(value: str) -> str:
    return _SAP_OUTPUT_BUSINESS_NAMES.get(
        str(value).casefold(),
        str(value),
    )
