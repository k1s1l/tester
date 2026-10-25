from aiogram import Router

from app.filters.admin import IsAdmin
from app.handlers.admin import channel, orders, panel, products


def build_admin_router() -> Router:
    router = Router(name="admin")
    # Every admin handler is reachable only for ADMIN_IDS; others fall through to the guard in common.py.
    router.message.filter(IsAdmin())
    router.callback_query.filter(IsAdmin())
    # channel posts are not messages, so the IsAdmin message filter does not apply to them;
    # posts are accepted only from CATALOG_CHANNEL_ID (or channels where the bot is an administrator)
    router.include_routers(panel.router, orders.router, products.router, channel.router)
    return router
