import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_admin_env = os.getenv("ADMIN_ID")
ADMIN_ID = int(_admin_env) if _admin_env else None
