import asyncio

from bleak import BleakClient, BleakScanner

ADDRESS = "A5:C2:37:63:DC:37"
DEVICE_NAME = "11608062501619"


async def main() -> None:
    print(f"Suche Berger-Batterie {DEVICE_NAME} / {ADDRESS} ...")

    device = await BleakScanner.find_device_by_address(
        ADDRESS,
        timeout=20.0,
    )

    if device is None:
        print("Suche über Gerätenamen ...")
        device = await BleakScanner.find_device_by_name(
            DEVICE_NAME,
            timeout=20.0,
        )

    if device is None:
        raise RuntimeError(
            "Berger-Batterie wurde nicht gefunden. "
            "Smartphone-App schließen und Batterie in Reichweite prüfen."
        )

    print(f"Gefunden: {device.name} [{device.address}]")
    print("Verbinde ...")

    async with BleakClient(device, timeout=20.0) as client:
        print(f"Verbunden: {client.is_connected}")
        print()

        for service in client.services:
            print(f"Service: {service.uuid}")
            print(f"  Beschreibung: {service.description}")

            for characteristic in service.characteristics:
                properties = ", ".join(characteristic.properties)
                print(f"  Characteristic: {characteristic.uuid}")
                print(f"    Eigenschaften: {properties}")

                for descriptor in characteristic.descriptors:
                    print(f"    Descriptor: {descriptor.uuid}")

            print()


if __name__ == "__main__":
    asyncio.run(main())
