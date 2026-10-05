with open("app/middleware/sanitization.py", "r") as f:
    lines = f.readlines()

out = []
skip = False
for line in lines:
    if "async def receive():" in line:
        out.append("""        _received = False
        async def receive():
            nonlocal _received
            if not _received:
                _received = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return {'type': 'http.request', 'body': b'', 'more_body': False}
""")
        skip = True
    elif skip and "request._receive = receive" in line:
        out.append(line)
        skip = False
    elif not skip:
        out.append(line)

with open("app/middleware/sanitization.py", "w") as f:
    f.write("".join(out))
