"""统一数据 schema —— 适配器模式的"统一格式"底座。

所有数据源适配器都必须把原始数据翻译成这里定义的标准结构，
上层（处理层/分析层）只认这些结构，永远不知道底层是哪个数据源。

★ 多源增强：增加 DataPoint（带来源标注的数据点）与 SourceInfo（数据源元信息），
  支撑"数据源徽章 + 交叉验证"的前端展示。
"""
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# 多源标注
# ---------------------------------------------------------------------------

@dataclass
class SourceInfo:
    """数据源元信息（用于前端展示"这条数据来自哪里、是否可信"）。"""
    key: str                      # 数据源标识，如 qcc / eastmoney / local
    label: str                    # 展示名，如"企查查MCP"
    category: str = ""            # 类别：工商 / 财务 / 行情 / 司法 / 舆情
    authoritative: bool = False   # 是否为权威源（用于可信度加权）
    url: str = ""                 # 数据源主页（可选）


@dataclass
class LegalEvent:
    """法律事件（统一格式）"""
    title: str                 # 事件标题，如"被列为被执行人"
    severity: str              # high / medium / low
    amount: Optional[str] = None       # 涉事金额（统一成"元"）
    occurred_at: Optional[str] = None  # 发生日期 YYYY-MM-DD
    source: str = ""                   # 数据来源，如"企查查"
    evidence: str = ""                 # 原始文本片段（证据链用）
    dimension: str = "legal"           # 归属维度（可用于归集到五个维度）
    count: Optional[int] = None        # 命中条数（如"裁判文书 18 条"）


@dataclass
class NewsItem:
    """舆情条目（统一格式）"""
    title: str
    sentiment: str             # negative / neutral / positive
    severity: str              # high / medium / low
    published_at: Optional[str] = None
    source: str = ""
    url: str = ""
    evidence: str = ""


@dataclass
class FinancialMetrics:
    """财务指标（统一格式）—— 注意：只含数字，不含主体标识，便于脱敏后上云"""
    debt_ratio: Optional[float] = None        # 资产负债率 %
    current_ratio: Optional[float] = None     # 流动比率
    quick_ratio: Optional[float] = None       # 速动比率
    net_margin: Optional[float] = None        # 净利率 %
    gross_margin: Optional[float] = None      # 毛利率 %
    roe: Optional[float] = None               # ROE %
    roa: Optional[float] = None               # ROA %
    operating_cashflow: Optional[float] = None  # 经营现金流（元）
    net_profit: Optional[float] = None        # 净利润（元）
    revenue: Optional[float] = None           # 营业收入（元）
    total_assets: Optional[float] = None      # 资产总额（元）
    revenue_yoy: Optional[float] = None       # 营收同比 %
    profit_yoy: Optional[float] = None        # 净利同比 %
    audit_opinion: Optional[str] = None       # 审计意见类型
    report_period: Optional[str] = None       # 报告期（如"2025年年报"）


@dataclass
class MarketData:
    """行情/估值数据（统一格式）—— 来自东方财富等免费实时源。"""
    stock_code: Optional[str] = None      # 股票代码
    stock_name: Optional[str] = None      # 股票简称
    price: Optional[float] = None         # 最新价
    change_pct: Optional[float] = None    # 涨跌幅 %
    pe: Optional[float] = None            # 市盈率
    pb: Optional[float] = None            # 市净率
    total_market_cap: Optional[float] = None  # 总市值（元）
    turnover_rate: Optional[float] = None # 换手率 %
    source: str = ""                      # 数据来源


@dataclass
class Shareholder:
    """股东信息（集中度维度用）。"""
    name: str
    ratio: Optional[float] = None      # 持股比例 %
    amount: Optional[str] = None       # 认缴出资额
    type: str = ""                     # 股东类型（自然人/企业）


@dataclass
class SupplierProfile:
    """供应商档案（统一格式）"""
    name: str
    important_outsourcing: bool = False       # 是否"重要外包"（《办法》第十二、十三条）
    non_resident: bool = False                # 是否非驻场外包（第十八条）
    cross_border: bool = False                # 是否跨境外包（第十九条）
    legal_events: List[LegalEvent] = field(default_factory=list)
    news: List[NewsItem] = field(default_factory=list)
    financials: Optional[FinancialMetrics] = None
    market: Optional[MarketData] = None        # 行情/估值（新增）
    shareholders: List[Shareholder] = field(default_factory=list)  # 股东（新增）
    ai_analysis: Optional[str] = None          # LLM 财务分析结论（可选）
    registration: Optional[dict] = None        # 工商登记信息
    sources: List[str] = field(default_factory=list)   # 本次参与的数据源 key（新增）
    source_meta: dict = field(default_factory=dict)    # key -> SourceInfo（新增）
    risk_alerts: List[dict] = field(default_factory=list)  # 综合风险提示（新增）
    # 涉密文件上传通道用：文件中是否确实提供了外包标记（重要/跨境/非驻场）。
    # bool 字段默认 False 无法区分"未提供"与"明确为否"，故用此标记显式记录。
    # 仅 strict_na 评分模式下消费（scoring.dim_has_data）。
    conc_flags_provided: bool = False
    # 上传文件里自带的履约指标快照（第25条效能质量指标的直接证据）：
    #   {"score": 93.0, "items": {"合同履约率": 98.4, "订单准时交付率": 96.2, ...}}
    # 尽调/履约报告常自带这些数据；未接入台账（service_levels）时用它兜底，
    # 避免"文件里明明有履约数据，履约维度却显示未录入"。
    perf_snapshot: Optional[dict] = None
    # 取数**失败**（≠ 确实为空）的维度名。适配器在某子工具调用失败时登记，
    # 评分层据此把该维度标 na（不臆造满分）—— 否则"没取到"会被当成"没风险"而虚高分数。
    # ⚠️ 与 conc_flags_provided 同理：空列表 ≠ 取数失败，绝不能混为一谈。
    unavailable_dims: List[str] = field(default_factory=list)


@dataclass
class RiskScore:
    """安全度评分结果（评级引擎输出，供前端/报告消费）"""
    supplier: str
    total_score: float                        # 0-100 综合分
    level: str                                # red / yellow / green
    dimension_scores: dict = field(default_factory=dict)  # 各维度分
    red_lines: list = field(default_factory=list)  # 命中的红线熔断规则
    weights: dict = field(default_factory=dict)    # AHP 权重（供前端展示加权过程）
    cr: float = 0.0                                # 一致性比率
    # ---- 六维扩展（2026-09）----
    dimension_labels: dict = field(default_factory=dict)   # 维度 key → 中文名
    dim_kind: dict = field(default_factory=dict)           # 维度 key → base/service
    na_dimensions: list = field(default_factory=list)      # 因缺数据未参与加权的维度
    raw_weights: dict = field(default_factory=dict)        # 归一化前的原始 AHP 权重
    performance: dict = field(default_factory=dict)        # 履约表现维度详情（含 decay/forecast）
    # 等级调整记录（短板约束 / 信号升级）。
    # 加权平均是"补偿性"的，单点重大风险会被其他维度稀释，故额外施加：
    #   短板约束（限上限）—— 最低维度分过低时等级不得偏高
    #   信号升级（抬下限）—— 履约维度出现恶化信号时等级不得偏低
    # 每项形如 {"type": "floor|signal", "level": "yellow", "reason": "..."}
    constraints: list = field(default_factory=list)
    # ---- 口径透明化（2026-10）----
    # 覆盖度：用了几个维度、缺哪几个、属于哪个"覆盖组"。
    # 缺维度的企业重归一化后有效权重不同，分数只能同组横比。
    coverage: dict = field(default_factory=dict)
    # 不含履约维度的对照分（履约在本演示中为模拟数据，用于验证"模拟部分占多少"）
    baseline_score: Optional[float] = None
    # 各维度数据出处：{"performance": "simulated" | "row-data" | ...}
    data_origin: dict = field(default_factory=dict)
