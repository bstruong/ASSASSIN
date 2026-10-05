from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from app.middleware.sanitization import PiiSanitizationMiddleware

app = FastAPI()

@app.post("/test")
async def test_endpoint(request: Request):
    body = await request.json()
    return {"received": body}

app.add_middleware(PiiSanitizationMiddleware)
client = TestClient(app)

response = client.post("/test", json={"ssn": "123-45-6789"}, headers={"content-type": "application/json"})
print(response.status_code)
print(response.json())
