from __future__ import annotations

import asyncio
import logging
import time
from types import MethodType, SimpleNamespace

import pytest

from caravan_energiemonitor.config import BergerConfig
from caravan_energiemonitor.devices.berger import worker as worker_module
from caravan_energiemonitor.devices.berger.worker import BergerWorker


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
