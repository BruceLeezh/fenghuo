"""数据源 & 能力配置中心 —— 统一管理所有可插拔数据源 / LLM 的开关与凭据。

设计目标
--------
1. 一张清单列出所有"能力"（数据源 + LLM），每个都能在网页上开关、改凭据。
2. 内置能力预先声明"可配置字段"元信息，前端据此动态渲染表单（改能力无需改前端）。
3. 支持"自定义数据源"：使用者可在网页新增任意 HTTP 数据源（走通用适配器）。
4. 凭据安全：默认读 .env 环境变量；网页修改后写 data/source_config.json
   并回写 os.environ，让现有代码（qcc_mcp / deepseek）即时生效，重启后仍保留。

命名约定
--------
- kind = "datasource"  数据源（参与多源聚合与交叉验证）
- kind = "llm"         大模型能力（事件抽取 / 财务分析）
"""
from core.quiet_log import log_swallow as _log_swallow
import os
import threading

from core.jsonstore import read_json, write_json

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_BASE, "data", "source_config.json")
_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 内置能力清单（可配置字段元信息，前端据此动态渲染表单）
# ---------------------------------------------------------------------------
# fields 每项：name（凭据字段名）/ label / secret（是否敏感）/ env（对应环境变量）/
#              placeholder（占位提示）
BUILTIN_SOURCES = {
    "qcc": {
        "label": "企查查 MCP",
        "kind": "datasource",
        "category": "工商登记 / 年报财务 / 司法风险",
        "authoritative": True,
        "default_enabled": True,
        "fields": [
            {"name": "token", "label": "API Token", "secret": True,
             "env": "QCC_API_TOKEN", "placeholder": "Bearer token（agent.qcc.com 获取）"},
        ],
    },
    "eastmoney": {
        "label": "东方财富公开接口",
        "kind": "datasource",
        "category": "实时行情 / 估值",
        "authoritative": False,
        "default_enabled": True,
        "fields": [],   # 公开接口，无需凭据
    },
    "local": {
        "label": "本地预采集库",
        "kind": "datasource",
        "category": "工商登记 / 年报财务 / 司法风险",
        "authoritative": True,
        "default_enabled": True,
        "fields": [],   # 离线内置数据，无需凭据
    },
    "ifind": {
        "label": "同花顺 iFinD",
        "kind": "datasource",
        "category": "行情 / 财务 / 新闻公告 / 宏观",
        "authoritative": True,
        "default_enabled": False,
        "fields": [
            {"name": "mcp_key", "label": "MCP 密钥", "secret": True,
             "env": "IFIND_MCP_KEY",
             "placeholder": "https://mcp.51ifind.com/#/profile 个人中心获取"},
        ],
    },
    "wind": {
        "label": "万得 Wind",
        "kind": "datasource",
        "category": "专业财务 / 行情",
        "authoritative": True,
        "default_enabled": False,
        "fields": [
            {"name": "api_key", "label": "API Key", "secret": True,
             "env": "WIND_API_KEY", "placeholder": "ak_ 开头的 API Key（aifinmarket.wind.com.cn 获取）"},
        ],
    },
    "deepseek": {
        "label": "DeepSeek 大模型",
        "kind": "llm",
        "category": "事件抽取 / 财务分析",
        "authoritative": False,
        "default_enabled": True,
        "fields": [
            {"name": "api_key", "label": "API Key", "secret": True,
             "env": "DEEPSEEK_API_KEY", "placeholder": "sk-..."},
            {"name": "base_url", "label": "Base URL", "secret": False,
             "env": "DEEPSEEK_BASE_URL", "placeholder": "https://api.deepseek.com"},
            {"name": "model", "label": "模型名", "secret": False,
             "env": "DEEPSEEK_MODEL",
             "placeholder": "DeepSeek-V4.1-Flash（调用时自动映射为 API 名）"},
        ],
    },
}

# 自定义数据源的固定可配置字段（前端据此渲染）
CUSTOM_FIELDS = [
    {"name": "url", "label": "接口 URL", "secret": False,
     "placeholder": "支持 {name} 占位符，如 https://api.example.com/risk?company={name}"},
    {"name": "token", "label": "认证 Token", "secret": True,
     "placeholder": "可选，填入后以 Authorization: Bearer 携带"},
]


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------
def _default_config() -> dict:
    return {"sources": {}, "custom": {}}


def load_config() -> dict:
    """读取磁盘配置（不存在则返回默认）。坏文件会被隔离留存，不会被默认配置静默覆盖。"""
    with _LOCK:
        if os.path.exists(CONFIG_PATH):
            data = read_json(CONFIG_PATH, None)
            if isinstance(data, dict):
                data.setdefault("sources", {})
                data.setdefault("custom", {})
                return data
        return _default_config()


def save_config(data: dict) -> dict:
    """写回磁盘（原子替换）。"""
    with _LOCK:
        write_json(CONFIG_PATH, data)
        return data


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _credential_from_env(fields: list) -> dict:
    """从环境变量读内置源的凭据初始值。"""
    cred = {}
    from core.credential import real_secret
    for fd in fields:
        env = fd.get("env")
        if env:
            v = real_secret(os.environ.get(env))
            if v:
                cred[fd["name"]] = v
    return cred


def get_source_state(key: str) -> dict:
    """返回单个内置源的 {enabled, credential}（合并磁盘配置与默认值）。"""
    cfg = load_config()
    saved = (cfg["sources"].get(key) or {})
    meta = BUILTIN_SOURCES.get(key) or {}
    enabled = saved.get("enabled", meta.get("default_enabled", False))
    # 凭据：磁盘显式填的优先，否则读环境变量。
    # ⚠️ 只让**非空**的磁盘值覆盖环境变量。脱敏后的 source_config.json 里凭据是空串，
    #    若照单全收会拿空串覆盖 .env 里的真实凭据 → 该源被判为「凭据未就绪」而静默退出
    #    聚合（available_source_keys 会把它滤掉），对应维度零扣分、总分虚高。
    #    实测：线上 iFinD/Wind 因这一条从未参与过聚合，舆情维度恒为满分 100，
    #    科蓝线上 60.92 vs 本机（含 iFinD 新闻）59.61。
    cred = dict(_credential_from_env(meta.get("fields", [])))
    from core.credential import real_secret
    for _k, _v in (saved.get("credential") or {}).items():
        if real_secret(_v):
            cred[_k] = _v
    return {"enabled": bool(enabled), "credential": cred}


def is_enabled(key: str) -> bool:
    """某个内置源是否启用。"""
    if key in BUILTIN_SOURCES:
        return get_source_state(key)["enabled"]
    cfg = load_config()
    c = cfg["custom"].get(key) or {}
    return bool(c.get("enabled", True))


def get_credential(key: str, field: str):
    """取某个能力某字段的凭据值（自定义源也从 credential 读）。"""
    if key in BUILTIN_SOURCES:
        return get_source_state(key)["credential"].get(field)
    cfg = load_config()
    c = cfg["custom"].get(key) or {}
    return (c.get("credential") or {}).get(field)


def _has_credential(meta: dict, state: dict) -> bool:
    """判断某源"凭据是否就绪"（无字段的源视为就绪）。"""
    fields = meta.get("fields", [])
    if not fields:
        return True
    cred = state.get("credential") or {}
    from core.credential import real_secret
    # 占位符（your_xxx / <YOUR_KEY> / changeme）不算就绪：否则界面显示"凭据已填写"，
    # 实际一调就 401/未配置，用户会以为是网络问题。
    return any(real_secret(cred.get(fd["name"])) for fd in fields)


def unavailable_reasons() -> dict:
    """返回"启用但当前不可用"的数据源及原因（凭据缺失 / 适配器不可用）。

    用途：查询与扫描结果带出 `credential_gaps`，让"为什么分数只用了本地库"
    在界面上可解释，而不是让用户对着静默降级猜。只列**启用了**的源——
    被显式关掉的源不算缺口。
    """
    from core.adapter import DATA_SOURCE_REGISTRY
    import sys as _sys
    out = {}
    for key, meta in BUILTIN_SOURCES.items():
        if meta.get("kind") != "datasource":
            continue
        if key not in DATA_SOURCE_REGISTRY:
            continue
        st = get_source_state(key)
        if not st["enabled"]:
            continue
        # 判定顺序要与 available_source_keys 一致：凭据 → 适配器。
        # 先看凭据的原因：部分适配器（历史实现）is_available() 恒为 True，
        # 仅凭它判断会把"没配凭据"误报成"可用"。
        if not _has_credential(meta, st):
            missing = [fd["label"] for fd in meta.get("fields", [])
                       if not (st["credential"] or {}).get(fd["name"])]
            out[key] = {
                "label": meta["label"],
                "reason": "凭据未配置" + (f"：{ '、'.join(missing) }" if missing else ""),
            }
            continue
        try:
            avail = DATA_SOURCE_REGISTRY[key]().is_available()
        except Exception:
            _log_swallow("core/datasource_config.py:unavailable_reasons/%s" % key)
            out[key] = {"label": meta["label"], "reason": "适配器自检异常"}
            continue
        if avail:
            continue
        # 凭据字段就绪但适配器仍报不可用：ifind/wind 这类把密钥放在**环境变量**里的源，
        # 其适配器 is_configured() 直接读 env。此时说清"环境变量未配置"，而不是笼统的
        # "适配器不可用"——后者会让人以为代码有问题。
        reason = "适配器当前不可用"
        try:
            mod = _sys.modules.get(type(DATA_SOURCE_REGISTRY[key]()).__module__)
            cfg = getattr(mod, "is_configured", None)
            if callable(cfg) and not cfg():
                reason = "凭据未配置（环境变量未设置）"
        except Exception:
            _log_swallow("core/datasource_config.py:cfg_probe/%s" % key)
        out[key] = {"label": meta["label"], "reason": reason}
    return out


def available_source_keys() -> list:
    """返回当前"真正可用"的数据源 key 列表（供聚合层自动选择）。

    可用 = 启用 且 凭据就绪 且 适配器已注册（占位适配器 is_available 返回 False）。
    """
    from core.adapter import DATA_SOURCE_REGISTRY
    out = []
    for key, meta in BUILTIN_SOURCES.items():
        if meta.get("kind") != "datasource":
            continue
        if key not in DATA_SOURCE_REGISTRY:
            continue
        st = get_source_state(key)
        if not st["enabled"]:
            continue
        if not _has_credential(meta, st):
            continue
        try:
            if not DATA_SOURCE_REGISTRY[key]().is_available():
                continue
        except Exception:
            _log_swallow("core/datasource_config.py:216")
            continue
        out.append(key)
    # 自定义源：启用且填了 url 即视为可用
    cfg = load_config()
    for ck, c in cfg["custom"].items():
        if c.get("enabled", True) and (c.get("url") or "").strip():
            out.append(ck)
    return out


# ---------------------------------------------------------------------------
# 清单（供前端展示）
# ---------------------------------------------------------------------------
def list_all() -> dict:
    """返回全部能力清单（凭据脱敏，供前端渲染管理界面）。"""
    builtin = []
    for key, meta in BUILTIN_SOURCES.items():
        st = get_source_state(key)
        fields_out = []
        for fd in meta.get("fields", []):
            from core.credential import real_secret
            has_val = bool(real_secret((st["credential"] or {}).get(fd["name"])))
            fields_out.append({
                "name": fd["name"], "label": fd["label"], "secret": fd["secret"],
                "has_value": has_val,
                "placeholder": fd.get("placeholder", ""),
                # 非敏感字段回显当前值，方便编辑
                "value": "" if fd["secret"] else (st["credential"] or {}).get(fd["name"], ""),
            })
        builtin.append({
            "key": key, "label": meta["label"], "kind": meta["kind"],
            "category": meta["category"], "authoritative": meta["authoritative"],
            "enabled": st["enabled"], "fields": fields_out,
            "credential_ready": _has_credential(meta, st),
        })

    cfg = load_config()
    custom = []
    for ck, c in cfg["custom"].items():
        custom.append({
            "key": ck, "label": c.get("label", ck), "kind": "datasource",
            "category": "自定义数据源", "authoritative": False,
            "enabled": bool(c.get("enabled", True)),
            "url": c.get("url", ""),
            "has_token": bool((c.get("credential") or {}).get("token")),
            "custom": True,
        })

    # 后端再补一个"当前实际参与查询的源"快照
    return {"builtin": builtin, "custom": custom,
            "active": available_source_keys()}


# ---------------------------------------------------------------------------
# 修改
# ---------------------------------------------------------------------------
def _apply_env(key: str, credential: dict):
    """把某源的凭据回写 os.environ，让现有代码（qcc_mcp/deepseek）即时生效。"""
    meta = BUILTIN_SOURCES.get(key) or {}
    for fd in meta.get("fields", []):
        env = fd.get("env")
        if not env:
            continue
        v = (credential or {}).get(fd["name"])
        if v:                      # 显式填了才覆盖，否则不动环境变量
            os.environ[env] = str(v)


def update_source(key: str, enabled=None, credential: dict = None) -> dict:
    """更新内置源（开关 / 凭据）。返回最新状态。"""
    if key not in BUILTIN_SOURCES:
        raise ValueError(f"未知内置能力：{key}")
    cfg = load_config()
    entry = cfg["sources"].setdefault(key, {})
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if credential is not None:
        # 只写入非空值（留空 = 保留原值，便于"不改密钥"的编辑场景）
        cred = dict(entry.get("credential") or {})
        for k, v in credential.items():
            if v not in (None, ""):
                cred[k] = str(v)
        entry["credential"] = cred
        _apply_env(key, cred)
    save_config(cfg)
    return get_source_state(key)


def add_custom(label: str, url: str, token: str = "", enabled: bool = True) -> dict:
    """新增自定义数据源。返回 {key, ...}。"""
    label = (label or "").strip() or "自定义数据源"
    url = (url or "").strip()
    if not url:
        raise ValueError("自定义数据源的接口 URL 不能为空")
    # 生成唯一 key：custom_<时间戳>_<序号>
    import time
    cfg = load_config()
    key = f"custom_{int(time.time())}"
    cfg["custom"][key] = {
        "label": label, "url": url, "enabled": bool(enabled),
        "credential": {"token": token} if token else {},
    }
    save_config(cfg)
    return {"key": key, "label": label, "url": url, "enabled": bool(enabled)}


def update_custom(key: str, enabled=None, label: str = None,
                  url: str = None, token: str = None) -> dict:
    """更新自定义数据源。"""
    cfg = load_config()
    if key not in cfg["custom"]:
        raise ValueError(f"未知自定义数据源：{key}")
    c = cfg["custom"][key]
    if enabled is not None:
        c["enabled"] = bool(enabled)
    if label is not None and label.strip():
        c["label"] = label.strip()
    if url is not None and url.strip():
        c["url"] = url.strip()
    if token is not None and token.strip():
        c.setdefault("credential", {})["token"] = token.strip()
    save_config(cfg)
    return c


def remove_custom(key: str) -> bool:
    """删除自定义数据源。"""
    cfg = load_config()
    if key in cfg["custom"]:
        del cfg["custom"][key]
        save_config(cfg)
        return True
    return False


def apply_env_on_startup():
    """启动时调用：把磁盘里显式配置的凭据回写环境变量（覆盖 .env 默认值）。

    应在 server.py 加载 .env 之后调用，保证"网页配置 > .env > 默认值"的优先级。
    """
    cfg = load_config()
    for key in BUILTIN_SOURCES:
        entry = cfg["sources"].get(key) or {}
        cred = entry.get("credential") or {}
        if cred:
            _apply_env(key, cred)
