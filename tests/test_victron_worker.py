from __future__ import annotations

import asyncio
import logging

from bleak.exc import BleakError
import victron_ble.scanner as victron_scanner_module

from caravan_energiemonitor.config import VictronConfig
from caravan_energiemonitor.devices.victron import worker as worker_module


PASSIVE_UNSUPPORTED = BleakError(
    "passive scanning on Linux requires BlueZ >= 5.56 with --experimental enabled "
    "and Linux kernel >= 5.10"
)


def make_config() -> VictronConfig:
    return VictronConfig(
        name="Victron",
        address="F9:9F:71:BC:CC:D0",
        advertisement_key="0123456789abcdef0123456789abcdef",
    )


def make_async_worker():
    worker = worker_module.VictronWorker(make_config())
    worker._loop = asyncio.get_running_loop()
    worker._async_stop = asyncio.Event()
    worker._scan_command = asyncio.Event()
    return worker


class FakeScanner:
    def __init__(self, *, start_error=None, on_start=None):
        self.start_error = start_error
        self.on_start = on_start
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        if self.on_start is not None:
            self.on_start()

    async def stop(self):
        self.stop_calls += 1


def test_victron_uses_bluez_passive_advertisement_monitor(monkeypatch):
    scanner_arguments = {}

    class FakeBleakScanner:
        def __init__(self, **kwargs):
            scanner_arguments.update(kwargs)

    monkeypatch.setattr(worker_module, "BleakScanner", FakeBleakScanner)
    worker_module.SmartSolarScanner(
        make_config(),
        lambda _snapshot: None,
        lambda: None,
        lambda _message: None,
        passive=True,
    )

    assert scanner_arguments["scanning_mode"] == "passive"
    assert scanner_arguments["bluez"]["or_patterns"] == [
        (
            0,
            worker_module.AdvertisementDataType.MANUFACTURER_SPECIFIC_DATA,
            b"\xe1\x02\x10",
        )
    ]


def test_active_scanner_uses_original_base_scanner_configuration(monkeypatch):
    scanner_arguments = {}

    class FakeBleakScanner:
        def __init__(self, *args, **kwargs):
            scanner_arguments["args"] = args
            scanner_arguments["kwargs"] = kwargs

    monkeypatch.setattr(victron_scanner_module, "BleakScanner", FakeBleakScanner)
    worker_module.SmartSolarScanner(
        make_config(),
        lambda _snapshot: None,
        lambda: None,
        lambda _message: None,
        passive=False,
    )

    assert scanner_arguments["args"] == ()
    assert set(scanner_arguments["kwargs"]) == {"detection_callback"}


def test_passive_scan_starts_successfully(caplog):
    async def scenario():
        worker = make_async_worker()
        passive = FakeScanner(on_start=worker._async_stop.set)
        requested_modes = []

        def new_scanner(*, passive: bool):
            requested_modes.append(passive)
            return passive_scanner

        passive_scanner = passive
        worker._new_scanner = new_scanner
        await worker._scan()

        assert requested_modes == [True]
        assert passive.start_calls == 1
        assert passive.stop_calls == 1

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Passiver Victron-Scan wird versucht" in caplog.text
    assert "Passiver Victron-Scan erfolgreich gestartet" in caplog.text


def test_missing_experimental_support_starts_active_fallback(caplog):
    async def scenario():
        worker = make_async_worker()
        bluetooth_errors = []
        worker.bluetooth_error.connect(bluetooth_errors.append)
        passive = FakeScanner(start_error=PASSIVE_UNSUPPORTED)
        active = FakeScanner(on_start=worker._async_stop.set)
        requested_modes = []

        def new_scanner(*, passive: bool):
            requested_modes.append(passive)
            return passive_scanner if passive else active_scanner

        passive_scanner = passive
        active_scanner = active
        worker._new_scanner = new_scanner
        await worker._scan()

        assert requested_modes == [True, False]
        assert passive.start_calls == 1
        assert active.start_calls == 1
        assert active.stop_calls == 1
        assert bluetooth_errors == []

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Passiver Victron-Scan nicht verfügbar" in caplog.text
    assert "Fallback auf aktiven Victron-Scan" in caplog.text
    assert "Aktiver Victron-Scan erfolgreich gestartet" in caplog.text


def test_other_bleak_start_error_is_not_swallowed():
    worker = worker_module.VictronWorker(make_config())
    error = BleakError("org.bluez.Error.Failed: adapter unavailable")
    passive_scanner = FakeScanner(start_error=error)
    requested_modes = []
    reported_errors = []

    def new_scanner(*, passive: bool):
        requested_modes.append(passive)
        return passive_scanner

    worker._new_scanner = new_scanner
    worker.bluetooth_error.connect(reported_errors.append)
    worker.run()

    assert requested_modes == [True]
    assert len(reported_errors) == 1
    assert "adapter unavailable" in reported_errors[0]


def test_stop_works_while_active_fallback_is_running():
    async def scenario():
        worker = make_async_worker()
        active_started = asyncio.Event()
        passive = FakeScanner(start_error=PASSIVE_UNSUPPORTED)
        active = FakeScanner(on_start=active_started.set)

        def new_scanner(*, passive: bool):
            return passive_scanner if passive else active_scanner

        passive_scanner = passive
        active_scanner = active
        worker._new_scanner = new_scanner

        task = asyncio.create_task(worker._scan())
        await asyncio.wait_for(active_started.wait(), timeout=0.2)
        worker.request_stop()
        await asyncio.wait_for(task, timeout=0.2)
        assert active.stop_calls == 1

    asyncio.run(scenario())


def test_multiple_pause_and_resume_requests_are_idempotent():
    async def scenario():
        worker = make_async_worker()
        active_started = asyncio.Event()
        paused = asyncio.Event()
        resumed = asyncio.Event()
        worker.scan_paused.connect(paused.set)
        worker.scan_resumed.connect(resumed.set)
        passive = FakeScanner(start_error=PASSIVE_UNSUPPORTED)
        active = FakeScanner(on_start=active_started.set)

        def new_scanner(*, passive: bool):
            return passive_scanner if passive else active_scanner

        passive_scanner = passive
        active_scanner = active
        worker._new_scanner = new_scanner
        task = asyncio.create_task(worker._scan())
        await asyncio.wait_for(active_started.wait(), timeout=0.2)

        worker.request_scan_pause()
        worker.request_scan_pause()
        await asyncio.wait_for(paused.wait(), timeout=0.2)
        assert active.stop_calls == 1

        worker.request_scan_resume()
        worker.request_scan_resume()
        await asyncio.wait_for(resumed.wait(), timeout=0.2)
        assert active.start_calls == 2

        worker.request_stop()
        await asyncio.wait_for(task, timeout=0.2)

    asyncio.run(scenario())


def test_shutdown_does_not_resume_paused_active_scan():
    async def scenario():
        worker = make_async_worker()
        active_started = asyncio.Event()
        paused = asyncio.Event()
        worker.scan_paused.connect(paused.set)
        passive = FakeScanner(start_error=PASSIVE_UNSUPPORTED)
        active = FakeScanner(on_start=active_started.set)

        def new_scanner(*, passive: bool):
            return passive_scanner if passive else active_scanner

        passive_scanner = passive
        active_scanner = active
        worker._new_scanner = new_scanner
        task = asyncio.create_task(worker._scan())
        await asyncio.wait_for(active_started.wait(), timeout=0.2)
        worker.request_scan_pause()
        await asyncio.wait_for(paused.wait(), timeout=0.2)

        worker.request_stop()
        worker.request_scan_resume()
        await asyncio.wait_for(task, timeout=0.2)
        assert active.start_calls == 1

    asyncio.run(scenario())
