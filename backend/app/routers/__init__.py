"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

跨模块固定路径必须排在资源路由之前，否则 /api/{module}/{entry_id}
会把 /api/cross/events 之类的固定路径当成 entry_id 抢先匹配。
"""
from __future__ import annotations

from app.routers import resource, cross_module

ROUTERS = [cross_module, resource]