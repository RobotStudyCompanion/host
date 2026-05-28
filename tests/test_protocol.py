"""Schema round-trip and validation tests for rsc_host.protocol.

Covers:
  - JSON round-trip preserves equality across all three message types
  - ``extra="forbid"`` rejects unexpected fields
  - Discriminator fields ("type") are pinned to the right literal
  - ErrorCode enum values serialise as plain strings on the wire
  - Source enum on Event rejects values outside {host, cyd}
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from rsc_host.protocol import Ack, Cmd, ErrorCode, Event


class TestCmd:
    def test_roundtrip_with_args(self) -> None:
        original = Cmd(id="abc", verb="flipper.left", args={"speed": 0.5})
        parsed = Cmd.model_validate_json(original.model_dump_json())
        assert parsed == original

    def test_roundtrip_without_args(self) -> None:
        original = Cmd(id="def", verb="status")
        parsed = Cmd.model_validate_json(original.model_dump_json())
        assert parsed == original
        assert parsed.args == {}

    def test_type_discriminator_fixed(self) -> None:
        cmd = Cmd(id="x", verb="y")
        assert cmd.type == "cmd"

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            Cmd.model_validate(
                {"type": "cmd", "id": "x", "verb": "y", "trespasser": True}
            )

    def test_rejects_missing_id(self) -> None:
        with pytest.raises(ValidationError):
            Cmd.model_validate({"type": "cmd", "verb": "y"})

    def test_rejects_missing_verb(self) -> None:
        with pytest.raises(ValidationError):
            Cmd.model_validate({"type": "cmd", "id": "x"})


class TestAck:
    def test_success_helper(self) -> None:
        a = Ack.success("abc", result={"angle": 42})
        assert a.ok is True
        assert a.result == {"angle": 42}
        assert a.code is None
        assert a.message is None

    def test_success_helper_no_result(self) -> None:
        a = Ack.success("abc")
        assert a.ok is True
        assert a.result is None

    def test_failure_helper(self) -> None:
        a = Ack.failure("abc", ErrorCode.PERIPHERAL_BUSY, "left flipper mid-motion")
        assert a.ok is False
        assert a.code == ErrorCode.PERIPHERAL_BUSY
        assert a.message == "left flipper mid-motion"
        assert a.result is None

    def test_success_roundtrip(self) -> None:
        original = Ack.success("abc", result={"value": 42})
        parsed = Ack.model_validate_json(original.model_dump_json())
        assert parsed == original

    def test_failure_roundtrip(self) -> None:
        original = Ack.failure("abc", ErrorCode.UNKNOWN_VERB, "no such verb")
        parsed = Ack.model_validate_json(original.model_dump_json())
        assert parsed == original

    def test_type_discriminator_fixed(self) -> None:
        a = Ack.success("x")
        assert a.type == "ack"

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            Ack.model_validate(
                {"type": "ack", "id": "x", "ok": True, "trespasser": 1}
            )


class TestEvent:
    def test_host_source(self) -> None:
        e = Event(topic="button.press", source="host", data={"ts": 1234})
        assert e.source == "host"

    def test_cyd_source(self) -> None:
        e = Event(topic="host_vol", source="cyd", data={"value": 42})
        assert e.source == "cyd"

    def test_rejects_unknown_source(self) -> None:
        with pytest.raises(ValidationError):
            Event.model_validate(
                {"type": "event", "topic": "x", "source": "external", "data": {}}
            )

    def test_roundtrip(self) -> None:
        original = Event(
            topic="flipper.left.state", source="host", data={"speed": 0.5}
        )
        parsed = Event.model_validate_json(original.model_dump_json())
        assert parsed == original

    def test_type_discriminator_fixed(self) -> None:
        e = Event(topic="x", source="host")
        assert e.type == "event"

    def test_default_empty_data(self) -> None:
        e = Event(topic="x", source="host")
        assert e.data == {}


class TestErrorCode:
    def test_codes_serialise_as_strings(self) -> None:
        a = Ack.failure("x", ErrorCode.INVALID_ARGS, "bad speed")
        payload = a.model_dump(mode="json")
        assert payload["code"] == "INVALID_ARGS"

    def test_all_codes_have_string_values(self) -> None:
        # Sanity: the str-Enum mixin means values are their names. Lock it in.
        for member in ErrorCode:
            assert isinstance(member.value, str)
            assert member.value == member.name
