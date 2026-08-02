"""TokenAuth behaviour: validation, subprotocol extraction, edge cases."""
from __future__ import annotations

import pytest

from rsc_host.auth import AuthError, TokenAuth


class TestConstruction:
    def test_empty_token_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenAuth("")


class TestCheck:
    def test_valid_token_passes(self) -> None:
        auth = TokenAuth("s3cret")
        auth.check("s3cret")  # no raise

    def test_wrong_token_raises(self) -> None:
        auth = TokenAuth("s3cret")
        with pytest.raises(AuthError, match="invalid token"):
            auth.check("nope")

    def test_missing_token_raises(self) -> None:
        auth = TokenAuth("s3cret")
        with pytest.raises(AuthError, match="missing token"):
            auth.check(None)

    def test_empty_string_raises(self) -> None:
        auth = TokenAuth("s3cret")
        with pytest.raises(AuthError, match="missing token"):
            auth.check("")

    def test_close_but_wrong_length_rejected(self) -> None:
        # compare_digest requires equal length; the wrapper handles it.
        auth = TokenAuth("s3cret")
        with pytest.raises(AuthError):
            auth.check("s3cretX")


class TestSubprotocolExtraction:
    def test_bearer_then_token(self) -> None:
        auth = TokenAuth("s3cret")
        assert auth.extract_from_subprotocols(["bearer", "s3cret"]) == "s3cret"

    def test_case_insensitive_scheme(self) -> None:
        auth = TokenAuth("s3cret")
        assert auth.extract_from_subprotocols(["Bearer", "s3cret"]) == "s3cret"
        assert auth.extract_from_subprotocols(["BEARER", "s3cret"]) == "s3cret"

    def test_empty_list_returns_none(self) -> None:
        auth = TokenAuth("s3cret")
        assert auth.extract_from_subprotocols([]) is None

    def test_none_returns_none(self) -> None:
        auth = TokenAuth("s3cret")
        assert auth.extract_from_subprotocols(None) is None

    def test_only_bearer_no_token_returns_none(self) -> None:
        auth = TokenAuth("s3cret")
        assert auth.extract_from_subprotocols(["bearer"]) is None

    def test_wrong_scheme_returns_none(self) -> None:
        auth = TokenAuth("s3cret")
        assert auth.extract_from_subprotocols(["basic", "s3cret"]) is None

    def test_whitespace_stripped(self) -> None:
        auth = TokenAuth("s3cret")
        assert auth.extract_from_subprotocols(["  bearer  ", "  s3cret  "]) == "s3cret"


class TestIntegration:
    def test_end_to_end_extract_then_check(self) -> None:
        auth = TokenAuth("s3cret")
        token = auth.extract_from_subprotocols(["bearer", "s3cret"])
        auth.check(token)  # no raise

    def test_end_to_end_bad_token(self) -> None:
        auth = TokenAuth("s3cret")
        token = auth.extract_from_subprotocols(["bearer", "wrong"])
        with pytest.raises(AuthError):
            auth.check(token)
