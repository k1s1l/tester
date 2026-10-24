from aiogram import Router

from app.filters.admin import IsAdmin
from app.handlers.admin import orders, panel, products


def build_admin_router() -> Router:
    router = Router(name="admin")
    # Every admin handler is reachable only for ADMIN_IDS; others fall through to the guard in common.py.
    router.message.filter(IsAdmin())
    router.callback_query.filter(IsAdmin())
    router.include_routers(panel.router, orders.router, products.router)
    return router
