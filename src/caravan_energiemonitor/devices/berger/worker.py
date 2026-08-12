"""Active, read-only Berger/JBD polling in a dedicated Qt thread."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Awaitable, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from PySide6.QtCore import QThread, Signal

from ...config import BergerConfig
from ...models import BatterySnapshot
from .protocol import (
    BASIC_INFO_COMMAND,
    BASIC_REGISTER,
    CELL_REGISTER,
    CELL_VOLTAGES_COMMAND,
    BergerProtocolError,
    FrameAssembler,
    parse_basic_response,
    parse_cell_response,
)

LOG = logging.getLogger(__name__)

SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

SCAN_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
REQUEST_TIMEOUT = 5.0
CLEANUP_TIMEOUT = 3.0
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 15.0
MEASUREMENT_WINDOW_TIMEOUT = 5.0
MEASUREMENT_RELEASE_TIMEOUT = 5.0
MAX_FAILED_MEASUREMENT_WINDOWS = 2

T = TypeVar("T")


class _StopRequested(Exception):
    """Internal control-flow exception used to interrupt BLE operations."""


class _HistoryPauseRequested(Exception):
    """A safe discovery/connect boundary was interrupted for exclusive History."""


class BergerWorker(QThread):
    """Find, connect and poll one Berger battery without changing BMS state."""

    snapshot_received = Signal(object)
    target_activity = Signal()
    worker_heartbeat = Signal()
    bluetooth_error = Signal(str)
    connection_state_changed = Signal(bool)
    initial_connection_attempt_finished = Signal()
    measurement_window_requested = Signal()
    measurement_window_finished = Signal()
    history_pause_changed = Signal(bool)
    normal_operation_ready = Signal()

    def __init__(
        self, config: BergerConfig, parent: Any = None, poll_interval: float = 5.0
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._poll_interval = poll_interval
        self._stop_requested = threading.Event()
        self._history_pause_requested = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._measurement_permission: asyncio.Event | None = None
        self._measurement_released: asyncio.Event | None = None
        self._control_changed: asyncio.Event | None = None
        self._normal_operation_permission: asyncio.Event | None = None
        self._normal_resume_pending = threading.Event()
        self._assembler = FrameAssembler()
        self._responses: dict[int, asyncio.Future[bytes]] = {}
        self._rssi: int | None = None
        self._initial_attempt_reported = False
        self._connected_in_attempt = False
        self._snapshot_received_in_attempt = False
        self._connection_attempt = 0

    def _set_async_event(self, event: asyncio.Event | None) -> None:
        loop = self._loop
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def grant_measurement_permission(self) -> None:
        """Thread-safe acknowledgement from the central BLE coordinator."""
        self._set_async_event(self._measurement_permission)

    def confirm_measurement_window_finished(self) -> None:
        """Thread-safe confirmation that Victron scanning is usable again."""
        self._set_async_event(self._measurement_released)

    def request_history_pause(self) -> None:
        """Finish the current frame, disconnect GATT, then confirm exclusivity."""
        if self._stop_requested.is_set():
            return
        self._history_pause_requested.set()
        if not self.isRunning():
            self.history_pause_changed.emit(True)
        self._set_async_event(self._control_changed)

    def request_history_resume(self) -> None:
        if self._stop_requested.is_set():
            return
        was_paused = self._history_pause_requested.is_set()
        self._history_pause_requested.clear()
        if was_paused:
            self._normal_resume_pending.set()
        if not self.isRunning():
            self._normal_resume_pending.clear()
            self.history_pause_changed.emit(False)
            self.normal_operation_ready.emit()
        self._set_async_event(self._control_changed)

    def grant_normal_operation(self) -> None:
        """Release a restored worker only after the BLE transition is safe."""
        self._set_async_event(self._normal_operation_permission)

    def request_stop(self) -> None:
        """Request shutdown and wake every stop-aware async wait."""
        LOG.info("Stop für Berger-Worker angefordert")
        self._stop_requested.set()
        self._history_pause_requested.set()
        loop = self._loop
        stop = self._async_stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass  # The worker completed between reading and using the loop.

    def run(self) -> None:
        LOG.info("Berger-Worker gestartet (%s)", self._config.address)
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._async_stop = asyncio.Event()
        self._measurement_permission = asyncio.Event()
        self._measurement_released = asyncio.Event()
        self._control_changed = asyncio.Event()
        self._normal_operation_permission = asyncio.Event()
        try:
            loop.run_until_complete(self._run_reconnecting())
        except Exception as exc:  # defensive last-resort boundary for Qt thread
            message = f"Berger-Bluetooth konnte nicht gestartet werden: {exc}"
            LOG.exception("Berger-Worker endgültig abgebrochen: %s", message)
            self.bluetooth_error.emit(message)
        finally:
            self._report_initial_attempt_finished()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._async_stop = None
            self._measurement_permission = None
            self._measurement_released = None
            self._control_changed = None
            self._normal_operation_permission = None
            self._loop = None
            if self._stop_requested.is_set():
                LOG.info("Berger-Worker Shutdown abgeschlossen")
            else:
                LOG.info("Berger-Worker beendet")

    def _report_initial_attempt_finished(self) -> None:
        """Emit the startup gate exactly once, on both success and failure."""
        if self._initial_attempt_reported:
            return
        self._initial_attempt_reported = True
        LOG.info("Erster Berger-Verbindungsversuch abgeschlossen")
        self.initial_connection_attempt_finished.emit()

    async def _wait_or_stop(self, seconds: float) -> bool:
        """Wait without a busy loop; return true when shutdown was requested."""
        assert self._async_stop is not None
        if self._control_changed is None:
            self._control_changed = asyncio.Event()
        self._control_changed.clear()
        stop_task = asyncio.create_task(self._async_stop.wait())
        control_task = asyncio.create_task(self._control_changed.wait())
        try:
            done, _ = await asyncio.wait(
                {stop_task, control_task}, timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            return stop_task in done and stop_task.result()
        finally:
            for task in (stop_task, control_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, control_task, return_exceptions=True)

    async def _wait_while_history_paused(self) -> None:
        assert self._async_stop is not None
        assert self._control_changed is not None
        self.history_pause_changed.emit(True)
        LOG.info("Berger-GATT für Victron-History vollständig getrennt")
        try:
            while self._history_pause_requested.is_set() and not self._stop_requested.is_set():
                self._control_changed.clear()
                stop_task = asyncio.create_task(self._async_stop.wait())
                control_task = asyncio.create_task(self._control_changed.wait())
                done, pending = await asyncio.wait(
                    {stop_task, control_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stop_task in done and stop_task.result():
                    break
        finally:
            self.history_pause_changed.emit(False)

    async def _operation_or_stop(
        self,
        awaitable: Awaitable[T],
        timeout: float,
        description: str,
        *,
        interrupt_for_history: bool = False,
    ) -> T:
        """Run one BLE operation until completion, timeout, or worker stop."""
        assert self._async_stop is not None
        operation = asyncio.ensure_future(awaitable)
        stop_task = asyncio.create_task(self._async_stop.wait())
        pause_task = (
            asyncio.create_task(self._wait_for_history_pause())
            if interrupt_for_history
            else None
        )
        try:
            waiters = {operation, stop_task}
            if pause_task is not None:
                waiters.add(pause_task)
            done, _ = await asyncio.wait(
                waiters,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return await operation
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if stop_task in done or self._stop_requested.is_set():
                raise _StopRequested
            if pause_task is not None and (
                pause_task in done or self._history_pause_requested.is_set()
            ):
                raise _HistoryPauseRequested
            raise TimeoutError(f"Zeitüberschreitung bei {description}.")
        finally:
            for task in (stop_task, pause_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (stop_task, pause_task) if task is not None),
                return_exceptions=True,
            )

    async def _wait_for_history_pause(self) -> None:
        assert self._control_changed is not None
        while not self._history_pause_requested.is_set():
            self._control_changed.clear()
            if self._history_pause_requested.is_set():
                break
            await self._control_changed.wait()

    async def _normal_operation_barrier(self) -> bool:
        """Wait until the coordinator admits one post-History reconnect cycle."""
        if not self._normal_resume_pending.is_set():
            return True
        assert self._async_stop is not None
        assert self._normal_operation_permission is not None
        self._normal_operation_permission.clear()
        LOG.info("Berger-Restore wartet auf zentrale BLE-Freigabe")
        self.normal_operation_ready.emit()
        permission_task = asyncio.create_task(self._normal_operation_permission.wait())
        stop_task = asyncio.create_task(self._async_stop.wait())
        pause_task = asyncio.create_task(self._wait_for_history_pause())
        try:
            done, _ = await asyncio.wait(
                {permission_task, stop_task, pause_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done or self._stop_requested.is_set():
                raise _StopRequested
            if pause_task in done or self._history_pause_requested.is_set():
                return False
            self._normal_resume_pending.clear()
            LOG.info("Berger-Restore zentral freigegeben")
            return True
        finally:
            for task in (permission_task, stop_task, pause_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                permission_task, stop_task, pause_task, return_exceptions=True
            )

    @staticmethod
    async def _bounded_cleanup(awaitable: Awaitable[Any], description: str) -> None:
        """Best-effort cleanup that cannot hold shutdown indefinitely."""
        try:
            await asyncio.wait_for(awaitable, timeout=CLEANUP_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Berger-Cleanup fehlgeschlagen: %s", description)

    async def _disconnect_client(self, client: Any) -> None:
        """Disconnect with shutdown-specific handling for delayed BlueZ replies."""
        try:
            await asyncio.wait_for(client.disconnect(), timeout=CLEANUP_TIMEOUT)
            LOG.info("Berger-Disconnect bestätigt")
        except asyncio.CancelledError:
            if self._stop_requested.is_set():
                LOG.warning(
                    "Berger-Disconnect beim Shutdown abgebrochen; "
                    "BlueZ räumt die Verbindung asynchron auf."
                )
                return
            LOG.exception("Berger-Cleanup fehlgeschlagen: Berger trennen")
            raise
        except TimeoutError:
            if self._stop_requested.is_set():
                LOG.warning(
                    "Berger-Disconnect innerhalb des Cleanup-Timeouts nicht "
                    "bestätigt; BlueZ räumt die Verbindung asynchron auf."
                )
                return
            LOG.exception("Berger-Cleanup fehlgeschlagen: Berger trennen")
        except Exception:
            LOG.exception("Berger-Cleanup fehlgeschlagen: Berger trennen")

    async def _run_reconnecting(self) -> None:
        backoff = INITIAL_BACKOFF
        attempt = 0
        while not self._stop_requested.is_set():
            if self._history_pause_requested.is_set():
                await self._wait_while_history_paused()
                continue
            attempt += 1
            self._connection_attempt = attempt
            self._connected_in_attempt = False
            self._snapshot_received_in_attempt = False
            self.worker_heartbeat.emit()
            LOG.info("Berger-Verbindungsversuch %d", attempt)
            try:
                # Exactly one discovery is performed for this connection attempt.
                device = await self._find_device()
                if device is None:
                    raise TimeoutError(
                        f"Berger-Batterie {self._config.address} nicht gefunden."
                    )
                if self._history_pause_requested.is_set():
                    continue
                await self._connected_session(device)
                backoff = INITIAL_BACKOFF
            except _StopRequested:
                break
            except _HistoryPauseRequested:
                LOG.info("Berger-Reconnect vor History kontrolliert gestoppt")
                continue
            except asyncio.CancelledError:
                raise
            except (BleakError, TimeoutError, ConnectionError, BergerProtocolError) as exc:
                if self._snapshot_received_in_attempt:
                    backoff = INITIAL_BACKOFF
                self.connection_state_changed.emit(False)
                message = f"Berger-Bluetooth-Fehler: {exc}"
                LOG.warning("%s", message, exc_info=True)
                self.bluetooth_error.emit(message)
            except Exception as exc:
                if self._snapshot_received_in_attempt:
                    backoff = INITIAL_BACKOFF
                self.connection_state_changed.emit(False)
                message = f"Unerwarteter Berger-Bluetooth-Fehler: {exc}"
                LOG.exception(message)
                self.bluetooth_error.emit(message)
            finally:
                self._report_initial_attempt_finished()

            if self._stop_requested.is_set():
                break
            if self._history_pause_requested.is_set():
                continue
            if self._normal_resume_pending.is_set():
                try:
                    if not await self._normal_operation_barrier():
                        continue
                except _StopRequested:
                    break
            LOG.info("Berger-Reconnect in %.1f Sekunden", backoff)
            if await self._wait_or_stop(backoff):
                break
            backoff = min(backoff * 2, MAX_BACKOFF)

    async def _find_device(self):
        """Scan once for the configured MAC and retain its advertisement RSSI."""
        found: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

        def detection_callback(device, advertisement) -> None:
            if device.address.casefold() != self._config.address.casefold():
                return
            self._rssi = advertisement.rssi
            self.target_activity.emit()
            if not found.done():
                LOG.info("Berger-Gerät gefunden: %s", device.address)
                found.set_result(device)

        scanner = BleakScanner(detection_callback=detection_callback)
        start_attempted = False
        LOG.info("Berger-Scan beginnt (%s)", self._config.address)
        try:
            start_attempted = True
            await self._operation_or_stop(
                scanner.start(),
                CONNECT_TIMEOUT,
                "Start des Berger-Scans",
                interrupt_for_history=True,
            )
            return await self._operation_or_stop(
                found,
                SCAN_TIMEOUT,
                "Berger-Gerätesuche",
                interrupt_for_history=True,
            )
        finally:
            if not found.done():
                found.cancel()
            if start_attempted:
                await self._bounded_cleanup(scanner.stop(), "Berger-Scan stoppen")
            LOG.info("Berger-Scan beendet")

    async def _connected_session(self, device) -> None:
        disconnected = asyncio.Event()
        disconnect_expected = False

        def on_disconnect(_client) -> None:
            if not disconnect_expected:
                LOG.warning("Berger-Verbindung verloren (%s)", device.address)
            disconnected.set()

        self._assembler.clear()
        client = BleakClient(
            device,
            disconnected_callback=on_disconnect,
            timeout=CONNECT_TIMEOUT,
        )
        notifications_started = False
        connected = False
        try:
            LOG.info("Berger-Verbindung wird aufgebaut (%s)", device.address)
            await self._operation_or_stop(
                client.connect(),
                CONNECT_TIMEOUT,
                "Berger-Verbindungsaufbau",
                interrupt_for_history=True,
            )
            if not client.is_connected:
                raise ConnectionError("Berger-Verbindung wurde nicht hergestellt.")
            connected = True
            self._connected_in_attempt = True
            self.connection_state_changed.emit(True)
            LOG.info("Berger-Verbindung hergestellt (%s)", device.address)
            if self._connection_attempt > 1:
                LOG.info(
                    "Berger-Reconnect erfolgreich (Versuch %d)",
                    self._connection_attempt,
                )
            self._report_initial_attempt_finished()

            await self._operation_or_stop(
                client.start_notify(NOTIFY_UUID, self._notification),
                CONNECT_TIMEOUT,
                "Start der Berger-Notifications",
            )
            notifications_started = True
            if not await self._normal_operation_barrier():
                raise _HistoryPauseRequested

            failed_measurement_windows = 0
            while (
                not self._stop_requested.is_set()
                and not self._history_pause_requested.is_set()
                and client.is_connected
            ):
                self.worker_heartbeat.emit()
                try:
                    await self._measurement_cycle(client, device)
                    failed_measurement_windows = 0
                    self._snapshot_received_in_attempt = True
                except _StopRequested:
                    raise
                except (TimeoutError, BergerProtocolError) as exc:
                    failed_measurement_windows += 1
                    LOG.warning(
                        "Berger-Messfenster fehlgeschlagen (%d/%d): %s",
                        failed_measurement_windows,
                        MAX_FAILED_MEASUREMENT_WINDOWS,
                        exc,
                    )
                    if not client.is_connected:
                        raise ConnectionError(
                            "Verbindung zur Berger-Batterie abgebrochen."
                        ) from exc
                    if failed_measurement_windows >= MAX_FAILED_MEASUREMENT_WINDOWS:
                        LOG.warning(
                            "Berger-Reconnect erst nach wiederholtem Messfehlschlag"
                        )
                        raise ConnectionError(
                            "Zwei Berger-Messfenster nacheinander fehlgeschlagen."
                        ) from exc

                stop_task = asyncio.create_task(self._wait_or_stop(self._poll_interval))
                disconnect_task = asyncio.create_task(disconnected.wait())
                done, pending = await asyncio.wait(
                    {stop_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if disconnect_task in done and disconnect_task.result():
                    break
                if stop_task in done and stop_task.result():
                    break

            if not self._stop_requested.is_set() and (
                disconnected.is_set() or not client.is_connected
            ):
                raise ConnectionError("Verbindung zur Berger-Batterie abgebrochen.")
        finally:
            if connected:
                self.connection_state_changed.emit(False)
            for future in self._responses.values():
                if not future.done():
                    future.cancel()
            self._responses.clear()
            if notifications_started and client.is_connected:
                await self._bounded_cleanup(
                    client.stop_notify(NOTIFY_UUID), "Berger-Notifications stoppen"
                )
            if client.is_connected:
                disconnect_expected = True
                await self._disconnect_client(client)
            if connected:
                LOG.info("Berger-GATT getrennt (%s)", device.address)

    async def _measurement_cycle(self, client, device) -> None:
        """Read both registers inside one centrally coordinated radio window."""
        assert self._measurement_permission is not None
        assert self._measurement_released is not None
        self._measurement_permission.clear()
        self._measurement_released.clear()
        started_at = time.monotonic()
        LOG.info("Berger-Messfenster angefordert")
        self.measurement_window_requested.emit()
        try:
            await self._operation_or_stop(
                self._measurement_permission.wait(),
                MEASUREMENT_WINDOW_TIMEOUT,
                "Freigabe des Berger-Messfensters",
            )
            LOG.info("Berger-Messfenster gestartet")
            basic_frame = await self._request_with_retry(
                client, BASIC_REGISTER, BASIC_INFO_COMMAND
            )
            if self._history_pause_requested.is_set():
                raise _HistoryPauseRequested
            cell_frame = await self._request_with_retry(
                client, CELL_REGISTER, CELL_VOLTAGES_COMMAND
            )
            self._emit_snapshot(device, basic_frame, cell_frame)
        finally:
            duration_ms = (time.monotonic() - started_at) * 1000
            LOG.info("Berger-Messfenster beendet (%.0f ms)", duration_ms)
            self.measurement_window_finished.emit()
            if not self._stop_requested.is_set():
                try:
                    await self._operation_or_stop(
                        self._measurement_released.wait(),
                        MEASUREMENT_RELEASE_TIMEOUT,
                        "Freigabe des Victron-Scans",
                    )
                except TimeoutError:
                    # A resume acknowledgement is cleanup/coordination state and
                    # must not replace the actual Berger measurement result.
                    LOG.warning(
                        "Bestätigung der Victron-Scanfreigabe blieb aus"
                    )

    async def _request_with_retry(
        self, client, register: int, command: bytes
    ) -> bytes:
        """Retry one timed-out register response exactly once in the same session."""
        try:
            return await self._request(client, register, command)
        except TimeoutError:
            LOG.warning(
                "Wiederholung einer einzelnen Berger-Abfrage 0x%02X", register
            )
            return await self._request(client, register, command)

    def _emit_snapshot(self, device, basic_frame: bytes, cell_frame: bytes) -> None:
        try:
            basic = parse_basic_response(basic_frame)
            cells = parse_cell_response(cell_frame)
        except BergerProtocolError:
            LOG.warning("Ungültiger Berger-Frame empfangen", exc_info=True)
            raise
        minimum = min(cells) if cells else None
        maximum = max(cells) if cells else None
        snapshot = BatterySnapshot(
            timestamp=datetime.now(timezone.utc),
            device_name=self._config.name,
            address=device.address,
            rssi=self._rssi,
            voltage=basic.voltage,
            current=basic.current,
            remaining_capacity_ah=basic.remaining_capacity_ah,
            nominal_capacity_ah=basic.nominal_capacity_ah,
            state_of_charge_percent=basic.state_of_charge_percent,
            cycle_count=basic.cycle_count,
            production_date=basic.production_date,
            software_version=basic.software_version,
            protection_status=basic.protection_status,
            charge_mosfet_enabled=basic.charge_mosfet_enabled,
            discharge_mosfet_enabled=basic.discharge_mosfet_enabled,
            cell_count=basic.cell_count,
            temperatures_celsius=basic.temperatures_celsius,
            cell_voltages=cells,
            minimum_cell_voltage=minimum,
            maximum_cell_voltage=maximum,
            cell_delta=(
                maximum - minimum
                if minimum is not None and maximum is not None
                else None
            ),
            raw_basic_response=basic_frame,
            raw_cell_response=cell_frame,
        )
        LOG.info(
            "Berger-Snapshot erfolgreich: Spannung %.2f V, Strom %.2f A, SOC %d %%",
            snapshot.voltage,
            snapshot.current,
            snapshot.state_of_charge_percent,
        )
        self.snapshot_received.emit(snapshot)

    def _notification(self, _characteristic: Any, data: bytearray) -> None:
        self.target_activity.emit()
        for frame in self._assembler.feed(data):
            if len(frame) < 2:
                continue
            future = self._responses.get(frame[1])
            if future is not None and not future.done():
                future.set_result(frame)

    async def _request(self, client, register: int, command: bytes) -> bytes:
        """Send one of the two allow-listed read queries and await its response."""
        if command not in (BASIC_INFO_COMMAND, CELL_VOLTAGES_COMMAND):
            raise BergerProtocolError("Nicht erlaubter Berger-Befehl.")
        loop = asyncio.get_running_loop()
        response: asyncio.Future[bytes] = loop.create_future()
        self._responses[register] = response
        try:
            await self._operation_or_stop(
                client.write_gatt_char(WRITE_UUID, command, response=False),
                REQUEST_TIMEOUT,
                f"Berger-Schreibzugriff 0x{register:02X}",
            )
            LOG.info("Berger 0x%02X gesendet", register)
            try:
                frame = await self._operation_or_stop(
                    response,
                    REQUEST_TIMEOUT,
                    f"Berger-Antwort 0x{register:02X}",
                )
            except TimeoutError:
                LOG.warning(
                    "Berger-Notification-Timeout nach Request 0x%02X", register
                )
                raise
            LOG.info("Berger 0x%02X Antwort empfangen", register)
            return frame
        finally:
            self._responses.pop(register, None)
