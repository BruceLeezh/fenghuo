"""网络守卫 —— 让「不出网」从流程约定变成可按代码验证的技术约束。

为什么需要这个模块
------------------
涉密通道承诺「不出网」。但只靠"这条代码路径恰好没写网络调用"是不够的：
同一个进程里，MCP 客户端（企查查 / iFinD / Wind）、行情适配器都具备完整的
出网能力。一次无意的改动、一次顺手的函数复用，就可能把敏感文本送出去，
而且**不会报错、不会告警、没有任何痕迹**。

本模块提供 `no_network()` 上下文管理器：在守卫作用域内，任何试图建立网络
连接的调用都会**立即抛错**（fail-closed），而不是静默放行。

⚠️ 作用域是**线程本地**（2026-10-24 修正）
----------------------------------------
早期实现是进程级开关：`no_network()` 生效期间，全进程所有线程的出网都被拦住。
而涉密解析被放进线程池执行（解析是 CPU/IO 重活，不能占事件循环），于是
"上传一个大 PDF"会把同一进程里并发的公开查询、iFinD/Wind 取数一起掐断 ——
安全守卫变成了可用性缺陷。

现在：monkeypatch 仍是进程级安装（socket 入口只有一份），但**是否拦截由当前线程的
守卫深度决定** —— 守卫内的线程抛错，守卫外的线程照常调用原函数。
配套约束：`with no_network()` 必须写在**真正执行解析的那个线程**里（见
server.py::_parse_with_guard），套在 `await run_in_threadpool(...)` 外层是无效的。

为什么拦在 socket 层
--------------------
`socket.connect` 是所有 HTTP 客户端的公共汇合点——不管上层是 urllib、
httpx 还是 openai SDK，最终都要落到 `socket.socket.connect`。
拦在这一层，等于用一处改动覆盖全部出网路径，不依赖对上层库的逐个枚举。
同时拦 `getaddrinfo`，避免连"解析域名"这一步都不必要地发生。

用法
----
    from security.netguard import no_network, NetworkBlockedError

    def parse_in_worker(path):
        with no_network():                 # 必须在工作线程里进入
            return parse_supplier_file(path)


自测
----
    python security/netguard.py
"""
import socket
import threading

__all__ = ["no_network", "NetworkBlockedError", "is_active", "blocked_count"]


class NetworkBlockedError(RuntimeError):
    """守卫生效期间试图建立网络连接。

    这不是"网络故障"，而是**主动拒绝**：调用方不该重试，而应检查
    自己是否在不该出网的路径上调用了网络。
    """


# 被替换掉的原函数（安装守卫时保存，卸载时还原）
# name -> (持有者对象, 属性名, 原可调用对象)
_ORIGINALS = {}
_INSTALLED = False

# 进程内「正在守卫」的线程数：只有 0→1 时安装、1→0 时卸载。
# 每个线程自己的嵌套深度在 _TLS 里，见 _depth()。
_ACTIVE = 0
_LOCK = threading.RLock()
_TLS = threading.local()

# 累计拦截次数（供自检与演示取证使用）
_BLOCKED = 0

# 需要拦截的 socket 入口
_TARGETS = (
    ("socket.socket.connect", None),      # 实例方法，需绑到 socket 类上
    ("socket.socket.connect_ex", None),
    ("socket.create_connection", None),
    ("socket.getaddrinfo", None),
)


def _depth() -> int:
    """当前线程的守卫嵌套深度（0 = 未在守卫内）。"""
    return getattr(_TLS, "depth", 0)


def _resolve(name):
    """把 "socket.socket.connect" 解析成 (持有者对象, 属性名)。"""
    parts = name.split(".")
    obj = __import__(parts[0])
    for p in parts[1:-1]:
        obj = getattr(obj, p)
    return obj, parts[-1]


def _make_blocker(name, orig):
    """生成替换函数：本线程在守卫内 → 抛错；否则原样调用原实现。

    `orig` 在安装时以闭包捕获，不在调用时查 _ORIGINALS ——
    避免"另一线程正好卸载守卫、字典被清空"导致的 KeyError。
    """
    def _guarded(*args, **kwargs):
        if _depth() > 0:
            global _BLOCKED
            # 计数是安全事件指标，多线程下无锁自增会丢计数、低报真实拦截次数
            with _LOCK:
                _BLOCKED += 1
            raise NetworkBlockedError(
                "涉密通道禁止联网：已在网络守卫作用域内拦截 %s 调用。" % name
            )
        return orig(*args, **kwargs)
    _guarded.__name__ = "guarded_" + name.replace(".", "_")
    return _guarded


def _install():
    global _INSTALLED
    if _INSTALLED:
        return
    for name, _ in _TARGETS:
        obj, attr = _resolve(name)
        orig = getattr(obj, attr)
        _ORIGINALS[name] = (obj, attr, orig)
        setattr(obj, attr, _make_blocker(name, orig))
    _INSTALLED = True


def _uninstall():
    global _INSTALLED
    if not _INSTALLED:
        return
    # 逆序还原，避免中途异常留下半装状态
    for name in reversed(list(_ORIGINALS)):
        obj, attr, orig = _ORIGINALS[name]
        setattr(obj, attr, orig)
    _ORIGINALS.clear()
    _INSTALLED = False


class _NetworkGuard:
    """`no_network()` 的返回值；支持 with 语句与嵌套，作用域为当前线程。"""

    def __enter__(self):
        global _ACTIVE
        with _LOCK:
            if _ACTIVE == 0:
                _install()
            _ACTIVE += 1
        _TLS.depth = _depth() + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        global _ACTIVE
        with _LOCK:
            if getattr(_TLS, "depth", 0) > 0:
                _TLS.depth -= 1
            if _ACTIVE > 0:
                _ACTIVE -= 1
                if _ACTIVE == 0:
                    _uninstall()
        return False   # 不吞异常


def no_network():
    """返回网络守卫上下文管理器。可重入、可嵌套。"""
    return _NetworkGuard()


def is_active() -> bool:
    """守卫当前是否生效（供上游做契约断言）。"""
    return _INSTALLED


def blocked_count() -> int:
    """本次进程内累计拦截次数。"""
    return _BLOCKED


if __name__ == "__main__":
    # —— 自测：守卫内出网必须失败，守卫外必须恢复，且不影响其它线程 ——
    import socket
    import urllib.request

    def _try(url):
        try:
            urllib.request.urlopen(url, timeout=2)
            return "放行"
        except NetworkBlockedError as e:
            return "拦截（%s）" % type(e).__name__
        except Exception as e:                          # noqa: BLE001
            return "其他异常（%s）" % type(e).__name__

    print("守卫外 访问 example.com :", _try("http://example.com"))
    with no_network():
        print("守卫内 访问 example.com :", _try("http://example.com"))
        with no_network():
            print("嵌套内 访问 example.com :", _try("http://example.com"))
        print("  嵌套退出后 is_active  :", is_active())

        # 线程隔离：守卫期间别的线程不应被牵连（否则"上传文件"会掐断并发查询）
        seen = {}

        def _other_thread():
            try:
                socket.create_connection(("127.0.0.1", 9), timeout=1).close()
                seen["r"] = "放行"
            except NetworkBlockedError:
                seen["r"] = "被拦截（错误：守卫泄漏到其它线程）"
            except Exception as e:                      # noqa: BLE001
                seen["r"] = "放行（%s）" % type(e).__name__

        t = threading.Thread(target=_other_thread)
        t.start()
        t.join()
        print("守卫期间其它线程建连:", seen.get("r"))
    print("守卫后 访问 example.com :", _try("http://example.com"))
    print("累计拦截次数            :", blocked_count())
