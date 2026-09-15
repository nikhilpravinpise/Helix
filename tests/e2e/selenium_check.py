"""Integration tier (subphase 1.9): the real browser-based functional
check - the leg the unit tier stubs out.

A `kubectl port-forward` is opened to the payment-api service on the
kind sandbox, a real headless browser navigates it, and the endpoint's
rendered content is asserted - the "does the service actually work"
proof that pod readiness can never give. Also asserted: the webdriver
quits on both success and failure paths, and the port-forward process is
gone when the context manager exits (the leak the sandbox bled once).

Requirements to run: the kind sandbox up (scripts/up.sh), a Chrome or
Chromium binary on PATH for selenium-manager to drive, and
`uv run pytest -m integration`.
"""

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration

selenium = pytest.importorskip("selenium")
from selenium import webdriver  # noqa: E402
from selenium.common.exceptions import WebDriverException  # noqa: E402

from agent.verification.functional import port_forward  # noqa: E402

NAMESPACE = "payment-api"
SERVICE = "payment-api"
PORT = 8000
CONTEXT = "kind-helix-sandbox"


def _sandbox_up() -> bool:
    """kind cluster present AND the service deployed - the two things a
    missing sandbox lacks."""
    if not shutil.which("kubectl"):
        return False
    try:
        result = subprocess.run(
            [
                "kubectl",
                "--context",
                CONTEXT,
                "get",
                "svc",
                SERVICE,
                "-n",
                NAMESPACE,
            ],
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


requires_sandbox = pytest.mark.skipif(
    not _sandbox_up(), reason="kind sandbox (svc payment-api) is not up"
)


def _chrome_driver():
    """A headless Chrome driver, or skip the test when no browser/driver
    can be started (selenium-manager still needs a Chrome binary)."""
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    try:
        return webdriver.Chrome(options=options)
    except WebDriverException as exc:
        pytest.skip(f"no usable Chrome/driver for the functional check: {exc}")


@requires_sandbox
def test_selenium_health_check_through_port_forward():
    """The verification leg end to end: forward -> browser -> content.

    Asserts the health endpoint's rendered body over a real forward, that
    the webdriver's session is gone after quit, and that the
    port-forward process was reaped when the context manager exited."""
    driver = _chrome_driver()
    created: list = []

    def recording_popen(cmd, **kw):
        proc = subprocess.Popen(cmd, **kw)
        created.append(proc)
        return proc

    with port_forward(NAMESPACE, SERVICE, PORT, popen=recording_popen) as base_url:
        try:
            driver.get(f"{base_url}/health")
            assert "ok" in driver.page_source.lower()
        finally:
            driver.quit()
    assert driver.session_id is None  # the browser process is gone
    assert created[0].poll() is not None  # and so is kubectl port-forward


@requires_sandbox
def test_webdriver_quits_even_when_the_check_fails():
    """Quit-on-failure is the whole point of the finally: a browser left
    behind on a failed assert is a leaked process, exactly the class of
    bug this check exists to prevent."""
    driver = _chrome_driver()
    with port_forward(NAMESPACE, SERVICE, PORT) as base_url:
        try:
            driver.get(f"{base_url}/health")
            with pytest.raises(AssertionError):
                assert "never-present-marker" in driver.page_source
        finally:
            driver.quit()
    assert driver.session_id is None
