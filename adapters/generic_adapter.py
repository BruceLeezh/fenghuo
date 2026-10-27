"""自定义数据源通用适配器 —— 让使用者接入任意额外 HTTP 数据源。

设计意图
--------
除了内置数据源，使用者在「数据源管理」页可新增"自定义数据源"：
填一个接口 URL（支持 {name} 占位符）+ 可选认证 Token，
系统即用本适配器按通用规则调用，并把返回尽力映射进统一 schema，
从而进入多源聚合与交叉验证。

映射策略（尽力而为）
--------------------
1. 返回 JSON 若直接含标准字段（name/registration/financials/...），优先解析；
2. 否则按常见字段名（中英文）映射到 registration；
3. 至少返回 name + 来源标注，保证该源在"数据源覆盖"面板可见。
"""
from core.quiet_log import log_swallow as _log_swallow
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.schemas import SupplierProfile, SourceInfo
from core.adapter import DataSourceAdapter, register

# RFC 6598 运营商级 NAT 保留段。ipaddress 的 is_private 对它返回 False，
# 必须单独列一张网表，否则 http://100.64.0.1/ 会被判为"公网"放行。
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _assert_public_url(url: str) -> None:
    """SSRF 防护：拒绝非 http(s) 协议与指向内网/回环/链路本地的地址。

    自定义数据源的 URL 由使用者在「数据源管理」页填写，属**用户可控输入**。
    若不校验，可填 `http://169.254.169.254/latest/meta-data/`（云元数据）、
    `http://127.0.0.1:8000/...`（本机其他服务）或 `file:///etc/passwd`，
    借服务端发起请求探测内网。
    """
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError(f"自定义数据源仅支持 http/https，收到协议：{p.scheme or '(空)'}")
    host = p.hostname
    if not host:
        raise ValueError("自定义数据源 URL 缺少主机名")

    # 解析成 IP 后逐个校验（一个域名可能解析到多个地址）
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"自定义数据源域名无法解析：{host}（{e}）")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"自定义数据源地址指向内网/保留地址，已拒绝：{host} → {ip}")
        # ⚠️ 100.64.0.0/10（RFC 6598 运营商级 NAT）在 Python 的 ipaddress 里
        #    **不**命中 is_private / is_reserved（实测 100.64.0.1 直接放行）。
        #    云上这张段常被网关/元数据代理占用，属于必须显式排除的例外。
        if ip.version == 4 and ip in _CGNAT_NET:
            raise ValueError(f"自定义数据源地址指向运营商级 NAT 网段，已拒绝：{host} → {ip}")

class _PublicOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """SSRF 跳转防护：每一跳目标都重跑 _assert_public_url，最多跟随 3 次。

    urlopen 默认最多跟 10 跳且**不复查跳转目标**，公网 URL 302 到
    127.0.0.1 / 169.254.169.254 即可绕过上面的首跳校验。
    urllib 自带的 max_redirections 只数"不同 URL"的个数，A→B→A→B 这种
    循环绕不过它的计数，故额外在 Request 上记跳数，硬上限 3 跳。
    """

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(req, "_ssrf_hops", 0) + 1
        if hops > self.max_redirections:
            raise ValueError(f"重定向超过 {self.max_redirections} 次，已中止：{newurl}")
        # 这里拿到的已是 urljoin 后的绝对地址；拒绝即 raise，请求在此中断
        _assert_public_url(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new._ssrf_hops = hops
        return new


# 常见字段名 → registration 字段
_REG_FIELD_ALIASES = {
    "credit_code": ["credit_code", "uscc", "creditCode", "统一社会信用代码"],
    "legal_person": ["legal_person", "legalPerson", "法定代表人", "法人"],
    "registered_capital": ["registered_capital", "registeredCapital", "注册资本"],
    "industry": ["industry", "行业", "国标行业"],
    "region": ["region", "地区", "所属地区"],
    "status": ["status", "登记状态", "经营状态"],
    "established": ["established", "成立日期", "establish_date"],
}


@register("__generic__")
class GenericAdapter(DataSourceAdapter):
    """自定义数据源通用适配器（由聚合层按 custom key 实例化）。"""

    def __init__(self, custom_key: str = None):
        self.custom_key = custom_key

    def is_available(self) -> bool:
        return True

    def source_info(self) -> dict:
        return {"label": self.custom_key or "自定义数据源", "category": "自定义数据源",
                "authoritative": False}

    def fetch_supplier(self, name: str) -> SupplierProfile:
        from core import datasource_config as dsc
        cfg = dsc.load_config().get("custom", {}).get(self.custom_key)
        if not cfg:
            raise ValueError(f"自定义数据源 {self.custom_key} 不存在")
        url = (cfg.get("url") or "").strip()
        label = cfg.get("label") or self.custom_key
        token = (cfg.get("credential") or {}).get("token") or ""

        # 支持 {name} 占位符
        target = url.replace("{name}", urllib.parse.quote(name or ""))
        # SSRF 防护：校验协议与目标地址，拒绝内网/回环/链路本地
        _assert_public_url(target)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = "Bearer " + token

        try:
            req = urllib.request.Request(target, headers=headers)
            opener = urllib.request.build_opener(_PublicOnlyRedirectHandler())
            with opener.open(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", "ignore")
            data = json.loads(body)
        except urllib.error.HTTPError as e:
            raise ValueError(f"自定义数据源 {label} 返回 HTTP {e.code}")
        except ValueError:
            # 上面 _assert_public_url 之外，json 解析失败也走这里，保持原样抛出
            raise
        except Exception as e:
            raise ValueError(f"自定义数据源 {label} 调用失败：{e}")

        return self._map(data, name, label)

    def _map(self, data, name: str, label: str) -> SupplierProfile:
        """把未知 JSON 尽力映射成 SupplierProfile。"""
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = {"_raw": data}

        # 1) 若返回结构直接贴近标准字段，优先解析
        real_name = (data.get("name") or data.get("company")
                     or data.get("企业名称") or name)

        registration = {}
        for field, aliases in _REG_FIELD_ALIASES.items():
            for a in aliases:
                v = data.get(a)
                if v not in (None, ""):
                    registration[field] = v
                    break

        # 2) 记录返回字段数（便于前端说明"该源返回了什么"），不塞完整 JSON 以免刷屏
        registration.setdefault("_data_source", f"{label}(自定义)")
        registration["_custom_summary"] = f"返回 {len(data)} 个字段"

        # 3) 若返回里带 legal_events / financials 等标准键，透传（尽力而为）
        from core.schemas import LegalEvent, FinancialMetrics
        legal = []
        raw_events = data.get("legal_events") or data.get("风险事件")
        if isinstance(raw_events, list):
            for e in raw_events:
                if isinstance(e, dict) and e.get("title"):
                    legal.append(LegalEvent(
                        title=str(e.get("title")),
                        severity=e.get("severity") or "medium",
                        source=label,
                        evidence=str(e.get("evidence") or e.get("detail") or ""),
                    ))
        fin = None
        raw_fin = data.get("financials")
        if isinstance(raw_fin, dict):
            try:
                fin = FinancialMetrics(**{
                    k: raw_fin[k] for k in raw_fin
                    if k in FinancialMetrics.__dataclass_fields__
                })
            except Exception:
                fin = None

        return SupplierProfile(
            name=str(real_name),
            legal_events=legal,
            financials=fin,
            registration=registration,
            sources=[self.custom_key],
            source_meta={self.custom_key: SourceInfo(
                key=self.custom_key, label=label, category="自定义数据源",
                authoritative=False)},
        )


if __name__ == "__main__":
    # 自检：跳转目标必须逐跳复检（内网/元数据地址在此被拦下）
    h = _PublicOnlyRedirectHandler()
    assert h.max_redirections == 3, h.max_redirections
    for bad in ("http://127.0.0.1:8000/admin", "http://169.254.169.254/latest/meta-data/",
                "file:///etc/passwd"):
        req = urllib.request.Request("http://example.com/")
        try:
            h.redirect_request(req, None, 302, "Found", {}, bad)
        except ValueError:
            _log_swallow("adapters/generic_adapter.py:213")
            continue
        raise AssertionError(f"跳转目标未被拦截：{bad}")
    print("OK: 跳转目标逐跳复检、最多 3 跳")
