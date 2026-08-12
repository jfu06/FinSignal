# SEC XBRL 数值数据字典（Data Dictionary）

> 覆盖 FinSignal「数字类数据来源」决策所依赖的 SEC 官方免费 XBRL API（data.sec.gov）。本文档基于 2026-08-12 对真实 API 返回的实测整理，样本为 Apple Inc.（CIK 0000320193，US-GAAP 申报）和 SAP SE（CIK 0001000184，IFRS 申报）。所有字段名、单位、日期范围均来自实际返回值，不是转述文档。相关决策见 [decisions.md](./decisions.md)。

## 0. 通用约定（所有端点适用）

| 约定 | 内容 |
|---|---|
| CIK 格式 | URL 里必须是 **10 位、前导零填充**（`CIK0000320193`）；但返回 JSON 里的 `cik` 字段是不带前导零的数字（`320193`） |
| 访问要求 | 无需 API key；必须带自报身份的 `User-Agent` 头；遵守 SEC 公平访问政策（约 10 请求/秒上限） |
| 更新频率 | XBRL 三个端点在 filing 被受理后约 1 分钟内更新；另有每晚 ~3am ET 更新的全量包 `companyfacts.zip`（离线摄取建议用这个，不要爬 API） |
| 数值口径 | `val` 是**原始单位**数值：美元就是美元（AAPL 净利润 `3496000000`），不是千/百万；每股值是每股美元 |
| 覆盖范围 | 只包含**标准 taxonomy**（us-gaap / ifrs-full / dei / srt）的标签；公司自定义扩展标签（如 `aapl:` 前缀的分产品收入）**不在任何端点里**，要拿只能回原始 filing |
| 历史起点 | XBRL 强制申报从 2009 年分批开始；大公司数据实际起点约为 2009 年提交的 filing（其中比较期可回溯到更早，AAPL 最早数据点 `end=2006-09-30`） |

## 1. Company Facts 端点（主表，FinSignal 摄取入口）

`GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` — 一家公司的全部标准标签数值，一次拿全。

### 1.1 顶层结构

| 字段 | 类型 | 说明 |
|---|---|---|
| `cik` | number | 不带前导零 |
| `entityName` | string | 公司名（如 `Apple Inc.`） |
| `facts` | object | 两级嵌套：`facts[taxonomy][tag]` |

### 1.2 `facts` 的两级键

| 层级 | 可能取值 | 实测 |
|---|---|---|
| taxonomy | `dei`、`us-gaap`、`ifrs-full`（外国发行人）、`srt` 等 | AAPL：`dei`(2 tags) + `us-gaap`(503 tags)；SAP：`dei`(1) + `ifrs-full`(368)。**同一指标在两套 taxonomy 下标签完全不同** |
| tag | 标准 taxonomy 概念名 | 如 `NetIncomeLoss`、`Assets` |

每个 tag 对象含三个字段：`label`（人类可读名）、`description`（准则定义，长文本）、`units`（按计量单位分组的数据点数组）。

### 1.3 `units` 的键（计量单位）

单位是**字符串键**，一个 tag 可以同时有多个单位数组。实测出现过：

| 单位 | 含义 | 实测例子 |
|---|---|---|
| `USD`、`EUR` 等 ISO 币种 | 金额 | AAPL 446 个 tag 用 `USD`；SAP 345 个 tag 用 `EUR`、91 个用 `USD` |
| `shares` | 股数 | 流通股、加权股数 |
| `USD/shares`、`EUR/shares` | 每股金额 | EPS、每股分红 |
| `pure` | 无量纲（比率、税率） | `EffectiveIncomeTaxRateContinuingOperations` |
| `Year` / `Y` | 年数（期限类） | **同一含义两种写法**：AAPL 用 `Year`，SAP 用 `Y` |
| `USD/EUR` 等汇率 | 折算率 | SAP 有 `AUD/EUR`、`JPY/EUR` 等 |
| 自定义单位 | 任意字符串 | AAPL 有 `Store`（门店数），SAP 有 `employee`、`item` |

### 1.4 数据点记录结构（本端点与 companyconcept 完全一致）

`units` 下每个数组元素是一次「某张 filing 里申报的一个数值」：

| 字段 | 类型 | 必有 | 含义 | 陷阱 |
|---|---|---|---|---|
| `start` | date | 否 | 期间起点 | **instant 型概念没有这个字段**（见 5.5） |
| `end` | date | 是 | 期间终点 / 时点日期 | 取数按期间过滤要用 `start`/`end` |
| `val` | number | 是 | 数值，原始单位 | — |
| `accn` | string | 是 | 申报编号（accession number），可回溯到具体 filing | 引用溯源就靠它 |
| `fy` | number | 是 | **该 filing 所属的财年**，不是数据所属财年 | 见 5.2，最大坑 |
| `fp` | string | 是 | 该 filing 的财期：`FY`/`Q1`/`Q2`/`Q3`/`Q4` | 同上 |
| `form` | string | 是 | 表单类型：`10-K`、`10-Q`、`10-K/A`、`8-K` 等 | `/A` 是修正版，值可能和原版不同 |
| `filed` | date | 是 | 提交日期 | 去重时「取最新」的依据 |
| `frame` | string | 否 | 日历期标记，如 `CY2007`、`CY2009Q1` | **只有被 SEC 选为该期间「官方代表点」的记录才有**（见 5.4） |

## 2. Company Concept 端点（单指标明细）

`GET https://data.sec.gov/api/xbrl/companyconcept/CIK##########/{taxonomy}/{tag}.json` — 一家公司 × 一个概念。

| 字段 | 类型 | 说明 |
|---|---|---|
| `cik` | number | 同上 |
| `taxonomy` / `tag` | string | 如 `us-gaap` / `EarningsPerShareDiluted` |
| `label` / `description` | string | 概念名与准则定义 |
| `entityName` | string | 公司名 |
| `units` | object | 与 1.3、1.4 结构完全一致 |

用途：companyfacts 太大时按需取单指标；数据点结构与主表相同，不再重复。

## 3. Frames 端点（横截面：一期 × 一指标 × 全市场）

`GET https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json` — 某个日历期内，所有申报了该概念的公司各取一条。

### 3.1 URL 参数约定

| 参数 | 格式 | 例子 | 注意 |
|---|---|---|---|
| `unit` | 复合单位用 `-per-` 连接 | `USD`、`USD-per-shares` | 与数据点里的 `USD/shares` 写法**不一致** |
| `period` | 年度 `CY####`（365±30 天）；季度 `CY####Q#`（91±30 天）；时点 `CY####Q#I` | `CY2025Q4`、`CY2026Q1I` | instant 概念必须带 `I` 后缀 |

### 3.2 返回结构

| 字段 | 类型 | 说明 |
|---|---|---|
| `taxonomy` / `tag` / `uom` / `ccp` | string | `ccp` 即请求的日历期（如 `CY2026Q1I`） |
| `label` / `description` | string | 概念定义 |
| `pts` | number | 数据点数 = `data.length` |
| `data[]` | array | 每家公司一条：`accn`、`cik`、`entityName`、`loc`（注册地，如 `US-IL`）、`start`（duration 型才有）、`end`、`val` |

实测：`Assets/USD/CY2026Q1I` 有 5,512 家公司；`Revenues/USD/CY2025Q4` 只有 378 家（原因见 5.1、5.6——多数公司收入用别的 tag，且 Q4 单季很少直接申报）。

## 4. 核心数值指标一览（AAPL 实测：单位、类型、时间范围）

| 指标 | tag（us-gaap） | 单位 | 类型 | 实测范围（end 日期） | 点数 |
|---|---|---|---|---|---|
| 收入（ASC 606 后） | `RevenueFromContractWithCustomerExcludingAssessedTax` | USD | duration | 2017-09-30 ~ 2026-06-27 | 117 |
| 收入（旧标签，已停用） | `SalesRevenueNet` | USD | duration | 2007-09-29 ~ 2018-06-30 | 210 |
| 收入（通用标签，AAPL 仅短暂使用） | `Revenues` | USD | duration | 2016-09-24 ~ 2018-09-29 | 11 |
| 销售成本 | `CostOfGoodsAndServicesSold` | USD | duration | 2007-09-29 ~ 2026-06-27 | 234 |
| 毛利 | `GrossProfit` | USD | duration | 2007-09-29 ~ 2026-06-27 | 338 |
| 研发费用 | `ResearchAndDevelopmentExpense` | USD | duration | 2007-09-29 ~ 2026-06-27 | 234 |
| 营业利润 | `OperatingIncomeLoss` | USD | duration | 2007-09-29 ~ 2026-06-27 | 234 |
| 所得税费用 | `IncomeTaxExpenseBenefit` | USD | duration | 2007-09-29 ~ 2026-06-27 | 234 |
| 净利润 | `NetIncomeLoss` | USD | duration | 2007-09-29 ~ 2026-06-27 | 338 |
| 基本 EPS | `EarningsPerShareBasic` | USD/shares | duration | 2007-09-29 ~ 2026-06-27 | 338 |
| 稀释 EPS | `EarningsPerShareDiluted` | USD/shares | duration | 2007-09-29 ~ 2026-06-27 | 338 |
| 总资产 | `Assets` | USD | **instant** | 2008-09-27 ~ 2026-06-27 | 146 |
| 总负债 | `Liabilities` | USD | **instant** | 2008-09-27 ~ 2026-06-27 | 144 |
| 股东权益 | `StockholdersEquity` | USD | **instant** | 2006-09-30 ~ 2026-06-27 | 264 |
| 现金及等价物（期末余额） | `CashAndCashEquivalentsAtCarryingValue` | USD | **instant** | 2006-09-30 ~ 2026-06-27 | 228 |
| 经营现金流 | `NetCashProvidedByUsedInOperatingActivities` | USD | duration | 2007-09-29 ~ 2026-06-27 | 134 |
| 回购支出 | `PaymentsForRepurchaseOfCommonStock` | USD | duration | 2011-09-24 ~ 2026-06-27 | 126 |
| 流通股数（资产负债表口径） | `CommonStockSharesOutstanding` | shares | **instant** | 2008-09-27 ~ 2026-06-27 | 144 |
| 流通股数（封面口径） | `dei:EntityCommonStockSharesOutstanding` | shares | **instant** | 至最近一期 10-Q/10-K 封面日 | — |
| 公众持股市值 | `dei:EntityPublicFloat` | USD | **instant** | 每年 10-K 一个点 | — |

> 点数差异本身有信息量：338（每季 + 比较期都报）vs 134（只在半年/年度累计口径报）——**不是每个指标每季度都有独立数据点**。

## 5. 易混淆口径清单（重点，做 SQL/指标层前必读）

### 5.1 同一个指标，好几个 tag（收入是重灾区）
AAPL 的「收入」历史上用过 3 个 tag（见第 4 节前三行）：2018 财年前是 `SalesRevenueNet`，ASC 606 之后换成 `RevenueFromContractWithCustomerExcludingAssessedTax`，中间还有 11 个点落在通用标签 `Revenues` 上。IFRS 公司则完全是另一套（`ifrs-full:Revenue`）。**指标层必须建「指标 → tag 优先级列表」的映射表**，按优先级取数并拼接时间序列，否则查 2016 年收入会返回空或错位。

### 5.2 `fy`/`fp` 不是数据所属期间，是 filing 所属期间
实测：`start=2006-10-01, end=2007-09-29`（FY2007 净利润）这个点挂着 `fy=2009, fp=FY`——因为它作为比较期出现在 2009 年的 10-K 里。**按年份/季度筛数据只能用 `start`/`end`，用 `fy`/`fp` 会整期错位。**

### 5.3 同一期间多条记录，值还可能不一样
NetIncomeLoss 的 338 个点里，112 个期间（start,end 组合）出现不止一次——同一数字会被原始 filing、修正版（10-K/A）、后续年份的比较期反复申报。实测 FY2007 净利润：10-K 报 3,496M，10-K/A 报 **3,495M**。去重规则建议：按 `(tag, unit, start, end)` 分组取 `filed` 最新的一条；或只取带 `frame` 的点（见下）。

### 5.4 `frame` 字段只标「官方代表点」
NetIncomeLoss 338 个点中只有 86 个带 `frame`。SEC 为每个日历期在重复记录里只选一条打上 `frame` 标记（frames 端点返回的就是这些）。适合做横截面比较；但注意它按**日历期**分箱，非日历财年公司会被归到最近的日历期。

### 5.5 instant vs duration：没有 `start` 字段的是时点值
资产负债表类概念（`Assets`、`StockholdersEquity`、`CommonStockSharesOutstanding`、现金**余额**）是 instant 型，数据点没有 `start`。利润表/现金流量表类是 duration 型。两个坑：把 `CashAndCashEquivalentsAtCarryingValue`（期末余额）当成现金流量；对 instant 概念请求 frames 时忘了 `I` 后缀。

### 5.6 Q4 单季数据基本拿不到，要自己算
利润表指标的短周期（≤120 天）数据点里，Q4 形态的期间 AAPL 只有 11 个、且集中在 2009–2012 年（来自当年的年报附注/8-K），之后再也没有。原因：10-K 只报全年，Q4 单季 = FY − Q1 − Q2 − Q3，**必须在指标层计算**。frames 的 `CY####Q4` duration 数据同样稀疏。

### 5.7 财年 ≠ 日历年
AAPL 财年 9 月底结束（如 FY2025 = 2024-09-29 ~ 2025-09-27）。frames 用日历期分箱、按 365±30 / 91±30 天窗口匹配，所以同一个 frame 里各公司的实际起止日期并不相同；跨公司同期比较时要意识到「同期」是近似的。

### 5.8 「流通股数」有三个口径
`dei:EntityCommonStockSharesOutstanding`（**封面日**、每次 10-Q/10-K 提交时点）、`us-gaap:CommonStockSharesOutstanding` 与 `CommonStockSharesIssued`（**资产负债表日**，发行数 ≥ 流通数）、`WeightedAverageNumberOfBasic/DilutedSharesOutstanding`（**期间加权**，算 EPS 专用，duration 型）。算市值、算 EPS、核对披露，各用各的，不能混。

### 5.9 历史值按申报原样保留，拆股不追溯
同一个期间的 EPS 在不同 filing 里的值差好几倍是正常的。实测 AAPL FY2019 稀释 EPS：2019 年 10-K 报 **11.89**，2020/2021 年 10-K 的比较期报 **2.97**（2020 年 8 月 1:4 拆股后重述）。API 不做任何追溯调整，老申报的点原样留在数组里。**每股类、股数类指标跨期取数必须固定「取该期间 filed 最新的一条」，否则时间序列会在拆股处断裂。**

### 5.10 单位字符串不规范、同一 tag 可以有多种单位
年限类单位 AAPL 写 `Year`、SAP 写 `Y`；同一个 tag（`FiniteLivedIntangibleAssetsUsefulLifeMaximum`）在 AAPL 数据里同时出现在 `pure` 和 `Year` 两个单位数组下。比率税率类用 `pure`。**聚合前先按 unit 过滤，不能把一个 tag 的所有单位数组直接拼起来。**

### 5.11 币种不只 USD
外国私人发行人按本币申报（SAP 345 个 tag 是 `EUR`），还有 `USD/EUR` 这类汇率单位。若未来覆盖非美国公司，金额聚合必须带币种维度。

### 5.12 公司自定义标签不在 API 里
分产品/分部收入等常用自定义扩展标签申报（`aapl:...`），XBRL API 一概没有。用户问「iPhone 收入」这类问题时，SQL/指标层无法回答，应路由到 RAG（文档层）或明确告知数据边界——这正好对应 decisions.md 里的问答路由决策。

## 6. 对 FinSignal 落库的直接建议

单表即可起步：`xbrl_facts(cik, taxonomy, tag, unit, start, end, val, accn, fy, fp, form, filed, frame)`，唯一键 `(cik, taxonomy, tag, unit, start, end, accn)`；其上建两个视图——`facts_dedup`（每期间取 `filed` 最新）和 `facts_canonical`（只留 `frame` 非空）；另建 `metric_map(metric, taxonomy, tag, priority)` 处理 5.1 的多标签问题。摄取用每晚的 `companyfacts.zip` 全量包而非逐个调 API。

---

*数据来源：SEC EDGAR APIs（data.sec.gov），实测日期 2026-08-12。官方文档：sec.gov/search-filings/edgar-application-programming-interfaces。*
