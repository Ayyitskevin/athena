"""A reference Icarus executor — the other side of Athena's dispatch contract.

Athena is a control plane: it hands work OUT (`POST {executor}/dispatch`, an
HMAC-signed envelope) and hears back through a signed, idempotent callback
(`POST {athena}/callbacks/icarus`). This is the smallest honest counterparty to
that contract, in one file, so an operator can watch the whole loop run on
their own machine and an integrator can read exactly what each side must do.

**It deliberately imports nothing from Athena.** The executor is a separate
system: the two share a secret and a wire format, never code or a database. It
uses only the Python standard library, so it runs anywhere Python runs:

    ATHENA_URL=http://127.0.0.1:8000 \
    EXECUTOR_SECRET=change-me \
    EXECUTOR_PORT=8443 \
    python examples/icarus_executor.py

Point Athena at it with:

    ATHENA_ICARUS_URL=http://127.0.0.1:8443
    ATHENA_ICARUS_SECRET=change-me            # the SAME shared secret
    ATHENA_EGRESS_PRIVATE_HOSTS=127.0.0.1     # loopback delivery is opt-in

What a real executor must get right, all shown here:

* **Verify the signature before trusting a byte.** `X-Athena-Signature` is
  `sha256=<hex HMAC>` over the exact request body with the shared secret,
  compared in constant time. An unsigned or mis-signed dispatch is refused.
* **Answer acceptance with your own run id.** The `icarus_run_id` in the 200
  body is the executor's claim about itself; Athena stores it to correlate
  callbacks and never treats it as proof of anything.
* **Echo the policy digest back verbatim.** The callback's `policy_digest` is
  Athena's tamper-evidence: report the digest from the envelope you actually
  acted on. (Send a different one and Athena records a mismatch rather than
  discarding your report — try it.)
* **Sign your callbacks the same way.** Same secret, same `sha256=<hex>` shape,
  over the exact callback body.
* **Expect retries, cause no forks.** Athena's callback endpoint is idempotent;
  the `Idempotency-Key` header on the dispatch is the shared single-flight key.

This reference "works" by immediately reporting evidence and then a completed
outcome — the executor equivalent of a hello-world. A real fleet would enqueue
the envelope, do the work, and call back when there is something true to say.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, Request, build_opener

ATHENA_URL = os.environ.get("ATHENA_URL", "http://127.0.0.1:8000").rstrip("/")
SECRET = os.environ.get("EXECUTOR_SECRET", "")
PORT = int(os.environ.get("EXECUTOR_PORT", "8443"))
BIND = os.environ.get("EXECUTOR_BIND", "127.0.0.1")
# A real executor works asynchronously; this one reports back after a token
# delay so the operator can watch a dispatch sit briefly in `accepted`.
CALLBACK_DELAY_SECONDS = float(os.environ.get("EXECUTOR_CALLBACK_DELAY", "0.2"))


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _verified(headers, body: bytes) -> bool:
    provided = headers.get("X-Athena-Signature", "")
    return bool(provided) and hmac.compare_digest(provided, _sign(body))


# Callbacks go straight to the operator-configured Athena URL, never through an
# ambient HTTP(S)_PROXY — mirroring Athena's own delivery poster, which ignores
# proxy variables on purpose. A control-plane callback detouring through a proxy
# nobody chose is a misconfiguration hazard, and on a loopback/tailnet deployment
# it simply breaks.
_OPENER = build_opener(ProxyHandler({}))

# The contract says callbacks are idempotent and executors retry. This reference
# retries a few times with a short backoff — a one-shot callback would lose a
# report to any transient failure, including the benign race where the callback
# lands before Athena finishes recording the acceptance it is a callback to.
CALLBACK_ATTEMPTS = 5
CALLBACK_BACKOFF_SECONDS = 0.5


def _call_back(payload: dict) -> tuple[int, str]:
    body = json.dumps(payload, separators=(",", ":")).encode()
    last_error = ""
    for attempt in range(1, CALLBACK_ATTEMPTS + 1):
        request = Request(
            f"{ATHENA_URL}/callbacks/icarus",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Athena-Signature": _sign(body),
            },
            method="POST",
        )
        try:
            with _OPENER.open(request, timeout=10) as response:  # noqa: S310
                return response.status, response.read().decode()
        except OSError as exc:  # HTTPError is an OSError; so are socket failures
            last_error = str(exc)
            if attempt < CALLBACK_ATTEMPTS:
                time.sleep(CALLBACK_BACKOFF_SECONDS * attempt)
    return 0, f"gave up after {CALLBACK_ATTEMPTS} attempts: {last_error}"


def _do_the_work(envelope: dict, run_id: str) -> None:
    """The pretend work, then the two callbacks a well-behaved executor sends.

    Everything reported is a CLAIM: Athena records what it is told and verifies
    nothing, which is exactly the contract — so a reference executor must not
    pretend its refs point anywhere real.
    """
    digest = envelope["policy_digest"]
    key = envelope["idempotency_key"]
    status, _ = _call_back(
        {
            "icarus_run_id": run_id,
            "policy_digest": digest,
            "evidence_ref": f"example://evidence/{key}",
        }
    )
    print(f"[executor] progress callback for {run_id}: HTTP {status}", flush=True)
    status, body = _call_back(
        {
            "icarus_run_id": run_id,
            "policy_digest": digest,
            "completion_ref": f"example://result/{key}",
            "outcome": "completed",
        }
    )
    print(
        f"[executor] terminal callback for {run_id}: HTTP {status} {body}", flush=True
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "IcarusReference/1.0"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        if self.path != "/dispatch":
            self._reply(404, {"error": "unknown path"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        # Signature FIRST, before parsing: unauthenticated bytes get no parser.
        if not _verified(self.headers, body):
            self._reply(401, {"error": "invalid signature"})
            return
        try:
            envelope = json.loads(body)
        except ValueError:
            self._reply(400, {"error": "malformed envelope"})
            return
        if envelope.get("schema") != "athena.icarus_dispatch.v1":
            self._reply(422, {"error": "unknown schema"})
            return

        run_id = f"icarus-ref-{envelope['idempotency_key']}"
        print(
            f"[executor] accepted dispatch {envelope['dispatch_id']} "
            f"({envelope['capability']} on {envelope['repo']}@"
            f"{envelope['base_commit']}) as {run_id}",
            flush=True,
        )
        timer = threading.Timer(
            CALLBACK_DELAY_SECONDS, _do_the_work, args=(envelope, run_id)
        )
        timer.daemon = True
        timer.start()
        self._reply(200, {"icarus_run_id": run_id})

    def log_message(self, *args) -> None:  # keep stdout for the transcript
        pass


def main() -> int:
    if not SECRET:
        raise SystemExit("EXECUTOR_SECRET is required (the shared dispatch secret)")
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    actual_port = server.server_address[1]
    print(
        f"[executor] reference Icarus executor on http://{BIND}:{actual_port}, "
        f"calling back to {ATHENA_URL}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
