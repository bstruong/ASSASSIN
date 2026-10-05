with open('tests/middleware/test_sanitization.py', 'r') as f:
    code = f.read()

# Just replace `from fastapi import FastAPI, Request` inside the fixture with global import
code = "from fastapi import FastAPI, Request\n" + code
with open('tests/middleware/test_sanitization.py', 'w') as f:
    f.write(code)
