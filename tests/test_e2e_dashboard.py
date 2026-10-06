import subprocess
import time

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture(scope="module")
def live_server():
    # Start the FastAPI server
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "app.api.app:create_app",
            "--factory",
            "--port",
            "8000",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the server to be ready
    import urllib.request
    from urllib.error import URLError

    timeout = 10
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            urllib.request.urlopen("http://localhost:8000/dashboard")
            break
        except URLError:
            time.sleep(0.5)

    yield "http://localhost:8000"

    proc.terminate()
    proc.wait()


def test_htmx_dom_swap(page: Page, live_server: str):
    page.goto(f"{live_server}/dashboard")

    # Check that htmx is loaded
    assert page.evaluate("window.htmx !== undefined")

    # Fill the form
    page.fill("#question", "Show me my spending")

    # Click analyze button
    analyze_btn = page.locator("button", has_text="Analyze")
    analyze_btn.click()

    # Wait for the DOM swap - look for analysis result
    result_div = page.locator(".analysis-result")
    expect(result_div).to_contain_text("Analysis Result", timeout=5000)
