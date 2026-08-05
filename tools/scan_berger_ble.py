import asyncio
from bleak import BleakScanner

TARGET_ADDRESS = "A5:C2:37:63:DC:37"
TARGET_NAME = "11608062501619"


async def main() -> None:
    print("BLE-Scan läuft 60 Sekunden ...")

    found = False

    def callback(device, advertisement_data) -> None:
        nonlocal found

        name = (
            advertisement_data.local_name
            or device.name
            or ""
        )

        print(
            f"{device.address:17} "
            f"{name:24} "
            f"RSSI={advertisement_data.rssi}"
        )

        if (
            device.address.casefold() == TARGET_ADDRESS.casefold()
            or name == TARGET_NAME
        ):
            found = True
            print("\n*** Berger-Batterie erkannt ***\n")

    async with BleakScanner(callback):
        await asyncio.sleep(60)

    if not found:
        print("\nBerger-Batterie wurde während des Scans nicht empfangen.")


if __name__ == "__main__":
    asyncio.run(main())
