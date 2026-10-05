with open('app/middleware/sanitization.py', 'r') as f:
    lines = f.readlines()

out = []
for line in lines:
    if line.strip() == "async def receive():":
        out.append("        async def receive():\n")
    elif line.strip() == "return {'type': 'http.request', 'body': body}":
        out.append("            return {'type': 'http.request', 'body': body, 'more_body': False}\n")
    else:
        out.append(line)

with open('app/middleware/sanitization.py', 'w') as f:
    f.write("".join(out))
