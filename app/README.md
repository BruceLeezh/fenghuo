# 烽火 · 信息科技外包风险智能预警系统 — 可运行实现

本目录是技术方案的可运行实现，包含完整的前后端闭环：

- **公开公司查询**：输入真实公司名/简称/股票代码 → 公开数据源 → 云端分析（DeepSeek-V4.1-Flash）→ 评级结果
- **涉密文件上传**：上传 Excel/PDF/Word → 本机解析 → **纯规则抽取与评分**（不出网、不调用任何模型）
- **评级引擎**：规则打分（扣分项标注监管条款）+ AHP 定权 + 三道闸（红线熔断 / 短板约束 / 信号升级）
- **周期监控**：组合看板、台账、集中度、专属场景、服务水平、处置推演沙盘、合规日历、处置工单、监管报送
- **报告导出**：一键生成 HTML 风险报告

## 公开公司查询：真实数据从哪来

采用"**企查查实时 + 本地预采集库 + 免费实时源**"三级策略，全部真实公开数据，无虚拟样本：

| 级别 | 数据源 | 覆盖范围 | 说明 |
|---|---|---|---|
| 1 ★ | **企查查 MCP（实时）** | 全国 4 亿+ 市场主体 | 真实工商 + 年报财务（近 3 期）+ **35 项风险因子扫描** |
| 2 | `data/public/*.json` 本地库 | 银行 IT 外包上市公司 7 家 | 离线可用、秒级响应 |
| 3 | 东方财富公开接口 | 沪深 A 股全量 | 免费实时行情/估值 |

前端"数据源"下拉框可切换：`自动（优先企查查）` / `企查查实时` / `本地预采集库`。

### 企查查实时能力（本项目核心数据源）

通过官方 MCP Server 接入（`integrations/qcc_mcp.py`），配置 Token 后自动启用：

| 能力 | 工具 | 对应风险维度 |
|---|---|---|
| 企业锚定 | `get_company_by_query` | —（实体识别，多候选时提示用户选择） |
| 工商登记 | `get_company_registration_info` | 经营风险 |
| 年报财务 | `get_financial_data`（近 3 期） | 财务风险 |
| **35 项风险因子扫描** | `get_company_risk_scan` | 法律风险 ★核心★ |
| 失信/被执行/行政处罚 | `get_dishonest_info` 等 | 法律风险（红线熔断） |

**实测示例（本地库快照口径，可复现）**：科蓝软件 —— 资产负债率 76.87%、销售净利率
−66.89%、ROE −64.06%、营收同比 −29.88%，财务维度 45 分（四项规则命中）；六维综合
**67.33 分，等级 red**，触发原因是**短板约束**（履约表现仅 29.9 分 < 35，木桶效应封顶为红）。

> 说明：该数值取自 `data/public/` 的离线快照，`python pipeline.py` 或界面查询均可复现。
> 走「企查查实时」时数据以查询当时为准，分数与司法风险条目会随数据更新而变化。

### 配置企查查 Token

在 `app/.env` 中设置：

```
QCC_API_TOKEN=你的企查查MCP_API_KEY
```

Token 获取：登录 https://agent.qcc.com 个人中心 → 获取我的 API KEY。
未配置时自动回落本地库，不影响演示。

### 如何扩充本地库
把企查查等来源的数据按 `core/schemas.py` 的 `SupplierProfile` 结构整理成 JSON，
落到 `data/public/<公司全称>.json` 即可（可参考目录下现有 7 家样本文件的字段格式）。

## 快速启动

```bash
cd app
python server.py
# 浏览器打开 http://127.0.0.1:8000/
# Windows 也可直接双击「启动.bat」
```

把涉密通道拆成独立实例（可选，仅监听回环地址，不对外暴露）：

```bash
python classified_server.py          # 127.0.0.1:8001，路由表只有 /api/upload
# Windows 双击「启动涉密通道.bat」
```

两者默认由同一个进程提供（前端零配置）；若已用独立实例，给主实例设
`CLASSIFIED_STANDALONE=1`，并把 `static/index.html` 的 `CLASSIFIED_API` 指向 8001。

停止服务：Windows 双击「停止.bat」（同时释放 8000 / 8001）。

## 配置 LLM（DeepSeek-V4.1-Flash）

```bash
# 环境变量方式（OpenAI 兼容 API）
export DEEPSEEK_API_KEY="你的key"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"   # 默认
export DEEPSEEK_MODEL="deepseek-flash"                 # 默认，可改为 deepseek-v4-pro
```

未配置 key 时自动降级（不阻断流程，演示仍可离线运行）。

## 安全边界

三条都能在代码里逐条核对：

| 通道 | 约束 | 实现位置 |
|---|---|---|
| 涉密上传 | 解析期间**禁止建立任何网络连接**（出网即抛错，fail-closed） | `security/netguard.py` → `server.py::upload` |
| 涉密上传 | 只做规则抽取，**不调用任何模型**；上传前自检密级路由，被改指向云端即拒绝服务 | `server.py::_assert_classified_route` |
| 涉密上传 | 临时文件落本机、处理完在 finally 分支删除，异常路径也删 | `server.py::upload` |
| 涉密部署 | 可独立进程/端口，仅监听 127.0.0.1，路由表只留上传一条 | `classified_server.py` |
| 公开查询 | 送往云端 LLM 前**先在本机脱敏**（公司名/金额/账号/手机号 → 占位符），返回后本机还原，映射表不落盘不过网 | `server.py::_mask_text` / `security/desensitize.py` |
| 涉密上传 | **前端云摘要守卫**：涉密通道结果页 AI 区显示固定说明（"不调用任何模型"），绝不触发任何云 LLM 调用，评分明细零外发 | `static/index.html::showResult`（`isClassified` 分支） |
| 公开查询 | 前端 WorkBuddy 云摘要（后端未配 DeepSeek key 时的降级通道）**只服务公开通道**；提示词中公司名按脱敏口径以「目标供应商」指代，不外发 | `static/index.html::fhBuildPrompt` / `ui_verify_cloud_guard.js` |
| 请求来源 | 跨域白名单由通配符收紧为显式枚举，可用 `ALLOWED_ORIGINS` 覆盖 | `server.py` |

回归验证：`python scripts/verify_security.py`（15 条断言，含"把解析器换成会联网的实现，
守卫必须拦住"的端到端取证）；前端云通道守卫另见 `scripts/ui_verify_cloud_guard.js`
（14 条断言：涉密零外发 / 公开通道脱敏 / 后端有叙述不重复调用）。

## API 接口

| 接口 | 方法 | 说明 |
|---|---|---|
| /api/query | POST | 公开公司查询（`source`: auto/qcc/public） |
| /api/upload | POST | 涉密文件上传分析（classified 通道） |
| /api/sources | GET | 查询可用数据源状态 |
| /api/report | POST | 生成 HTML 报告 |

完整 50 个路由（49 REST + 1 静态目录挂载）见技术方案书附录一。

## 目录结构

```
app/
├── server.py           # FastAPI 后端（全部 HTTP 接口 + 静态前端）
├── classified_server.py# 涉密通道独立实例（仅 /api/upload，绑定 127.0.0.1）
├── pipeline.py         # 命令行版流水线（无需 HTTP）
├── static/
│   └── index.html      # Web 单页前端（双入口 + ECharts + 导出）
├── core/
│   ├── schemas.py      # 统一数据 schema
│   ├── adapter.py      # 数据源适配器基类 + 注册中心
│   ├── aggregator.py   # 多源聚合（并发调度 + 字段级合并 + 交叉验证）
│   ├── scoring.py      # 规则打分引擎（23 条，扣分项标注监管条款）
│   ├── ahp.py          # AHP 定权（一致性检验，纯 Python）
│   ├── rating.py       # 评级引擎：加权 + 三道闸（红线/短板/信号升级）
│   ├── servicelevel.py # 服务水平指标（第25条 6 类 17 指标）
│   ├── scenarios.py    # 专属场景评价（第12条 5 场景 + 信号）
│   ├── performance.py  # 履约表现维度（折成 AHP 第六维）
│   ├── concentration.py# 关联集中度量化（第33条 HHI）
│   ├── simulator.py    # 处置策略推演沙盘（第27/31/33条 what-if）
│   ├── roster.py       # 供应商台账
│   ├── roster_import.py# 台账批量导入（Excel/Word/PDF）
│   ├── workorder.py    # 处置工单
│   ├── compliance.py   # 合规日历
│   ├── regreport.py    # 监管报送
│   ├── datasource_config.py # 数据源配置中心（清单/开关/凭据/自定义源）
│   └── report.py       # HTML 报告渲染
├── adapters/
│   ├── local_adapter.py    # 本地预采集库（工商/财务/司法）
│   ├── qcc_adapter.py      # 企查查 MCP（工商+财务+司法+股东）
│   ├── eastmoney_adapter.py# 东方财富公开接口（行情/估值）
│   ├── ifind_adapter.py    # 同花顺 iFinD（行情/财务/资讯）
│   ├── wind_adapter.py     # 万得 Wind（专业财务/行业对比）
│   ├── generic_adapter.py  # 自定义 HTTP 数据源
│   └── mock_adapter.py     # Mock 适配器（自测基线）
├── integrations/
│   ├── qcc_mcp.py      # 企查查 MCP 客户端
│   ├── qcc_cache.py    # 企查查结果缓存（两级 + 熔断）
│   ├── ifind_mcp.py    # 同花顺 iFinD MCP 客户端
│   └── wind_mcp.py     # 万得 Wind MCP 客户端
├── llm/
│   ├── client.py       # LLM 客户端适配器 + 密级路由
│   └── deepseek.py     # DeepSeek-V4.1-Flash 接入（OpenAI 兼容）
├── security/
│   ├── netguard.py     # 网络守卫：守卫生效期间任何建连调用直接抛错
│   ├── desensitize.py  # 本地脱敏：公司名/金额/账号/手机号 → 占位符
│   └── file_parser.py  # 涉密文件解析（Excel/PDF/Word）
├── scripts/            # 工程脚本 + E2E 回归
│   ├── run_all_checks.py   # ★ 一键跑测：夹具→数据快照→全部 E2E→数据还原→产物清理
│   ├── verify_security.py  # ★ 安全边界回归（15 条断言）
│   ├── make_upload_fixtures.py  # 涉密上传解析夹具（含布局回归自检）
│   └── ui_verify_*.js      # 各功能页 E2E 回归（截图统一落 _shots/）
└── data/
    ├── public/         # 公开公司样本数据
    ├── mock/           # 模拟数据
    ├── source_config.json  # 数据源清单/开关（凭据请填 .env，勿写这里）
    └── *.json          # 台账、工单、服务水平等业务数据
```

## 关键设计

- **统一 schema**：所有数据源（公开/涉密）最终都落到同一 `SupplierProfile`，下游评级引擎零改动。
- **适配器模式**：换数据源 = 换适配器类，上层不变。
- **规则打分**：23 条规则，每条扣分项标注《办法》条款，可追溯。
- **AHP 定权**：纯 Python，CR = 0.0199 < 0.1 通过一致性检验；36 次权重扰动测试下等级零翻转。
- **三道闸**：红线熔断（被执行/审计无法表示意见等一票否决）、短板约束（最低维度 <35 封顶为红）、
  信号升级（履约连续未达标 ≥3 项 / 悬崖式下滑 / 预测 <70 且下行，等级至少抬到黄）。
- **涉密不出网**：涉密通道不调用任何模型，解析入口套网络守卫，详见上文「安全边界」。
- **脱敏归属**：脱敏服务的是**公开查询通道**（上云前脱敏、返回后本机还原）；
  涉密通道不出网，不经过这一步。

## 关于 scripts/ 里的打包脚本

`make_deploy_dir.py` / `make_server_bundle.py` / `push_to_github.py` / `deploy_to_server.py`
是**项目树内**使用的构建与部署脚本，按 `app/` 与 `deploy/` 的目录关系定位路径。
单独拷出本目录后，它们缺少 `deploy/` 同级目录，无法直接运行——这是设计如此，
不影响系统本身运行（启动只需 `python server.py`）。

## 下一步（待接入）

接真实数据源时，照 `mock_adapter.py` 的模板写类即可，例如：

```python
@register("qichacha")
class QichachaAdapter(DataSourceAdapter):
    def fetch_supplier(self, name):
        raw = qichacha_api.query(name)   # 真实 API
        return to_supplier_profile(raw)  # 翻译成统一 schema
```
