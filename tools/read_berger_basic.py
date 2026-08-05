import asyncio

from bleak import BleakClient, BleakScanner

ADDRESS = "A5:C2:37:63:DC:37"

NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"

# JBD-Befehl: allgemeine BMS-Daten lesen
BASIC_INFO_COMMAND = bytes.fromhex("DD A5 03 00 FF FD 77")


async def main() -> None:
    print(f"Suche Berger-Batterie {ADDRESS} ...")

    device = await BleakScanner.find_device_by_address(
        ADDRESS,
        timeout=30.0,
    )

    if device is None:
        raise RuntimeError("Berger-Batterie wurde nicht gefunden.")

    print(f"Gefunden: {device.name} [{device.address}]")

    response_complete = asyncio.Event()
    received = bytearray()

    def notification_handler(_sender, data: bytearray) -> None:
        chunk = bytes(data)
        received.extend(chunk)

        print("Empfangen:", chunk.hex(" ").upper())

        if received and received[-1] == 0x77:
            response_complete.set()

    async with BleakClient(device, timeout=20.0) as client:
        print(f"Verbunden: {client.is_connected}")

        await client.start_notify(
            NOTIFY_UUID,
            notification_handler,
        )

        print(
            "Sende:",
            BASIC_INFO_COMMAND.hex(" ").upper(),
        )

        await client.write_gatt_char(
            WRITE_UUID,
            BASIC_INFO_COMMAND,
            response=False,
        )

        try:
            await asyncio.wait_for(
                response_complete.wait(),
                timeout=10.0,
            )
        except TimeoutError:
            print("Timeout: Keine vollständige Antwort innerhalb von 10 Sekunden.")

        await client.stop_notify(NOTIFY_UUID)

    print()

    if received:
        print("Gesamtantwort:")
        print(bytes(received).hex(" ").upper())
    else:
        print("Keine Daten empfangen.")


if __name__ == "__main__":
    asyncio.run(main())
