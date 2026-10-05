import os

from app.mcp.cloud_tools import _ALLOWED_TABLES, get_schema_info

os.environ["DATABASE_URL"] = "postgresql://postgres:assassin@localhost:54329/assassin_test"

print("ALLOWED:", _ALLOWED_TABLES)
print("TABLES:", get_schema_info().get("tables"))
