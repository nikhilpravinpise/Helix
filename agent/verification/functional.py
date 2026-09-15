"""Functional verification leg (subphase 1.9): prove the service answers.

The desired state for a recovered service is not "pods Ready" alone -
it is "the service does its job". The leg:

1. `kubectl port-forward svc/<service> :<remote_port>` - the current
   surface; the sandbox cluster has no ingress controller, so the service
   is reached by forward, not by ingress (the target moves to
   `payment-api.local` through ingress in subphase 1.13).
2. A Selenium browser loads `/docs` - the Swagger UI FastAPI generates.
   For an API-only service `/docs` is the honest browser surface: a real
   browser exercise that requires no changes to payment-api.
3. `httpx` POST `/payments` - the business round-trip end to end.

Leak-safety is the contract: the port-forward process is terminated on
every exit path - success, check failure, timeout, or exception - via
`port_forward`'s try/finally (terminate, then kill after a grace).

Selenium is a dev-only dependency, so the browser import is lazy inside
`check_swagger_docs`: environments without it get a recorded `error`
(never a silent pass), and fixture mode / `HELIX_VERIFY_FUNCTIONAL=false`
record `skipped` - a skipped check is never counted as passed.
"""

import re
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from queue import Empty, Queue

from agent.verification import CheckResult, CheckStatus, VerificationContext

#: How long the port-forward handshake may take before the leg errors.
_HANDSHAKE_TIMEOUT = 20.0
#: Seconds to wait for a graceful terminate before killing the process.
_TERMINATE_GRACE = 5.0
#: Browser/API timeouts - deliberately short; this is a probe, not a load test.
_PROBE_TIMEOUT = 30.0


class PortForwardError(RuntimeError):
    """The port-forward could not be established - a check error, not a verdict."""


def _read_forwarded_port(proc: subprocess.Popen, deadline: float) -> int:
    """Parse kubectl's `Forwarding from 127.0.0.1:PORT -> REMOTE` line on a
    reader thread (pipes are not select()able on Windows). Raises
    PortForwardError on process death, timeout, or unparseable output."""
    lines: Queue[str] = Queue()

    def _drain() -> None:
        if proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            lines.put(line)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    seen: list[str] = []
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise PortForwardError(
                f"kubectl port-forward exited rc={proc.returncode}: {''.join(seen)}"
            )
        try:
            line = lines.get(timeout=0.2)
        except Empty:
            continue
        seen.append(line)
        match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
        if match:
            return int(match.group(1))
    raise PortForwardError(f"no forwarding handshake within {deadline:.0f}s: {''.join(seen)}")


def _wait_for_tcp(host: str, port: int, deadline: float) -> None:
    """The handshake line can precede the listener; poll until connectable."""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise PortForwardError(f"127.0.0.1:{port} never accepted a connection")


@contextmanager
def port_forward(
    service: str,
    namespace: str,
    remote_port: int,
    *,
    context: str | None = None,
    popen=subprocess.Popen,
    handshake_timeout: float = _HANDSHAKE_TIMEOUT,
) -> Iterator[str]:
    """`kubectl port-forward svc/<service> :<remote_port>` yielding the
    local base URL (kubectl picks the port). The process is ALWAYS
    terminated on exit - check success, failure, or exception alike.

    `popen` is the test seam: unit tests inject a fake process emitting
    the handshake line, proving the cleanup path without a cluster.
    """
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += [
        "-n",
        namespace,
        "port-forward",
        f"svc/{service}",
        f":{remote_port}",  # empty local port -> kubectl allocates one
    ]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + handshake_timeout
    try:
        port = _read_forwarded_port(proc, deadline)
        _wait_for_tcp("127.0.0.1", port, deadline)
        yield f"http://127.0.0.1:{port}"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_TERMINATE_GRACE)


def check_swagger_docs(base_url: str) -> tuple[bool, str]:
    """Selenium loads `/docs` (the FastAPI Swagger UI) and waits for the
    swagger-ui DOM to render - the honest browser surface for an API-only
    service. ImportError propagates: `run` maps a missing browser/stack
    to `error`, never a silent pass."""
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)
    try:
        driver.get(f"{base_url}/docs")
        WebDriverWait(driver, _PROBE_TIMEOUT).until(
            EC.presence_of_element_located((By.CLASS_NAME, "swagger-ui"))
        )
        title = driver.title
        return True, f"swagger-ui rendered (title={title!r})"
    finally:
        driver.quit()


def check_payments(base_url: str) -> tuple[bool, str]:
    """POST /payments end to end - the business path, not just /health."""
    import httpx

    response = httpx.post(
        f"{base_url}/payments",
        json={
            "amount_cents": 100,
            "currency": "USD",
            "recipient": "helix-verification-probe",
        },
        timeout=_PROBE_TIMEOUT,
    )
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    ok = response.status_code == 201 and body.get("status") == "settled"
    return ok, f"POST /payments -> {response.status_code} status={body.get('status')!r}"


def run(ctx: VerificationContext) -> CheckResult:
    """The functional leg: skipped in fixture mode or when disabled;
    otherwise port-forward + browser + API probe. A probe that cannot run
    (no kubectl, no browser, dead cluster) records `error` - never a
    pass, never silently skipped."""
    name = "functional"
    if ctx.is_fixture:
        return CheckResult(
            name,
            CheckStatus.SKIPPED,
            "fixture mode: no live service to port-forward to - verifying "
            "against recorded state instead",
        )
    if not ctx.functional_enabled:
        return CheckResult(
            name,
            CheckStatus.SKIPPED,
            "functional leg disabled via HELIX_VERIFY_FUNCTIONAL",
        )
    if ctx.functional_probe is not None:
        return ctx.functional_probe(ctx)
    try:
        with port_forward(ctx.service, ctx.namespace, 8000) as base_url:
            docs_ok, docs_detail = check_swagger_docs(base_url)
            pay_ok, pay_detail = check_payments(base_url)
    except ImportError as exc:
        return CheckResult(name, CheckStatus.ERROR, f"functional probe could not run: {exc}")
    except (PortForwardError, OSError) as exc:
        return CheckResult(name, CheckStatus.ERROR, f"functional probe could not run: {exc}")
    except Exception as exc:  # httpx errors, selenium session failures...
        return CheckResult(name, CheckStatus.ERROR, f"{type(exc).__name__}: {exc}")
    detail = f"/docs: {docs_detail}; /payments: {pay_detail}"
    if docs_ok and pay_ok:
        return CheckResult(name, CheckStatus.PASS, detail)
    return CheckResult(name, CheckStatus.FAIL, detail)
