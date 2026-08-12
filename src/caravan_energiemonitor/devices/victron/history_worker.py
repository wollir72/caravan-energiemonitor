"""Exclusive Victron GATT history reader in a dedicated Qt thread."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Awaitable, TypeVar

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from PySide6.QtCore import QThread, Signal

from ...config import VictronConfig
from .gatt_protocol import (
    COMMAND_START,
    COMMAND_UUID,
    FLOW_CONTROL_UUID,
    FLOW_ENABLE,
    FLOW_KEEPALIVE,
    FLOW_START,
    LONG_COMMAND_UUID,
    MPPT_INITIALIZE,
    REQUIRED_CHARACTERISTIC_UUIDS,
    SERVICE_UUID,
    TOTAL_HISTORY_REGISTER,
    NotificationState,
    ProtocolError,
    SafetyViolation,
    build_history_read_request,
    decode_history_day,
    decode_history_summary,
    history_day_registers,
    parse_history_frame,
    validate_outgoing_frame,
)

LOG = logging.getLogger(__name__)
T = TypeVar("T")

DISCOVERY_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
OPERATION_TIMEOUT = 8.0
CLEANUP_TIMEOUT = 5.0
KEEPALIVE_INTERVAL = 20.0


class _Cancelled(Exception):
    pass


class VictronHistoryWorker(QThread):
    """Read the allowlisted SmartSolar history without blocking Qt's GUI loop."""

    loading_started = Signal()
    progress_changed = Signal(int, int)
    history_received = Signal(object, object)
    error_occurred = Signal(str)
    cancelled = Signal()
    gatt_state_changed = Signal(bool)

    def __init__(self, config: VictronConfig, parent: Any = None) -> None:
        super().__init__(parent)
        self._config = config
        self._cancel_requested = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task[None] | None = None

    def request_cancel(self) -> None:
        """Cancel at the next register boundary; never interrupt an active frame."""
        LOG.info("Kontrollierter Abbruch des Victron-History-Abrufs angefordert")
        self._cancel_requested.set()

    def request_stop(self) -> None:
        """Shutdown variant: cancel an active await and proceed to GATT cleanup."""
        self.request_cancel()
        loop = self._loop
        task = self._main_task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass

    def run(self) -> None:
        LOG.info("Victron-History-Worker gestartet (%s)", self._config.address)
        self.loading_started.emit()
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._main_task = loop.create_task(self._read_history())
        try:
            loop.run_until_complete(self._main_task)
        except _Cancelled:
            LOG.info("Victron-History-Abruf kontrolliert beendet")
            self.cancelled.emit()
        except asyncio.CancelledError:
            LOG.info("Victron-History-Abruf für Shutdown beendet")
            self.cancelled.emit()
        except (SafetyViolation, ProtocolError, BleakError, TimeoutError, OSError, ConnectionError) as exc:
            LOG.warning("Victron-History konnte nicht gelesen werden: %s", exc, exc_info=True)
            self.error_occurred.emit(str(exc) or "Verlauf konnte nicht geladen werden.")
        except Exception as exc:
            LOG.exception("Unerwarteter Victron-History-Fehler")
            self.error_occurred.emit(str(exc) or "Verlauf konnte nicht geladen werden.")
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._main_task = None
            self._loop = None
            LOG.info("Victron-History-GATT beendet")
            LOG.info("Victron-History-Worker beendet")

    @staticmethod
    async def _bounded(awaitable: Awaitable[T], timeout: float, label: str) -> T:
        try:
            return await asyncio.wait_for(awaitable, timeout)
        except TimeoutError as exc:
            raise TimeoutError(f"Zeitüberschreitung bei {label}.") from exc

    @staticmethod
    def _characteristics(services: Any) -> dict[str, Any]:
        service = next(
            (candidate for candidate in services if str(candidate.uuid).lower() == SERVICE_UUID),
            None,
        )
        if service is None:
            raise ProtocolError("VictronConnect-GATT-Service wurde nicht gefunden.")
        found = {str(item.uuid).lower(): item for item in service.characteristics}
        missing = [uuid for uuid in REQUIRED_CHARACTERISTIC_UUIDS if uuid not in found]
        if missing:
            raise ProtocolError("VictronConnect-GATT-Characteristics sind unvollständig.")
        return found

    async def _send(self, client: Any, characteristic: Any, payload: bytes, label: str) -> None:
        validate_outgoing_frame(str(characteristic.uuid), payload)
        await self._bounded(
            client.write_gatt_char(characteristic, payload, response=False),
            OPERATION_TIMEOUT,
            label,
        )

    async def _send_and_ack(
        self,
        client: Any,
        characteristic: Any,
        state: NotificationState,
        payload: bytes,
        label: str,
    ) -> None:
        await self._send(client, characteristic, payload, label)
        await state.wait_for_ack(OPERATION_TIMEOUT, label)

    async def _read_register(
        self,
        client: Any,
        command: Any,
        state: NotificationState,
        register: int,
    ) -> bytes:
        state.arm(register)
        await self._send(client, command, build_history_read_request(register), f"READ 0x{register:04X}")
        _, frame = await asyncio.gather(
            state.wait_for_ack(OPERATION_TIMEOUT, f"READ 0x{register:04X}"),
            state.wait_for_response(OPERATION_TIMEOUT, register),
        )
        return frame

    async def _keepalive(self, client: Any, flow: Any, lock: asyncio.Lock) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            async with lock:
                # Confirmed fire-and-forget behavior: deliberately no F9-01 wait.
                await self._send(client, flow, FLOW_KEEPALIVE, "Keepalive F9 41")

    async def _read_history(self) -> None:
        LOG.info("Victron-History-GATT gestartet")
        if self._cancel_requested.is_set():
            raise _Cancelled
        device = await self._bounded(
            BleakScanner.find_device_by_address(self._config.address, timeout=DISCOVERY_TIMEOUT),
            DISCOVERY_TIMEOUT + 1,
            "Victron-Gerätesuche",
        )
        if device is None:
            raise ConnectionError(f"Victron-Gerät {self._config.address} wurde nicht gefunden.")
        if self._cancel_requested.is_set():
            raise _Cancelled

        client = BleakClient(device, timeout=CONNECT_TIMEOUT)
        notifications: list[Any] = []
        keepalive_task: asyncio.Task[None] | None = None
        try:
            await self._bounded(client.connect(), CONNECT_TIMEOUT, "Victron-Verbindungsaufbau")
            if not client.is_connected:
                raise ConnectionError("Victron-GATT-Verbindung wurde nicht hergestellt.")
            self.gatt_state_changed.emit(True)
            LOG.info("Exklusive Victron-GATT-Verbindung hergestellt")
            if self._cancel_requested.is_set():
                raise _Cancelled
            characteristics = self._characteristics(client.services)
            state = NotificationState()
            for uuid in REQUIRED_CHARACTERISTIC_UUIDS:
                characteristic = characteristics[uuid]
                await self._bounded(
                    client.start_notify(characteristic, state.callback_for(uuid)),
                    OPERATION_TIMEOUT,
                    f"Notifications {uuid}",
                )
                notifications.append(characteristic)

            await self._send(client, characteristics[FLOW_CONTROL_UUID], FLOW_ENABLE, "Flow Control aktivieren")
            await self._send_and_ack(client, characteristics[FLOW_CONTROL_UUID], state, FLOW_START, "Flow Control starten")
            await self._send_and_ack(client, characteristics[COMMAND_UUID], state, COMMAND_START, "Command-Session starten")
            await self._send_and_ack(client, characteristics[COMMAND_UUID], state, MPPT_INITIALIZE, "MPPT initialisieren")

            operation_lock = asyncio.Lock()
            keepalive_task = asyncio.create_task(
                self._keepalive(client, characteristics[FLOW_CONTROL_UUID], operation_lock)
            )
            async with operation_lock:
                summary_frame = await self._read_register(
                    client, characteristics[COMMAND_UUID], state, TOTAL_HISTORY_REGISTER
                )
            summary = decode_history_summary(
                parse_history_frame(summary_frame, TOTAL_HISTORY_REGISTER)
            )
            registers = history_day_registers(summary.days_available)
            self.progress_changed.emit(0, len(registers))
            days = []
            for index, register in enumerate(registers):
                if self._cancel_requested.is_set():
                    raise _Cancelled
                async with operation_lock:
                    frame = await self._read_register(client, characteristics[COMMAND_UUID], state, register)
                days.append(decode_history_day(parse_history_frame(frame, register), index))
                self.progress_changed.emit(index + 1, len(registers))
            self.history_received.emit(summary, tuple(days))
        finally:
            LOG.info("Victron-GATT-Cleanup gestartet")
            if keepalive_task is not None:
                keepalive_task.cancel()
                await asyncio.gather(keepalive_task, return_exceptions=True)
            for characteristic in reversed(notifications):
                if client.is_connected:
                    try:
                        await asyncio.wait_for(client.stop_notify(characteristic), CLEANUP_TIMEOUT)
                    except Exception:
                        LOG.warning("Victron-Notification-Cleanup fehlgeschlagen", exc_info=True)
            if client.is_connected:
                try:
                    await asyncio.wait_for(client.disconnect(), CLEANUP_TIMEOUT)
                except Exception:
                    LOG.warning("Victron-GATT-Disconnect konnte nicht bestätigt werden", exc_info=True)
            self.gatt_state_changed.emit(False)
            LOG.info("Victron-GATT-Cleanup beendet")
