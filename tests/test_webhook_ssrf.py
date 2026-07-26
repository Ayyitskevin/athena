"""Webhook delivery is hardened against SSRF at delivery time, not just registration.

is_safe_url gates registration and each pass, but two holes remained in the actual
POST: the old urllib opener FOLLOWED 3xx redirects (a registered public URL could 302
to http://169.254.169.254/ or an internal service) and RE-RESOLVED DNS at connect time
(rebinding past the check). The hardened poster refuses redirects and pins the socket
to the exact IP it validated. These tests exercise the real poster (http.client), not
the injected stub used in test_webhooks.py.
"""

import http.server
import ipaddress
import threading

import pytest

from athena.core import webhooks


@pytest.fixture
def local_server():
    """A throwaway HTTP server on loopback that records the paths it is hit on and
    returns a scriptable status (so a test can make it answer 200 or a 302)."""
    state = {
        "status": 200,
        "location": "http://169.254.169.254/latest/meta-data/",
        "requests": [],
        "host_header": None,
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            state["requests"].append(self.path)
            state["host_header"] = self.headers.get("Host")
            self.send_response(state["status"])
            if 300 <= state["status"] < 400:
                self.send_header("Location", state["location"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):  # keep the test output clean
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, state
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_address_policy_blocks_internal_targets():
    # Literal internal IPs resolve to themselves, so no DNS is needed here.
    for bad in (
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "ftp://example.com/",  # wrong scheme
    ):
        ok, _reason = webhooks.is_safe_url(bad)
        assert not ok, f"{bad} should be refused"
    # A public IP literal is allowed.
    ok, _ = webhooks.is_safe_url("http://93.184.216.34/hook")
    assert ok


def test_poster_refuses_to_reach_an_internal_address():
    # No monkeypatching: the guard must refuse a loopback/metadata target outright.
    post = webhooks.urllib_poster(2.0)
    ok, reason = post(
        "http://127.0.0.1:9/", b"{}", {"Content-Type": "application/json"}
    )
    assert not ok
    assert "internal" in reason
    ok, reason = post(
        "http://169.254.169.254/latest/", b"{}", {"Content-Type": "application/json"}
    )
    assert not ok
    assert "internal" in reason


def test_poster_delivers_to_a_reachable_endpoint(local_server, monkeypatch):
    base, state = local_server
    state["status"] = 200
    # The controllable server is on loopback, which the guard blocks; allow it only
    # here so the real request path can be exercised end to end.
    monkeypatch.setattr(webhooks, "_address_blocked", lambda ip: False)
    post = webhooks.urllib_poster(2.0)
    ok, reason = post(base + "/hook", b'{"x":1}', {"Content-Type": "application/json"})
    assert ok and reason is None
    assert state["requests"] == ["/hook"]


def test_poster_refuses_redirect_and_does_not_follow(local_server, monkeypatch):
    base, state = local_server
    state["status"] = 302  # Location points at the metadata endpoint
    monkeypatch.setattr(webhooks, "_address_blocked", lambda ip: False)
    post = webhooks.urllib_poster(2.0)
    ok, reason = post(base + "/hook", b"{}", {"Content-Type": "application/json"})
    assert not ok
    assert "redirect" in reason
    # The crux: exactly ONE request was made — the Location was never followed.
    assert state["requests"] == ["/hook"]


def test_pin_dials_the_vetted_ip_and_keeps_the_hostname_host_header(
    local_server, monkeypatch
):
    """The real rebinding defense: with the guard LEFT ON, a hostname is resolved+
    vetted once, and the socket dials that exact numeric IP (never re-resolving the
    hostname), while the Host header still carries the hostname."""
    base, state = local_server
    server_port = int(base.rsplit(":", 1)[1])
    public_ip = "93.184.216.34"  # passes the real _address_blocked
    real_getaddrinfo = webhooks.socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        # Only the test hostname is faked; real IPs (the redirect's 127.0.0.1) pass
        # through, so create_connection's own resolution isn't hijacked.
        if host == "rebind.test":
            return [(2, 1, 6, "", (public_ip, port))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    # The pinned connect() would dial the vetted public IP; redirect that dial to the
    # loopback test server so the request completes, capturing what it dialed.
    captured = {}
    real_create = webhooks.socket.create_connection

    def redirecting_create(address, *args, **kwargs):
        captured["dialed"] = address
        return real_create(("127.0.0.1", server_port), *args, **kwargs)

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(webhooks.socket, "create_connection", redirecting_create)
    post = webhooks.urllib_poster(2.0)
    ok, _ = post("http://rebind.test/hook", b"{}", {"Content-Type": "application/json"})
    assert ok
    # Dialed the numeric vetted IP, not the hostname (no second resolution to rebind).
    assert captured["dialed"] == (public_ip, 80)
    # And the Host header carried the registered hostname, not the pinned IP.
    assert state["host_header"] == "rebind.test"


def test_safe_connect_target_fails_closed_on_internal(monkeypatch):
    # A hostname that resolves to an internal IP at delivery time is refused.
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 80))],
    )
    with pytest.raises(webhooks._UnsafeAddress):
        webhooks._safe_connect_target("rebind.evil.test", 80)


def test_safe_connect_target_fails_closed_on_split_answer(monkeypatch):
    """A split-horizon answer (one public, one internal record) must fail closed —
    otherwise the connect could pick the internal one. This is the invariant that
    separates this hardening from a first-record-only check."""
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 80)),  # public first
            (2, 1, 6, "", ("169.254.169.254", 80)),  # internal second
        ],
    )
    with pytest.raises(webhooks._UnsafeAddress):
        webhooks._safe_connect_target("split.evil.test", 80)


def test_address_policy_decodes_embedded_ipv4():
    """An internal v4 wrapped in an IPv4-mapped or 6to4 IPv6 literal must be blocked —
    the kernel would route it to the v4 target. Independent of interpreter version."""
    for internal in (
        "::ffff:169.254.169.254",  # mapped metadata
        "::ffff:10.0.0.1",  # mapped private
        "2002:a9fe:a9fe::",  # 6to4 of 169.254.169.254
        "2002:0a00:0001::",  # 6to4 of 10.0.0.1
    ):
        assert webhooks._address_blocked(ipaddress.ip_address(internal)), internal
    for public in ("2001:4860:4860::8888", "2002:5db8:d822::"):
        assert not webhooks._address_blocked(ipaddress.ip_address(public)), public


def test_malformed_port_and_bad_resolution_fail_closed(monkeypatch):
    # An out-of-range port is a clean refusal, never an exception that aborts a pass.
    ok, reason = webhooks.is_safe_url("http://example.com:70000/")
    assert not ok and "port" in reason
    ok, reason = webhooks.urllib_poster(1.0)("http://h:70000/", b"{}", {})
    assert not ok and "port" in reason

    # getaddrinfo can raise UnicodeError (pathological IDNA), not just gaierror — both
    # must fail closed rather than propagate and stall delivery.
    def boom(*a, **k):
        raise UnicodeError("idna encoding failed")

    monkeypatch.setattr(webhooks.socket, "getaddrinfo", boom)
    ok, _ = webhooks.is_safe_url("http://weird.test/")
    assert not ok
    with pytest.raises(webhooks._UnsafeAddress):
        webhooks._safe_connect_target("weird.test", 80)


def _self_signed_cert(tmp_path, hostname):
    from datetime import datetime, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2020, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2035, 1, 1, tzinfo=timezone.utc))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


def test_https_pin_still_validates_cert_against_the_hostname(tmp_path, monkeypatch):
    """Pinning to an IP must NOT weaken TLS: the cert is still checked against the
    registered hostname (server_hostname=self.host), so a name the cert doesn't
    cover fails even though the socket reaches the same server."""
    import ssl as ssl_mod

    cert_file, key_file = _self_signed_cert(tmp_path, "pinned.test")
    state = {"requests": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["requests"].append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server_ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_file, key_file)
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    # The poster's client context trusts our self-signed CA; both test hostnames
    # resolve to the loopback TLS server. _address_blocked allowed so loopback is
    # reachable for the test.
    real_create_default_context = ssl_mod.create_default_context
    monkeypatch.setattr(
        webhooks.ssl,
        "create_default_context",
        lambda: real_create_default_context(cafile=str(cert_file)),
    )
    monkeypatch.setattr(webhooks, "_address_blocked", lambda ip: False)
    monkeypatch.setattr(
        webhooks.socket,
        "getaddrinfo",
        lambda host, prt, *a, **k: [(2, 1, 6, "", ("127.0.0.1", port))],
    )
    try:
        post = webhooks.urllib_poster(3.0)
        # The cert covers pinned.test -> validates against server_hostname -> delivers.
        ok, reason = post(
            f"https://pinned.test:{port}/hook",
            b"{}",
            {"Content-Type": "application/json"},
        )
        assert ok, reason
        # Same server + cert, but a hostname the cert does NOT cover -> TLS name mismatch,
        # proving the pin did not silently validate against the IP.
        ok, reason = post(
            f"https://wrong.test:{port}/hook",
            b"{}",
            {"Content-Type": "application/json"},
        )
        assert not ok
        assert any(w in reason.lower() for w in ("match", "hostname", "certificate"))
    finally:
        server.shutdown()
        thread.join(timeout=2)


# --- the operator's private-host allowlist ----------------------------------


def test_the_allowlist_is_empty_by_default_and_the_policy_stays_absolute():
    from athena import config

    assert config.EGRESS_PRIVATE_HOSTS == ()
    ok, reason = webhooks.is_safe_url("http://127.0.0.1/hook")
    assert not ok and "internal" in reason


def test_a_named_host_may_resolve_private_and_unnamed_hosts_still_may_not(
    monkeypatch,
):
    """The exemption is exact-match: naming one host opens exactly that host.

    Discovered by the field exercise: without this, a self-hosted operator cannot
    point ATHENA_ICARUS_URL or a webhook at their own machine, LAN, or tailnet —
    the deployments Athena is FOR — and every dispatch to a local executor lands
    `undeliverable`.
    """
    from athena import config

    monkeypatch.setattr(config, "EGRESS_PRIVATE_HOSTS", ("127.0.0.1",))
    ok, reason = webhooks.is_safe_url("http://127.0.0.1:8443/dispatch")
    assert ok, reason
    # A different private target is still refused — the list is not a mode switch.
    ok, _ = webhooks.is_safe_url("http://10.0.0.5/")
    assert not ok
    ok, _ = webhooks.is_safe_url("http://169.254.169.254/latest/meta-data/")
    assert not ok
    # And the connect-time pin honors the same exemption, so delivery agrees with
    # registration about what is reachable.
    family, sockaddr = webhooks._safe_connect_target("127.0.0.1", 8443)
    assert sockaddr[0] == "127.0.0.1"
    with pytest.raises(webhooks._UnsafeAddress):
        webhooks._safe_connect_target("169.254.169.254", 80)


def test_the_allowlist_match_is_case_insensitive_but_never_fuzzy(monkeypatch):
    from athena import config

    monkeypatch.setattr(config, "EGRESS_PRIVATE_HOSTS", ("localhost",))
    assert webhooks._host_exempt("LOCALHOST")
    assert webhooks._host_exempt("localhost")
    # No prefix/suffix/subdomain matching: an exemption names one host.
    assert not webhooks._host_exempt("localhost.attacker.example")
    assert not webhooks._host_exempt("a-localhost")


def test_delivery_reaches_a_loopback_endpoint_through_the_allowlist_alone(
    local_server, monkeypatch
):
    """End to end with NO monkeypatch of the address policy: the allowlist is the
    only thing standing between the poster and a loopback receiver, exactly as it
    would be for an operator's local executor."""
    from athena import config

    base, state = local_server
    state["status"] = 200
    monkeypatch.setattr(config, "EGRESS_PRIVATE_HOSTS", ("127.0.0.1",))
    post = webhooks.urllib_poster(2.0)
    ok, reason = post(base + "/hook", b'{"x":1}', {"Content-Type": "application/json"})
    assert ok, reason
    assert state["requests"] == ["/hook"]
