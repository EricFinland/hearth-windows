#!/usr/bin/env python3
"""hearth desktop sidecar auth: bearer token, Host, and Origin checks.

Loopback is not a trust boundary. Every process on the machine can connect to
a 127.0.0.1 port, and every website the user's browser visits can make
requests to it from script. DNS rebinding defeats origin isolation: a page
served from an attacker-controlled hostname can be pointed at 127.0.0.1 after
the browser's same-origin check has already passed, so a fetch() from that
page arrives at this server with a Host header naming the attacker's
hostname, not "localhost". So every request except the trivial health check
must pass three independent checks:

  1. bearer token, compared with hmac.compare_digest rather than ==, since a
     naive == comparison stops at the first mismatched byte and leaks timing
     information proportional to how many leading bytes matched
  2. Host header must be exactly 127.0.0.1:<port> or localhost:<port> -- this
     is the DNS-rebinding defence, because a rebound hostname still resolves
     to 127.0.0.1 on the wire but its Host header carries the attacker's
     registered name, not one of these two literal values
  3. Origin header, when the client sends one, must match one of the same
     allowed host:port pairs (a browser always sends Origin on a
     cross-origin fetch; a same-origin request or a non-browser client may
     omit it, so an absent Origin is not itself a failure)

These three only stay independent as long as a request that fails one of
them can never let a second, attacker-shaped request piggyback on the same
TCP connection and get parsed as if it were the next legitimate request --
otherwise an attacker could smuggle arbitrary Host/Origin/Authorization
values past whichever check runs first, on a rejected request's own
connection. That is a transport-layer property, not something these
functions themselves can provide: see app.py's SidecarHandler.
_prepare_body, which reads every request's full declared body off the
socket before any of do_GET/do_POST's checks run (including before the
unauthenticated /healthz route), so no early rejection here can ever leave
unread bytes behind for HTTP pipelining to misinterpret as a second
request. With that in place, these three checks are genuinely independent;
without it, they are not, no matter how they are written.

These are pure, I/O-free functions on purpose: no socket, no server, so the
security contract is unit-testable in isolation from the transport that
calls it (see app.py).
"""

import hmac
import sys


def check_bearer(auth_header, token):
    """True only if `auth_header` is exactly 'Bearer <token>' for the
    configured token, checked with hmac.compare_digest. A missing header, a
    missing/empty configured token, or a non-Bearer scheme is rejected before
    ever reaching compare_digest."""
    if not token or not auth_header:
        return False
    scheme, _, value = auth_header.partition(" ")
    if scheme != "Bearer" or not value:
        return False
    return hmac.compare_digest(value, token)


def allowed_hosts(port):
    return {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}


def check_host(host_header, port):
    """True only if the Host header is exactly 127.0.0.1:<port> or
    localhost:<port>. A bare '127.0.0.1' with no port, a mismatched port, or
    any other hostname (including a rebound DNS name that happens to resolve
    to 127.0.0.1) is rejected."""
    if not host_header:
        return False
    return host_header.strip().lower() in allowed_hosts(port)


def check_origin(origin_header, port):
    """True if Origin is absent (same-origin and non-browser requests often
    omit it), or if present, names an allowed http(s) origin for this port."""
    if not origin_header:
        return True
    allowed = set()
    for scheme in ("http", "https"):
        for host in ("127.0.0.1", "localhost"):
            allowed.add("{}://{}:{}".format(scheme, host, port))
    return origin_header.strip().lower() in allowed


def _self_test():
    token = "s3cret-token-value-for-testing"

    # check_bearer
    assert check_bearer("Bearer " + token, token) is True
    assert check_bearer("Bearer " + token + "x", token) is False
    assert check_bearer("Bearer wrong-token", token) is False
    assert check_bearer("", token) is False
    assert check_bearer(None, token) is False
    assert check_bearer("Bearer", token) is False  # scheme with no value
    assert check_bearer("Basic " + token, token) is False  # wrong scheme
    assert check_bearer(token, token) is False  # missing "Bearer " prefix entirely
    assert check_bearer("Bearer " + token, "") is False  # no token configured
    assert check_bearer("Bearer " + token, None) is False

    # check_host: DNS-rebinding defence
    assert check_host("127.0.0.1:5555", 5555) is True
    assert check_host("localhost:5555", 5555) is True
    assert check_host("LOCALHOST:5555", 5555) is True  # case-insensitive
    assert check_host("127.0.0.1:9999", 5555) is False  # wrong port
    assert check_host("evil.example.com:5555", 5555) is False
    assert check_host("attacker.test", 5555) is False
    assert check_host("127.0.0.1", 5555) is False  # no port at all
    assert check_host("", 5555) is False
    assert check_host(None, 5555) is False
    # A rebound hostname that still resolves to 127.0.0.1 on the wire must
    # still fail: the check is on the literal header text, not on where the
    # name resolves.
    assert check_host("rebound.attacker.test:5555", 5555) is False

    # check_origin
    assert check_origin(None, 5555) is True
    assert check_origin("", 5555) is True
    assert check_origin("http://127.0.0.1:5555", 5555) is True
    assert check_origin("http://localhost:5555", 5555) is True
    assert check_origin("https://127.0.0.1:5555", 5555) is True
    assert check_origin("http://evil.example.com:5555", 5555) is False
    assert check_origin("http://127.0.0.1:9999", 5555) is False  # wrong port

    print("hearth-desktop-auth self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test())
