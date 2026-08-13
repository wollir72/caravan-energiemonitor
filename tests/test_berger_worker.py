from __future__ import annotations

import asyncio
import logging
import time
from types import MethodType, SimpleNamespace

import pytest

from caravan_energiemonitor.config import BergerConfig
from caravan_energiemonitor.devices.berger import worker as worker_module
from caravan_energiemonitor.devices.berger.worker import BergerWorker
from caravan_energiemonitor.devices.berger.protocol import FrameBoundaryError


BASIC_RESPONSE = bytes.fromhex(
    "DD 03 00 22 05 80 00 00 4D BD 4E 20 00 01 32 DB 00 00 00 00 "
    "00 00 29 64 03 04 01 0B D4 00 00 00 4E 20 4D BD 00 00 F9 E7 77"
)
CELL_RESPONSE = bytes.fromhex(
    "DD 04 00 08 0D B4 0D B8 0D B8 0D B7 FC E9 77"
)


def make_worker() -> BergerWorker:
    return BergerWorker(
        BergerConfig(
            enabled=True,
            name="Test-Batterie",
            bluetooth_name="TEST",
            address="A5:C2:37:63:DC:37",
            capacity_ah=200,
        ),
        poll_interval=0.01,
    )


def run_worker_test(scenario):
    async def run():
        worker = make_worker()
        worker._loop = asyncio.get_running_loop()
        worker._async_stop = asyncio.Event()
        worker._measurement_permission = asyncio.Event()
        worker._measurement_released = asyncio.Event()
        worker._control_changed = asyncio.Event()
        worker._normal_operation_permission = asyncio.Event()
        await scenario(worker)

    asyncio.run(run())


def test_does_not_scan_again_while_connected():
    async def scenario(worker):
        scans = 0
        session_entered = asyncio.Event()

        async def find_device(self):
            nonlocal scans
            scans += 1
            return SimpleNamespace(address=self._config.address)

        async def connected_session(self, _device):
            session_entered.set()
            assert self._async_stop is not None
            await self._async_stop.wait()

        worker._find_device = MethodType(find_device, worker)
        worker._connected_session = MethodType(connected_session, worker)
        task = asyncio.create_task(worker._run_reconnecting())
        await asyncio.wait_for(session_entered.wait(), timeout=0.2)
        await asyncio.sleep(0.02)
        assert scans == 1
        worker._async_stop.set()
        await asyncio.wait_for(task, timeout=0.2)

    run_worker_test(scenario)


def test_disconnect_causes_bounded_reconnect_backoff(monkeypatch):
    monkeypatch.setattr(worker_module, "INITIAL_BACKOFF", 1.0)
    monkeypatch.setattr(worker_module, "MAX_BACKOFF", 3.0)

    async def scenario(worker):
        attempts = 0
        waits = []

        async def find_device(self):
            return SimpleNamespace(address=self._config.address)

        async def connected_session(self, _device):
            nonlocal attempts
            attempts += 1
            if attempts == 5:
                self._stop_requested.set()
                return
            raise ConnectionError("test disconnect")

        async def wait_or_stop(self, seconds):
            waits.append(seconds)
            return False

        worker._find_device = MethodType(find_device, worker)
        worker._connected_session = MethodType(connected_session, worker)
        worker._wait_or_stop = MethodType(wait_or_stop, worker)
        await worker._run_reconnecting()

        assert attempts == 5
        assert waits == [1.0, 2.0, 3.0, 3.0]

    run_worker_test(scenario)


def test_stop_interrupts_backoff_promptly():
    async def scenario(worker):
        wait_started = asyncio.Event()

        async def wait_in_backoff():
            wait_started.set()
            return await worker._wait_or_stop(30.0)

        task = asyncio.create_task(wait_in_backoff())
        await wait_started.wait()
        worker.request_stop()
        assert await asyncio.wait_for(task, timeout=0.2) is True

    run_worker_test(scenario)


def test_initial_attempt_signal_is_emitted_once_on_success():
    async def scenario(worker):
        emissions = []
        worker.initial_connection_attempt_finished.connect(lambda: emissions.append(1))

        async def find_device(self):
            return SimpleNamespace(address=self._config.address)

        async def connected_session(self, _device):
            self._report_initial_attempt_finished()
            self._report_initial_attempt_finished()
            self._stop_requested.set()

        worker._find_device = MethodType(find_device, worker)
        worker._connected_session = MethodType(connected_session, worker)
        await worker._run_reconnecting()
        assert emissions == [1]

    run_worker_test(scenario)


def test_initial_attempt_signal_is_emitted_once_on_failure():
    async def scenario(worker):
        emissions = []
        worker.initial_connection_attempt_finished.connect(lambda: emissions.append(1))

        async def find_device(self):
            return None

        async def wait_or_stop(self, _seconds):
            self._stop_requested.set()
            return True

        worker._find_device = MethodType(find_device, worker)
        worker._wait_or_stop = MethodType(wait_or_stop, worker)
        await worker._run_reconnecting()
        assert emissions == [1]

    run_worker_test(scenario)


def test_registers_03_and_04_share_one_measurement_window():
    async def scenario(worker):
        registers = []
        windows = []
        worker.measurement_window_requested.connect(
            lambda: (windows.append("requested"), worker.grant_measurement_permission())
        )
        worker.measurement_window_finished.connect(
            lambda: (windows.append("finished"), worker.confirm_measurement_window_finished())
        )

        async def request_with_retry(self, _client, register, _command):
            registers.append(register)
            return b"frame"

        worker._request_with_retry = MethodType(request_with_retry, worker)
        worker._emit_snapshot = lambda *_args: windows.append("snapshot")
        await worker._measurement_cycle(object(), SimpleNamespace(address="test"))

        assert registers == [worker_module.BASIC_REGISTER, worker_module.CELL_REGISTER]
        assert windows == ["requested", "snapshot", "finished"]

    run_worker_test(scenario)


def test_scan_is_released_even_when_berger_times_out():
    async def scenario(worker):
        released = []
        worker.measurement_window_requested.connect(worker.grant_measurement_permission)
        worker.measurement_window_finished.connect(
            lambda: (released.append(True), worker.confirm_measurement_window_finished())
        )

        async def request_with_retry(self, _client, _register, _command):
            raise TimeoutError("test response timeout")

        worker._request_with_retry = MethodType(request_with_retry, worker)
        with pytest.raises(TimeoutError):
            await worker._measurement_cycle(object(), SimpleNamespace(address="test"))
        assert released == [True]

    run_worker_test(scenario)


def test_single_response_timeout_is_retried_in_same_connection():
    async def scenario(worker):
        calls = 0

        async def request(self, _client, _register, _command):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("first response timeout")
            return b"response"

        worker._request = MethodType(request, worker)
        result = await worker._request_with_retry(
            object(), worker_module.BASIC_REGISTER, worker_module.BASIC_INFO_COMMAND
        )
        assert result == b"response"
        assert calls == 2

    run_worker_test(scenario)


def test_reconnect_only_after_two_failed_measurement_windows(monkeypatch):
    class FakeClient:
        instances = 0

        def __init__(self, *_args, **_kwargs):
            type(self).instances += 1
            self.is_connected = False

        async def connect(self):
            self.is_connected = True

        async def start_notify(self, *_args):
            return None

        async def stop_notify(self, *_args):
            return None

        async def disconnect(self):
            self.is_connected = False

    monkeypatch.setattr(worker_module, "BleakClient", FakeClient)

    async def scenario(worker):
        windows = 0

        async def measurement_cycle(self, _client, _device):
            nonlocal windows
            windows += 1
            raise TimeoutError("measurement timeout")

        async def wait_or_stop(self, _seconds):
            return False

        worker._measurement_cycle = MethodType(measurement_cycle, worker)
        worker._wait_or_stop = MethodType(wait_or_stop, worker)
        with pytest.raises(ConnectionError, match="Zwei Berger-Messfenster"):
            await worker._connected_session(
                SimpleNamespace(address=worker._config.address)
            )

        assert windows == 2
        assert FakeClient.instances == 1

    run_worker_test(scenario)


def test_disconnect_succeeds(caplog):
    class Client:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    async def scenario(worker):
        client = Client()
        await worker._disconnect_client(client)
        assert client.disconnected is True

    with caplog.at_level(logging.INFO):
        run_worker_test(scenario)
    assert "Berger-Disconnect bestätigt" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_disconnect_timeout_during_shutdown_is_not_error(monkeypatch, caplog):
    monkeypatch.setattr(worker_module, "CLEANUP_TIMEOUT", 0.01)

    class Client:
        async def disconnect(self):
            await asyncio.sleep(10)

    async def scenario(worker):
        worker._stop_requested.set()
        await worker._disconnect_client(Client())

    with caplog.at_level(logging.INFO):
        run_worker_test(scenario)
    assert "BlueZ räumt die Verbindung asynchron auf" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_disconnect_timeout_during_normal_operation_remains_error(
    monkeypatch, caplog
):
    monkeypatch.setattr(worker_module, "CLEANUP_TIMEOUT", 0.01)

    class Client:
        async def disconnect(self):
            await asyncio.sleep(10)

    async def scenario(worker):
        await worker._disconnect_client(Client())

    with caplog.at_level(logging.ERROR):
        run_worker_test(scenario)
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "Berger trennen" in errors[0].message
    assert errors[0].exc_info is not None


def test_hanging_disconnect_finishes_within_cleanup_bound(monkeypatch):
    monkeypatch.setattr(worker_module, "CLEANUP_TIMEOUT", 0.02)

    class Client:
        async def disconnect(self):
            await asyncio.sleep(10)

    async def scenario(worker):
        worker._stop_requested.set()
        started = time.monotonic()
        await worker._disconnect_client(Client())
        assert time.monotonic() - started < 0.2

    run_worker_test(scenario)


def test_history_pause_interrupts_discovery_and_stops_scanner_once(monkeypatch):
    async def scenario(worker):
        created = asyncio.Event()

        class FakeScanner:
            instance = None

            def __init__(self, **_kwargs):
                self.started = asyncio.Event()
                self.start_calls = 0
                self.stop_calls = 0
                type(self).instance = self
                created.set()

            async def start(self):
                self.start_calls += 1
                self.started.set()

            async def stop(self):
                self.stop_calls += 1

        monkeypatch.setattr(worker_module, "BleakScanner", FakeScanner)
        task = asyncio.create_task(worker._find_device())
        await created.wait()
        scanner = FakeScanner.instance
        await scanner.started.wait()
        worker._history_pause_requested.set()
        worker._control_changed.set()
        with pytest.raises(worker_module._HistoryPauseRequested):
            await task
        assert scanner.start_calls == 1
        assert scanner.stop_calls == 1

    run_worker_test(scenario)


def test_history_pause_during_scanner_start_still_runs_scanner_cleanup(monkeypatch):
    async def scenario(worker):
        start_entered = asyncio.Event()
        release_start = asyncio.Event()

        class FakeScanner:
            instance = None

            def __init__(self, **_kwargs):
                self.stop_calls = 0
                type(self).instance = self

            async def start(self):
                start_entered.set()
                await release_start.wait()

            async def stop(self):
                self.stop_calls += 1

        monkeypatch.setattr(worker_module, "BleakScanner", FakeScanner)
        task = asyncio.create_task(worker._find_device())
        await start_entered.wait()
        worker._history_pause_requested.set()
        worker._control_changed.set()
        with pytest.raises(worker_module._HistoryPauseRequested):
            await task
        assert FakeScanner.instance.stop_calls == 1

    run_worker_test(scenario)


def test_shutdown_during_berger_discovery_stops_scanner_and_finishes(monkeypatch):
    async def scenario(worker):
        scan_started = asyncio.Event()

        class FakeScanner:
            instance = None

            def __init__(self, **_kwargs):
                self.stop_calls = 0
                type(self).instance = self

            async def start(self):
                scan_started.set()

            async def stop(self):
                self.stop_calls += 1

        monkeypatch.setattr(worker_module, "BleakScanner", FakeScanner)
        task = asyncio.create_task(worker._run_reconnecting())
        await scan_started.wait()
        worker.request_stop()
        await task
        assert FakeScanner.instance.stop_calls == 1

    run_worker_test(scenario)


def test_restore_barrier_prevents_reconnect_until_coordinator_grants():
    async def scenario(worker):
        scans = 0
        ready = asyncio.Event()
        worker.normal_operation_ready.connect(ready.set)
        worker._normal_resume_pending.set()

        async def find_device(self):
            nonlocal scans
            scans += 1
            return None

        async def wait_or_stop(self, _seconds):
            self._stop_requested.set()
            return True

        worker._find_device = MethodType(find_device, worker)
        worker._wait_or_stop = MethodType(wait_or_stop, worker)
        task = asyncio.create_task(worker._run_reconnecting())
        assert scans == 0
        await ready.wait()
        assert scans == 1
        worker.grant_normal_operation()
        await task
        assert scans == 1

    run_worker_test(scenario)


def test_history_pause_between_03_and_04_skips_cell_read():
    async def scenario(worker):
        registers = []
        worker.measurement_window_requested.connect(worker.grant_measurement_permission)
        worker.measurement_window_finished.connect(
            worker.confirm_measurement_window_finished
        )

        async def request_with_retry(self, _client, register, _command):
            registers.append(register)
            if register == worker_module.BASIC_REGISTER:
                self._history_pause_requested.set()
            return b"frame"

        worker._request_with_retry = MethodType(request_with_retry, worker)
        with pytest.raises(worker_module._HistoryPauseRequested):
            await worker._measurement_cycle(object(), SimpleNamespace(address="test"))
        assert registers == [worker_module.BASIC_REGISTER]

    run_worker_test(scenario)


def test_worker_starts_with_clean_rx_state():
    worker = make_worker()
    assert worker._response_future is None
    assert worker._expected_register is None
    assert worker._assembler.feed(BASIC_RESPONSE) == [BASIC_RESPONSE]


def test_central_reset_cancels_future_and_discards_partial_frame():
    async def scenario(worker):
        old_future = asyncio.get_running_loop().create_future()
        worker._response_future = old_future
        worker._expected_register = worker_module.BASIC_REGISTER
        assert worker._assembler.feed(BASIC_RESPONSE[:12]) == []

        worker._reset_rx_state("test")

        assert old_future.cancelled()
        assert worker._response_future is None
        assert worker._expected_register is None
        assert worker._assembler.feed(BASIC_RESPONSE[12:]) == []
        assert worker._assembler.feed(BASIC_RESPONSE) == [BASIC_RESPONSE]

    run_worker_test(scenario)


def test_connection_boundaries_reset_rx_state(monkeypatch):
    async def scenario(worker):
        checks = []

        def assert_clean(boundary):
            checks.append(boundary)
            assert worker._response_future is None
            assert worker._expected_register is None
            assert worker._assembler.feed(b"") == []

        def dirty_state():
            worker._response_future = asyncio.get_running_loop().create_future()
            worker._expected_register = worker_module.BASIC_REGISTER
            worker._assembler.feed(BASIC_RESPONSE[:9])

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                self.is_connected = False

            async def connect(self):
                assert_clean("vor Connect")
                self.is_connected = True
                dirty_state()

            async def start_notify(self, *_args):
                assert_clean("nach Connect")
                dirty_state()

            async def stop_notify(self, *_args):
                return None

            async def disconnect(self):
                self.is_connected = False

        monkeypatch.setattr(worker_module, "BleakClient", FakeClient)

        dirty_state()

        async def measurement_cycle(self, _client, _device):
            assert_clean("nach start_notify")
            self._stop_requested.set()

        worker._measurement_cycle = MethodType(measurement_cycle, worker)
        await worker._connected_session(SimpleNamespace(address="test"))
        assert_clean("Session verlassen")
        assert checks == [
            "vor Connect",
            "nach Connect",
            "nach start_notify",
            "Session verlassen",
        ]

    run_worker_test(scenario)


def test_disconnect_resets_pending_rx_state(monkeypatch):
    async def scenario(worker):
        class FakeClient:
            def __init__(self, *_args, disconnected_callback, **_kwargs):
                self.is_connected = False
                self.disconnected_callback = disconnected_callback

            async def connect(self):
                self.is_connected = True

            async def start_notify(self, *_args):
                return None

            async def disconnect(self):
                self.is_connected = False

        monkeypatch.setattr(worker_module, "BleakClient", FakeClient)

        async def measurement_cycle(self, client, _device):
            pending = asyncio.get_running_loop().create_future()
            self._response_future = pending
            self._expected_register = worker_module.BASIC_REGISTER
            self._assembler.feed(BASIC_RESPONSE[:8])
            client.is_connected = False
            client.disconnected_callback(client)
            assert pending.cancelled()
            assert self._response_future is None
            assert self._expected_register is None

        worker._measurement_cycle = MethodType(measurement_cycle, worker)
        with pytest.raises(ConnectionError):
            await worker._connected_session(SimpleNamespace(address="test"))

    run_worker_test(scenario)


def test_timeout_retry_uses_new_future_and_discards_partial_frame(
    monkeypatch, caplog
):
    monkeypatch.setattr(worker_module, "REQUEST_TIMEOUT", 0.01)

    async def scenario(worker):
        worker._begin_rx_session()
        futures = []

        class Client:
            calls = 0

            async def write_gatt_char(self, *_args, **_kwargs):
                self.calls += 1
                futures.append(worker._response_future)
                if self.calls == 1:
                    worker._notification(
                        worker._rx_session, None, bytearray(BASIC_RESPONSE[:13])
                    )
                else:
                    asyncio.get_running_loop().call_soon(
                        worker._notification,
                        worker._rx_session,
                        None,
                        bytearray(BASIC_RESPONSE),
                    )

        frame = await worker._request_with_retry(
            Client(), worker_module.BASIC_REGISTER, worker_module.BASIC_INFO_COMMAND
        )
        assert frame == BASIC_RESPONSE
        assert len(futures) == 2
        assert futures[0] is not futures[1]
        assert futures[0].cancelled()
        assert worker._response_future is None
        assert worker._expected_register is None

    with caplog.at_level(logging.INFO):
        run_worker_test(scenario)
    assert "RX-State nach Notification-Timeout verworfen" in caplog.text
    assert "Retry 0x03 mit neuem Response-State" in caplog.text


def test_old_register_cannot_fulfil_current_request(caplog):
    async def scenario(worker):
        worker._begin_rx_session()
        current = asyncio.get_running_loop().create_future()
        worker._response_future = current
        worker._expected_register = worker_module.CELL_REGISTER

        worker._notification(
            worker._rx_session, None, bytearray(BASIC_RESPONSE)
        )
        assert not current.done()
        worker._notification(
            worker._rx_session, None, bytearray(CELL_RESPONSE)
        )
        assert await current == CELL_RESPONSE

    with caplog.at_level(logging.DEBUG):
        run_worker_test(scenario)
    assert "Unerwartete Berger-Notification verworfen: DD 03" in caplog.text
    assert f"Länge={len(BASIC_RESPONSE)}" in caplog.text
    assert "erwartet=0x04" in caplog.text
    assert "tatsächlich=0x03" in caplog.text


def test_callback_from_old_session_is_ignored():
    async def scenario(worker):
        old_session = worker._begin_rx_session()
        new_session = worker._begin_rx_session()
        current = asyncio.get_running_loop().create_future()
        worker._response_future = current
        worker._expected_register = worker_module.BASIC_REGISTER

        worker._notification(old_session, None, bytearray(BASIC_RESPONSE))
        assert not current.done()
        worker._notification(new_session, None, bytearray(BASIC_RESPONSE))
        assert await current == BASIC_RESPONSE

    run_worker_test(scenario)


def test_two_sessions_do_not_share_partial_frames():
    async def scenario(worker):
        first = worker._begin_rx_session()
        worker._notification(first, None, bytearray(BASIC_RESPONSE[:15]))
        second = worker._begin_rx_session()
        current = asyncio.get_running_loop().create_future()
        worker._response_future = current
        worker._expected_register = worker_module.BASIC_REGISTER

        worker._notification(second, None, bytearray(BASIC_RESPONSE))
        assert await current == BASIC_RESPONSE

    run_worker_test(scenario)


def test_worker_shutdown_resets_pending_rx_state():
    async def scenario(worker):
        pending = asyncio.get_running_loop().create_future()
        worker._response_future = pending
        worker._expected_register = worker_module.BASIC_REGISTER
        worker._assembler.feed(BASIC_RESPONSE[:11])

        worker.request_stop()
        await asyncio.sleep(0)

        assert pending.cancelled()
        assert worker._response_future is None
        assert worker._expected_register is None
        assert worker._assembler.feed(BASIC_RESPONSE[11:]) == []

    run_worker_test(scenario)


def test_parser_error_resets_rx_and_next_valid_snapshot_succeeds(caplog):
    async def scenario(worker):
        snapshots = []
        worker.snapshot_received.connect(snapshots.append)
        pending = asyncio.get_running_loop().create_future()
        worker._response_future = pending
        worker._expected_register = worker_module.BASIC_REGISTER
        broken = BASIC_RESPONSE[:-1] + b"\x00"

        with pytest.raises(FrameBoundaryError):
            worker._emit_snapshot(
                SimpleNamespace(address="test"), broken, CELL_RESPONSE
            )
        assert pending.cancelled()
        assert worker._response_future is None
        assert worker._expected_register is None

        worker._emit_snapshot(
            SimpleNamespace(address="test"), BASIC_RESPONSE, CELL_RESPONSE
        )
        assert len(snapshots) == 1
        assert snapshots[0].voltage == 14.08

    with caplog.at_level(logging.WARNING):
        run_worker_test(scenario)
    assert "Berger-Frame ungültig: DD 03" in caplog.text
    assert f"Länge={len(BASIC_RESPONSE)}" in caplog.text
    assert "erwartet=0x03" in caplog.text
    assert "tatsächlich=0x03" in caplog.text
    assert "advertisement_key" not in caplog.text


def test_observed_reconnect_timeout_sequence_produces_snapshot(monkeypatch):
    monkeypatch.setattr(worker_module, "REQUEST_TIMEOUT", 0.01)

    async def scenario(worker):
        # Session 1 completes normally and is then disconnected.
        worker._begin_rx_session()

        class NormalClient:
            async def write_gatt_char(self, _uuid, command, **_kwargs):
                frame = (
                    BASIC_RESPONSE
                    if command == worker_module.BASIC_INFO_COMMAND
                    else CELL_RESPONSE
                )
                asyncio.get_running_loop().call_soon(
                    worker._notification,
                    worker._rx_session,
                    None,
                    bytearray(frame),
                )

        normal_client = NormalClient()
        first_basic = await worker._request_with_retry(
            normal_client,
            worker_module.BASIC_REGISTER,
            worker_module.BASIC_INFO_COMMAND,
        )
        first_cells = await worker._request_with_retry(
            normal_client,
            worker_module.CELL_REGISTER,
            worker_module.CELL_VOLTAGES_COMMAND,
        )
        snapshots = []
        worker.snapshot_received.connect(snapshots.append)
        worker._emit_snapshot(
            SimpleNamespace(address="test"), first_basic, first_cells
        )
        worker._end_rx_session("simulierter Disconnect")

        # Session 2 starts clean. Its first 0x03 response remains partial,
        # times out, and the retry plus 0x04 complete normally.
        worker._begin_rx_session()

        class Client:
            calls = []

            async def write_gatt_char(self, _uuid, command, **_kwargs):
                self.calls.append(command)
                if self.calls == [worker_module.BASIC_INFO_COMMAND]:
                    worker._notification(
                        worker._rx_session, None, bytearray(BASIC_RESPONSE[:17])
                    )
                    return
                frame = (
                    BASIC_RESPONSE
                    if command == worker_module.BASIC_INFO_COMMAND
                    else CELL_RESPONSE
                )
                asyncio.get_running_loop().call_soon(
                    worker._notification,
                    worker._rx_session,
                    None,
                    bytearray(frame),
                )

        client = Client()
        basic = await worker._request_with_retry(
            client, worker_module.BASIC_REGISTER, worker_module.BASIC_INFO_COMMAND
        )
        cells = await worker._request_with_retry(
            client, worker_module.CELL_REGISTER, worker_module.CELL_VOLTAGES_COMMAND
        )
        worker._emit_snapshot(SimpleNamespace(address="test"), basic, cells)

        assert client.calls == [
            worker_module.BASIC_INFO_COMMAND,
            worker_module.BASIC_INFO_COMMAND,
            worker_module.CELL_VOLTAGES_COMMAND,
        ]
        assert len(snapshots) == 2
        assert snapshots[-1].cell_voltages == pytest.approx(
            (3.508, 3.512, 3.512, 3.511)
        )

    run_worker_test(scenario)


def test_complete_corrupt_frame_remains_error():
    worker = make_worker()
    broken = BASIC_RESPONSE[:-1] + b"\x76"
    with pytest.raises(FrameBoundaryError):
        worker._emit_snapshot(SimpleNamespace(address="test"), broken, CELL_RESPONSE)
