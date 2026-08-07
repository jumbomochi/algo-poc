from __future__ import annotations

import os

import uvicorn

from services.api.app import create_app

app = create_app()

if __name__ == "__main__":
    # request.client.host (used by the auth failure-lockout tracker in
    # services/api/auth.py) is the *direct* TCP peer. Behind the
    # TLS-terminating reverse proxy documented in
    # docs/operations/api-security.md, that peer is the proxy itself, so
    # every real client would share one lockout bucket unless uvicorn is
    # told to trust the proxy's X-Forwarded-For header.
    #
    # API_FORWARDED_ALLOW_IPS should be set to the proxy's address (e.g. the
    # docker bridge IP or "127.0.0.1" for a loopback sidecar) — never "*" or
    # a value an untrusted client could occupy, since trusting the header
    # from the wrong peer lets a client spoof its own client_id.
    forwarded_allow_ips = os.environ.get("API_FORWARDED_ALLOW_IPS")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        proxy_headers=bool(forwarded_allow_ips),
        forwarded_allow_ips=forwarded_allow_ips,
    )
