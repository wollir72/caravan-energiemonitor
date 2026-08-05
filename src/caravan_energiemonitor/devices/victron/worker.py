"""Victron BLE scanning in a dedicated Qt thread and asyncio loop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import struct
import threading
from typing import Any, Awaitable, Callable, TypeVar

from bleak import BleakScanner
from bleak.assigned_numbers import AdvertisementDataType
from bleak.exc import BleakError
from PySide6.QtCore import QThread, Signal
from victron_ble.devices import detect_device_type
from victron_ble.devices.solar_charger import SolarChargerData
from victron_ble.exceptions import (
    AdvertisementKeyMismatchError,
    AdvertisementKeyMissingError,
    UnknownDeviceError,
)
from victron_ble.scanner import BaseScanner

from ...config import VictronConfig
from ...models import SolarSnapshot

LOG = logging.getLogger(__name__)

T = TypeVar("T")
BLE_OPERATION_TIMEOUT = 10.0
BLE_CLEANUP_TIMEOUT = 2.0
PASSIVE_BLUEZ_ERROR_PARTS = (
    "passive scanning on Linux requires BlueZ >= 5.56 with --experimental enabled",
    "Linux kernel >= 5.10",
)


class _StopRequested(Exception):
    """Internal control flow used to interrupt a scanner operation."""


class SmartSolarScanner(BaseScanner):
    """Parse advertisements for one SmartSolar without opening GATT."""

    def __init__(
        self,
        config: VictronConfig,
        on_snapshot: Callable[[SolarSnapshot], None],
        on_activity: Callable[[], None],
        on_error: Callable[[str], None],
        *,
        passive: bool = True,
    ) -> None:
        if passive:
            # Victron instant-readout data is wholly contained in manufacturer
            # advertisements, so BlueZ's passive AdvertisementMonitor is preferred.
            self._scanner = BleakScanner(
                detection_callback=self._detection_callback,
                scanning_mode="passive",
                bluez={
                    "or_patterns": [
                        (
                            0,
                            AdvertisementDataType.MANUFACTURER_SPECIFIC_DATA,
                            b"\xe1\x02\x10",
                        )
                    ]
                },
            )
            self._seen_data: set[bytes] = set()
        else:
            # Preserve victron_ble's originally working active BleakScanner:
            # detection callback only, with no scanning_mode or BlueZ monitor args.
            super().__init__()
        self._config = config
        self._target = config.address.casefold()
        self._on_snapshot = on_snapshot
        self._on_activity = on_activity
        self._on_error = on_error
        self._device_parser = None
        self._last_error: tuple[type[BaseException], str] | None = None
        self._target_found = False

    def _detection_callback(self, device, advertisement) -> None:
        """Report target radio activity before BaseScanner de-duplicates it."""
        raw_data = advertisement.manufacturer_data.get(0x02E1)
        if (
            device.address.casefold() == self._target
            and raw_data
            and raw_data.startswith(b"\x10")
        ):
            if not self._target_found:
                LOG.info("Victron-Gerät gefunden: %s", device.address)
                self._target_found = True
            self._on_activity()
        super()._detection_callback(device, advertisement)

    def callback(self, device, raw_data: bytes, advertisement) -> None:
        if device.address.casefold() != self._target:
            return
        try:
            if self._device_parser is None:
                parser_class = detect_device_type(raw_data)
                if parser_class is None:
                    raise UnknownDeviceError("Unbekannter Victron-Gerätetyp")
                self._device_parser = parser_class(self._config.advertisement_key)

            values = self._device_parser.parse(raw_data)
            if not isinstance(values, SolarChargerData):
                raise UnknownDeviceError("Das Zielgerät ist kein Solar-Laderegler")

            snapshot = SolarSnapshot(
                timestamp=datetime.now(timezone.utc),
                device_name=(
                    advertisement.local_name or device.name or self._config.name
                ),
                address=device.address,
                rssi=advertisement.rssi,
                model_name=values.get_model_name(),
                battery_voltage=values.get_battery_voltage(),
                battery_charging_current=values.get_battery_charging_current(),
                solar_power=values.get_solar_power(),
                yield_today=values.get_yield_today(),
                external_device_load=values.get_external_device_load(),
                charge_state=values.get_charge_state(),
                charger_error=values.get_charger_error(),
            )
        except AdvertisementKeyMismatchError as exc:
            self._report_error(
                exc, "Der Advertisement Key passt nicht zum Victron-Gerät."
            )
            return
        except AdvertisementKeyMissingError as exc:
            self._report_error(exc, "Der Advertisement Key fehlt.")
            return
        except UnknownDeviceError as exc:
            self._report_error(exc, str(exc) or "Unbekanntes Victron-Gerät.")
            return
        except (ValueError, struct.error) as exc:
            self._report_error(exc, "Ungültige Bluetooth-Advertisement-Daten.")
            return
        except Exception as exc:  # Bleak/BlueZ and parser runtime failures
            LOG.exception("Unerwarteter Victron-Bluetooth-Verarbeitungsfehler")
            self._report_error(exc, "Bluetooth-Verarbeitungsfehler.")
            return

        self._last_error = None
        self._on_snapshot(snapshot)

    def _report_error(self, exc: BaseException, friendly_message: str) -> None:
        signature = (type(exc), str(exc))
        if signature != self._last_error:
            LOG.warning(
                "Victron-BLE-Verarbeitung fehlgeschlagen: %s: %s",
                friendly_message,
                exc,
            )
            self._last_error = signature
        self._on_error(friendly_message)


class VictronWorker(QThread):
    """Owns the scanner and a regular asyncio event loop."""

    snapshot_received = Signal(object)
    target_activity = Signal()
    scanner_heartbeat = Signal()
    bluetooth_error = Signal(str)
    scan_paused = Signal()
    scan_resumed = Signal()
    scan_mode_changed = Signal(str)

    def __init__(self, config: VictronConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._stop_requested = threading.Event()
        self._pause_requested = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._scan_command: asyncio.Event | None = None
        self._scan_mode = "stopped"
        self._mode_lock = threading.Lock()

    @property
    def scan_mode(self) -> str:
        with self._mode_lock:
            return self._scan_mode

    def _set_scan_mode(self, mode: str) -> None:
        with self._mode_lock:
            if self._scan_mode == mode:
                return
            self._scan_mode = mode
        self.scan_mode_changed.emit(mode)

    def _wake_scan_loop(self) -> None:
        loop = self._loop
        command = self._scan_command
        if loop is not None and command is not None:
            try:
                loop.call_soon_threadsafe(command.set)
            except RuntimeError:
                pass

    def request_scan_pause(self) -> None:
        """Thread-safely request suspension of an active fallback scan."""
        if self._stop_requested.is_set() or self._pause_requested.is_set():
            return
        self._pause_requested.set()
        self._wake_scan_loop()

    def request_scan_resume(self) -> None:
        """Thread-safely release a prior pause unless shutdown has started."""
        if self._stop_requested.is_set() or not self._pause_requested.is_set():
            return
        self._pause_requested.clear()
        self._wake_scan_loop()

    def request_stop(self) -> None:
        LOG.info("Stop für Victron-Worker angefordert")
        self._stop_requested.set()
        self._pause_requested.set()
        loop = self._loop
        stop = self._async_stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        self._wake_scan_loop()

    def run(self) -> None:
        LOG.info("Victron-Worker gestartet (%s)", self._config.address)
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._async_stop = asyncio.Event()
        self._scan_command = asyncio.Event()
        try:
            loop.run_until_complete(self._scan())
        except _StopRequested:
            pass
        except Exception as exc:
            message = f"Bluetooth/BlueZ konnte nicht gestartet werden: {exc}"
            LOG.exception(message)
            self.bluetooth_error.emit(message)
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._set_scan_mode("stopped")
            self._scan_command = None
            self._async_stop = None
            self._loop = None
            LOG.info("Victron-Worker beendet")

    async def _operation_or_stop(
        self, awaitable: Awaitable[T], timeout: float, description: str
    ) -> T:
        assert self._async_stop is not None
        operation = asyncio.ensure_future(awaitable)
        stop_task = asyncio.create_task(self._async_stop.wait())
        try:
            done, _ = await asyncio.wait(
                {operation, stop_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return await operation
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if stop_task in done or self._stop_requested.is_set():
                raise _StopRequested
            raise TimeoutError(f"Zeitüberschreitung bei {description}.")
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)

    @staticmethod
    async def _bounded_cleanup(awaitable: Awaitable[Any]) -> None:
        try:
            await asyncio.wait_for(awaitable, timeout=BLE_CLEANUP_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Victron-Scanner konnte nicht sauber gestoppt werden")

    @staticmethod
    def _passive_scanning_is_unsupported(exc: BleakError) -> bool:
        message = str(exc)
        return all(part in message for part in PASSIVE_BLUEZ_ERROR_PARTS)

    def _new_scanner(self, *, passive: bool) -> SmartSolarScanner:
        return SmartSolarScanner(
            self._config,
            self.snapshot_received.emit,
            self.target_activity.emit,
            self.bluetooth_error.emit,
            passive=passive,
        )

    async def _scan(self) -> None:
        scanner = self._new_scanner(passive=True)
        started = False
        paused = False
        scan_mode = "passiv"
        try:
            LOG.info("Passiver Victron-Scan wird versucht (%s)", self._config.address)
            try:
                await self._operation_or_stop(
                    scanner.start(),
                    BLE_OPERATION_TIMEOUT,
                    "Start des passiven Victron-Scans",
                )
                LOG.info("Passiver Victron-Scan erfolgreich gestartet")
                self._set_scan_mode("passive")
            except BleakError as exc:
                if not self._passive_scanning_is_unsupported(exc):
                    raise
                LOG.warning("Passiver Victron-Scan nicht verfügbar: %s", exc)
                assert self._async_stop is not None
                if self._async_stop.is_set() or self._stop_requested.is_set():
                    raise _StopRequested
                LOG.info("Fallback auf aktiven Victron-Scan")
                scanner = self._new_scanner(passive=False)
                scan_mode = "aktiv"
                await self._operation_or_stop(
                    scanner.start(),
                    BLE_OPERATION_TIMEOUT,
                    "Start des aktiven Victron-Scans",
                )
                LOG.info("Aktiver Victron-Scan erfolgreich gestartet")
                self._set_scan_mode("active")
            started = True
            assert self._async_stop is not None
            assert self._scan_command is not None
            while not self._async_stop.is_set():
                self._scan_command.clear()
                if scan_mode == "aktiv" and self._pause_requested.is_set() and not paused:
                    LOG.info("Aktiver Victron-Scan wird pausiert")
                    await self._operation_or_stop(
                        scanner.stop(),
                        BLE_CLEANUP_TIMEOUT,
                        "Pausieren des aktiven Victron-Scans",
                    )
                    started = False
                    paused = True
                    if self._stop_requested.is_set() or self._async_stop.is_set():
                        raise _StopRequested
                    LOG.info("Aktiver Victron-Scan pausiert")
                    self.scan_paused.emit()
                elif (
                    scan_mode == "aktiv"
                    and paused
                    and not self._pause_requested.is_set()
                    and not self._stop_requested.is_set()
                ):
                    LOG.info("Aktiver Victron-Scan wird fortgesetzt")
                    await self._operation_or_stop(
                        scanner.start(),
                        BLE_OPERATION_TIMEOUT,
                        "Fortsetzen des aktiven Victron-Scans",
                    )
                    started = True
                    paused = False
                    LOG.info("Aktiver Victron-Scan fortgesetzt")
                    self.scan_resumed.emit()
                self.scanner_heartbeat.emit()
                try:
                    await asyncio.wait_for(self._scan_command.wait(), timeout=1.0)
                except TimeoutError:
                    pass
        finally:
            if started:
                await self._bounded_cleanup(scanner.stop())
            self._set_scan_mode("stopped")
            LOG.info("Victron-Scan beendet (Modus: %s)", scan_mode)
