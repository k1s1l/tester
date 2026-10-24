from aiogram import Router

from app.handlers import client, common, order
from app.handlers.admin import build_admin_router


def build_root_router() -> Router:
    """Order matters: admin (filtered by ADMIN_IDS) -> common commands/guards -> checkout FSM -> free dialog."""
    root = Router(name="root")
    root.include_routers(build_admin_router(), common.router, order.router, client.router)
    return root
