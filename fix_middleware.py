import re

with open("app/middleware/sanitization.py", "r") as f:
    content = f.read()

new_func = """def _redact_value(value: Any, key: str | None = None) -> Any:
    \"\"\"Recursively redact PII from a value (string, dict, list, or primitive).\"\"\"
    if key and isinstance(key, str) and re.search(r"amount|balance|payment|charge|fee|total", key, re.IGNORECASE):
        if isinstance(value, (int, float, str)):
            return "[REDACTED_AMOUNT]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value
"""

# Replace the existing _redact_value function
content = re.sub(
    r"def _redact_value\(value: Any\) -> Any:.*?return value",
    new_func,
    content,
    flags=re.DOTALL
)

with open("app/middleware/sanitization.py", "w") as f:
    f.write(content)
