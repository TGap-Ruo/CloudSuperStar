# -*- coding: utf-8 -*-
"""章节检测判分逻辑的回归测试。

真实案例（2026-09-30，人工智能通识 / 3.5 经典算法：枚举与搜索）：
成绩 100 分、两题都标记为正确，但旧逻辑用「我的答案 != 正确答案」判断对错，
而超星把正确答案隐藏了（解析出来是空字符串），于是判定 2/2 题答错 →
触发无意义重做 → 重做时题目页已变成「已批阅」取不到题目 → 章节被判失败并重试 5 次。
"""

from chaoxing_core.base import (
    _parse_work_record_detail,
    _parse_work_total_score,
    answers_equal,
    judge_work_detail,
)
from chaoxing_core.decode import decode_questions_info
from chaoxing_core.answer_check import check_multiple, check_single


def question_block(
    qid: str,
    type_label: str,
    title: str,
    my_answer: str,
    *,
    correct_answer: str = "",
    marked: str = "marking_dui",
) -> str:
    """构造与真实"已批阅"页面同构的题目块（正确答案可选，通常被隐藏）。"""
    correct_part = ""
    if correct_answer:
        correct_part = (
            '<div class="myAnswer"><span class="answerFont fl">正确答案：</span>'
            f'<div class="fl answerCon">{correct_answer}</div><p class="clear"></p></div>'
        )
    return f'''
      <div class="TiMu newTiMu ans-cc singleQuesId" data="{qid}" id="question{qid}">
        <div class="Zy_TItle clearfix">
          <div class="clearfix font-cxsecret qtContent">
            <span class="newZy_TItle">{type_label}</span>{title}</div>
        </div>
        <div class="newAnswerBx">
          <div class="myAnswerBx marBot16">
            <div class="myAnswer">
              <span class="answerFont fl">我的答案：</span>
              <div class="fl answerCon">{my_answer}</div>
              <p class="clear"></p>
            </div>
            {correct_part}
            <div class="answerScore">
              <div class="CorrectOrNot fl"><span class="{marked}"></span></div>
              <div class="fr newAnswerScore"><span class="scoreNum">50.0</span>分</div>
            </div>
          </div>
        </div>
      </div>'''


def graded_page(blocks: str, score: str = "100") -> str:
    return (
        '<html><body><form id="questionErrorForm1"></form>'
        f'<div class="aiArea"><div class="aiAreaContent">{blocks}</div></div>'
        f'<span>本次成绩<i>{score}</i>分</span>'
        "</body></html>"
    )


# ───────────────────── 复现用户遇到的真实场景 ─────────────────────

REAL_CASE_HTML = graded_page(
    question_block("405577802", "【多选题】", "枚举策略的通用步骤包括?", "ABC", marked="marking_dui")
    + question_block(
        "405577803", "【判断题】", "枚举策略适用于任何规模的解空间。( )", "错", marked="marking_dui"
    )
)


def test_real_case_no_longer_misjudged():
    detail = _parse_work_record_detail(REAL_CASE_HTML)
    assert len(detail) == 2
    # 正确答案被隐藏 → 空字符串；但对错标记是可用的
    assert all(q["correct_answer"] == "" for q in detail)
    assert all(q["is_correct"] is True for q in detail)
    assert _parse_work_total_score(REAL_CASE_HTML) == 100.0

    result = judge_work_detail(detail, 100.0)
    assert result is not None
    assert result["all_correct"] is True
    assert result["feedback"] == []


def test_wrong_answers_are_still_detected():
    html = graded_page(
        question_block("1", "【单选题】", "题一", "A", marked="marking_dui")
        + question_block("2", "【判断题】", "题二", "对", marked="marking_cuo"),
        score="50",
    )
    result = judge_work_detail(_parse_work_record_detail(html), 50.0)
    assert result["all_correct"] is False
    assert len(result["feedback"]) == 1
    assert "题二" in result["feedback"][0]
    assert "正确答案：(未公开)" in result["feedback"][0]


def test_partial_score_with_hidden_answers_requests_redo():
    html = graded_page(
        question_block("1", "【单选题】", "题一", "A", marked=""),
        score="66.6",
    )
    result = judge_work_detail(_parse_work_record_detail(html), 66.6)
    assert result["all_correct"] is False
    assert result["feedback"] == []


def test_no_information_skips_check():
    """既没有对错标记、也没有成绩、正确答案又被隐藏 → 不判定（按通过处理）。"""
    html = graded_page(question_block("1", "【单选题】", "题一", "A", marked=""), score="")
    assert judge_work_detail(_parse_work_record_detail(html), None) is None


def test_visible_correct_answers_are_compared_with_normalization():
    html = graded_page(
        question_block("1", "【多选题】", "题一", "AC", correct_answer="A、C", marked="")
    )
    result = judge_work_detail(_parse_work_record_detail(html), None)
    assert result["all_correct"] is True


def test_score_100_is_authoritative():
    html = graded_page(question_block("1", "【单选题】", "题一", "A", marked=""), score="100")
    result = judge_work_detail(_parse_work_record_detail(html), 100.0)
    assert result["all_correct"] is True


def test_parse_total_score_variants():
    assert _parse_work_total_score("<span>本次成绩<i>100</i>分</span>") == 100.0
    assert _parse_work_total_score("<span>本次成绩<i>66.6</i>分</span>") == 66.6
    assert _parse_work_total_score("<div>本次成绩：80 分</div>") == 80.0
    assert _parse_work_total_score("<div>没有成绩</div>") is None


def test_answers_equal_rules():
    assert answers_equal("AC", "A、C") is True
    assert answers_equal("ABC", "A、B、C") is True
    assert answers_equal("错", "错误") is True
    assert answers_equal("对", "正确") is True
    assert answers_equal("A", "B") is False
    # 拿不到正确答案时不得判为答错
    assert answers_equal("ABC", "") is None
    assert answers_equal("", "ABC") is None


def test_decode_questions_info_without_form_is_safe():
    """没有 <form> 的页面（已批阅）不应抛异常，应返回空题目列表。"""
    result = decode_questions_info("<html><body><div>已批阅</div></body></html>")
    assert result["questions"] == []


def test_check_single_accepts_chinese_punctuation_in_text():
    """实测踩坑：含顿号的单选答案被误判为"类型不符"，导致丢弃正确答案后随机作答。"""
    # 正确答案是文本、里面带顿号 → 仍是单选题答案
    assert check_single("靠近数据源的边缘计算设备(如基站、智能网关)") is True
    assert check_single("通过简化表征来辅助信息处理和预测") is True
    assert check_single("错误") is True
    # 纯选项字母串 → 判定为多选，交给多选分支处理
    assert check_single("A、B、C") is False
    assert check_single("AB") is False
    # 换行分隔的多段答案 → 也不是单选
    assert check_single("第一行\n第二行") is False
    assert check_multiple("A、B、C") is True
