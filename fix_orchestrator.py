with open("app/routers/orchestrator.py", "r") as f:
    code = f.read()

replacement = """        for table_name in schema_info.get("tables", []):
            try:
                table_details = get_schema_info(table_name=table_name)
                tables.append(table_details)
            except CloudToolError:
                pass"""

code = code.replace(
    """        for table_name in schema_info.get("tables", []):
            table_details = get_schema_info(table_name=table_name)
            tables.append(table_details)""",
    replacement,
)

with open("app/routers/orchestrator.py", "w") as f:
    f.write(code)
