# wjx-seed — 问卷星示例数据生成器

给自己的问卷灌一批结构自洽的示例答卷,用来在问卷星后台看统计和交叉分析效果。

## 为什么不用现成的开源项目

调研了 GitHub 上 16 个问卷星自动填写项目,**没有一个能直接用**:
- 有 star 的(★123~394)全是 2020-2023 停更的死项目
- 近期还在更新的 8 个全是 0-4 star 个人仓库,零用户验证
- **8 个里有 7 个完全没处理条件逻辑跳题**(`跳题|条件逻辑|relation=|logicjump` 全库命中 0 文件),
  会生成"第 6 题选了不在家完成、却把第 7~10 题也填了"这种自相矛盾的答卷
- 问卷星官方 API 全部只读,**没有任何写入答卷的接口**

所以自己写。核心是把条件逻辑做对。

## 用法

问卷链接是**必填**的,不写死在代码里(公开仓库不该默认指向某一份具体问卷):

```bash
git clone https://github.com/54cwh/wjx-seed && cd wjx-seed
python3 -m venv .venv && .venv/bin/pip install playwright
.venv/bin/playwright install chromium        # 或用系统 Chrome,见下

export WJX_URL=https://www.wjx.cn/vm/xxxxxx.aspx   # 填答链接,后台「分享链接」
export LLM_API_KEY=sk-...                        # 任意 OpenAI 兼容端点
```

### 纯 AI 模式(看填写质量)

LLM 一次调用生成**整份**答卷:先定一个人设(年级/家庭环境/作业态度),再照人设答全部题。
同一份里的题因此彼此自洽,交叉分析出来的数据才有意义。

```bash
# 先演练:不开浏览器、不提交,看人设和每题答案
.venv/bin/python wjx_seed.py --plan -n 3 \
    --context "关于寒暑假作业完成情况与家庭学习环境的调查"

# 真的提交 20 份
.venv/bin/python wjx_seed.py -n 20 --submit --gap 8 15 \
    --context "关于寒暑假作业完成情况与家庭学习环境的调查"
```

### 固定答案模式(测提交链路)

给了 `--answers` 就完全不调 LLM。要它是因为 LLM 每次生成不同,
"这份能不能跑通"没法复现;而验证浏览器自动化 + 条件逻辑 + 签名提交是否稳定,
需要完全确定的输入。

```bash
# answers.sample.json 里有 8 份固定答案,覆盖条件逻辑的不同分支
.venv/bin/python wjx_seed.py -n 3 --plan   --answers answers.sample.json
.venv/bin/python wjx_seed.py -n 3 --submit --answers answers.sample.json
```

JSON 格式是 `{题号: 值}` 或 `[{...}, {...}]`(后者按份循环用)。
仓库里的 `answers.sample.json` 是自动生成的样例,可以照着改。

参数:`-n` 份数 · `--plan` 只演练 · `--submit` 真提交 · `--seed` 固定随机种子 ·
`--context` 问卷背景(喂给 LLM) · `--answers` 固定答案文件 · `--gap MIN MAX` 提交间隔秒数

其他环境变量:`WJX_URL`(必填)· `LLM_API_KEY` · `LLM_BASE_URL`(默认 `https://api.deepseek.com/v1`)·
`LLM_MODEL`(默认 `deepseek-chat`)。

没装 Chromium 的话脚本会回退到系统 Chrome(`channel="chrome"`,启动参数带 `--no-sandbox`)。

## 原理

**条件逻辑**靠 `relation` 属性求值,不是靠可见性:
```html
<div class='field ui-field-contain' topic='9' relation='8,1'>   ← 第8题选第1项才显示
```
问卷星初始化时会把**所有**题设成 `display:none`,所以拿可见性判断分支会全错。
本题的条件依赖题号恒小于被依赖题号,所以按题号正序遍历一遍就够,不需要拓扑排序。

**答案生成**分两层,分工明确:
- **选哪一项由我们定**。让 LLM 自由选实测会全部挤在同一格(年级 3/3 都是"准初三",
  "是否在家完成" 6/6 都是"是"),跑出来的交叉分析全是无效数据。所以每份先把所有
  单选题的边缘分布随机好,写进提示词要求它照着答。
- **答案内容由 LLM 写**,并且必须和已指定的选项自洽。填空题也是它写,不再单独抽池子。

题目从 `<div class='label' for='q1_1'>准初一</div>` 解析出**选项文字**——
只给模型编号的话它只能瞎猜每题在问什么。

**提交**交给真实 Chrome(Playwright 驱动)。问卷星的提交是带签名的:`jqsign = dataenc(jqnonce)`
是逐字符 `charCode ^ (ktimes % 10)`,`ktimes` 是鼠标悬停计数器,答案打包 `clientAnswerSend`
由页面遍历 DOM 现算。手写 HTTP 等于重做浏览器已经做好的活。

**哪些题该出现永远由代码裁定**(`plan_answers()` 按 `relation` 求值),LLM 只提供值。
它给了非法选项编号就退回随机——把跳题判断交给模型是这类工具最容易出错的地方。

## 已验证

在一份 15 题、含 7 道条件逻辑题的问卷上实测:

- 单选、多选、下拉、量表(1-10)、填空 5 种题型都能正确填写
- 条件逻辑链运行时验证:点第 6 题选"在家完成",第 7/8 题 computed display 从 `none` 变 `block`
- **两种模式各 3 份,6/6 全部真正落地**(拿到 joinid),ktimes 落在 64-111(每题 7-11 次悬停)
- 实际填的题数在 9~15 之间浮动,跳过的题集合覆盖 `[12]`/`[7,8,9,10,15]`/`[7,8,9,10,12,15]` 等分支
  —— 条件逻辑真的在走,不是无脑全填
- LLM 生成的人设有分化(准初一新生 / 准初三偏科 / 在家长面前装模作样),不是复读

判断成功**不看页面跳没跳转**——问卷星失败时页面原地不动、什么都不显示,看起来和成功一模一样。
脚本读 `processjq.ashx` 的 XHR 响应体:`10〒<URL>` 成功,`7〒需要安全校验` 被频控,
点提交后一个请求都不发则是弹了验证码。

`selfcheck()` 每次运行前自动执行,断言条件逻辑求值、`relation` 解析、脏值拦截和复读检测。

## 被墙的规则(实测,非猜测)

问卷星客户端侧的风控开关在页面上全是**关**的,规则全在服务端,只能靠对照实验测出来:

| 全局变量 | 值 | 说明 |
|---|---|---|
| `maxCheatTimes` | 0 | 防作弊上限关闭 |
| `useAliVerify` | 0 | 客户端验证码关闭 |
| `getAnswerSpeedSubmitJSON` | undefined | **答题速度追踪未启用** |
| `bindAnswerSpeedTracker` | undefined | 同上 |
| `captchaType` | 2 | 阿里云验证码,sceneId `q0hcfsca` |

提交包(实测抓取)里 query 为 `shortid starttime cst submittype ktimes rn nw jwt jpm capt t wxfs jqnonce jqsign`,
POST 为 `submitdata`(`topic$value` 用 `}` 分隔) + `sceneId`。**没有任何答题时长字段**——
答了 157 秒和答了 4 秒,服务端看到的东西完全一样。

### 三层墙

**第一层:`ktimes` / 答题数 的比值(主判据)**

`ktimes` 是页面里的鼠标悬停计数器,随提交上报。它是提交包里**唯一的行为信号**。

| 填法 | ktimes 比值 | 落地率 |
|---|---|---|
| 裸 `page.click()` | ≈ 1.7 | 11/20 = 55% |
| `mouse.move` 轨迹后点 | ≈ 7 | 8/10 = 80%,补退避后 3/3 |

Playwright 的 `click()` 只产生一次 hover,所以自动化脚本的 ktimes 天然低 3-5 倍。
**答题速度对此毫无影响**——4-8 秒填完 15 题照样过。这就是 `_human_click()` 存在的原因。

**第二层:IP 提交频次**

独立于 ktimes。就算 ktimes 比值高达 8.8(全场最高),连续提交到第 6 份仍会返回
`7〒需要安全校验，请重新提交！`。软拦,退避 25s→50s 能过。

**第三层:阿里云验证码**

点提交后**一个请求都不发**(点击被验证码流程吞掉),页面出现 `#captchaOut` 容器。
概率性触发,退避无效,只能等冷却或人工过。这是这条路的天花板。

### 没测出来的部分

- **ktimes 的确切阈值**:只测到 ≈1.7 会挂、≈7 能过,没二分出边界
- **是比值还是绝对值**:我的测试里答题数(8-15)和 ktimes(75-104)同向变动,
  没解耦。换更长的问卷时两者关系可能改变
- **验证码的触发条件**:只有 3 次观测(#1 触发、随后 5 次不触发),样本太小,规律不明

## 其他限制

- 只实现了 5 种题型(单选/多选/下拉/量表/填空)。矩阵题(type=7)、排序题(type=8)、
  滑块题(type=9)、分组题(type=10)会在启动时**硬失败并告诉你哪几题**,不会静默留空
- 问卷星改了 DOM 时,选择题解析出 0 个选项同样硬失败并提示去改正则——
  这个 bug 最典型的表现是"提交被拒",而失败原因和"被反垃圾拦了"长得一模一样
- LLM 返回的 JSON 偶尔截断或没转义引号,已加 2 次重试;仍失败会告警而不是静默填空
- **单选题的边缘分布是随机采样的,不按问卷主题做加权**。想要"高一比初二更焦虑"这类
  相关性,得自己在 `--context` 里描述,或者去改 `llm_response()` 里的 `hint` 采样段
- `code=7` 的退避是固定 `25 × attempt` 秒,不是指数退避;撞验证码直接停手,
  但仍会把当前那份剩下的重试次数跑完
