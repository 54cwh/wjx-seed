"""wjx_seed 的单元测试。

`selfcheck()` 是运行时守卫（每次跑脚本前执行）；这里把它覆盖的逻辑拆成可单独
定位的用例，并补上解析边界。用 `python -m pytest` 从仓库根目录运行。
"""
import random

import wjx_seed as w


def one(html):
    return w.parse_questions(html)[0]


def field(topic, typ="1", extra="", body=""):
    return (f"<div class='field ui-field-contain' id='div{topic}' type='{typ}' "
            f"{extra}>{body}</div></body>")


# ---------------------------------------------------------------- relation

def test_parse_relation():
    assert w.parse_relation("8,1") == (8, {"1"})
    assert w.parse_relation("3,2;3") == (3, {"2", "3"})
    assert w.parse_relation(None) is None


# ---------------------------------------------------------------- 矩阵解析

def test_matrix_with_corner():
    q = one(field(10, "6", body=(
        "<table class='matrix-rating matrixtable'>"
        "<tr class='trlabel'><th></th><th>非常不同意</th><th>非常同意</th></tr>"
        "<tr tp='d' fid='q10_0' rowIndex='0'>"
        "<td><span class='itemTitleSpan'>行一</span></td>"
        "<td><a class='rate-off' dval='1'></a></td>"
        "<td><a class='rate-off' dval='2'></a></td></tr></table>")))
    assert q["type"] == "matrix"
    assert q["rows"] == ["行一"]
    assert q["cols"] == {"1": "非常不同意", "2": "非常同意"}


def test_matrix_without_corner_does_not_shift():
    q = one(field(8, "6", body=(
        "<table class='matrix-rating matrixtable'>"
        "<tr class='trlabel'><th>1</th><th>2</th></tr>"
        "<tr tp='d' fid='q8_0' rowIndex='0'>"
        "<td><span class='itemTitleSpan'>行一</span></td>"
        "<td><a class='rate-off' dval='1'></a></td>"
        "<td><a class='rate-off' dval='2'></a></td></tr></table>")))
    assert q["cols"] == {"1": "1", "2": "2"}, "无空角表头不许错位"


def test_scale_options_with_modifier_class():
    q = one(field(6, "5", body=(
        "<a style='x' class='rate-off rate-offlarge' val='1'></a>"
        "<a style='x' class='rate-off rate-off6' val='2'></a>")))
    assert q["type"] == "scale"
    assert q["options"] == {"1": "1", "2": "2"}


# ---------------------------------------------------------------- 题型识别

def test_detect_multi_fill():
    q = one(field(2, "9", extra="gapfill='1'", body=(
        "<label><span class='textCont'></span></label>"
        "<label><span class='textCont'></span></label>")))
    assert q["type"] == "multi_fill" and q["blanks"] == 2


def test_detect_matrix_fill():
    q = one(field(3, "9", body=(
        "<table class='matrix-rating'>"
        "<tr id='drv3_1t'><td><span class='itemTitleSpan'>外观</span></td></tr>"
        "<tr id='drv3_1'><td><textarea id='q3_0'></textarea></td></tr></table>")))
    assert q["type"] == "matrix_fill" and q["rows"] == ["外观"]


def test_detect_slider():
    q = one(field(9, "9", body=(
        "<table class='matrix-rating'>"
        "<tr id='drv9_1t'><td><span class='itemTitleSpan'>外观</span></td></tr>"
        "<tr id='drv9_1'><td><input class='ui-slider-input' id='q9_0' "
        "min='0' max='100'></td></tr></table>")))
    assert q["type"] == "slider" and q["rows"] == ["外观"]
    assert (q["smin"], q["smax"]) == (0, 100)


def test_detect_matrix_multi():
    q = one(field(7, "6", extra="ischeck='1'", body=(
        "<table class='matrix-rating matrixtable'>"
        "<tr class='trlabel'><th></th><th>a</th><th>b</th></tr>"
        "<tr tp='d' fid='q7_0' rowIndex='0'>"
        "<td><span class='itemTitleSpan'>行一</span></td>"
        "<td><a class='rate-off' dval='1'></a></td>"
        "<td><a class='rate-off' dval='2'></a></td></tr></table>")))
    assert q["type"] == "matrix_multi" and q["cols"] == {"1": "a", "2": "b"}


def test_detect_citypick():
    q = one(field(4, "1", body="<input type='text' verify='多级下拉' readonly>"))
    assert q["type"] == "citypick"


def test_detect_rating():
    q = one(field(13, "5", extra="pj='1'", body=(
        "<a class='rate-off rate-off2' val='1' title='很不满意'></a>"
        "<a class='rate-off rate-off2' val='2' title='满意'></a>")))
    assert q["type"] == "rating"
    assert q["options"] == {"1": "很不满意", "2": "满意"}


def test_detect_reorder():
    q = one(field(5, "11", body=(
        "<ul><li serial=1><input type='hidden' value='1' id='q5_1' name='q5'>"
        "<span class='sortnum'></span><span>选项1</span></li>"
        "<li serial=2><input type='hidden' value='2' id='q5_2' name='q5'>"
        "<span class='sortnum'></span><span>选项2</span></li></ul>")))
    assert q["type"] == "reorder" and q["options"] == {"1": "选项1", "2": "选项2"}


def test_detect_droplist():
    q = one(field(9, "6", body=(
        "<select><option value='0'>请选择</option>"
        "<option value='1'>甲</option><option value='2'>乙</option></select>")))
    assert q["type"] == "droplist" and q["options"] == {"1": "甲", "2": "乙"}


# ---------------------------------------------------------------- 校验

def _matrix():
    return one(field(10, "6", body=(
        "<table class='matrix-rating matrixtable'>"
        "<tr class='trlabel'><th></th><th>甲</th><th>乙</th></tr>"
        "<tr tp='d' fid='q10_0' rowIndex='0'>"
        "<td><span class='itemTitleSpan'>行一</span></td>"
        "<td><a class='rate-off' dval='1'></a></td>"
        "<td><a class='rate-off' dval='2'></a></td></tr>"
        "<tr tp='d' fid='q10_1' rowIndex='1'>"
        "<td><span class='itemTitleSpan'>行二</span></td>"
        "<td><a class='rate-off' dval='1'></a></td>"
        "<td><a class='rate-off' dval='2'></a></td></tr></table>")))


def test_valid_matrix():
    q = _matrix()
    assert w._valid(q, "1┋2")
    assert not w._valid(q, "1")
    assert not w._valid(q, "1┋9")


def test_valid_single_rejects_bad_option():
    q = {"topic": 1, "type": "single", "options": {"1": "a", "2": "b"}}
    assert w._valid(q, "2")
    assert not w._valid(q, "9")
    assert not w._valid(q, "")


def test_valid_fill_rejects_duplicated_sentence():
    q = {"topic": 2, "type": "fill_blank", "options": {}}
    assert not w._valid(q, "作业也太多了吧" * 2)
    assert w._valid(q, "作业也太多了吧")


def test_valid_reorder_needs_permutation():
    q = {"topic": 5, "type": "reorder", "options": {"1": "a", "2": "b"}}
    assert w._valid(q, "2,1")
    assert not w._valid(q, "1")
    assert not w._valid(q, "1,1")


def test_valid_slider_bounds():
    q = {"topic": 9, "type": "slider", "rows": ["a", "b"], "smin": 0, "smax": 100}
    assert w._valid(q, "30┋70")
    assert not w._valid(q, "30")
    assert not w._valid(q, "30┋200")


# ---------------------------------------------------------------- 随机兜底

def test_random_value_is_always_valid():
    cases = [
        _matrix(),
        one(field(2, "9", extra="gapfill='1'",
                  body="<span class='textCont'></span><span class='textCont'></span>")),
        one(field(9, "9", body=(
            "<table class='matrix-rating'><tr id='drv9_1'>"
            "<td><input class='ui-slider-input' id='q9_0' min='0' max='100'></td>"
            "</tr></table>"))),
        one(field(13, "5", extra="pj='1'", body="<a class='rate-off' val='1' title='差'></a>")),
        one(field(4, "1", body="<input type='text' verify='多级下拉' readonly>")),
        one(field(5, "11", body=(
            "<ul><li serial=1><input type='hidden' value='1' id='q5_1'>"
            "<span class='sortnum'></span><span>甲</span></li>"
            "<li serial=2><input type='hidden' value='2' id='q5_2'>"
            "<span class='sortnum'></span><span>乙</span></li></ul>"))),
    ]
    for q in cases:
        for seed in range(20):
            assert w._valid(q, w._random_value(q, random.Random(seed))), q["type"]


# ---------------------------------------------------------------- 条件逻辑

def test_plan_answers_follows_relation():
    qs = [
        {"topic": 6, "type": "single", "options": {"1": "在家里", "2": "学校"}, "relation": None},
        {"topic": 7, "type": "single", "options": {"1": "能", "2": "不能"},
         "relation": (6, {"1"})},
        {"topic": 8, "type": "multiple", "options": {"1": "a", "2": "b"}, "relation": None},
        {"topic": 9, "type": "fill_blank", "options": {}, "relation": (8, {"2"})},
    ]
    # q6 只在选「在家里」时 q7 才出现
    for seed in range(30):
        a, s = w.plan_answers(qs, random.Random(seed), {})
        if a.get(6) == "1":
            assert 7 in a
        else:
            assert 7 in s
        if "2" in str(a.get(8, "")).split(w.MULTI_SEP):
            assert 9 in a
        else:
            assert 9 in s


def test_selfcheck_runs():
    w.selfcheck()
