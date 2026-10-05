
with open('app/middleware/sanitization.py', 'r') as f:
    code = f.read()

replacement = """        async def receive():
            return {"type": "http.request", "body": body}
        request._receive = receive"""

code = code.replace('request._receive = lambda: {"type": "http.request", "body": body}', replacement)

with open('app/middleware/sanitization.py', 'w') as f:
    f.write(code)
