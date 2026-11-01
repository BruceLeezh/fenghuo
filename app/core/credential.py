"""凭据有效性判定：把「占位符」当成「未配置」。

为什么需要单独一处
------------------
交付包里 `.env` 为了让人知道该填哪个变量，长期带着
`DEEPSEEK_API_KEY=your_deepseek_api_key` 这样的占位值。各模块只判断
`bool(token)`，就会把占位符当成真凭据：启动后真的去调用外部 API，
拿到 401/鉴权失败，而不会走"未配置 → 优雅降级"的路径。

表现上很迷惑：界面显示"已启用"，点自检却报 `AuthenticationError: api key ****_key is invalid`；
公开查询虽最终降级，但每次都要白等一次失败的 HTTP 往返（含超时风险）。

因此所有读凭据的地方统一过 `is_placeholder()`，占位即视为未配置。
"""
import re

__all__ = ["is_placeholder", "real_secret"]

# 常见的"示例值"形态；比较时忽略大小写与首尾空白
_PATTERNS = (
    re.compile(r"^your[_-].*", re.I),          # your_api_key / your_deepseek_api_key
    re.compile(r"^<.*>$"),                      # <YOUR_KEY>
    re.compile(r"^\$\{.*\}$"),                   # ${VAR}
    re.compile(r"^(xxx+|x{4,}|todo|tbd|none|null|n/a)$", re.I),
    re.compile(r"^changeme$", re.I),
    re.compile(r"^(sk-)?x{4,}.*$", re.I),
    re.compile(r"^placeholder$", re.I),
)


def is_placeholder(value) -> bool:
    """该值是否是占位符/示例值（应视作"未配置"）。

    刻意**不**按后缀（如 `_api_key`）判断：真实密钥偶尔也长这样，把真凭据误判成
    占位符等于功能静默关闭，比多报一次 401 更难排查。宁可只匹配明确的示例形态。
    """
    s = str(value or "").strip()
    if not s:
        return True
    low = s.lower()
    if any(p.match(low) for p in _PATTERNS):
        return True
    # "your_xxx" / "your-xxx" 这类组合（your 在开头之外的位置也常见）
    return bool(re.match(r"^your[-_].*", low))


def real_secret(value) -> str:
    """返回可用于真实调用的凭据；占位符一律返回空串。"""
    s = str(value or "").strip()
    return "" if is_placeholder(s) else s
