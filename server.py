"""FastAPI 后端 —— 提供两个入口，对应两条数据安全通道。

- POST /api/query   公开公司查询（public 通道，走云端/预置样本）
- POST /api/upload  涉密文件分析（classified 通道，本机解析 + 纯规则评分，不出网）

两个接口共用同一套评级流水线，仅数据入口和密级不同。

安全边界（可验证，非口头承诺）：
- 涉密通道密级由本文件的路由自身固定为 classified，**不接受任何来自请求的密级覆盖**；
  并在每次上传前自检密级路由，确保不会被改坏而落到云端模型（见 _assert_classified_route）。
- 涉密解析入口套用 `security.netguard.no_network()`：解析期间任何建连调用直接抛错，
  把「不出网」从"这条路径恰好没写网络调用"变成"这条路径联网即失败"。
- **公网通道出网前先本地脱敏**：送进云端 LLM 的文本已把公司名/金额/账号/手机号
  换成占位符，模型返回后再在本机还原（见 _mask_strings / _restore_text）。
- 临时文件落本机磁盘，处理完在 finally 分支删除，异常路径也删。
- 跨域白名单默认只放本机；线上如需其他来源，用环境变量 ALLOWED_ORIGINS 显式追加。

若需把涉密通道**独立部署**（独立进程 + 独立端口 + 仅监听 127.0.0.1），
用 `python classified_server.py` 启动涉密实例，并给本实例设 CLASSIFIED_STANDALONE=1。
"""
from core.quiet_log import log_swallow as _log_swallow
import os
import re
import sys
import tempfile
from datetime import datetime
from datetime import date as _date
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 加载 .env 配置（简易实现，零依赖）
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# 网页配置的数据源凭据回写环境变量（优先级：网页配置 > .env > 默认值）
from core import datasource_config as _dsc  # noqa: E402
_dsc.apply_env_on_startup()

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core.adapter import get_adapter
from core.rating import rate_supplier, level_to_action
from security.file_parser import (parse_supplier_file, merge_profiles,
                                  profile_contributions, profile_diagnostics)
from security.desensitize import desensitize
from security.netguard import no_network, NetworkBlockedError
from security import auth as auth_mod
from core.report import render_report
from adapters.qcc_adapter import MultiCandidateError
from core.scoring import dimension_risk_note
from core import aggregator


def _parse_with_guard(path: str):
    """在工作线程内进入网络守卫并解析文件（守卫是线程本地的，见 security/netguard.py）。"""
    with no_network():
        return parse_supplier_file(path)


# import 适配器模块触发注册
from adapters import (  # noqa: F401
    mock_adapter, local_adapter, qcc_adapter, eastmoney_adapter,
    ifind_adapter, wind_adapter, generic_adapter,
)

app = FastAPI(title="烽火 · 信息科技外包风险智能预警系统")

# 跨域白名单。前端与后端**同源**（页面里的 API 地址是空串，走相对路径），
# 所以正常使用完全不依赖 CORS；这里只为本地调试与「涉密通道独立实例」跨端口调用放行。
# 从前这里是 allow_origins=["*"]——任一网站都能带着用户浏览器去打这套接口，
# 对一套要处理行内敏感材料的系统来说没有必要。现改为显式枚举，且可被环境变量覆盖：
#   ALLOWED_ORIGINS="https://your.domain,https://other.domain"
_DEFAULT_ORIGINS = [
    "http://127.0.0.1:8000", "http://localhost:8000",     # 公开查询实例
    "http://127.0.0.1:8001", "http://localhost:8001",     # 涉密通道独立实例
]
_allowed_origins = [o.strip() for o in
                    os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or _DEFAULT_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Operator", "X-Write-Token"],
)

# 挂载静态文件（前端页面）
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---- 写接口总闸（按来源分级：本机免密，跨主机必须令牌）----
# 背景：本服务没有登录体系，任何能访问到端口的人都能删台账、删工单、改数据源密钥。
# 策略（见 security/auth.py 的说明）：
#   * 本机 127.0.0.1/::1 —— 单机演示形态，写操作放行，操作人记「本机操作员」；
#   * 跨主机 —— 必须带 `Authorization: Bearer $FENGHUO_API_TOKEN`；未配置 token 时
#     **拒绝**（fail-closed），不允许"没配就等于全开"。
# 另保留运维用的只读总闸：设 `FENGHUO_READONLY_WRITES=1` 即整个站点只读（GET 全放行）。
# 写操作的请求一律记 request.state.identity，供处置类接口落盘留痕。
_WRITE_PREFIXES = (
    "/api/roster", "/api/workorders", "/api/alerts/state", "/api/qcc/cache",
    "/api/servicelevel", "/api/contract/register", "/api/outsourcing/special",
    "/api/sources", "/api/scan", "/api/upload",
)


@app.middleware("http")
async def _write_guard(request: Request, call_next):
    is_write = request.method not in ("GET", "HEAD", "OPTIONS")
    touches = _touches_writes(request.url.path)
    ident = auth_mod.identity(request)
    if is_write and touches:
        ok, status, detail, ident = auth_mod.authorize_write(request)
        if not ok:
            return JSONResponse(status_code=status, content={"detail": detail})
    request.state.identity = ident

    if (is_write and touches
            and os.environ.get("FENGHUO_READONLY_WRITES", "").strip() in ("1", "true", "yes")):
        token = os.environ.get("FENGHUO_WRITE_TOKEN", "").strip()
        if not token or request.headers.get("x-write-token", "") != token:
            return JSONResponse(status_code=403, content={
                "detail": "当前为只读演示模式，写操作已禁用。如需变更请联系管理员。"})
    return await call_next(request)


def _touches_writes(path: str) -> bool:
    """路径是否属于会改状态的前缀（/api/sources 的 GET 只读，但前缀相同）。"""
    return path.startswith(_WRITE_PREFIXES)


class QueryRequest(BaseModel):
    name: str
    source: str = "auto"   # auto / qcc（企查查实时）/ public（本地库+东财）
    key: str = ""          # 多候选时用户选定主体的统一社会信用代码（可选）
    refresh: bool = False  # 是否绕过企查查缓存强制刷新（默认用缓存，省积分）


def _match_roster_id(name: str) -> str:
    """把查询主体名称匹配到台账 ID（用于关联第25条服务水平数据）。

    匹配失败返回空串 —— 此时履约表现维度会标记为 na，不影响其余五维。
    """
    try:
        from core import servicelevel as sl
        return sl.find_supplier_id_by_name(name)
    except Exception:
        return ""


def _assert_roster_id(sid: str, field: str = "供应商 ID") -> None:
    """写入类接口的台账校验：ID 必须已在供应商台账中，否则 404。

    为什么必须校验：服务水平（第25条）与合同条款（第18/19条）都是**台账主体的附属数据**，
    孤立 ID 写入后不会出现在任何总览里（build_overview 按台账遍历），成为永久垃圾。
    历史缺陷：POST /api/servicelevel 曾接受 S999 并落盘，总览看不到、删除又需知道 ID 才能清。
    """
    from core import roster as roster_mod
    try:
        items = roster_mod.load_roster()
    except Exception:
        return                      # 台账读失败不阻断写入（避免误伤正常业务）
    ids = {str(it.get("id") or "") for it in items}
    if sid not in ids:
        raise HTTPException(
            status_code=404,
            detail=f"台账中不存在{field}「{sid}」，请先在供应商台账中登记该主体。",
        )


def _to_result(profile, rating, verified=None, failed=None):
    """把评级结果转成前端友好的结构（含完整评分细则与多源标注）。"""
    verified = verified or {}
    failed = failed or {}

    # 各维度：附带权重、风险提示语、完整检查清单
    # 维度 → 关联的验证字段（用于标注"本维度数据是否多源交叉验证"）
    # 注意：履约表现维度不来自外部数据源，故不在 DIM_FIELD 中（verified 为 None）
    DIM_FIELD = {
        "legal": "legal_events",
        "operating": "registration",
        "financial": "financials",
        "reputation": "news",
        "concentration": "shareholders",
    }
    dims = {}
    for dim, d in rating.dimension_scores.items():
        vkey = DIM_FIELD.get(dim)
        dims[dim] = {
            "label": d.get("label") or _DIM_LABELS.get(dim, dim),
            "score": d["score"],
            "deducted": d["deducted"],
            "weight": d.get("weight", 0),
            "raw_weight": d.get("raw_weight", d.get("weight", 0)),
            "kind": d.get("kind", "base"),
            "na": bool(d.get("na")),
            "verified": verified.get(vkey) if vkey else None,
            "note": d.get("note") or dimension_risk_note(dim, d["score"] or 0),
            # 命中的规则（前端"扣分明细"用）
            "rules": [
                {"name": t["name"], "clause": t["clause"], "deduct": t["deduct"],
                 "metric": t["metric"], "threshold": t["threshold"],
                 "actual": t["actual"], "hint": t["hint"], "basis": t["basis"]}
                for t in d["triggered"]
            ],
            # 全部检查项（前端"完整评分细则"用：含未命中项）
            "checks": d.get("checks", []),
        }

    # 数据源元信息
    sources = []
    for k in profile.sources:
        meta = profile.source_meta.get(k)
        if meta:
            sources.append({
                "key": meta.key, "label": meta.label, "category": meta.category,
                "authoritative": meta.authoritative,
            })
        else:
            sources.append({"key": k, "label": k, "category": "", "authoritative": False})

    return {
        "supplier": rating.supplier,
        "important_outsourcing": profile.important_outsourcing,
        "non_resident": profile.non_resident,
        "cross_border": profile.cross_border,
        "total_score": rating.total_score,
        "level": rating.level,
        "action": level_to_action(rating.level),
        "red_lines": rating.red_lines,
        "constraints": getattr(rating, "constraints", None) or [],
        # 口径透明：缺维度的企业有效权重不同，只能在同覆盖组内横比分数
        "coverage": getattr(rating, "coverage", None) or {},
        # 不含履约（模拟数据）维度的对照分
        "baseline_score": getattr(rating, "baseline_score", None),
        "performance_origin": (getattr(rating, "data_origin", None) or {}).get("performance", ""),
        "weights": rating.weights,
        "raw_weights": rating.raw_weights,
        "cr": rating.cr,
        "dim_labels": rating.dimension_labels,
        "dim_kind": rating.dim_kind,
        "na_dimensions": rating.na_dimensions,
        "performance": rating.performance,
        "dimensions": dims,
        "legal_events": [
            {"title": e.title, "severity": e.severity, "amount": e.amount,
             "occurred_at": e.occurred_at, "source": e.source, "evidence": e.evidence,
             "dimension": e.dimension, "count": e.count}
            for e in profile.legal_events
        ],
        "news": [
            {"title": n.title, "sentiment": n.sentiment, "severity": n.severity,
             "published_at": n.published_at, "source": n.source}
            for n in profile.news
        ],
        "financials": profile.financials.__dict__ if profile.financials else None,
        "market": profile.market.__dict__ if profile.market else None,
        "shareholders": [
            {"name": s.name, "ratio": s.ratio, "amount": s.amount, "type": s.type}
            for s in profile.shareholders
        ],
        "registration": profile.registration,
        "ai_analysis": profile.ai_analysis,
        "risk_alerts": profile.risk_alerts,
        "sources": sources,
        "verified": verified,
        "failed_sources": failed,
    }


_DIM_LABELS = {
    "legal": "法律风险",
    "operating": "经营风险",
    "financial": "财务风险",
    "reputation": "舆情风险",
    "concentration": "关联与集中度风险",
}


@app.get("/")
def root():
    """返回前端页面（禁用缓存，确保前端改动即时生效）。

    ⚠️ 部署形态与令牌注入（2026-10-21 增加）：
      本服务有两类部署形态——
        · 单机演示（本机直连 127.0.0.1）——写操作免密，页面不需要任何令牌；
        · 公网部署（nginx 反代）——写操作要求 `Authorization: Bearer <令牌>`。
      公网形态下前端若不携带令牌，「录入服务水平」「转处置工单」等按钮会直接 403，
      演示当场卡死。故设计成**按部署意图决定是否注入**，而不是把令牌硬编码进
      static/index.html（那会让令牌随源码与交付包外流）。

      注入规则：
        · 设了 FENGHUO_FRONTEND_TOKEN → 注入它。这是"公网可演示写入"形态：
          任何访客打开的页面都带令牌，按钮可用（等价于把写权限公开，仅适用于
          演示/内网环境，务必知悉其含义）。
        · 未设该变量（默认）→ 不注入，页面原样返回。此时公网访客的写操作
          一律 403，符合"公网只读"；本机访问仍走免密通道，不受影响。
    """
    from fastapi.responses import FileResponse, HTMLResponse

    path = os.path.join(STATIC_DIR, "index.html")
    token = (os.environ.get("FENGHUO_FRONTEND_TOKEN") or "").strip()
    if not token or token in ("", " ", "changeme"):
        return FileResponse(path,
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    # 注入点选 </head> 前：确保早于所有业务脚本执行，
    # 供 fetchWithTimeout 读取（见 static/index.html 的 FH_API_TOKEN 读取处）。
    # 用 JSON 编码而非字符串拼接，避免令牌含引号/反斜杠时破脚本。
    import json as _json
    snippet = ("<script>window.FH_API_TOKEN = %s;</script>\n</head>"
               % _json.dumps(token))
    html = html.replace("</head>", snippet, 1)
    return HTMLResponse(content=html,
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/favicon.ico")
def favicon():
    """浏览器默认请求 /favicon.ico，直接返回 204 避免 404 噪音。"""
    from fastapi import Response
    return Response(status_code=204)


def _fetch_profile(name: str, source: str = "auto", key: str = "", refresh: bool = False):
    """按数据源策略获取供应商档案（多源聚合）。

    auto   → 并发调用全部可用数据源并交叉验证（推荐）
    qcc    → 仅企查查实时（含真实司法风险）
    public → 仅本地预采集库 + 东方财富免费源
    all    → 同 auto，但显式要求全部源

    key     → 多候选时用户选定的主体（统一社会信用代码），直取该主体
    refresh → 绕过企查查缓存强制拉取（默认 False：优先命中缓存，省积分）
    """
    # 用户已明确选定主体 → 直接按 key 抓取，不再走模糊检索
    if key:
        from adapters.qcc_adapter import QCComAdapter
        profile = QCComAdapter().fetch_supplier_by_key(key, fallback_name=name, refresh=refresh)
        return aggregator.MultiSourceResult(
            profile=profile, used=["qcc"], failed={},
            verified={"registration": False, "financials": False,
                      "legal_events": False, "shareholders": False, "news": False},
        )

    if source == "qcc":
        wanted = ["qcc"]
    elif source == "public":
        wanted = ["local", "eastmoney"]
    else:  # auto / all
        wanted = aggregator.auto_sources()

    res = aggregator.aggregate(name, sources=wanted, refresh=refresh)
    res.profile.registration = res.profile.registration or {}
    res.profile.registration.setdefault("_adapter", ",".join(res.used))
    return res


def _has_subject_data(profile) -> bool:
    """判断 profile 是否有任何「主体真实存在」的证据。

    背景：iFinD 等适配器对不存在/非上市主体会"优雅返回空"而非抛错，
    若所有权威工商源（企查查/本地库）都未找到，仅剩空 profile 参与评分，
    会导致"查不到的公司反而满分"（各维度无扣分项 = 100 分）。
    故在此做主体锚定检查：profile 完全为空 → 视为"未找到该供应商"。

    ★ 刻意排除 news：新闻搜索对不存在的公司名可能返回**无关公司的新闻**
      （实测查询不存在的名称返回了"华天酒店"公告），不能作为主体存在的证据。
    """
    reg = profile.registration or {}
    return bool(
        reg.get("name") or reg.get("unified_code") or reg.get("credit_code")
        or profile.financials
        or profile.legal_events
        or profile.shareholders
        or profile.market
    )


def _restore_text(text: str, mapping: Dict[str, str]) -> str:
    """本地还原：占位符换回原文。**按长度倒序替换**。

    为什么强调顺序：`<M_COMPANY1>` 是 `<M_COMPANY10>` 的前缀，
    若短的先替换，会把长的拆坏（`<M_COMPANY10>` → `<原文>0>`）。
    """
    out = text if isinstance(text, str) else ""
    for ph in sorted(mapping, key=len, reverse=True):
        out = out.replace(ph, mapping[ph])
    return out


def _mask_terms(text: str, terms, mapping: Dict[str, str], _seq=None) -> str:
    """把一批**已知词汇**（主体全称与简称）按字面遮蔽，长词优先。

    为什么正则之外还需要这一步：`desensitize()` 的公司名正则只认带后缀的全称
    （…有限公司 / 银行 / 集团），新闻标题里常见的简称会原样漏出 ——
    实测"深圳市长亮科技股份有限公司被列为被执行人"里的全称被遮蔽了，
    紧跟着的"长亮科技因合同纠纷被诉"却带着简称出网。
    主体名在本次请求里是**已知**的，按字面遮蔽即可，不需要上 NER。

    长度门槛 3 字：过短的词容易误伤正文（比如把通用词遮蔽掉反而干扰模型判断）。
    """
    seq = _seq if _seq is not None else [0]
    out = text or ""
    for t in sorted({x for x in (terms or []) if x and len(x) >= 3},
                    key=len, reverse=True):
        if t in out:
            seq[0] += 1
            uniq = "<M%d_ALIAS>" % seq[0]
            out = out.replace(t, uniq)
            mapping[uniq] = t
    return out


_COMPANY_SUFFIX_RE = re.compile(r"(?:股份有限公司|有限责任公司|有限公司|集团有限公司|集团|公司)$")
_REGION_PREFIX_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}(?:省|市|自治区|特别行政区)")


def _subject_aliases(name: str) -> List[str]:
    """主体全称 + 常见简称（去公司后缀、去地区前缀），供本地脱敏遮蔽。

    例：「深圳市长亮科技股份有限公司」→ 全称 / 「深圳市长亮科技」/「长亮科技」。
    最后一档才是新闻里真正会出现的写法。
    """
    n = (name or "").strip()
    if not n:
        return []
    out = [n]
    core = _COMPANY_SUFFIX_RE.sub("", n)
    if core and core != n:
        out.append(core)
        short = _REGION_PREFIX_RE.sub("", core)
        if short:
            out.append(short)
    return out


def _mask_text(text: str, mapping: Dict[str, str], _seq=None) -> str:
    """脱敏一段文本，占位符统一成 `<M{序号}_{类型}>` 并登记进 mapping。

    为什么不让调用方直接用 `desensitize()`：它的计数是**每次调用重置**的 ——
    两次调用各产出一个 `<COMPANY1>`，却指向不同公司。若两处结果放进同一个 mapping，
    还原时就会张冠李戴（这个 bug 实测踩到过，靠 E1 的往返一致性断言抓出来）。
    这里统一加全局序号，形如 `<M7_COMPANY>`，彼此不互为前缀，替换与还原都不会误伤；
    顺带让"指标"和"新闻"两条出网路径交给模型的是同一种占位符形状。
    """
    seq = _seq if _seq is not None else [0]
    masked, local = desensitize(text or "")
    for ph in sorted(local, key=len, reverse=True):   # 长占位符先换，防前缀误吃
        seq[0] += 1
        kind = ph.strip("<>").rstrip("0123456789") or "VAL"
        uniq = "<M%d_%s>" % (seq[0], kind)
        masked = masked.replace(ph, uniq)
        mapping[uniq] = local[ph]
    return masked


def _mask_strings(obj, mapping: Dict[str, str], _seq=None):
    """递归脱敏 dict / list 里的**字符串值**（财务指标走这条）。

    为什么不用「序列化成 JSON 再整体脱敏」：
    脱敏会把长数字串换成占位符，若这个数字恰好是一个 JSON 数值
    （`{"total_assets": 1234567890123456}`），整段 JSON 就废了。逐字段处理，
    数值字段原样保留，只动字符串。
    """
    seq = _seq if _seq is not None else [0]
    if isinstance(obj, str):
        return _mask_text(obj, mapping, seq)
    if isinstance(obj, dict):
        return {k: _mask_strings(v, mapping, seq) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_strings(v, mapping, seq) for v in obj]
    return obj


@app.post("/api/query")
def query(req: QueryRequest):
    """公开公司查询：多源聚合 + LLM 智能分析（出网前本地脱敏）。"""
    try:
        res = _fetch_profile(req.name, req.source, req.key, refresh=req.refresh)
    except MultiCandidateError as e:
        # ★ 按相关性排序：名称含查询词的排前，不含（推荐/弱相关）排后
        cands = e.candidates or []
        q = (req.name or "").strip()
        def _rel(c):
            n = c.get("name") or ""
            if not q: return (3, 999, n)
            if n == q: return (0, 0, n)         # 完全相同
            if n.startswith(q): return (1, 0, n)  # 前缀匹配
            if q in n: return (2, n.index(q), n)  # 中间包含（按出现位置）
            return (3, 999, n)                     # 不含（企查查的"推荐"等弱相关）
        cands = sorted(cands, key=_rel)
        return JSONResponse(status_code=300, content={
            "detail": str(e),
            "candidates": cands,
            "type": "multi_candidate",
        })
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    profile = res.profile

    # 主体锚定检查：无任何有效数据 → 视为未找到（避免"查不到的公司反而满分"）
    if not _has_subject_data(profile):
        raise HTTPException(
            status_code=404,
            detail="未找到该供应商的工商、财务或司法等有效信息，请确认公司全称后重试。",
        )

    # LLM 增强：财务指标 AI 分析 + 新闻事件智能抽取。
    # 走云端（public 密级），可失败降级 —— 但**上云前必过本地脱敏**：
    # 公司名 / 金额 / 账号 / 手机号在本机被替换成占位符，云端只看得见 <M_COMPANY1> 这类标记；
    # 模型返回后再在本机还原。脱敏表与还原都不过网（mapping 只活在本次请求的内存里）。
    try:
        from llm.client import get_llm_client

        llm = get_llm_client("public")

        if profile.financials:
            # 指标本身只含数字与报告期，结构上就没有主体标识；仍统一过一遍脱敏，
            # 让「上云前必过本地脱敏」成为一条没有例外的规则，也给日后新增字段留好护栏。
            metrics_map: Dict[str, str] = {}
            payload = _mask_strings(profile.financials.__dict__, metrics_map)
            profile.ai_analysis = _restore_text(llm.analyze_metrics(payload), metrics_map)

        news_text = "；".join(n.title for n in profile.news if n.sentiment == "negative")
        if news_text:
            # 新闻标题是真正的标识载体（含公司名与涉事金额），必须脱敏后再出网。
            # 先过实体正则，再按主体全称/简称做字面遮蔽 —— 正则认不出"长亮科技"这类简称。
            news_map: Dict[str, str] = {}
            masked_news = _mask_text(news_text, news_map)
            masked_news = _mask_terms(masked_news, _subject_aliases(profile.name), news_map)
            extracted = llm.extract_events(masked_news)
            from core.schemas import LegalEvent
            for ev in extracted:
                if ev.get("event_type") == "legal":
                    profile.legal_events.append(LegalEvent(
                        title=_restore_text(ev.get("title", ""), news_map),
                        severity=ev.get("severity", "medium"),
                        amount=_restore_text(ev.get("amount") or "", news_map) or None,
                        occurred_at=ev.get("occurred_at"),
                        source="LLM抽取",
                        evidence=_restore_text(ev.get("evidence", "") or "", news_map),
                    ))
    except Exception:
        _log_swallow("server.py:499")

    rating = rate_supplier(profile, supplier_id=_match_roster_id(req.name))
    result = _to_result(profile, rating, res.verified, res.failed)
    # 凭据缺口透明化：auto 模式下"启用了但没配凭据"的源如实带出，
    # 让"分数只用了本地库"在界面上可解释（而不是静默降级让用户猜）。
    if req.source in ("auto", "all"):
        try:
            from core import datasource_config as dsc
            result["credential_gaps"] = dsc.unavailable_reasons()
        except Exception:
            _log_swallow("server.py:credential_gaps")
    # 名称不完整提示：本地库按"简称/包含"模糊匹配到唯一主体时，提示用户输入完整名称
    fuzzy_full = (profile.registration or {}).get("_fuzzy_matched")
    if fuzzy_full:
        result["name_hint"] = (
            f"您输入的名称不完整，系统已自动匹配到「{fuzzy_full}」。"
            f"若需查询其他主体，请输入完整公司名称。"
        )
    return {"channel": "public",
            "result": result}


# 涉密通道密级。**由本路由自身固定**，不接受任何来自请求的密级覆盖：
# 需求方无法通过多传一个字段把"涉密"降级成"公开"，因为这里根本没有读取密级入参。
_UPLOAD_CHANNEL_LEVEL = "classified"

# 上传体积上限（字节）。原实现 `await file.read()` 会把整个文件一次性读进内存，
# 无上限时超大文件可致内存激增（DoS）。25 MB 足以覆盖财务表/司法清单/尽调报告，
# 可通过环境变量 MAX_UPLOAD_MB 调整。
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "25"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
# 单次上传的文件数上限：只限体积不限个数时，1000 个小文件同样能把 CPU/磁盘打满。
MAX_UPLOAD_FILES = int(os.environ.get("MAX_UPLOAD_FILES", "10"))


async def _write_upload_to(file: UploadFile, tmp) -> None:
    """分块写入临时文件，超限即中止并抛 ValueError。

    Starlette 的 UploadFile 基于 SpooledTemporaryFile，读取时已部分落盘，
    但直接 `read()` 仍会把全部内容物化到内存。改为分块 + 累计计数。
    """
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise ValueError(f"文件超过 {MAX_UPLOAD_MB} MB 上限，请拆分后上传")
        tmp.write(chunk)


def _assert_classified_route() -> None:
    """密级自检：涉密通道必须路由到非云端实现，否则拒绝服务。

    这是「不调云端模型」的第二道保险。密级路由表（llm/client.py）是可能被改动的，
    一旦有人把 classified 指向 CloudLLM，敏感文本就会被送到公网——届时不会有任何报错。
    因此在每次上传前做一次廉价的断言：宁可让接口 500，也不静默外发。
    """
    from llm.client import get_llm_client, CloudLLM
    client = get_llm_client(_UPLOAD_CHANNEL_LEVEL)
    if isinstance(client, CloudLLM):
        raise HTTPException(
            status_code=500,
            detail="安全自检未通过：classified 密级被路由到云端模型，已拒绝本次上传。",
        )


@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...),
                 supplier_id: str = Form("")):
    """涉密文件分析（单次上传一家供应商的多个文件，合并分析）：本机解析 + 纯规则评分，不出网。

    语义约定：**一次上传的多个文件视为同一家供应商的多份资料**（如财务表 + 司法清单 +
    尽调报告），后端会把这些文件的字段合并成一份完整档案，再做**一次**统一评分。

    安全承诺（每条都可在代码里核对）：
    1. 密级由本路由固定为 classified，不读任何密级入参，无法被调用方降级；
    2. 解析入口套 `no_network()` 网络守卫，解析期间任何建连调用直接抛错（fail-closed）；
    3. 只做规则抽取，**不调用任何模型**——本地模型也没有，规避"本地模型其实没接"的隐患；
    4. 文件只落本机临时目录，处理完在 finally 分支删除，异常路径也删；
    5. 若要对外隔离，用 classified_server.py 起独立实例，仅监听 127.0.0.1。

    评分原则（strict_na）：合并后仍无任何内容的维度 → 显示"无信息"（na），
    不参与加权，绝不因"没数据"而默认满分。

    supplier_id（可选）：关联台账主体 ID。涉密档案的服务水平（第25条）数据来自台账，
    文档本身无法携带；指定后可让「履约表现」维度并入评分，使六维完整。
    """
    _assert_classified_route()

    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400,
                            detail=f"一次最多上传 {MAX_UPLOAD_FILES} 个文件，请分批处理")

    profiles = []
    file_items = []
    for file in files:
        # 浏览器/测试框架可能把完整路径塞进 filename，统一只取文件名
        item = {"filename": os.path.basename(file.filename or "未命名文件")}
        suffix = os.path.splitext(file.filename or "")[1].lower()
        if suffix not in (".xlsx", ".xls", ".pdf", ".docx", ".doc"):
            item.update(ok=False, error=f"不支持的格式 {suffix}，支持 Excel/PDF/Word")
            file_items.append(item)
            continue

        # 本地临时保存（安全：仅本地磁盘，处理后删除）
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            await _write_upload_to(file, tmp)
            tmp.close()
            # 涉密硬约束：解析期间禁止建立任何网络连接（守卫内出网即抛错）
            # 解析是纯 CPU/IO 的重活（PDF/Excel 可跑几十秒），必须放到线程池里跑，
            # 否则会占住事件循环、把整个站点的其它请求一起卡死。
            #
            # ⚠️ 守卫必须进入**工作线程内部**（见 _parse_with_guard）：网络守卫是
            #    线程本地的，套在 await run_in_threadpool(...) 外层只保护事件循环线程，
            #    解析线程实际未被约束；反过来若做成进程级开关，一个大文件上传会把
            #    并发的公开查询一起掐断。
            profile = await run_in_threadpool(_parse_with_guard, tmp.name)
            profiles.append(profile)
            item.update(ok=True, fields=profile_contributions(profile),
                        diag=profile_diagnostics(profile))
        except NetworkBlockedError as e:
            # 解析路径试图出网 —— 这属于安全事件，不能当成普通解析失败吞掉
            item.update(ok=False, error=f"安全守卫拦截：{e}")
        except ValueError as e:
            # 解析/评分的业务性错误（如"未识别到任何可评估内容"）
            item.update(ok=False, error=str(e))
        except Exception as e:
            item.update(ok=False, error=f"文件解析失败: {e}")
        finally:
            try:
                os.unlink(tmp.name)  # 处理完立即删除，不留痕
            except OSError:
                _log_swallow("server.py:623")
        file_items.append(item)

    # 合并所有成功解析的文件 → 一次统一评分
    merged_result = None
    supplier_name = None
    linked = None
    if profiles:
        try:
            merged = merge_profiles(profiles)
            # 关联台账主体：指定 supplier_id 时取其服务水平数据，令履约维度并入评分
            sid = (supplier_id or "").strip()
            if not sid:
                sid = _match_roster_id(merged.name) or ""
            rating = rate_supplier(merged, supplier_id=sid,
                                   strict_na=True)  # 无数据维度 → 无信息，不臆造分数
            merged_result = _to_result(merged, rating)
            supplier_name = merged.name
            if sid and rating.performance.get("score") is not None:
                linked = {
                    "id": sid,
                    "score": rating.performance.get("score"),
                    "grade": rating.performance.get("grade"),
                    "latest": rating.performance.get("latest"),
                    "avg": rating.performance.get("avg"),
                    "forecast": rating.performance.get("forecast"),
                    "summary": rating.performance.get("summary"),
                }
        except ValueError:
            # 理论上合并后不应为空，兜底避免整个接口失败
            merged_result = None

    return {"channel": "classified",
            "count": len(file_items),
            "ok_count": len(profiles),
            "supplier_name": supplier_name,
            "linked_roster": linked,     # 关联台账主体信息（履约维度来源），未关联为 null
            "merged": merged_result,
            "files": file_items}


# ---- 数据源配置改动：仅限本机（公网只读） ----
# 背景：站点在公网（nginx 反代），而 /api/sources/config、/api/sources/custom* 可直接
# 替换企查查/iFinD/Wind/DeepSeek 的密钥、增删数据源 —— 若不限制，任何访客都能改。
# 判别：nginx 已设 X-Real-IP=$remote_addr（覆盖客户端伪造值，最可信）；
#       X-Forwarded-For 用 $proxy_add_x_forwarded_for（真实地址追加在【尾部】，
#       客户端伪造值只会出现在前部，故取最后一跳）；本机直连（curl/E2E）无头，回落 client.host。
def _client_ip(request: Request) -> str:
    # 实现与策略集中在 security/auth.py（单一事实来源，避免两处漂移）。
    return auth_mod.client_ip(request)


def _is_local_request(request: Request) -> bool:
    return auth_mod.is_local_request(request)


def _require_local_ip(request: Request) -> None:
    if not _is_local_request(request):
        raise HTTPException(
            status_code=403,
            detail="数据源配置仅限本机（127.0.0.1）修改，公网访问为只读。如需更换密钥，请登录服务器在本机操作。",
        )


@app.get("/api/sources")
def sources():
    """返回可用数据源状态（供前端展示当前查询走哪条通道）。"""
    from core import datasource_config as dsc
    catalog = aggregator.catalog()
    active = dsc.available_source_keys()
    qcc_on = "qcc" in active
    return {
        "qcc": {
            "available": qcc_on,
            "label": "企查查实时数据",
            "desc": "真实工商 + 年报财务 + 司法风险扫描 + 股东穿透" if qcc_on
                    else "未配置 Token（请在「数据源管理」配置企查查 Token）",
        },
        "public": {
            "available": "local" in active or "eastmoney" in active,
            "label": "本地预采集库 + 东方财富",
            "desc": "7 家银行 IT 外包公司完整档案 + 沪深 A 股免费实时行情",
        },
        "default": "auto",
        "catalog": catalog,
    }


# ============================ 数据源管理（配置中心 + 自定义源） ============================

class SourceConfigUpdate(BaseModel):
    key: str
    enabled: Optional[bool] = None
    credential: Optional[Dict[str, str]] = None


class CustomSourceCreate(BaseModel):
    label: str = ""
    url: str
    token: str = ""
    enabled: bool = True


class CustomSourceUpdate(BaseModel):
    enabled: Optional[bool] = None
    label: Optional[str] = None
    url: Optional[str] = None
    token: Optional[str] = None


@app.get("/api/sources/config")
def sources_config(request: Request):
    """数据源配置清单（凭据脱敏），供「数据源管理」界面渲染。"""
    from core import datasource_config as dsc
    out = dict(dsc.list_all())
    out["mutable"] = _is_local_request(request)
    return out


@app.post("/api/sources/config")
def update_source_config(req: SourceConfigUpdate, request: Request):
    _require_local_ip(request)
    """更新内置数据源的开关 / 凭据（写盘 + 回写环境变量即时生效）。"""
    from core import datasource_config as dsc
    try:
        state = dsc.update_source(req.key, enabled=req.enabled, credential=req.credential)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # ⚠️ 凭据脱敏：update_source 返回的 state.credential 里是**明文密钥**，
    #    原样回显会把真实 key 泄给调用方（该接口曾无本机限制，风险更大）。
    #    前端只消费 ok，凭据状态走 GET /api/sources/config 的 has_value 脱敏口径。
    pub = dict(state or {})
    if pub.get("credential"):
        pub["credential"] = {k: bool(v) for k, v in pub["credential"].items()}
    return {"ok": True, "state": pub, "catalog": aggregator.catalog()}


@app.post("/api/sources/test/{key}")
def test_source_connection(key: str):
    """连通性自检：验证某个已配置能力当前是否真的可用。

    - LLM（kind=llm）：发一条极小的真实请求，确认密钥/地址/模型可用
    - 数据源：检查开关 + 凭据 + 适配器可用性（不发外部请求，避免消耗积分）
    演示价值：让"已启用"不只是一句配置，而是当场可验证的事实。
    """
    from core import datasource_config as dsc
    if key not in dsc.BUILTIN_SOURCES:
        raise HTTPException(status_code=404, detail=f"未知能力：{key}")
    meta = dsc.BUILTIN_SOURCES[key]
    st = dsc.get_source_state(key)

    if not st["enabled"]:
        return {"ok": False, "key": key, "status": "disabled",
                "message": "该能力当前处于停用状态，请先打开开关。"}
    if not dsc._has_credential(meta, st):
        return {"ok": False, "key": key, "status": "no_credential",
                "message": "尚未配置凭据，请填写后保存。"}

    if meta.get("kind") == "llm":
        try:
            import time as _t
            from llm.deepseek import DeepseekClient, display_name
            cred = st["credential"]
            client = DeepseekClient(
                api_key=cred.get("api_key"),
                base_url=cred.get("base_url"),
                model=cred.get("model"),
            )
            # 未配置（或仍是占位符）→ 明确返回 not_configured，而不是拿假 key 真调一次再报
            # "调用失败：AuthenticationError"（那样既浪费一次往返，也让人误以为是网络故障）
            if not client.api_key:
                return {"ok": False, "key": key, "status": "not_configured",
                        "message": ("未配置 DEEPSEEK_API_KEY（占位符不算配置）："
                                    "AI 分析会自动降级，不影响规则评分。")}
            t0 = _t.time()
            reply = client._chat([{"role": "user", "content": "回复两个字：可用"}])
            cost = round(_t.time() - t0, 2)
            shown = display_name(client.model)
            return {"ok": True, "key": key, "status": "ready",
                    "model": client.model, "model_display": shown, "latency": cost,
                    "message": f"连通正常（{shown} · API: {client.model} · {cost}s）",
                    "sample": (reply or "").strip()[:60]}
        except Exception as e:
            return {"ok": False, "key": key, "status": "error",
                    "message": f"调用失败：{type(e).__name__}: {e}"}

    # 数据源：静态判定，不消耗额度
    from core.adapter import DATA_SOURCE_REGISTRY
    if key not in DATA_SOURCE_REGISTRY:
        return {"ok": False, "key": key, "status": "not_implemented",
                "message": "适配器尚未接入实现（占位槽位）。"}
    try:
        ok = DATA_SOURCE_REGISTRY[key]().is_available()
    except Exception as e:
        ok = False
    return {"ok": bool(ok), "key": key, "status": "ready" if ok else "unavailable",
            "message": "凭据与适配器均就绪，已参与多源聚合。" if ok else "适配器当前不可用。"}


@app.post("/api/sources/custom")
def add_custom_source(req: CustomSourceCreate, request: Request):
    _require_local_ip(request)
    """新增自定义数据源（走通用 HTTP 适配器）。

    URL 属用户可控输入：**保存时就**跑一遍出网守卫（adapters/generic_adapter 里
    的 _assert_public_url），而不是等到第一次抓取才发现指向内网。守卫本身在
    取数路径上还有一次（含逐跳重定向复查），两处共用同一实现。
    """
    from core import datasource_config as dsc
    from adapters.generic_adapter import _assert_public_url
    if (req.url or "").strip():
        try:
            _assert_public_url(req.url.strip())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        created = dsc.add_custom(req.label, req.url, req.token, req.enabled)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "created": created, "catalog": aggregator.catalog()}


@app.post("/api/sources/custom/{key}")
def update_custom_source(key: str, req: CustomSourceUpdate, request: Request):
    _require_local_ip(request)
    """更新自定义数据源（开关 / 名称 / URL / Token）。URL 改动同样先过出网守卫。"""
    from core import datasource_config as dsc
    from adapters.generic_adapter import _assert_public_url
    if (req.url or "").strip():
        try:
            _assert_public_url(req.url.strip())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        c = dsc.update_custom(key, enabled=req.enabled, label=req.label,
                              url=req.url, token=req.token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # ⚠️ 剥离 credential：update_custom 返回完整条目（含 token 明文），不能回显
    c = {k: v for k, v in (c or {}).items() if k != "credential"}
    return {"ok": True, "custom": c, "catalog": aggregator.catalog()}


@app.delete("/api/sources/custom/{key}")
def remove_custom_source(key: str, request: Request):
    _require_local_ip(request)
    """删除自定义数据源。"""
    from core import datasource_config as dsc
    removed = dsc.remove_custom(key)
    return {"ok": removed, "catalog": aggregator.catalog()}


# ============================ 候选搜索：输入时实时候选 ============================

@app.get("/api/search/suggest")
def search_suggest(q: str = ""):
    """轻量级候选搜索（仅查本地预采集库，零成本）—— 用于输入框实时下拉提示。

    说明：
      - 输入"科蓝"→ 本地库模糊匹配到"北京科蓝软件系统股份有限公司"→ 1 候选，提示已匹配
      - 输入"科技"→ 命中多家（信雅达科技/宇信科技/长亮科技）→ 多候选，弹下拉
      - 输入"孚日"→ 本地库无 → 返回 0 候选 + 提示"请输入完整公司全称"
        （点击"开始风险扫描"时仍会走企查查 search_candidates 全面校验，弹 qcc 候选）
    """
    name = (q or "").strip()
    if not name:
        return {"candidates": [], "hint": ""}

    from adapters.local_adapter import LocalAdapter
    adapter = LocalAdapter()
    raw, exact, cands = adapter._match_local(name)
    candidates = []
    if not exact and cands:
        # 简称/模糊：把所有模糊候选都列出来（即使唯一，让用户确认）
        for c in cands:
            reg = c.get("registration") or {}
            candidates.append({
                "name": c.get("name", ""),
                "credit_code": reg.get("credit_code") or "",
                "legal_person": reg.get("legal_person") or "—",
                "established": reg.get("established") or "—",
                "status": reg.get("status") or "—",
                "source": "local",
            })
    elif exact and raw:
        reg = raw.get("registration") or {}
        candidates.append({
            "name": raw.get("name", ""),
            "credit_code": reg.get("credit_code") or "",
            "legal_person": reg.get("legal_person") or "—",
            "established": reg.get("established") or "—",
            "status": reg.get("status") or "—",
            "source": "local",
        })

    hint = ""
    if cands and not exact and len(candidates) > 1:
        hint = f"名称不完整，命中 {len(candidates)} 个主体，请选择目标。"
    elif cands and not exact and len(candidates) == 1:
        hint = f"名称可能不完整，已匹配到「{candidates[0]['name']}」，请确认。"
    elif not cands and name:
        hint = "本地预采集库中未找到，请输入完整公司全称（点击查询时仍会调企查查校验）。"
    return {"candidates": candidates, "hint": hint}


# ============================ 企查查积分优化：缓存管理 ============================

@app.get("/api/qcc/cache")
def qcc_cache_stats():
    """企查查缓存统计：命中率、已节省的调用次数、熔断状态。"""
    from integrations import qcc_cache, qcc_mcp
    out = qcc_cache.stats()
    out["breaker"] = qcc_mcp.breaker_state()
    return out


@app.post("/api/qcc/cache/purge")
def qcc_cache_purge():
    """清理已过期缓存条目（不影响有效缓存）。"""
    from integrations import qcc_cache
    n = qcc_cache.purge_expired()
    return {"ok": True, "purged": n, "stats": qcc_cache.stats()}


@app.post("/api/qcc/cache/clear")
def qcc_cache_clear():
    """清空企查查缓存并解除熔断（下次查询将重新拉取，会消耗积分）。"""
    from integrations import qcc_cache, qcc_mcp
    n = qcc_cache.clear()
    qcc_mcp.reset_breaker()   # 充值后立刻恢复调用
    return {"ok": True, "cleared": n, "stats": qcc_cache.stats()}


# ============================ 周期监控：台账 / 扫描 / 日历 / 报送 ============================

class RosterItem(BaseModel):
    id: str = ""
    name: str
    category: str = "运行维护类"
    important: bool = False
    non_resident: bool = False
    cross_border: bool = False
    contract_start: str = ""
    contract_end: str = ""
    last_dd: str = ""
    last_onsite: str = ""
    business_lines: List[str] = []
    service_name: str = ""
    group: str = ""            # 集团/关联方归集名（第28条 关联外包），空则由名称推断
    annual_amount: float = 0   # 年度外包金额（万元），用于集中度量化（第33条）


class WorkOrderCreate(BaseModel):
    supplier: str
    supplier_id: str = ""
    level: str = ""
    # 注意：必须允许 None。前端表单在未预填分数时会显式发送 "score": null，
    # 若声明为 float，显式 null 会被 pydantic 判为 float_type 错误并返回 422，
    # 导致「新建处置工单」在无预填分数时整体不可用。
    score: Optional[float] = None
    trigger: str = ""
    clause: str = "办法第27条"
    disposal_type: str = "约谈"
    measures: str = ""
    owner: str = ""
    dept: str = ""
    days: int = 0
    due_date: str = ""


class WorkOrderAdvance(BaseModel):
    to_status: str
    note: str = ""
    by: str = ""


class WorkOrderNote(BaseModel):
    note: str
    by: str = ""


@app.get("/api/roster")
def get_roster():
    """供应商台账：清单 + 统计 + 可选枚举值。"""
    from core import roster as roster_mod
    return {
        "items": roster_mod.load_roster(),
        "stats": roster_mod.stats(),
        "categories": roster_mod.OUTSOURCE_CATEGORIES,
        "important_criteria": [{"name": n, "desc": d} for n, d in roster_mod.IMPORTANT_CRITERIA],
    }


@app.post("/api/roster")
def save_roster_item(item: RosterItem):
    """新增或更新一条台账记录。空名称、负数或天文数字金额一律 400，不落盘。"""
    from core import roster as roster_mod
    try:
        items = roster_mod.upsert(item.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "items": items, "stats": roster_mod.stats()}


@app.delete("/api/roster/{rid}")
def delete_roster_item(rid: str):
    """删除一条台账记录。查无此条返回 404（不再无条件报成功）。"""
    from core import roster as roster_mod
    try:
        items = roster_mod.remove(rid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"台账中不存在 ID 为「{rid}」的记录")
    return {"ok": True, "items": items, "stats": roster_mod.stats()}


# ============================ 台账批量导入（Excel/Word/PDF） ============================

class RosterImportCommit(BaseModel):
    rows: List[dict] = []


@app.post("/api/roster/import/preview")
async def roster_import_preview(file: UploadFile = File(...)):
    """上传 Excel/Word/PDF，解析出供应商清单预览（不落盘）。"""
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in (".xlsx", ".xls", ".docx", ".doc", ".pdf"):
        raise HTTPException(status_code=400, detail=f"不支持的格式 {suffix}，支持 Excel/Word/PDF")

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        await _write_upload_to(file, tmp)
        tmp.close()
        from core import roster_import
        result = roster_import.parse_roster_file(tmp.name)
    except ValueError as e:
        # 体积超限等入参问题 → 400，语义比 500 准确
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件解析失败：{e}")
    finally:
        # 删除失败不能覆盖上面的 HTTPException（原实现会抛错并变成语义错误的 500）
        try:
            os.unlink(tmp.name)   # 处理完立即删除，不留痕
        except OSError:
            _log_swallow("server.py:1061")

    return {"ok": result.get("ok"), "source": result.get("source"),
            "rows": result.get("rows", []), "summary": result.get("summary", {})}


@app.post("/api/roster/import/commit")
def roster_import_commit(req: RosterImportCommit):
    """把预览通过的供应商清单批量写入台账（按名称去重）。"""
    from core import roster as roster_mod
    try:
        out = roster_mod.import_rows(req.rows or [])
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **out}


@app.get("/api/compliance/calendar")
def compliance_calendar():
    """合规日历：按《办法》时限自动排期的待办事项。"""
    from core import compliance
    return compliance.build_calendar()


# ================== 集中度风险（第28条关联外包 / 第33条集中度） ==================

@app.get("/api/concentration")
def concentration_view():
    """集中度风险量化：单供应商 / 集团合并 / 业务条线 / HHI 指数。"""
    from core import concentration
    return concentration.build_concentration()


# ============================ 处置闭环工单（第27/29/30条） ============================

@app.get("/api/workorders")
def list_workorders():
    """处置工单清单 + 统计 + 处置类型枚举。"""
    from core import workorder
    return {
        "items": [workorder._decorate(o) for o in workorder.load_orders()],
        "stats": workorder.stats(),
        "disposal_types": workorder.DISPOSAL_TYPES,
        "status_flow": workorder.STATUS_FLOW,
        "status_order": workorder.STATUS_ORDER,
    }


@app.post("/api/workorders")
def create_workorder(req: WorkOrderCreate):
    """新建处置工单。"""
    from core import workorder
    try:
        order = workorder.create_order(req.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "order": order, "stats": workorder.stats()}


@app.post("/api/workorders/{wo_id}/advance")
def advance_workorder(wo_id: str, req: WorkOrderAdvance, request: Request):
    """推进工单状态（状态机校验 + 留痕）。操作人缺省取请求级身份。"""
    from core import workorder
    ident = getattr(request.state, "identity", None) or auth_mod.identity(request)
    by = req.by or ident.get("operator", "")
    try:
        order = workorder.advance(wo_id, req.to_status, req.note, by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "order": order, "stats": workorder.stats()}


@app.post("/api/workorders/{wo_id}/note")
def note_workorder(wo_id: str, req: WorkOrderNote, request: Request):
    """追加处置记录（留痕，不改状态）。操作人缺省取请求级身份。"""
    from core import workorder
    ident = getattr(request.state, "identity", None) or auth_mod.identity(request)
    by = req.by or ident.get("operator", "")
    try:
        order = workorder.add_note(wo_id, req.note, by)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "order": order}


@app.delete("/api/workorders/{wo_id}")
def delete_workorder(wo_id: str):
    """删除工单。查无此条返回 404。"""
    from core import workorder
    try:
        items = workorder.remove_order(wo_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"工单 {wo_id} 不存在")
    return {"ok": True, "items": items, "stats": workorder.stats()}


@app.post("/api/disposal/suggest")
def disposal_suggest(req: QueryRequest):
    """根据供应商当前风险等级，给出建议处置措施（供工单表单一键带出）。"""
    from core import workorder
    from core.rating import rate_supplier

    try:
        res = _fetch_profile(req.name, req.source, req.key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # 主体锚定：空档案会被判成"无扣分 = 高分"，进而建议"green 无需处置" ——
    # 给一家根本不存在的公司出"无需处置"的结论，比报错糟糕得多。
    if not _has_subject_data(res.profile):
        raise HTTPException(
            status_code=404,
            detail="未找到该供应商的工商、财务或司法等有效信息，请确认公司全称后重试。",
        )
    rating = rate_supplier(res.profile, supplier_id=_match_roster_id(req.name))
    sug = workorder.suggest_disposal(rating.level, rating.red_lines)
    return {
        "supplier": req.name,
        "score": rating.total_score,
        "level": rating.level,
        "red_lines": rating.red_lines,
        "suggestion": sug,
    }


# ---- /api/scan 并行度（有界线程池）----
# 单家扫描 ≈ 0.3–0.6s（本地快照命中缓存时），37 家串行 ~12s；并行后 ~1.5s。
# 上限取 min(8, 家数)：本场景受企查查/iFinD/Wind 的 QPS 与积分约束，8 已足够，
# 不随机器核数放大（避免压垮外部数据源或触发其熔断）。
SCAN_MAX_WORKERS = int(os.environ.get("FENGHUO_SCAN_WORKERS", "8").strip() or "8")


@app.post("/api/scan")
def scan_all():
    """周期监控：批量扫描全部台账供应商，输出组合风险视图。

    对应《办法》第23条（持续监控）、第26条（财务/内控/安全持续监控）。

    实现说明：单家扫描互相独立（取数 + 评分），故用**有界**线程池并发；
    结果按台账顺序归位（列表按索引写入），保证输出行序与串行版一致 ——
    E2E 与看板都依赖稳定的行序。
    """
    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    from core import roster as roster_mod
    from core.regreport import judge_pre_report, detect_major_events

    items = roster_mod.load_roster()
    name2item = {it.get("name"): it for it in items}
    names = [it.get("name") for it in items if it.get("name")]

    def scan_one(nm: str) -> dict:
        try:
            res = _fetch_profile(nm, "auto")
            it = name2item.get(nm, {})
            # 传入台账 ID，让履约表现维度（第25条服务水平）参与评分
            rating = rate_supplier(res.profile, supplier_id=it.get("id", ""))
            d = _to_result(res.profile, rating, res.verified, res.failed)
            pre = judge_pre_report(it)
            major = detect_major_events(res.profile)

            # 场景标签（第12条分类管理）—— 让组合看板显示每家供应商适用的评价场景
            from core import scenarios as sc_mod
            skey = sc_mod.scenario_for_category(it.get("category", ""))
            sc_obj = sc_mod.SCENARIOS.get(skey) if skey else None
            scenario = None
            if sc_obj:
                top_dim = max(sc_obj["weights"].items(), key=lambda kv: kv[1])[0]
                scenario = {
                    "key": sc_obj["key"],
                    "name": sc_obj["name"],
                    "short": sc_obj["short"],
                    "top_dim": top_dim,
                    "top_dim_label": sc_mod.DIM_LABEL[top_dim],
                }

            return {
                "id": it.get("id", ""),
                "name": d["supplier"],
                "roster_name": nm,
                "category": it.get("category", ""),
                "important": bool(it.get("important")),
                "non_resident": bool(it.get("non_resident")),
                "cross_border": bool(it.get("cross_border")),
                "total_score": d["total_score"],
                "level": d["level"],
                "action": d["action"],
                "dimensions": {k: v["score"] for k, v in d["dimensions"].items()},
                "dim_labels": {k: v.get("label", k) for k, v in d["dimensions"].items()},
                "dim_na": [k for k, v in d["dimensions"].items() if v.get("na")],
                # 覆盖组与对照分：缺维度的分数只能同组比较，别拿 3 维的 90 分比 6 维的 85 分
                "coverage": d.get("coverage") or {},
                "baseline_score": d.get("baseline_score"),
                "performance_origin": d.get("performance_origin", ""),
                "performance": d.get("performance") or {},
                "red_lines": d["red_lines"],
                "risk_alerts": len(d.get("risk_alerts", [])),
                "legal_events": len(d.get("legal_events", [])),
                "sources": [s["label"] for s in d.get("sources", [])],
                "failed_sources": res.failed or {},
                "pre_report": pre,
                "major_events": major,
                "scenario": scenario,
                "ok": True,
            }
        except MultiCandidateError as e:
            return {
                "id": (name2item.get(nm) or {}).get("id", ""),
                "name": nm, "roster_name": nm, "ok": False,
                "error": "命中多个主体，需人工确认",
                "candidates": e.candidates,
                "important": bool((name2item.get(nm) or {}).get("important")),
                "category": (name2item.get(nm) or {}).get("category", ""),
            }
        except Exception as e:
            return {
                "id": (name2item.get(nm) or {}).get("id", ""),
                "name": nm, "roster_name": nm, "ok": False,
                "error": str(e),
                "important": bool((name2item.get(nm) or {}).get("important")),
                "category": (name2item.get(nm) or {}).get("category", ""),
            }

    # 并发执行，按台账顺序归位（scan_one 自吞全部业务异常，必有返回值）
    results: list = [None] * len(names)
    workers = max(1, min(SCAN_MAX_WORKERS, len(names) or 1))
    _t0 = _t.time()
    if workers == 1:
        results = [scan_one(nm) for nm in names]
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan") as pool:
            futs = {pool.submit(scan_one, nm): i for i, nm in enumerate(names)}
            for fut, i in futs.items():
                results[i] = fut.result()   # scan_one 不抛异常；万一抛出则让请求 500（取数基础设施故障）

    ok_rows = [r for r in results if r.get("ok")]
    dist = {"red": 0, "yellow": 0, "green": 0}
    for r in ok_rows:
        lv = r.get("level")
        if lv in dist:
            dist[lv] += 1

    # 组合级：需事前报告的、命中重大事件的
    pre_needed = [r for r in ok_rows if r.get("pre_report", {}).get("required")]
    major_hit = [r for r in ok_rows if r.get("major_events")]

    return {
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(names),
        "ok": len(ok_rows),
        "failed": len(names) - len(ok_rows),
        "distribution": dist,
        "avg_score": round(sum(r["total_score"] for r in ok_rows) / len(ok_rows), 2) if ok_rows else 0,
        "rows": sorted(results, key=lambda r: (not r.get("ok"), r.get("total_score", 999))),
        "pre_report_needed": [{"name": r["name"], "hits": r["pre_report"]["hits"]} for r in pre_needed],
        "major_event_hits": [{"name": r["name"], "events": r["major_events"]} for r in major_hit],
        # 凭据缺口：启用了但没配凭据/不可用的源，界面据此提示"分数可能未覆盖全部数据源"
        "credential_gaps": _credential_gaps_safe(),
        "elapsed_ms": int((_t.time() - _t0) * 1000),
    }


def _credential_gaps_safe() -> dict:
    """取凭据缺口清单；任何异常都不影响主结果。"""
    try:
        from core import datasource_config as dsc
        return dsc.unavailable_reasons()
    except Exception:
        _log_swallow("server.py:credential_gaps")
        return {}


@app.post("/api/regreport")
def regreport(req: QueryRequest):
    """生成《监管报告材料》（附件1 目录口径）。"""
    from core import roster as roster_mod
    from core.regreport import build_report_material, to_html

    items = roster_mod.load_roster()
    it = next((x for x in items if x.get("name") == req.name), None)
    try:
        res = _fetch_profile(req.name, req.source, req.key)
    except MultiCandidateError as e:
        return JSONResponse(status_code=300, content={
            "detail": str(e), "candidates": e.candidates, "type": "multi_candidate"})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # 主体锚定：与 /api/query 同一道关。auto/多源模式下部分适配器（iFinD/Wind）
    # 对不存在的主体"优雅返回空"而非抛错，若不在这里拦，会拿一份空档案生成
    # 一份看似完整的监管报送材料 —— 报送口径下这比查询页出错严重得多。
    if not _has_subject_data(res.profile):
        raise HTTPException(
            status_code=404,
            detail="未找到该供应商的工商、财务或司法等有效信息，请确认公司全称后重试。",
        )

    material = build_report_material(res.profile, it)
    return {"material": material, "html": to_html(material, it)}


@app.post("/api/report")
def report(req: QueryRequest):
    """生成 HTML 报告（公开公司）。返回 HTML 内容供下载/预览。"""
    try:
        res = _fetch_profile(req.name, req.source, req.key)
    except MultiCandidateError as e:
        return JSONResponse(status_code=300, content={
            "detail": str(e), "candidates": e.candidates, "type": "multi_candidate"})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # 同 /api/query：无主体证据 → 404，不生成"满分报告"。
    # 这是给外部/评委下载的 HTML，一份查无此公司的报告比查询页报错更容易被误读。
    if not _has_subject_data(res.profile):
        raise HTTPException(
            status_code=404,
            detail="未找到该供应商的工商、财务或司法等有效信息，请确认公司全称后重试。",
        )

    # 若该主体在台账中，则带上 ID 让履约表现维度参与评分
    sid = _match_roster_id(req.name)
    rating = rate_supplier(res.profile, supplier_id=sid)
    result = _to_result(res.profile, rating, res.verified, res.failed)
    html = render_report(result, channel="public")
    return {"html": html}


# ============================ 服务水平指标（第25条 6 类） ============================

class ServiceLevelRecord(BaseModel):
    supplier_id: str
    period: str                      # 报告期，如 "2026-09"
    values: Dict[str, float] = {}    # {指标key: 数值}


@app.get("/api/servicelevel")
def service_level_overview():
    """服务水平总览：六类指标定义 + 各供应商达标情况。"""
    from core import servicelevel as sl, roster as roster_mod
    return sl.build_overview(roster_mod.load_roster())


@app.get("/api/servicelevel/{supplier_id}")
def service_level_detail(supplier_id: str):
    """单个供应商的服务水平明细：逐期评价 + 趋势 + 连续未达标 + 结论。"""
    from core import servicelevel as sl
    ev = sl.evaluate_supplier(supplier_id)
    ev["workorder_suggestion"] = sl.suggest_workorder(ev)
    return ev


@app.post("/api/servicelevel")
def service_level_save(req: ServiceLevelRecord):
    """录入/更新某供应商某报告期的服务水平指标。"""
    from core import servicelevel as sl
    sid = (req.supplier_id or "").strip()
    period = (req.period or "").strip()
    if not sl.SID_RE.match(sid):
        raise HTTPException(status_code=400, detail=f"供应商 ID 不合法「{sid}」（应为 S001 这类编号）")
    if not sl.PERIOD_RE.match(period):
        raise HTTPException(status_code=400, detail="报告期格式应为 YYYY-MM（如 2026-09）")
    bad = [k for k in req.values if k not in sl.METRIC_MAP]
    if bad:
        raise HTTPException(status_code=400, detail=f"未知指标：{', '.join(bad)}")
    _assert_roster_id(sid)
    rec = {"period": period, **req.values}
    try:
        entry = sl.upsert_record(sid, rec)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ev = sl.evaluate_supplier(sid)
    return {"ok": True, "entry": entry, "evaluation": ev}


@app.delete("/api/servicelevel/{supplier_id}/{period}")
def service_level_delete(supplier_id: str, period: str):
    """删除某供应商某报告期的服务水平记录。"""
    from core import servicelevel as sl
    try:
        entry = sl.remove_period(supplier_id, period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "entry": entry}


class ServiceLevelBatch(BaseModel):
    supplier_id: str
    period: str = ""                      # 留空则每行自带 period 列
    text: str = ""                        # 粘贴的 CSV/TSV 文本（首行为表头）
    preview: bool = False                 # True=仅校验不落盘


@app.post("/api/servicelevel/parse")
def service_level_parse(req: ServiceLevelBatch):
    """解析粘贴的表格文本 → 校验 → 预览（不落盘）。

    支持从 Excel 直接复制粘贴（TSV）或 CSV，首行为列名，
    列名可用指标中文名或 key，也接受常见别名。
    """
    from core import servicelevel as sl
    return sl.parse_pasted(req.text, default_period=req.period)


@app.post("/api/servicelevel/batch")
def service_level_batch(req: ServiceLevelBatch):
    """批量录入校验通过的记录（一次提交多期）。"""
    from core import servicelevel as sl
    _assert_roster_id((req.supplier_id or "").strip())
    parsed = sl.parse_pasted(req.text, default_period=req.period)
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error"), "rows": parsed.get("rows")}
    saved = []
    for row in parsed["rows"]:
        if not row.get("valid"):
            continue
        try:
            sl.upsert_record(req.supplier_id, {"period": row["period"], **row["values"]})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        saved.append(row["period"])
    ev = sl.evaluate_supplier(req.supplier_id)
    return {"ok": True, "saved_periods": saved, "evaluation": ev,
            "summary": parsed.get("summary")}


# ============================ 处置策略推演沙盘（第27/31/33条） ============================

class SimulateRequest(BaseModel):
    decisions: Dict[str, str] = {}       # {supplier_id: action_key}
    service_scores: Dict[str, float] = {}


@app.get("/api/simulator/baseline")
def simulator_baseline():
    """推演基线：现状组合（总额/HHI/条线覆盖）+ 可选处置动作 + 建议初始组合。"""
    from core import simulator as sim, roster as roster_mod
    from core import servicelevel as sl
    items = roster_mod.load_roster()

    # 服务水平衰减分（用于建议处置优先级）
    svc = {}
    for it in items:
        try:
            ev = sl.evaluate_supplier(it.get("id"))
            s = (ev.get("decay") or {}).get("score")
            if s is not None:
                svc[it.get("id")] = s
        except Exception:
            _log_swallow("server.py:1442")

    base = sim.build_baseline(items)
    return {
        "baseline": base,
        "actions": [
            {"key": k, **{kk: vv for kk, vv in v.items() if kk != "key"}}
            for k, v in sim.ACTIONS.items()
        ],
        "action_order": sim.ACTION_ORDER,
        "service_scores": svc,
    }


@app.post("/api/simulator/run")
def simulator_run(req: SimulateRequest):
    """按给定决策推演组合影响。"""
    from core import simulator as sim, roster as roster_mod
    from core import servicelevel as sl
    items = roster_mod.load_roster()

    # 不校验会让未知供应商/未知动作静默「不生效」，界面照常显示一份乐观结论。
    known_ids = {it.get("id") for it in items}
    bad_sid = [k for k in (req.decisions or {}) if k not in known_ids]
    if bad_sid:
        raise HTTPException(status_code=400,
                            detail=f"供应商不在台账中：{'、'.join(bad_sid[:5])}")
    bad_act = {v for v in (req.decisions or {}).values() if v not in sim.ACTION_ORDER}
    if bad_act:
        raise HTTPException(status_code=400,
                            detail=f"未知处置动作：{'、'.join(sorted(bad_act))}"
                                   f"（可选 {' / '.join(sim.ACTION_ORDER)}）")

    svc = dict(req.service_scores or {})
    if not svc:
        for it in items:
            try:
                ev = sl.evaluate_supplier(it.get("id"))
                s = (ev.get("decay") or {}).get("score")
                if s is not None:
                    svc[it.get("id")] = s
            except Exception:
                _log_swallow("server.py:1484")

    return sim.simulate(items, req.decisions or {}, svc)


@app.get("/api/simulator/suggest")
def simulator_suggest():
    """按当前风险等级 + 服务水平，给出建议的处置组合（供一键载入）。"""
    from core import simulator as sim, roster as roster_mod
    from core import servicelevel as sl
    items = roster_mod.load_roster()

    levels, svc = {}, {}
    for it in items:
        sid = it.get("id")
        try:
            ev = sl.evaluate_supplier(sid)
            s = (ev.get("decay") or {}).get("score")
            if s is not None:
                svc[sid] = s
        except Exception:
            _log_swallow("server.py:1505")

    # 风险等级：直接按服务水平给出（轻量，不触发外部抓取）
    for it in items:
        sid = it.get("id")
        lv = "green"
        s = svc.get(sid)
        if s is not None and s < 60:
            lv = "red"
        elif s is not None and s < 75:
            lv = "yellow"
        levels[sid] = lv

    sug = sim.suggest_actions(items, levels, svc)
    return {"levels": levels, "service_scores": svc, **sug}


# ============================ 专属场景风险评价（第12条 分类管理） ============================

class ScenarioEvaluate(BaseModel):
    name: str
    source: str = "auto"
    key: str = ""
    scenario: str = ""       # 留空则按台账分类自动匹配
    supplier_id: str = ""    # 用于取台账上下文与服务水平数据


@app.get("/api/scenarios")
def scenarios_list():
    """专属场景清单（供前端选择）。"""
    from core import scenarios as sc
    return {"scenarios": sc.list_scenarios(),
            "category_map": sc.CATEGORY_TO_SCENARIO,
            "dim_labels": sc.DIM_LABEL}


@app.post("/api/scenario/evaluate")
def scenario_evaluate(req: ScenarioEvaluate):
    """按指定场景对供应商做场景化二次加权评价。"""
    from core import scenarios as sc, servicelevel as sl, roster as roster_mod

    try:
        res = _fetch_profile(req.name, req.source, req.key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # 主体锚定：与 /api/query 一致。空档案算出的"场景分"没有任何意义，
    # 却会被当成一次正常的场景化评价展示给用户。
    if not _has_subject_data(res.profile):
        raise HTTPException(
            status_code=404,
            detail="未找到该供应商的工商、财务或司法等有效信息，请确认公司全称后重试。",
        )

    # 场景评价同样带上服务水平，避免"场景分与通用分口径不一致"
    _sid = req.supplier_id or _match_roster_id(req.name)
    rating = rate_supplier(res.profile, supplier_id=_sid)
    base = {
        "total_score": rating.total_score,
        "level": rating.level,
        "dimensions": {k: {"score": v["score"]} for k, v in rating.dimension_scores.items()},
        "weights": rating.weights,
    }
    d = _to_result(res.profile, rating, res.verified, res.failed)

    # 定位台账条目（提供驻场/条线等上下文）
    items = roster_mod.load_roster()
    it = None
    if req.supplier_id:
        it = next((x for x in items if x.get("id") == req.supplier_id), None)
    if not it:
        it = next((x for x in items if x.get("name") == req.name), None)

    scenario_key = sc.resolve_scenario(req.scenario) if req.scenario \
        else sc.scenario_for_category((it or {}).get("category", ""))
    if req.scenario and not scenario_key:
        raise HTTPException(status_code=400,
                            detail=f"未知场景：{req.scenario}（可选："
                                   + "、".join(s["name"] for s in sc.list_scenarios()) + "）")
    if not scenario_key:
        return {"ok": False, "reason": "未指定场景且台账分类无法匹配，请手工选择场景",
                "base": base, "result": d}

    service_eval = sl.evaluate_supplier((it or {}).get("id") or req.supplier_id or "")
    ev = sc.evaluate_scenario(res.profile, base, scenario_key, it, service_eval)
    ev["ok"] = True
    ev["supplier"] = d["supplier"]
    ev["category"] = (it or {}).get("category", "")
    ev["auto_matched"] = not bool(req.scenario)
    ev["service_level"] = {
        "score": (service_eval.get("latest") or {}).get("score"),
        "grade": service_eval.get("grade"),
    }
    return ev


# ============================ 外包专项 · 过往记录 ============================

class SpecialQuery(BaseModel):
    name: str
    supplier_id: str = ""
    refresh: bool = False


@app.post("/api/outsourcing/special")
def outsourcing_special(req: SpecialQuery):
    """外包专项：服务提供商的外包过往记录与专项风险分析。"""
    from core import outsourcing_special as osp
    r = osp.get_special(req.name, req.supplier_id, refresh=req.refresh)
    return {
        "ok": r["raw"].get("ok"),
        "error": r["raw"].get("error") or "",
        "cached": r.get("cached"),
        "cached_at": r["raw"].get("cached_at"),
        "source": r.get("source", "live"),
        "offline": bool(r.get("offline")),
        "live_error": r.get("live_error") or "",
        "enterprise": r["raw"].get("enterprise") or {},
        "analysis": r["analysis"],
    }


@app.delete("/api/outsourcing/special/{supplier_id}")
def outsourcing_special_clear(supplier_id: str):
    """清除外包专项缓存（下次将重新抓取）。"""
    from core import outsourcing_special as osp
    try:
        return {"ok": osp.clear_cache(supplier_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================== 预警中心（第23条持续监控 / 第27条处置） ====================
# 旁路模块：只出预警与处置建议，**不参与综合安全度加权**，六维权重与既有分数不变。

@app.get("/api/alerts")
def alerts_view(supplier_id: str = "", level: str = "", source: str = "",
                with_profile: int = 0):
    """统一预警聚合：集中度 / 履约表现 / 合同与知识产权 / 合规时限 / 企业级风险。

    只读本地已落盘数据（with_profile=1 时另读本地台账库），不联网，可做首页默认视图。
    """
    from core import alerts
    return alerts.collect_alerts(supplier_id=supplier_id, level=level,
                                 source=source, with_profile=bool(with_profile))


class AlertState(BaseModel):
    key: str
    state: str = ""
    workorder_id: str = ""
    note: str = ""


@app.post("/api/alerts/state")
def alerts_set_state(req: AlertState, request: Request):
    """更新预警闭环状态；绑定工单号即完成「预警 → 处置」闭环（第27条）。

    处置动作必须留痕到人：操作人取请求级身份（本机=本机操作员 / 令牌=远程调用），
    与工单的 advance/note 口径一致。
    """
    from core import alerts
    ident = getattr(request.state, "identity", None) or auth_mod.identity(request)
    try:
        alerts.update_state(req.key, state=req.state,
                            workorder_id=req.workorder_id, note=req.note,
                            operator=ident.get("operator", ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return alerts.collect_alerts()


# ============ 合同与知识产权（第22/29/30/31/34/37条） ============
# 同样为旁路模块：输出条款缺口与业务连续性传导链，不参与综合安全度加权。

@app.get("/api/contract")
def contract_view():
    """合同关键条款登记与缺口评价（19 类条款 + 传导链定级）。"""
    from core import contract
    return contract.build_all()


@app.get("/api/contract/catalog")
def contract_catalog():
    """19 类关键条款目录（供登记表单与对外说明）。"""
    from core import contract
    return {"groups": contract.GROUPS, "clauses": contract.CLAUSES}


class ContractRegister(BaseModel):
    supplier_id: str
    contract_name: str = ""
    signed_at: str = ""
    clauses: dict = {}
    positions: dict = {}


@app.post("/api/contract/register")
def contract_register(req: ContractRegister):
    """登记/更新某供应商的合同关键条款。clauses 取值：有 / 无 / 不适用。"""
    from core import contract
    known = {c["key"] for c in contract.CLAUSES}
    sid = (req.supplier_id or "").strip()
    if not re.match(r"^[A-Za-z0-9_-]{1,32}$", sid):
        raise HTTPException(status_code=400,
                            detail=f"供应商 ID 不合法「{sid}」（应为 S001 这类编号）")
    if req.signed_at:
        try:
            _date.fromisoformat(req.signed_at.strip())
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"签署日期应为合法的 YYYY-MM-DD，收到「{req.signed_at}」")
    unknown = [k for k in (req.clauses or {}) if k not in known]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"未知条款 key：{'、'.join(unknown[:5])}")
    _assert_roster_id(sid)
    # 读-改-写必须整体持锁：否则两个并发登记各自 load 到旧快照，后写者覆盖前者。
    with contract._LOCK:
        data = contract.load_contracts()
        rec = data.get(req.supplier_id) or {}
        for k in ("contract_name", "signed_at"):
            v = getattr(req, k)
            if v:
                rec[k] = v
        merged = dict(rec.get("clauses") or {})
        merged.update(req.clauses or {})
        rec["clauses"] = merged
        pos = dict(rec.get("positions") or {})
        pos.update(req.positions or {})
        rec["positions"] = pos
        data[req.supplier_id] = rec
        contract.save_contracts(data)
    return contract.build_all()


# 独立部署模式：涉密通道已由 classified_server.py 单独承载时，
# 本实例不再注册 /api/upload，避免同一能力出现两个入口（也就少一份暴露面）。
# 前端配合：把 index.html 里的 CLASSIFIED_API 指向涉密实例地址即可。
if os.environ.get("CLASSIFIED_STANDALONE", "").strip() == "1":
    app.routes[:] = [r for r in app.routes
                     if getattr(r, "path", "") != "/api/upload"]


if __name__ == "__main__":
    import uvicorn
    # 云端/容器会注入 PORT 环境变量；本地默认 8000。
    #
    # 绑定地址默认 0.0.0.0（本地开发、容器平台直连需要）。
    # 生产环境走 nginx 反代时，应设 BIND_HOST=127.0.0.1，让应用只监听回环：
    #   否则 8000 直接暴露公网，既绕过 nginx 的 50m 体积限制/日志，也让安全组
    #   放行 ≥8000 就成为一个隐患口子。systemd 单元里加一行 Environment=BIND_HOST=127.0.0.1 即可。
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
    uvicorn.run(app, host=host, port=port)
