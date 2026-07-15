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
    assert "问题1" in result
    assert "问题2" not in result


def test_numbered_prefix_truncated():
    content = "问题1：维度？\nA. x\nB. y\n\n问题2：范围？\nA. a\nB. b"
    result = _extract_first_question(content)
    assert "维度" in result
    assert "范围" not in result


def test_empty_string():
    assert _extract_first_question("") == ""


if __name__ == "__main__":
    test_single_question_unchanged()
    test_multiple_questions_truncated()
    test_numbered_prefix_truncated()
    test_empty_string()
    print("PASS")
