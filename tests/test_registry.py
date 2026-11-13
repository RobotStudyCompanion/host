"""Registry integration: setup() wires HAL + peripherals + verbs together."""
from __future__ import annotations

import asyncio
import json

import pytest

from rsc_host.dispatch import Dispatcher
from rsc_host.events import EventBus
from rsc_host.hal.fake import FakeGpioInput
from rsc_host.hal.types import Edge
from rsc_host.peripherals.registry import Pinout, setup
from rsc_host.protocol import Cmd
from rsc_host.state import StateStore
from tests.test_button_led import BAD_PARAMS as LED_BAD_PARAMS
from tests.test_ring import BAD_PARAMS as RING_BAD_PARAMS


@pytest.fixture
async def rig():
    dispatcher = Dispatcher()
    bus = EventBus()
    peripherals = await setup(dispatcher, bus, backend="fake")
    try:
        yield dispatcher, bus, peripherals
    finally:
        await peripherals.stop()


class TestStartupPush:
    """The panel keeps no copy of volume or mute, so it must be told at boot."""

    @staticmethod
    def _panel_answers(monkeypatch, reply: str) -> None:
        from rsc_host.hal.fake import FakeSerial

        original = FakeSerial.write_line

        async def write_line(self, line: str) -> None:
            await original(self, line)
            if line == "vol?":
                await self.inject(reply)

        monkeypatch.setattr(FakeSerial, "write_line", write_line)

    async def test_pushes_state_when_firmware_supports_it(
        self, monkeypatch, tmp_path
    ) -> None:
        from rsc_host.state import StateStore

        self._panel_answers(monkeypatch, "vol:            50")
        store = StateStore(tmp_path)
        store.write("audio", {"volume": 40})
        peripherals = await setup(Dispatcher(), EventBus(), backend="fake", state=store)
        try:
            written = peripherals.serial_backend.written()
            assert written == (
                "vol?", "vol:40", "mute:off", "mic_mute:off", "screen:on", "report",
            )
        finally:
            await peripherals.stop()

    async def test_pushes_nothing_when_firmware_lacks_the_commands(
        self, monkeypatch, tmp_path
    ) -> None:
        from rsc_host.state import StateStore

        self._panel_answers(monkeypatch, "ERR: unknown command 'vol'")
        peripherals = await setup(
            Dispatcher(), EventBus(), backend="fake", state=StateStore(tmp_path)
        )
        try:
            assert peripherals.serial_backend.written() == ("vol?", "resume", "report")
        finally:
            await peripherals.stop()


class TestVerbRegistration:
    async def test_flipper_verbs_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        verbs = set(dispatcher.registered_verbs())
        for side in ("left", "right", "m3"):
            assert f"flipper.{side}" in verbs
            assert f"flipper.{side}.stop" in verbs

    async def test_ring_verbs_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        verbs = set(dispatcher.registered_verbs())
        assert "ring.mode" in verbs
        assert "ring.modes" in verbs

    async def test_button_led_verb_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        assert "button_led" in dispatcher.registered_verbs()

    async def test_cyd_verbs_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        verbs = set(dispatcher.registered_verbs())
        assert "cyd.mood" in verbs
        assert "cyd.raw" in verbs

    async def test_audio_verbs_registered(self, rig) -> None:
        dispatcher, _, _ = rig
        verbs = set(dispatcher.registered_verbs())
        assert "audio.play_url" in verbs
        assert "audio.stop_play" in verbs
        assert "audio.capture.stop" in verbs
        assert "audio.devices" in verbs


class TestEndToEnd:
    async def test_flipper_verb_moves_backend(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, peripherals = rig
        cmd = Cmd(id="c1", verb="flipper.left", args={"speed": 0.5})
        ack = await dispatcher.dispatch(cmd)
        assert ack.ok is True
        assert await peripherals.servo_backend.get_speed("left") == 0.5

    async def test_ring_verb_lists_modes(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, _ = rig
        ack = await dispatcher.dispatch(Cmd(id="c2", verb="ring.modes", args={}))
        assert ack.ok is True
        assert "solid" in ack.result["modes"]

    async def test_cyd_curated_writes_serial(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, peripherals = rig
        ack = await dispatcher.dispatch(
            Cmd(id="c3", verb="cyd.mood", args={"value": "HAPPY"})
        )
        assert ack.ok is True
        assert peripherals.serial_backend.written() == ("mood:HAPPY",)

    async def test_button_edge_fires_event(self, rig) -> None:
        _, bus, peripherals = rig
        backend = peripherals.gpio_in_backend
        assert isinstance(backend, FakeGpioInput)
        async with bus.subscribe() as q:
            backend.trigger(23, Edge.RISING)
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "button.press"

    async def test_cyd_line_becomes_event(self, rig) -> None:
        from rsc_host.hal.fake import FakeSerial
        _, bus, peripherals = rig
        serial = peripherals.serial_backend
        assert isinstance(serial, FakeSerial)
        async with bus.subscribe() as q:
            await serial.inject("host_vol:88")
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt.topic == "host_vol"
        assert evt.data == {"value": 88}

    async def test_audio_devices_returns_shape(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, _ = rig
        ack = await dispatcher.dispatch(Cmd(id="cd", verb="audio.devices", args={}))
        assert ack.ok is True
        assert "input" in ack.result and "output" in ack.result
        assert "default_input" in ack.result and "default_output" in ack.result


class TestM3Disabled:
    async def test_m3_verb_succeeds_but_backend_untouched(self, rig) -> None:
        from rsc_host.protocol import Cmd
        dispatcher, _, peripherals = rig
        ack = await dispatcher.dispatch(
            Cmd(id="c4", verb="flipper.m3", args={"speed": 0.5})
        )
        assert ack.ok is True
        assert ack.result["enabled"] is False
        # Backend never saw an m3 speed set.
        assert "m3" not in peripherals.servo_backend.known_servos()


class TestBadParamsOnTheWire:
    """Bad ring and LED params come back as an error ack, not a crash afterwards."""

    @pytest.fixture
    async def wired(self, tmp_path):
        dispatcher = Dispatcher()
        bus = EventBus()
        peripherals = await setup(dispatcher, bus, backend="fake", state=StateStore(tmp_path))
        try:
            yield dispatcher, bus, peripherals
        finally:
            await peripherals.stop()

    # One rig per test rather than per value: setup() waits on the panel probe.
    async def test_ring(self, wired) -> None:
        dispatcher, bus, peripherals = wired
        running = await dispatcher.dispatch(
            Cmd(id="a", verb="ring.mode", args={"mode": "pulse", "params": {"period_ms": 200}})
        )
        assert running.ok
        task = peripherals.ring._task
        seq = bus.latest_seq()
        for params in RING_BAD_PARAMS:
            ack = await dispatcher.dispatch(
                Cmd(id="b", verb="ring.mode", args={"mode": "spin", "params": params})
            )
            assert ack.ok is False, params
            assert ack.code == "INVALID_ARGS", params
            assert peripherals.ring._task is task and not task.done(), params
            assert peripherals.ring.current_mode == "pulse", params
        assert bus.history(since_seq=seq) == []

    async def test_button_led(self, wired) -> None:
        dispatcher, bus, peripherals = wired
        running = await dispatcher.dispatch(
            Cmd(id="a", verb="button_led", args={"mode": "breathe", "params": {"period_ms": 800}})
        )
        assert running.ok
        task = peripherals.button_led._task
        seq = bus.latest_seq()
        for params in LED_BAD_PARAMS:
            ack = await dispatcher.dispatch(
                Cmd(id="b", verb="button_led", args={"mode": "pulse", "params": params})
            )
            assert ack.ok is False, params
            assert ack.code == "INVALID_ARGS", params
            assert peripherals.button_led._task is task and not task.done(), params
            assert peripherals.button_led.current_mode == "breathe", params
        assert bus.history(since_seq=seq) == []

    @pytest.mark.parametrize(
        ("verb", "mode"),
        [("ring.mode", "does_not_exist"), ("button_led", "does_not_exist")],
    )
    async def test_unknown_mode(self, wired, verb, mode) -> None:
        dispatcher, _, _ = wired
        ack = await dispatcher.dispatch(Cmd(id="a", verb=verb, args={"mode": mode}))
        assert ack.code == "INVALID_ARGS"

    async def test_valid_commands_still_work(self, wired) -> None:
        dispatcher, _, peripherals = wired
        commands = [
            ("ring.mode", {"mode": "off"}),
            ("ring.mode", {"mode": "solid"}),
            ("ring.mode", {"mode": "pulse"}),
            ("ring.mode", {"mode": "spin"}),
            # What the console sends today.
            (
                "ring.mode",
                {"mode": "pulse", "params": {"r": 10, "g": 20, "b": 30, "period_ms": 1500}},
            ),
            ("button_led", {"mode": "off"}),
            ("button_led", {"mode": "on"}),
            ("button_led", {"mode": "pulse"}),
            ("button_led", {"mode": "breathe"}),
            ("button_led", {"mode": "pulse", "params": {"period_ms": 800}}),
            ("button_led", {"mode": "breathe", "params": {"period_ms": 4000}}),
            ("button_led", {"mode": "on", "params": {"duty": 0.3}}),
        ]
        for verb, args in commands:
            ack = await dispatcher.dispatch(Cmd(id="a", verb=verb, args=args))
            assert ack.ok is True, args
            assert ack.result["mode"] == args["mode"], args
            await asyncio.sleep(0.05)
            peripheral = peripherals.ring if verb == "ring.mode" else peripherals.button_led
            assert peripheral.current_mode == args["mode"], args
            assert peripheral._task is None or not peripheral._task.done(), args

    async def test_nan_arriving_as_json_is_rejected(self, wired) -> None:
        # json.loads accepts NaN, so this is reachable from a real client.
        dispatcher, _, _ = wired
        payload = json.loads('{"type":"cmd","id":"a","verb":"button_led",'
                             '"args":{"mode":"on","params":{"duty":NaN}}}')
        ack = await dispatcher.dispatch(Cmd.model_validate(payload))
        assert ack.code == "INVALID_ARGS"


# ---- Step 2: screen sync through setup() ----


async def _quiet(q, timeout: float = 0.2) -> list:
    """Everything published within ``timeout``."""
    seen = []
    try:
        while True:
            seen.append(await asyncio.wait_for(q.get(), timeout=timeout))
    except asyncio.TimeoutError:
        return seen


@pytest.fixture
async def old_fw(tmp_path):
    """A rig whose screen never answers the probe (unplugged or old firmware)."""
    dispatcher, bus = Dispatcher(), EventBus()
    peripherals = await setup(
        dispatcher, bus, backend="fake", state=StateStore(tmp_path)
    )
    try:
        yield dispatcher, bus, peripherals
    finally:
        await peripherals.stop()


@pytest.fixture
async def new_fw(monkeypatch, tmp_path):
    """A rig whose screen answers ``vol?`` like the new firmware."""
    TestStartupPush._panel_answers(monkeypatch, "vol:   50")
    dispatcher, bus = Dispatcher(), EventBus()
    peripherals = await setup(
        dispatcher, bus, backend="fake", state=StateStore(tmp_path)
    )
    try:
        yield dispatcher, bus, peripherals
    finally:
        await peripherals.stop()


class TestScreenSync:
    async def test_startup_without_answer_is_old_firmware(self, old_fw) -> None:
        _, _, peripherals = old_fw
        assert peripherals.serial_backend.written() == ("vol?", "resume", "report")

    async def test_mood_verb_writes_and_publishes_state(self, old_fw) -> None:
        dispatcher, bus, peripherals = old_fw
        serial = peripherals.serial_backend
        before = len(serial.written())
        async with bus.subscribe() as q:
            ack = await dispatcher.dispatch(
                Cmd(id="m1", verb="cyd.mood", args={"value": "happy"})
            )
            assert ack.ok is True
            assert ack.result == {"verb": "cyd.mood", "value": "HAPPY"}
            evt = await asyncio.wait_for(q.get(), timeout=1.0)
            assert evt.topic == "cyd.state" and evt.source == "host"
            assert evt.data == {
                "mood": "HAPPY", "cycle": None, "bright": None,
                "auto_bright": None, "reports": False,
            }
            await serial.inject("host_face:HAPPY")
            later = await _quiet(q)
        assert serial.written()[before:] == ("mood:HAPPY",)
        # Only the cyd event and the reports flag; no second mood change.
        assert [e.topic for e in later] == ["host_face", "cyd.state"]
        assert later[1].data == {**evt.data, "reports": True}

    async def test_bad_value_is_invalid_args(self, old_fw) -> None:
        # One rig for every case: each setup() costs over a second.
        dispatcher, _, peripherals = old_fw
        before = peripherals.serial_backend.written()
        for verb, args in [
            ("cyd.mood", {"value": "SMILE"}),
            ("cyd.mood", {}),
            ("cyd.mood_cycle", {"value": "maybe"}),
            ("cyd.bright", {"value": "0"}),
            ("cyd.bright", {"value": "abc"}),
        ]:
            ack = await dispatcher.dispatch(Cmd(id="m2", verb=verb, args=args))
            assert ack.ok is False, (verb, args)
            assert ack.code == "INVALID_ARGS", (verb, args)
        assert peripherals.serial_backend.written() == before
        assert peripherals.cyd.snapshot()["mood"] is None

    async def test_report_lines_never_write(self, old_fw) -> None:
        _, bus, peripherals = old_fw
        serial = peripherals.serial_backend
        before = serial.written()
        async with bus.subscribe() as q:
            await serial.inject_many(
                ["host_face:SAD", "host_cycle:on", "host_bright:30",
                 "host_auto_bright:on", "host_bright:abc"]
            )
            await _quiet(q)
        assert serial.written() == before

    async def test_host_ready_after_failed_probe_pushes_audio(self, old_fw) -> None:
        dispatcher, bus, peripherals = old_fw
        serial = peripherals.serial_backend
        await dispatcher.dispatch(Cmd(id="v", verb="audio.volume", args={"percent": 35}))
        before = len(serial.written())
        assert peripherals.cyd.supports_push is False
        async with bus.subscribe() as q:
            await serial.inject("host_ready")
            events = await _quiet(q)
        assert peripherals.cyd.supports_push is True
        assert serial.written()[before:] == ("vol:35", "mute:off", "mic_mute:off")
        # No audio event: nothing changed on the host.
        assert not [e for e in events if e.topic.startswith("audio.")]

    async def test_status_carries_state(self, old_fw) -> None:
        dispatcher, _, _ = old_fw
        await dispatcher.dispatch(
            Cmd(id="b", verb="cyd.bright", args={"value": "40"})
        )
        ack = await dispatcher.dispatch(
            Cmd(id="s", verb="peripherals.status", args={})
        )
        cyd = ack.result["cyd"]
        assert cyd["reports"] is False
        assert cyd["state"] == {
            "mood": None, "cycle": None, "bright": 40,
            "auto_bright": False, "reports": False,
        }

    async def test_mic_toggle_reads_the_backend(self, old_fw) -> None:
        dispatcher, bus, peripherals = old_fw
        await dispatcher.dispatch(
            Cmd(id="mm", verb="audio.mic_mute", args={"muted": True})
        )
        async with bus.subscribe() as q:
            await peripherals.serial_backend.inject("host_mic")
            events = await _quiet(q)
        mic = [e for e in events if e.topic == "audio.mic_mute"]
        assert mic and mic[0].data["mic_muted"] is False


class TestPower:
    """B17: ask first; a refusal changes nothing."""

    @staticmethod
    def _logind(monkeypatch, peripherals, allowed: bool) -> list[str]:
        """Stand in for logind on this rig's power backend."""
        requested: list[str] = []

        async def status() -> dict:
            if allowed:
                return {"can_power_off": "yes", "can_reboot": "yes", "allowed": True}
            return {
                "can_power_off": "challenge", "can_reboot": "challenge",
                "allowed": False, "reason": "polkit says no",
            }

        async def request(action: str, delay: float = 2.0) -> dict:
            requested.append(action)
            return {"scheduled": True, "action": action, "delay_s": delay}

        monkeypatch.setattr(peripherals.power, "status", status)
        monkeypatch.setattr(peripherals.power, "request", request)
        return requested

    @staticmethod
    async def _moving(dispatcher) -> None:
        await dispatcher.dispatch(
            Cmd(id="f", verb="flipper.left", args={"speed": 0.5})
        )
        await dispatcher.dispatch(
            Cmd(id="r", verb="ring.mode",
                args={"mode": "solid", "params": {"color": "#00ff00"}})
        )

    async def _assert_untouched(self, peripherals, written_before) -> None:
        assert await peripherals.servo_backend.get_speed("left") == 0.5
        assert peripherals.ring.current_mode == "solid"
        assert peripherals.serial_backend.written() == written_before

    async def test_refused_verb_changes_nothing(self, old_fw, monkeypatch) -> None:
        dispatcher, bus, peripherals = old_fw
        requested = self._logind(monkeypatch, peripherals, allowed=False)
        await self._moving(dispatcher)
        before = peripherals.serial_backend.written()
        async with bus.subscribe() as q:
            for verb in ("system.poweroff", "system.reboot"):
                ack = await dispatcher.dispatch(
                    Cmd(id=verb, verb=verb, args={"confirm": True})
                )
                assert ack.ok is True
                assert ack.result["scheduled"] is False
                assert ack.result["reason"] == "polkit says no"
            events = await _quiet(q)
        assert not [e for e in events if e.topic.startswith("system.")]
        assert requested == []
        await self._assert_untouched(peripherals, before)

    async def test_refused_panel_changes_nothing(self, old_fw, monkeypatch) -> None:
        dispatcher, bus, peripherals = old_fw
        requested = self._logind(monkeypatch, peripherals, allowed=False)
        await self._moving(dispatcher)
        before = peripherals.serial_backend.written()
        async with bus.subscribe() as q:
            await peripherals.serial_backend.inject_many(
                ["host_poweroff", "host_reboot"]
            )
            events = await _quiet(q)
        assert [e.topic for e in events] == ["host_poweroff", "host_reboot"]
        assert requested == []
        await self._assert_untouched(peripherals, before)

    async def test_allowed_verb_blanks_old_screen(self, old_fw, monkeypatch) -> None:
        dispatcher, bus, peripherals = old_fw
        requested = self._logind(monkeypatch, peripherals, allowed=True)
        await self._moving(dispatcher)
        before = len(peripherals.serial_backend.written())
        async with bus.subscribe() as q:
            ack = await dispatcher.dispatch(
                Cmd(id="p", verb="system.poweroff", args={"confirm": True})
            )
            events = await _quiet(q)
        assert ack.result["scheduled"] is True
        assert requested == ["poweroff"]
        assert peripherals.serial_backend.written()[before:] == (
            "pause", "clear", "led:off",
        )
        assert await peripherals.servo_backend.get_speed("left") == 0.0
        pending = [e for e in events if e.topic == "system.poweroff"]
        assert [e.data for e in pending] == [{"pending": True}]

    async def test_allowed_panel_blanks_new_screen(self, new_fw, monkeypatch) -> None:
        _, bus, peripherals = new_fw
        requested = self._logind(monkeypatch, peripherals, allowed=True)
        before = len(peripherals.serial_backend.written())
        async with bus.subscribe() as q:
            await peripherals.serial_backend.inject("host_reboot")
            events = await _quiet(q)
        assert requested == ["reboot"]
        assert peripherals.serial_backend.written()[before:] == ("screen:off",)
        reboot = [e.data for e in events if e.topic == "system.reboot"]
        assert reboot[0] == {"pending": True}


class TestFakePower:
    """On the fake backend, power is simulated: busctl is never called."""

    @staticmethod
    def _no_busctl(monkeypatch) -> None:
        from rsc_host import power

        async def _busctl(*args, **kwargs):
            pytest.fail(f"fake backend called busctl {args}")

        monkeypatch.setattr(power, "_busctl", _busctl)

    async def test_setup_picks_by_backend(self, old_fw) -> None:
        from rsc_host import power

        _, _, peripherals = old_fw
        assert isinstance(peripherals.power, power.FakePower)

    async def test_verbs_and_panel_never_call_busctl(
        self, old_fw, monkeypatch, caplog
    ) -> None:
        dispatcher, bus, peripherals = old_fw
        self._no_busctl(monkeypatch)

        ack = await dispatcher.dispatch(
            Cmd(id="s", verb="system.power_status", args={})
        )
        assert ack.ok is True
        assert ack.result == {
            "can_power_off": "yes", "can_reboot": "yes", "allowed": True,
        }
        ack = await dispatcher.dispatch(
            Cmd(id="p", verb="system.poweroff", args={"confirm": True, "delay_s": 0})
        )
        assert ack.ok is True
        assert ack.result == {"scheduled": True, "action": "poweroff", "delay_s": 0.0}
        ack = await dispatcher.dispatch(
            Cmd(id="r", verb="system.reboot", args={"confirm": True})
        )
        assert ack.result == {"scheduled": True, "action": "reboot", "delay_s": 2.0}

        async with bus.subscribe() as q:
            await peripherals.serial_backend.inject("host_poweroff")
            events = await _quiet(q)
        assert [e.data for e in events if e.topic == "system.poweroff"][0] == {
            "pending": True
        }
        # Give any stray background task a chance to reach busctl.
        await asyncio.sleep(0.05)
        assert "fake backend: would power off" in caplog.text
        assert "fake backend: would reboot" in caplog.text

    async def test_fake_power_alone(self, monkeypatch) -> None:
        from rsc_host import power

        self._no_busctl(monkeypatch)
        fake = power.FakePower()
        assert (await fake.status())["allowed"] is True
        assert (await fake.request("reboot", 0.0))["scheduled"] is True
        with pytest.raises(ValueError):
            await fake.request("hibernate")

    async def test_real_backend_still_asks_logind(self, monkeypatch) -> None:
        from rsc_host import power

        calls: list[tuple] = []

        async def _busctl(*args, **kwargs):
            calls.append(args)
            return 0, 's "challenge"'

        monkeypatch.setattr(power, "_busctl", _busctl)
        monkeypatch.setattr(power.shutil, "which", lambda name: "/usr/bin/busctl")
        real = power.LogindPower()
        status = await real.status()
        assert status["allowed"] is False
        result = await real.request("poweroff", 0.0)
        assert result["scheduled"] is False
        methods = {args[-1] for args in calls}
        assert methods == {"CanPowerOff", "CanReboot"}
