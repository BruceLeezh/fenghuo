"""JSON 落盘的唯一入口：原子写 + 坏文件留证。

背景：原来各模块直接 `open(path, "w")` 覆盖写。一旦进程在写一半时被杀（或两个进程同时写），
文件会留下半截 JSON；而读取端统一 `except: 返回默认值`，于是"数据全没了"被静默吞掉，
下一次保存再把默认值写回去，原始数据永久丢失。这里把两件事收在一处：

- `write_json`：同目录临时文件 + fsync + `os.replace`，替换是原子的，读者永远只会看到旧版或新版。
- `read_json`：解码失败时把坏文件改名成 `<name>.corrupt-<时间戳>` 留存，再返回默认值。
"""

import json
import os
import sys
import threading
import time

# 允许直接 `python core/jsonstore.py` 跑自检（与其它 core 模块一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.quiet_log import log_swallow as _log_swallow


def write_json(path, data, indent=2):
    """原子写：先写同目录临时文件，再 os.replace 覆盖。失败不留半截文件。"""
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    # 临时名要同时区分进程与线程：只用 pid 时，同进程两个线程写同一路径会撞名，
    # 一个线程 unlink 掉另一个正在写的临时文件，落盘内容可能被截断。
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                _log_swallow("core/jsonstore.py:32")
    return data


def read_json(path, default):
    """读 JSON。文件不存在返回 default；内容损坏则隔离原文件后返回 default。

    ⚠️ 只对「内容解码失败」做隔离：`OSError`（Windows 下文件被其它进程独占、
    句柄耗尽、权限不足等）是**读取环境**的问题，文件本身可能是完好的。
    早期实现把 OSError 也当损坏隔离，会把一份正常台账改名成 .corrupt-*，
    下一次保存再把默认值写回去 —— 一次瞬时占用就造成真实数据丢失。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        quarantine(path)
        return default
    except OSError as e:
        _log_swallow("core/jsonstore.py:read_json/%s" % type(e).__name__)
        return default


def quarantine(path):
    """把损坏文件改名留存，返回新路径（改不动就返回 None）。"""
    if not os.path.exists(path):
        return None
    dst = "%s.corrupt-%s" % (path, time.strftime("%Y%m%d%H%M%S"))
    n = 1
    while os.path.exists(dst):
        dst = "%s.corrupt-%s-%d" % (path, time.strftime("%Y%m%d%H%M%S"), n)
        n += 1
    try:
        os.replace(path, dst)
        return dst
    except OSError:
        return None


def _demo():
    import tempfile

    d = tempfile.mkdtemp(prefix="jsonstore-")
    p = os.path.join(d, "a.json")

    # 1. 往返
    write_json(p, {"k": [1, 2, 3]})
    assert read_json(p, None) == {"k": [1, 2, 3]}

    # 2. 原子写不残留临时文件
    leftovers = [n for n in os.listdir(d) if ".tmp." in n]
    assert leftovers == [], leftovers

    # 3. 覆盖写后只看到新内容
    write_json(p, {"k": []})
    assert read_json(p, None) == {"k": []}

    # 4. 坏文件被隔离且原字节留存
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"k": [1, 2')
    got = read_json(p, "FALLBACK")
    assert got == "FALLBACK", got
    quarantined = [n for n in os.listdir(d) if ".corrupt-" in n]
    assert len(quarantined) == 1, quarantined
    assert open(os.path.join(d, quarantined[0]), encoding="utf-8").read() == '{"k": [1, 2'

    # 5. 文件不存在返回默认值，不抛
    assert read_json(os.path.join(d, "nope.json"), []) == []

    print("jsonstore 自检通过：%s" % d)


if __name__ == "__main__":
    _demo()
