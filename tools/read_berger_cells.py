import asyncio

from bleak import BleakClient, BleakScanner

ADDRESS = "A5:C2:37:63:DC:37"

NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

CELL_INFO_COMMAND = bytes.fromhex("DD A5 04 00 FF FC 77")


async def main() -> None:
    print(f"Suche Berger-Batterie {ADDRESS} ...")

    device = await BleakScanner.find_device_by_address(
        ADDRESS,
        timeout=30.0,
    )

    if device is None:
        raise RuntimeError("Berger-Batterie wurde nicht gefunden.")

    received = bytearray()
    complete = asyncio.Event()

    def notification_handler(_sender, data: bytearray) -> None:
        chunk = bytes(data)
        received.extend(chunk)
        print("Empfangen:", chunk.hex(" ").upper())

        if received and received[-1] == 0x77:
            complete.set()

    async with BleakClient(device, timeout=20.0) as client:
        print(f"Verbunden: {client.is_connected}")

        await client.start_notify(
            NOTIFY_UUID,
            notification_handler,
        )

        print("Sende:", CELL_INFO_COMMAND.hex(" ").upper())

        await client.write_gatt_char(
            WRITE_UUID,
            CELL_INFO_COMMAND,
            response=False,
        )

        try:
            await asyncio.wait_for(complete.wait(), timeout=10.0)
        except TimeoutError:
            print("Timeout: Keine vollständige Antwort erhalten.")

        await client.stop_notify(NOTIFY_UUID)

    if not received:
        print("Keine Daten empfangen.")
        return

    response = bytes(received)

    print()
    print("Gesamtantwort:")
    print(response.hex(" ").upper())

    if (
        len(response) < 7
        or response[0] != 0xDD
        or response[1] != 0x04
        or response[-1] != 0x77
    ):
        raise RuntimeError("Unerwartetes Antwortformat.")

    status = response[2]
    payload_length = response[3]
    payload = response[4 : 4 + payload_length]

    if status != 0:
        raise RuntimeError(f"BMS meldet Fehlerstatus 0x{status:02X}.")

    if len(payload) % 2 != 0:
        raise RuntimeError("Ungültige Länge der Zellspannungsdaten.")

    voltages = [
        int.from_bytes(payload[index : index + 2], "big") / 1000.0
        for index in range(0, len(payload), 2)
    ]

    print()
    for index, voltage in enumerate(voltages, start=1):
        print(f"Zelle {index}: {voltage:.3f} V")

    if voltages:
        minimum = min(voltages)
        maximum = max(voltages)

        print(f"Minimum: {minimum:.3f} V")
        print(f"Maximum: {maximum:.3f} V")
        print(f"Differenz: {(maximum - minimum) * 1000:.0f} mV")


if __name__ == "__main__":
    asyncio.run(main())
