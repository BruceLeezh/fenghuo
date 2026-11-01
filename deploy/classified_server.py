"""涉密通道独立实例 —— 把涉密文件处理从公开查询实例里分出来。

为什么需要它
------------
涉密通道与公开查询如果跑在同一个进程、同一个端口上，"物理或逻辑隔离"
就无从谈起——两者的网络暴露面完全相同：能访问公开查询的地址，就能访问上传接口。

本模块启动一个**只注册涉密上传路由**的 FastAPI 实例，并且**只监听回环地址**：

| 维度 | 公开查询实例 (server.py) | 涉密通道实例 (本文件) |
|---|---|---|
| 默认端口 | 8000 | 8001 |
| 监听地址 | 0.0.0.0（对外提供服务） | **127.0.0.1（仅本机可连）** |
| 路由表 | 40+ 条，含企查查/iFinD/Wind 等出网接口 | **仅 /api/upload 一条** |
| 出网能力 | 正常 | 解析入口套网络守卫，出网即抛错 |

这样"公网查询和涉密通道的隔离"就落到两件可核对的事实上：
**端口不同、监听地址不同、路由表不同**。

用法
----
    python classified_server.py                      # 默认 127.0.0.1:8001
    set CLASSIFIED_PORT=9001 && python classified_server.py

配套（可选）：给公开查询实例设 CLASSIFIED_STANDALONE=1，它就不再注册 /api/upload，
涉密能力全量收拢到本实例；前端把 index.html 的 CLASSIFIED_API 指向本实例地址。

实现说明
--------
本模块**复用** server.py 里已经写好并回归过的上传路由，而不是另写一份：
路由对象自带 endpoint 与依赖，直接挂到新实例的路由表上即可，
避免"两份实现各自演化、行为分叉"这种更危险的维护方式。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import server as _main

# 只放行这一条路由。名单是白名单而非黑名单：将来 server.py 新增接口，
# 不会因为"忘了排除"而自动出现在涉密实例上。
CLASSIFIED_ROUTES = ("/api/upload",)

_DEFAULT_ORIGINS = [
    "http://127.0.0.1:8000", "http://localhost:8000",     # 公开查询实例（前端页面来源）
    "http://127.0.0.1:8001", "http://localhost:8001",     # 本实例直连
]


def build_app() -> FastAPI:
    """构造只含涉密路由的实例。"""
    app = FastAPI(title="烽火 · 涉密通道（独立实例）")

    moved = []
    for route in _main.app.routes:
        if getattr(route, "path", None) in CLASSIFIED_ROUTES:
            app.routes.append(route)
            moved.append(route.path)

    if not moved:
        raise RuntimeError(
            "未从主实例找到涉密路由 %s —— server.py 可能被改动过，"
            "涉密实例拒绝以空路由启动。" % (CLASSIFIED_ROUTES,)
        )

    _origins = [o.strip() for o in
                os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins or _DEFAULT_ORIGINS,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    app.state.moved_routes = moved
    return app


app = build_app()


if __name__ == "__main__":
    import uvicorn
    # ⚠️ 固定 127.0.0.1：涉密实例不接受来自本机以外的连接。
    #    这不是配置疏漏，而是本模块的设计目的——对外暴露的职责归公开查询实例。
    host = os.environ.get("CLASSIFIED_HOST", "127.0.0.1")
    port = int(os.environ.get("CLASSIFIED_PORT", "8001"))
    print("烽火 · 涉密通道独立实例")
    print("  监听：http://%s:%d" % (host, port))
    print("  路由：%s" % "、".join(app.state.moved_routes))
    print("  出网：解析入口已套网络守卫（出网即抛错）")
    uvicorn.run(app, host=host, port=port)
