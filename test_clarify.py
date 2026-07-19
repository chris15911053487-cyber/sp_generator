"""clarify 相关纯函数测试。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.agent.nodes import _extract_first_question


def test_single_question_unchanged():
    content = "统计维度是按客户还是按月份？\nA. 按客户\nB. 按月份"
    assert _extract_first_question(content) == content


def test_multiple_questions_truncated():
    content = "### 问题1：统计维度？\nA. 按客户\nB. 按月份\n\n### 问题2：时间范围？\nA. 本月\nB. 本年"
    result = _extract_first_question(content)
    assert "问题1" not in result
    assert "统计维度" in result
    assert "问题2" not in result


def test_numbered_prefix_truncated():
    content = "问题1：维度？\nA. x\nB. y\n\n问题2：范围？\nA. a\nB. b"
    result = _extract_first_question(content)
    assert "维度" in result
    assert "范围" not in result


def test_single_wrong_internal_number_removed():
    content = (
        "基于已确认的信息，您需要输出基础发票信息且只包含未收清发票。"
        "为确定存储过程的具体应用场景和格式要求，请确认：\n\n"
        "Q3：这个存储过程的主要用途是什么？\n\n"
        "A. 用于SAP B1报表模块展示\n"
        "B. 用于外部系统接口调用\n"
        "C. 用于内部临时查询"
    )
    result = _extract_first_question(content)
    assert "Q3" not in result
    assert "这个存储过程的主要用途是什么" in result
    assert "A. 用于SAP B1报表模块展示" in result


def test_empty_string():
    assert _extract_first_question("") == ""


if __name__ == "__main__":
    test_single_question_unchanged()
    test_multiple_questions_truncated()
    test_numbered_prefix_truncated()
    test_single_wrong_internal_number_removed()
    test_empty_string()
    print("PASS")
