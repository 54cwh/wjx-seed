#!/usr/bin/env python3
"""
问卷星示例数据生成器 —— 条件逻辑感知 + LLM 生成填空题。

对着**自己创建的问卷**灌一批结构自洽的示例答卷，用来看统计/交叉分析效果。

为什么不用现成的 GitHub 项目：8 个被调研的项目里 7 个完全没处理条件逻辑跳题
（跳题|条件逻辑|relation=|logicjump 命中 0 个文件），无脑按顺序填会生成
"q6 选了不在家完成、却把 q7~q10 也填了" 这种自相矛盾的答卷。
本脚本按 relation 属性求值，只填该分支的题。

用法:
    export WJX_URL=https://www.wjx.cn/vm/xxxxxx.aspx
    export LLM_API_KEY=sk-...
    python wjx_seed.py --plan -n 5          # 只看会填哪些题，不提交
    python wjx_seed.py --submit -n 30       # 真的提交 30 份
    python wjx_seed.py --submit -n 30 --answers answers.sample.json   # 固定答案，不调 LLM
"""
import argparse
import json
from pathlib import Path
import os
import random
import re
import sys
import time
from urllib.request import Request, urlopen

# 故意留空：仓库是公开的，不该默认指向某一份具体问卷。main() 里强制要求。
SURVEY_URL = os.environ.get("WJX_URL", "")

# 问卷星 type 属性 -> 语义题型。1/2 都是填空(2 是多行)。
TYPE_NAME = {
    "1": "fill_blank", "2": "fill_blank", "3": "single", "4": "multiple",
    "5": "scale", "6": "droplist", "7": "matrix", "8": "reorder",
    "9": "slider", "10": "group",
}
# 多选答案内部用 U+250B 拼接（问卷星自己的分隔符）
MULTI_SEP = "┋"


# ---------------------------------------------------------------- 解析

def fetch(url):
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    })
    return urlopen(req, timeout=30).read().decode("utf-8", "replace")


def parse_relation(raw):
    """relation='8,1'      -> (8, {'1'})
       relation='3,2;3'    -> (3, {'2','3'})
       无 relation         -> None（恒显示）
    """
    if not raw:
        return None
    parts = raw.split(",", 1)
    if len(parts) != 2:
        return None
    dep = int(parts[0])
    return dep, set(parts[1].split(";"))


def _text(html):
    """HTML 片段 -> 纯文本。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def parse_questions(html):
    blocks = re.findall(
        r"<div class='field ui-field-contain'(.*?)(?=<div class='field ui-field-contain'|<div id='foot_submit'|</body>)",
        html, re.S)
    out = []
    for b in blocks:
        t = re.search(r"id='div(\d+)'", b)
        if not t:
            continue
        topic = int(t.group(1))
        typ = re.search(r"type='(\d+)'", b)
        title = re.search(r"<div class='topichtml'>(.*?)</div>", b, re.S)
        rel = re.search(r"relation='([^']*)'", b)

        q = {
            "topic": topic,
            "type": TYPE_NAME.get(typ.group(1), "unknown") if typ else "unknown",
            "raw_type": typ.group(1) if typ else "",
            "title": _text(title.group(1)) if title else "",
            "required": "req='1'" in b,
            "relation": parse_relation(rel.group(1)) if rel else None,
            "options": {},   # {编号: 文字}，填空题为空
        }
        # options = {选项编号: 选项文字}。编号要用来提交，文字要给 LLM 看，
        # 少了文字模型就只能瞎猜每题在问什么。
        if q["type"] in ("single", "multiple"):
            # 文字在 <div class='label' for='q1_1'>准初一</div>，用 for 关联 input 的 id
            lab = {m.group(1): _text(m.group(2)) for m in re.finditer(
                r"<div class='label'[^>]*for='(q\d+_\d+)'>(.*?)</div>", b, re.S)}
            q["options"] = {m.group(1): lab.get(m.group(2), "")
                            for m in re.finditer(
                                r"<input type='(?:radio|checkbox)' value='([^']*)'"
                                r"[^>]*id='(q\d+_\d+)'", b)}
        elif q["type"] == "scale":
            # 注意真实 DOM 是 <a style='...' class='rate-off' val='1'>，
            # class 前面还有 style，不能锚定 "<a class='rate-off'"。
            vals = re.findall(r"<a[^>]*class='rate-off'[^>]*val='([^']*)'", b)
            q["options"] = {v: v for v in vals}          # 量表的"文字"就是数字本身
        elif q["type"] == "droplist":
            q["options"] = {v: _text(t) for v, t in re.findall(
                r"<option value='([^']*)'>(.*?)</option>", b, re.S) if v != "0"}
        out.append(q)
    out.sort(key=lambda x: x["topic"])
    return out


# ------------------------------------------------- 条件逻辑（核心）

def _valid(q, val):
    """LLM 给的值能不能用。不能就用随机兜底，绝不把脏值提交上去。"""
    if val is None:
        return False
    val = str(val)
    if q["type"] == "fill_blank":
        val = val.strip()
        # deepseek 偶尔把同一句原样输出两遍（实测 2/6），这种脏数据会直接进问卷
        h = len(val) // 2
        if len(val) % 2 == 0 and val[:h] == val[h:]:
            return False
        return bool(val)
    parts = val.split(MULTI_SEP)
    return bool(parts) and all(p in q["options"] for p in parts)


def plan_answers(questions, rng, raw=None):
    """按题号顺序单趟生成，保证条件逻辑自洽。

    问卷星的条件依赖题号恒小于被依赖的题号，所以正序一遍即可：
    到某题时，它依赖的题已经答完，能直接判断该不该填。

    raw 是 LLM 一次生成的整份 {topic: value}，所以同一份里各题彼此自洽。
    **跳过逻辑始终由这里的 relation 求值裁定，LLM 不参与** —— 它只提供值，
    给了非法值就退回随机。
    """
    answers, skipped = {}, []
    for q in questions:
        topic = q["topic"]
        rel = q["relation"]
        if rel:
            dep, opts = rel
            prior = answers.get(dep)
            chosen = set(prior.split(MULTI_SEP)) if prior else set()
            if not (chosen & opts):
                skipped.append(topic)
                continue          # 分支未命中，这题不该出现
        val = (raw or {}).get(topic)
        if not _valid(q, val):
            val = _random_value(q, rng)
        answers[topic] = val
    return answers, skipped


def _random_value(q, rng):
    """没有 LLM（或 LLM 给的非法）时的兜底。均匀随机：能提交，但没结构。"""
    t, opts = q["type"], q["options"]
    keys = list(opts)
    if t == "multiple" and keys:
        return MULTI_SEP.join(rng.sample(keys, rng.randint(1, min(2, len(keys)))))
    if t == "fill_blank":
        return "无"
    return rng.choice(keys) if keys else ""


# ------------------------------------------------------------ LLM

def _extract_json_obj(text):
    """从 LLM 回复里挖出 JSON 对象：剥围栏 -> 首个{到末个}。"""
    t = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.M)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None


def llm_response(questions, context="", retries=2, rng=None):
    """一次调用生成**整份**答卷：一个人设 + 15 题的答案。

    为什么要整份而不是逐题：逐题独立生成等于没有相关性，交叉分析全是噪声。
    锁定一个具体学生（年级/家庭环境/作业态度）再让 LLM 照着人设答，
    同一份里的题之间才会有真实关联。

    返回 {topic: value}。**跳过的题不用管** —— 哪些题该出现由
    plan_answers 的 relation 求值裁定，LLM 不参与。
    """
    base = (os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
    key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    if not key:
        print("!! 未配置 LLM_API_KEY，退回均匀随机（交叉分析会是噪声）", file=sys.stderr)
        return {}

    spec = []
    for q in questions:
        line = f'{q["topic"]}. [{q["type"]}] {q["title"]}'
        if q["relation"]:
            dep, allow = q["relation"]
            line += f'（只有当第{dep}题选了 {" 或 ".join(sorted(allow))} 时才会被问到）'
        if q["type"] == "fill_blank":
            line += "\n   （填空题，写 5-40 字）"
        elif q["options"]:
            line += "\n   选项：" + " / ".join(
                f"{v}={w[:26]}" for v, w in q["options"].items())
        spec.append(line)

    # 边缘分布由我们指定，不交给模型自由发挥。
    # 实测让模型自己选：年级 3/3 都是"准初三"，q6「是否在家完成」6/6 都是"是"，
    # 二元题的选项文案自带语义引力，temperature 拉不高也拉不散。
    # 全部样本挤在同一格的话，交叉分析出来的全是无效数据。
    # 这里只定"选第几项"这个边缘分布，答案内容仍然交给 LLM 按人设写。
    hint = ""
    if rng:
        picks = [q for q in questions if q["type"] == "single" and len(q["options"]) >= 2]
        rng.shuffle(picks)
        for q in picks:
            v = rng.choice(list(q["options"]))
            hint += f"\n- 第{q['topic']}题选「{q['options'][v]}」。"
        if hint:
            hint += ("\n以上选择题已定，你不要再改；人设、以及量表题和填空题的答案"
                     "必须与这些选择自洽。")

    schema = ", ".join(f'"{q["topic"]}": ""' for q in questions)
    prompt = f"""你要为一份中学生问卷生成**一份**示例答卷。
{hint}

问卷背景：{context or "关于寒暑假作业完成情况与家庭学习环境的调查"}

问卷题目：
{chr(10).join(spec)}

先定一个人设，再照这个人设答全部题目。要求：
1. persona 用 3-5 句写清这个人的年级、家庭学习环境（家长管得严不严、有没有人打扰）、
   对作业的态度（认真/拖延/敷衍）。**这一段是整份答卷的锚，后面所有答案都要符合它**
2. answers 的每个键都要出现，值必须严格用上面给定的选项编号（或填空题的原文）
3. 条件题即使条件不满足也照样填一个合理的值，代码会自己判断该不该保留
4. 填空题的值只写回答本身，不要在值里重复题干，同一句话只写一遍
5. 适度的分化：认真作答的人不偷懒，敷衍的人别把每题都答满，量表别都填中间值
6. 回答里不要出现双引号、换行或反斜杠

只返回 JSON，不要任何解释：{{"persona": "...", "answers": {{{schema}}}}}"""

    for attempt in range(1, retries + 1):
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # 实测 1.5 会让 deepseek-chat 退化成复读机（同一句话输出两遍），
            # 1.2 还能保持措辞多样又不至于胡言乱语
            "temperature": 1.2,
        }).encode()
        req = Request(f"{base}/chat/completions", data=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            raw = urlopen(req, timeout=180).read().decode("utf-8", "replace")
            content = json.loads(raw)["choices"][0]["message"]["content"]
        except Exception as e:                                # noqa: BLE001
            print(f"!! LLM 调用失败(第{attempt}次): {e}", file=sys.stderr)
            continue

        data = _extract_json_obj(content)
        if not data or not isinstance(data.get("answers"), dict):
            print(f"!! LLM 返回不是预期 JSON(第{attempt}次)，重试", file=sys.stderr)
            continue

        out = {}
        bad = []
        for k, v in data["answers"].items():
            try:
                topic = int(k)
            except (TypeError, ValueError):
                continue
            out[topic] = str(v)
        for q in questions:
            if not _valid(q, out.get(q["topic"])):
                bad.append(q["topic"])
        if bad and attempt < retries:
            print(f"!! 第 {bad} 题的值不合法(第{attempt}次)，重试", file=sys.stderr)
            continue
        print(f"   人设：{str(data.get('persona', ''))[:110]}")
        return out

    print("!! LLM 重试用尽，这份退回均匀随机", file=sys.stderr)
    return {}


# ------------------------------------------------------- 浏览器填写

def _human_click(page, selector, rng):
    """先让鼠标"走"过去再点。

    问卷星把 `window.ktimes`（鼠标悬停计数器）随提交上报，实测这是它判自动化的
    主要信号：裸 page.click() 每题只产生 1 次 hover，ktimes/答题数 ≈ 1.7，
    落地率 55%；补上 mouse.move 轨迹后比值 ≈ 7，落地率 80%。
    而答题速度完全不影响 —— 4 秒填完 15 题照样过。
    """
    box = page.query_selector(selector).bounding_box()
    page.mouse.move(box["x"] + rng.randint(0, 40),
                    box["y"] + rng.randint(0, 40),
                    steps=rng.randint(5, 18))
    page.wait_for_timeout(rng.randint(50, 200))
    page.click(selector)


def fill_and_submit(page, questions, answers, rng):
    """在真实页面里点选。签名/答案打包/风控交给页面自己的 JS。"""

    for q in questions:
        topic = q["topic"]
        if topic not in answers:
            continue
        val = answers[topic]
        if not val:
            continue
        if q["type"] == "single":
            _human_click(page, f"#div{topic} a.jqradio >> "
                         f"nth={list(q['options']).index(val)}", rng)
        elif q["type"] == "multiple":
            for v in val.split(MULTI_SEP):
                _human_click(page, f"#div{topic} a.jqcheckbox >> "
                             f"nth={list(q['options']).index(v)}", rng)
        elif q["type"] == "scale":
            _human_click(page, f"#div{topic} a[val='{val}']", rng)
        elif q["type"] == "droplist":
            _human_click(page, f"#div{topic} select", rng)
            page.select_option(f"#div{topic} select", val)
        elif q["type"] == "fill_blank":
            sel = f"#div{topic} textarea"
            if page.query_selector(sel) is None:
                sel = f"#div{topic} input[type='text']"
            _human_click(page, sel, rng)
            page.type(sel, val, delay=rng.randint(25, 70))
        page.wait_for_timeout(120)   # 让页面 JS 处理条件显示

    # 滚一下，让页面把懒加载的分支跑完。顺带也让 ktimes 再涨一点。
    for _ in range(rng.randint(3, 8)):
        page.mouse.wheel(0, rng.randint(200, 600))
        page.wait_for_timeout(rng.randint(120, 400))

    # ---- 提交前自查：必填题真的填上了吗 ----
    # 不查这一步就只能靠"提交后猜"，而问卷星对漏填必填题的反应是
    # 弹一个 .errorMessage 然后**原地不动**，看起来跟成功一模一样。
    empty = page.evaluate("""
      (topics) => {
        const bad = [];
        for (const t of topics) {
          const el = document.getElementById('div' + t);
          if (!el) continue;
          if (getComputedStyle(el).display === 'none') continue;  // 分支未显示，不算漏填
          const sel = el.querySelector("input:checked, a.rate-on, [class*=rate-on]");
          const ta = el.querySelector("textarea, input[type=text]");
          const ok = !!sel || !!(ta && ta.value.trim());
          if (!ok) bad.push(t);
        }
        return bad;
      }
    """, sorted(answers.keys()))
    if empty:
        return False, "FILL", f"必填题空着(页面会判无效): {empty}"

    page.wait_for_timeout(300)

    # ktimes 是问卷星判自动化的主判据（实测），记下来，出问题能对照
    kt = page.evaluate("window.ktimes")

    # 提交。真实结果在 processjq.ashx 的 XHR 响应体里，格式是
    #   "10〒<跳转URL>"                      成功
    #   "7〒需要安全校验，请重新提交！"        被反垃圾频控拦下
    # 失败时页面**什么都不显示**，URL 也不动 —— 所以必须读响应体，
    # 只看"跳没跳转"会把被拒误判成成功。
    try:
        with page.expect_response(
                lambda r: "joinnew/processjq.ashx" in r.url, timeout=45000) as info:
            page.click("#ctlNext")
        raw = info.value.text().strip()
    except Exception as e:                                      # noqa: BLE001
        # 点提交后一个请求都不发 = 问卷星弹了网易验证码，点击被验证码流程吞掉。
        # 这是硬墙，退避绕不过去，只能降速或换 Excel 导入。
        captcha = page.evaluate("""
          () => ['#captchaOut','#captcha','#captchabtn']
                  .filter(s => { const e = document.querySelector(s);
                                 return e && getComputedStyle(e).display !== 'none'; })
        """)
        if captcha:
            return False, "CAPTCHA", f"弹了验证码({','.join(captcha)})，自动提交到此为止"
        return False, "NOXHR", f"没等到提交请求: {type(e).__name__}: {e}"

    code, _, msg = raw.partition("〒")
    if code == "10":
        return True, "10", f"已提交 ktimes={kt} -> " + msg[:70]
    if code == "7":
        return False, "7", "触发问卷星反垃圾频控，需要安全校验"
    return False, code or "?", f"问卷星拒绝 code={code!r}: {msg[:120]}"


def run(count, submit, questions, context, seed, gap=(5, 12), retries=3, fixed=None):
    """fixed 非空 = 固定答案模式：不调 LLM，用来单测提交链路本身。

    要两种模式的原因：LLM 每次生成不同，同一份"想跑通"没法复现；
    而验证浏览器自动化 + 条件逻辑 + 签名提交是否稳定，需要完全确定的输入。
    """
    rng = random.Random(seed)

    def pick(i):
        if not fixed:
            return llm_response(questions, context, rng=rng)
        return fixed[i % len(fixed)]

    if not submit:
        # dry run：只规划，不开浏览器
        by_topic = {q["topic"]: q for q in questions}
        for i in range(count):
            raw = pick(i)
            answers, skipped = plan_answers(questions, rng, raw)
            print(f"\n=== [{i+1}/{count}] 填 {len(answers)} 题, 跳过 {len(skipped)} 题: {skipped}")
            for t in sorted(answers):
                q = by_topic[t]
                lab = q["options"].get(answers[t], "")
                print(f"  q{t:<3} {q['title'][:20]:<20} -> {answers[t]}"
                      + (f"  {lab[:32]}" if lab else ""))
        return

    from playwright.sync_api import sync_playwright
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        for i in range(count):
            outcome = ("fail", "")
            raw = pick(i)   # 一次生成整份，重试时复用
            for attempt in range(1, retries + 1):
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
                page = ctx.new_page()
                try:
                    page.goto(SURVEY_URL, wait_until="domcontentloaded", timeout=40000)
                    page.wait_for_selector("#ctlNext", timeout=20000)
                    answers, skipped = plan_answers(questions, rng, raw)
                    head = (f"[{i+1}/{count}] 填 {len(answers)} 题, "
                            f"跳过 {len(skipped)} 题(分支未命中): {skipped}")
                    ok, code, msg = fill_and_submit(page, questions, answers, rng)
                    print(head)
                    print(f"      -> {'OK' if ok else 'FAIL'} {msg}")
                    if ok:
                        outcome = ("ok", msg)
                        break
                    outcome = (f"fail:{code}", msg)
                    if code == "7" and attempt < retries:
                        wait = 25 * attempt          # 反垃圾频控：退避，别硬刚
                        print(f"      频控，等 {wait}s 重试 ({attempt}/{retries-1})")
                        time.sleep(wait)
                        continue
                    break
                except Exception as e:                        # noqa: BLE001
                    print(f"      -> ERROR {type(e).__name__}: {e}")
                    outcome = ("error", str(e))
                    break
                finally:
                    ctx.close()
            results.append(outcome)
            if outcome[0] == "ok":
                time.sleep(rng.randint(*gap))                 # 别打太密
        browser.close()

    ok = sum(1 for s, _ in results if s in ("ok", "dry-run"))
    print(f"\n真正落地: {ok}/{count}")
    for s, m in results:
        if s not in ("ok", "dry-run"):
            print(f"  {s}: {m[:110]}")
    return results


# ------------------------------------------------------------ 自检

def selfcheck():
    """条件逻辑求值的正确性 —— 这段逻辑错了整个脚本就没意义。"""
    # options 用真实的 {编号: 文字} 形状，别用 list —— 否则自检跑的是另一套数据结构，
    # 生产代码换成 dict 后这里照样绿，就白检了。
    qs = [
        {"topic": 6, "type": "single", "options": {"1": "在家", "2": "学校"}, "relation": None},
        {"topic": 7, "type": "single", "options": {"1": "能", "2": "不能"}, "relation": (6, {"1"})},
        {"topic": 8, "type": "multiple", "options": {"1": "弟弟", "2": "电视", "3": "邻居"}, "relation": None},
        {"topic": 9, "type": "fill_blank", "options": {}, "relation": (8, {"2", "3"})},
    ]
    # 分支命中：q6=1 -> q7 该填
    a, s = plan_answers(qs, random.Random(1), {})
    assert 7 in a and 7 not in s, f"q6=1 时 q7 应被填: {a} skipped={s}"
    # 多选 q8 命中 2/3 -> q9 该填
    a, s = plan_answers(qs, random.Random(2), {})
    got8 = set(a.get(8, "").split(MULTI_SEP))
    if got8 & {"2", "3"}:
        assert 9 in a, f"q8={a.get(8)} 命中时 q9 应被填"
    else:
        assert 9 in s, f"q8={a.get(8)} 未命中时 q9 应被跳过"
    # 未被依赖的题永远不跳过
    assert 6 in a and 8 in a
    # relation 解析
    assert parse_relation("8,1") == (8, {"1"})
    assert parse_relation("3,2;3") == (3, {"2", "3"})
    assert parse_relation(None) is None
    # _valid 是挡住脏值的唯一一道闸：LLM 编出来的选项编号必须被拒
    q1 = {"topic": 1, "type": "single", "options": {"1": "准初一", "2": "准初二"}}
    assert _valid(q1, "2"), "合法选项编号应放行"
    assert not _valid(q1, "9"), "不存在的选项编号必须被拒"
    assert not _valid(q1, ""), "空值必须被拒"
    # 复读检测（实测 deepseek 会把一句话原样输出两遍）
    qb = {"topic": 2, "type": "fill_blank", "options": {}}
    assert not _valid(qb, "作业也太多了吧" * 2), "同一句输出两遍应被拒"
    assert _valid(qb, "作业也太多了吧"), "正常一句话应放行"
    print("selfcheck OK")


def main():
    global SURVEY_URL
    ap = argparse.ArgumentParser(description="问卷星示例数据生成器")
    ap.add_argument("-n", "--count", type=int, default=5, help="生成份数")
    ap.add_argument("--plan", action="store_true", help="只打印计划，不开浏览器")
    ap.add_argument("--submit", action="store_true", help="真的提交（默认只演练）")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--context", default="", help="问卷背景，喂给 LLM")
    ap.add_argument("--answers", help="固定答案 JSON（{题号: 值} 或 [{...}, {...}]），"
                                      "给了就用固定答案、完全不调 LLM")
    ap.add_argument("--gap", type=int, nargs=2, metavar=("MIN", "MAX"), default=[5, 12],
                    help="每份之间的间隔秒数。默认 5-12；被频控就调大")
    args = ap.parse_args()

    if not SURVEY_URL:
        sys.exit("缺问卷链接。填答链接在问卷星后台的「分享链接」里，形如\n"
                 "  https://www.wjx.cn/vm/xxxxxx.aspx\n"
                 "设置环境变量：export WJX_URL=<链接>")

    selfcheck()

    html = fetch(SURVEY_URL)
    questions = parse_questions(html)

    # 题型是逐个写死实现的，没实现的题型必须硬失败。
    # 静默留空更糟：问卷星把漏填的必填题判无效，整份答卷直接作废，
    # 而且失败原因和"被反垃圾拦了"长得一模一样，看不出是自己漏填的。
    unsupported = {"matrix", "reorder", "slider", "group", "unknown"}
    todo = [f"q{q['topic']}({q['type']})" for q in questions if q["type"] in unsupported]
    if todo:
        sys.exit(f"这份问卷里有本脚本还不会填的题型: {todo}\n"
                 f"在 fill_and_submit() 里加对应分支再跑（题型编号见 TYPE_NAME）。")

    # 解析器出 bug 时最典型的表现就是"选项数=0"，而它要等到提交被拒才暴露。
    # 这里直接硬失败，比事后查错误信息快一个数量级。
    choice = {"single", "multiple", "scale", "droplist"}
    broke = [f"q{q['topic']}({q['type']})" for q in questions
             if q["type"] in choice and not q["options"]]
    if broke:
        sys.exit(f"解析失败，这些选择题没抓到选项: {broke}\n"
                 f"多半是问卷星改了 DOM，检查 parse_questions 里的正则。")

    print(f"\n问卷: {SURVEY_URL}")
    print(f"解析到 {len(questions)} 题；条件逻辑题 "
          f"{sum(1 for q in questions if q['relation'])} 个")
    for q in questions:
        mark = f" <- 依赖 q{q['relation'][0]} 选 {'/'.join(sorted(q['relation'][1]))}" if q["relation"] else ""
        print(f"  q{q['topic']:<3}{q['type']:<12}{q['title'][:38]}{mark}")

    fixed = None
    if args.answers:
        raw = json.loads(Path(args.answers).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = [raw]
        # JSON 的对象键一定是字符串，而 plan_answers 用 int 题号查表，
        # 不在这里转成 int 就会全部 miss、静默退回随机答案。
        known = {q["topic"] for q in questions}
        fixed, unknown = [], set()
        for d in raw:
            d = {int(k): str(v) for k, v in d.items()}
            unknown |= set(d) - known
            fixed.append(d)
        if unknown:
            sys.exit(f"--answers 含问卷里没有的题号 {sorted(unknown)}；"
                     f"本问卷的题号是 {sorted(known)}")
        print(f"\n固定答案模式：{len(fixed)} 份，不调用 LLM")
    else:
        print("\n纯 AI 模式：每份由 LLM 一次生成")

    # --plan 就是 dry run：走完整的规划路径，只是最后不提交
    run(args.count, args.submit and not args.plan, questions,
        args.context, args.seed, gap=tuple(args.gap), fixed=fixed)


if __name__ == "__main__":
    main()