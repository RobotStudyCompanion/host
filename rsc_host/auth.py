"""Bearer token authentication for the WebSocket handshake.

Deliberately minimal:

* One shared secret loaded from config.
* :func:`hmac.compare_digest` for the check — constant-time, avoids timing
  side channels that would leak the token byte-by-byte.
* Empty / missing tokens are always invalid, no shortcut for dev — set
  ``RSC_HOST_TOKEN`` explicitly (or ``dev`` for laptop work) so it's obvious
  what's protecting the socket.

Token transport: the client sends its token in the ``Sec-WebSocket-Protocol``
header on the handshake (``Sec-WebSocket-Protocol: bearer, <token>``). The
server checks and reflects the subprotocol on success. This works over browser
WebSocket APIs, which can't set arbitrary headers but *can* set subprotocols.
"""
from __future__ import annotations

import hmac
import logging

log = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when a handshake presents an invalid or missing token."""


class TokenAuth:
    """Constant-time bearer-token check against a single configured secret."""

    def __init__(self, expected_token: str) -> None:
        if not expected_token:
            raise ValueError("expected_token must be non-empty")
        self._expected = expected_token.encode("utf-8")

    def check(self, presented: str | None) -> None:
        """Validate ``presented`` against the configured token.

        Raises:
            AuthError: if ``presented`` is missing, empty, or does not match.
        """
        if not presented:
            raise AuthError("missing token")
        if not hmac.compare_digest(presented.encode("utf-8"), self._expected):
            raise AuthError("invalid token")

    def extract_from_subprotocols(
        self, subprotocols: list[str] | tuple[str, ...] | None
    ) -> str | None:
        """Pull a bearer token out of the ``Sec-WebSocket-Protocol`` header.

        Expected shape::

            Sec-WebSocket-Protocol: bearer, <token>

        Returns the token if present, else None. Does not validate; call
        :meth:`check` on the result.
        """
        if not subprotocols:
            return None
        # Some clients send them as a single joined string; websockets library
        # parses them into a list already, but be defensive.
        items = [p.strip() for p in subprotocols]
        if len(items) >= 2 and items[0].lower() == "bearer":
            return items[1]
        return None
