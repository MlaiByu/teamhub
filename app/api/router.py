"""api/router.py —— 汇总各领域路由（PROJECT-PLAN 6.3）。"""

from fastapi import APIRouter

from app.api.v1 import api_v1_router

api_router = APIRouter()
api_router.include_router(api_v1_router)
