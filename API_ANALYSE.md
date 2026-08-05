# API-Analyse: `victron_ble`

## Untersuchungsumfang

Diese Analyse basiert ausschließlich auf dem lokal installierten Quellcode und
den lokal installierten Paketmetadaten. Es wurden weder Internetquellen benutzt
noch Pakete installiert.

- Python: 3.13.13
- `victron-ble`: 0.9.3
- `bleak`: 3.0.2
- `PySide6`: 6.11.1
- Paketpfad: `/home/wolli/bin/venvs/victron/lib64/python3.13/site-packages/victron_ble`
- Zielgerät: `SmartSolar HQ2444NCAFK`, Adresse `F9:9F:71:BC:CC:D0`

Das Top-Level-Modul `victron_ble` ist leer. Die Klassen müssen daher aus den
nachfolgend genannten Untermodulen importiert werden.

## Relevante Module und API

### `victron_ble.scanner`

#### `BaseScanner()`

Allgemeine, mit Bleak arbeitende Scanner-Basisklasse. Sie erzeugt intern einen
`bleak.BleakScanner(detection_callback=...)`.

- `async start()` startet den BLE-Scan.
- `async stop()` beendet ihn.
- `callback(device, data, advertisement)` ist der vorgesehene
  Erweiterungspunkt für eigene Verarbeitung.

Die interne Erkennung berücksichtigt nur Manufacturer Data mit Victrons
Company-ID `0x02E1`, deren Nutzdaten mit `0x10` beginnen. Der Callback erhält:

- `device`: `bleak.backends.device.BLEDevice`
- `data`: die Victron-Manufacturer-Data als `bytes`
- `advertisement`: `bleak.backends.scanner.AdvertisementData`, unter anderem
  mit `rssi`, `local_name` und `manufacturer_data`

Kurzes Erkennungsbeispiel:

```python
import asyncio

from victron_ble.scanner import BaseScanner


class DeviceDiscovery(BaseScanner):
    def callback(self, device, data, advertisement):
        print(device.address, advertisement.local_name, advertisement.rssi)


async def main():
    scanner = DeviceDiscovery()
    await scanner.start()
    try:
        await asyncio.sleep(10)
    finally:
        await scanner.stop()


asyncio.run(main())
```

#### `DiscoveryScanner()`

Unterklasse von `BaseScanner`. Sie erkennt Victron-Geräte mit Instant Readout,
schreibt jedes gefundene Gerät jedoch lediglich einmal per `logging` und gibt
keine Ergebnisliste zurück. Für eine Anwendung ist daher eine eigene
`BaseScanner`-Unterklasse zweckmäßiger.

#### `DebugScanner(address)`

Protokolliert für eine Adresse Zeitstempel und rohe Advertisement-Daten. Das
ist ein Diagnosewerkzeug, keine strukturierte Datenquelle.

#### `Scanner(device_keys: dict[str, str], indent=2)`

Nimmt eine Zuordnung aus BLE-Adresse und Advertisement Key entgegen. Adressen
werden intern kleingeschrieben. Beispiel:

```python
from victron_ble.scanner import Scanner

scanner = Scanner({
    "F9:9F:71:BC:CC:D0": "<ADVERTISEMENT_KEY_ALS_HEX>",
})
await scanner.start()
```

Bei jedem entschlüsselten Datensatz schreibt `Scanner.callback()` ein
JSON-Objekt nach Standardausgabe. Für eine GUI ist die Klasse deshalb nicht die
empfohlene Datenquelle. Nützliche öffentliche Methoden sind außerdem:

- `load_key(address) -> str`
- `get_device(ble_device, raw_data) -> Device`

Der Key wird von der Bibliothek nicht ausgelesen oder beschafft. Er muss als
Hex-String extern bereitgestellt werden. Aus der AES-Nutzung folgt für die
üblichen Victron-Keys eine Länge von 16 Byte beziehungsweise 32 Hex-Zeichen;
die Bibliothek selbst validiert Format und Länge nicht vorab.

### `victron_ble.devices`

#### `detect_device_type(data: bytes) -> Optional[type[Device]]`

Ermittelt anhand von Model-ID und Readout-Type die Parserklasse. Der
Readout-Type `0x01` wird als `SolarCharger` erkannt. Für unbekannte Typen wird
`None` geliefert.

```python
from victron_ble.devices import detect_device_type

device_class = detect_device_type(raw_manufacturer_data)
if device_class is None:
    raise RuntimeError("Unbekannter Victron-Gerätetyp")
```

#### `Device(advertisement_key)`

Abstrakte Basisklasse aller Geräteparser. Relevante Methoden:

- `parse_container(data) -> AdvertisementContainer`
- `get_model_id(data) -> int`
- `decrypt(data) -> bytes`
- `parse(data) -> DeviceData`
- `parse_decrypted(decrypted) -> dict` ist gerätespezifisch und abstrakt.

`parse()` ist der normale Einstieg: entschlüsseln, gerätespezifisch parsen
und ein typisiertes Datenobjekt erzeugen.

#### `DeviceData(model_id, data)`

Basisklasse der Messdaten. `get_model_name() -> str` liefert den Modellnamen
aus der eingebauten Model-ID-Tabelle oder einen Platzhalter für unbekannte
Modelle.

### `victron_ble.devices.solar_charger`

#### `SolarCharger(advertisement_key)`

Parser für SmartSolar/BlueSolar Solar Charger. Der Key wird beim Erzeugen
übergeben; `parse(raw_data)` liefert `SolarChargerData`.

```python
from victron_ble.devices import detect_device_type
from victron_ble.devices.solar_charger import SolarChargerData

device_class = detect_device_type(raw_manufacturer_data)
device = device_class("<ADVERTISEMENT_KEY_ALS_HEX>")
values = device.parse(raw_manufacturer_data)

assert isinstance(values, SolarChargerData)
print(values.get_battery_voltage())
print(values.get_solar_power())
```

Alternativ kann bei sicher bekanntem Gerätetyp direkt
`SolarCharger(key).parse(raw_data)` benutzt werden. Die automatische Erkennung
ist vorzuziehen, weil sie fehlerhafte Annahmen über das Advertisement vermeidet.

#### `SolarChargerData`

Die öffentlichen Getter und die tatsächlichen internen Feldnamen sind:

| Getter | Feldname | Typ | Einheit / Bedeutung |
|---|---|---:|---|
| `get_model_name()` | separat aus Model-ID ermittelt | `str` | Modellname |
| `get_charge_state()` | `charge_state` | `OperationMode \| None` | Ladezustand |
| `get_charger_error()` | `charger_error` | `ChargerError \| None` | Fehlerzustand |
| `get_battery_voltage()` | `battery_voltage` | `float \| None` | V |
| `get_battery_charging_current()` | `battery_charging_current` | `float \| None` | A |
| `get_yield_today()` | `yield_today` | `float \| None` | Wh |
| `get_solar_power()` | `solar_power` | `float \| None` | W |
| `get_external_device_load()` | `external_device_load` | `float \| None` | A |

Die Parser-Rohstruktur, also das interne `_data` von `SolarChargerData`, enthält
genau die sieben Felder von `charge_state` bis `external_device_load`.
`model_name` wird nicht darin gespeichert, sondern aus der separaten Model-ID
ermittelt.

Der mitgelieferte `Scanner` serialisiert dagegen folgende Struktur:

```json
{
  "name": "SmartSolar HQ2444NCAFK",
  "address": "F9:9F:71:BC:CC:D0",
  "rssi": -60,
  "payload": {
    "battery_charging_current": 1.2,
    "battery_voltage": 13.45,
    "charge_state": "bulk",
    "charger_error": "no_error",
    "external_device_load": 0.3,
    "model_name": "<modellabhängig>",
    "solar_power": 17,
    "yield_today": 240
  }
}
```

Die Zahlen sind nur ein Formbeispiel. Der Encoder nimmt alle `get_*`-Methoden,
entfernt das Präfix `get_`, wandelt Enums in kleingeschriebene Namen um und
lässt Felder mit Wert `None` vollständig weg. Die Reihenfolge ist nicht Teil
der API.

### `victron_ble.devices.base`

Für SmartSolar relevant sind außerdem:

- `OperationMode`: unter anderem `OFF`, `BULK`, `ABSORPTION`, `FLOAT`,
  `STORAGE` und weitere Betriebszustände.
- `ChargerError`: Victron-Ladefehler als Enum.
- `AdvertisementContainer`: Dataclass mit `prefix`, `model_id`,
  `readout_type`, `iv` und `encrypted_data`.

### `victron_ble.exceptions`

- `AdvertisementKeyMissingError`: für die Adresse ist kein Key hinterlegt.
- `AdvertisementKeyMismatchError`: das Key-Prüfbyte passt nicht.
- `UnknownDeviceError`: der Gerätetyp konnte nicht erkannt werden.

## Vollständiger Empfangsweg

Ein eigener Scanner kann Erkennung, Key-Zuordnung, Entschlüsselung und
Messwertzugriff ohne JSON-Ausgabe verbinden:

```python
from victron_ble.devices import detect_device_type
from victron_ble.exceptions import AdvertisementKeyMismatchError
from victron_ble.scanner import BaseScanner

TARGET = "F9:9F:71:BC:CC:D0"


class SmartSolarScanner(BaseScanner):
    def __init__(self, advertisement_key, on_values):
        super().__init__()
        self._key = advertisement_key
        self._on_values = on_values
        self._device = None

    def callback(self, ble_device, raw_data, advertisement):
        if ble_device.address.upper() != TARGET:
            return

        if self._device is None:
            device_class = detect_device_type(raw_data)
            if device_class is None:
                return
            self._device = device_class(self._key)

        try:
            data = self._device.parse(raw_data)
        except (AdvertisementKeyMismatchError, ValueError):
            return

        self._on_values({
            "battery_voltage": data.get_battery_voltage(),
            "battery_charging_current": data.get_battery_charging_current(),
            "solar_power": data.get_solar_power(),
            "yield_today": data.get_yield_today(),
            "external_device_load": data.get_external_device_load(),
            "charge_state": data.get_charge_state(),
            "charger_error": data.get_charger_error(),
            "rssi": advertisement.rssi,
        })
```

## `asyncio`- und Bleak-Verhalten

- `BaseScanner.start()` und `stop()` sind Coroutinen und delegieren an
  `BleakScanner.start()` beziehungsweise `stop()`.
- `start()` wartet nicht bis zum Ende eines Scans, sondern aktiviert den
  fortlaufenden Empfang. Der Event-Loop muss danach weiterlaufen.
- Der von `victron_ble` registrierte Bleak-Callback ist synchron. Entschlüsseln
  und Parsen erfolgen direkt im Callback und sollten kurz bleiben.
- Für Instant Readout wird keine GATT-Verbindung mit `BleakClient` aufgebaut.
  Die Werte kommen ausschließlich aus BLE-Advertisements.
- Die mitgelieferte CLI benutzt `asyncio.ensure_future(...)` und anschließend
  `loop.run_forever()`. In neuem Anwendungscode ist eine klar verwaltete
  Coroutine mit garantiertem `await scanner.stop()` vorzuziehen.

## Empfohlener Integrationsweg für PySide6

Empfohlen wird ein dedizierter `QThread` (oder ein Python-Worker-Thread), der
einen normalen `asyncio`-Event-Loop besitzt und darin genau eine
`BaseScanner`-Unterklasse betreibt:

1. GUI und Widgets bleiben ausschließlich im Qt-Hauptthread.
2. Der Worker startet seinen Standard-Event-Loop und darin den Scanner.
3. Der synchrone Scanner-Callback parst nur die Daten und emittiert ein
   thread-sicheres Qt-Signal mit einem unveränderlichen Daten-Snapshot.
4. Ein Slot im Hauptthread aktualisiert Anzeige und Diagramme.
5. Beim Beenden wird im Worker-Loop `await scanner.stop()` ausgeführt, danach
   werden Loop und Thread geordnet beendet.

Diese Trennung ist im lokal installierten Stand robuster als
`PySide6.QtAsyncio`: Das Modul ist vorhanden, aber sein Event-Loop implementiert
unter anderem `add_reader()`, `remove_reader()` sowie mehrere Socket- und
Verbindungsoperationen noch nicht. Das kann mit dem Linux-/D-Bus-Backend von
Bleak kollidieren. Das oft verwendete Drittanbieterpaket `qasync` ist lokal
nicht installiert und soll für diesen Schritt auch nicht installiert werden.

## Besonderheiten und Risiken

- **Advertisement Key erforderlich:** Ohne den korrekten Key kann nur entdeckt,
  aber nicht entschlüsselt werden. Der Bluetooth-PIN ist nicht automatisch der
  Advertisement Key.
- **Key-Fehler:** `bytes.fromhex()` kann bei ungültigem Hex-Text `ValueError`
  auslösen; AES kann eine ungültige Schlüssellänge ablehnen. Nur das falsche
  Prüfbyte wird als `AdvertisementKeyMismatchError` gekapselt.
- **Ausnahmen im Standard-Scanner:** `Scanner.callback()` fängt fehlenden Key
  und unbekannte Gerätetypen ab, nicht aber Key-Mismatch oder allgemeine
  Parse-/Crypto-Fehler. Eine GUI sollte diese im eigenen Callback abfangen und
  protokollieren.
- **Deduplizierung:** `BaseScanner` unterdrückt jedes bytegleich bereits
  gesehene Advertisement. Erst nach mehr als 1000 unterschiedlichen Paketen
  wird die gesamte Historie geleert. Gleichbleibende Messwerte erzeugen daher
  keine wiederholten GUI-Ereignisse; auch ein geänderter RSSI allein kommt dann
  nicht an den öffentlichen Callback.
- **Fehlende Werte:** Protokoll-Sentinelwerte werden zu `None`. Der JSON-Encoder
  lässt solche Felder weg. Eigener Anwendungscode sollte ein stabiles Schema
  herstellen und `None` ausdrücklich behandeln.
- **Enums:** Die direkte API liefert `OperationMode` und `ChargerError`, nicht
  Strings. Nur `DeviceDataEncoder` wandelt sie in kleingeschriebene Namen um.
- **Gerätename:** `Scanner` verwendet `BLEDevice.name`, das `None` sein kann.
  Bleak stellt zusätzlich `advertisement.local_name` bereit.
- **Adressnormalisierung:** Die Key-Tabelle von `Scanner` normalisiert Adressen
  zu Kleinbuchstaben. Eigene Vergleiche sollten ebenfalls Groß-/Kleinschreibung
  ignorieren.
- **Kurze oder defekte Daten:** `detect_device_type()` fängt laut Quellcode
  `IndexError`, die verwendeten `struct.unpack()`-Aufrufe werfen bei zu kurzen
  Daten jedoch `struct.error`. Der Filter von `BaseScanner` garantiert nur das
  Startbyte, nicht die vollständige Mindestlänge.
- **Datenkapselung:** Die tatsächlichen Felder liegen in `DeviceData._data`,
  einem privaten Attribut. Produktiv sollten die öffentlichen Getter benutzt
  werden.
- **Exportfehler in 0.9.3:** `victron_ble.devices.__all__` nennt
  `SmartCharger` und `SmartChargerData`, definiert/importiert diese Namen aber
  nicht. `from victron_ble.devices import *` scheitert deshalb mit
  `AttributeError`. Explizite Imports verwenden.
- **Mutable Default Arguments:** `Scanner(device_keys={})` und auch Bleaks
  lokale Signatur enthalten veränderliche Default-Dictionaries. Beim Aufruf
  immer eine eigene Key-Zuordnung übergeben und sie nicht nachträglich
  mutieren.
- **BlueZ-Berechtigungen:** Unter Linux kann das Scannen an Bluetooth-/D-Bus-
  Berechtigungen oder einem belegten/abgeschalteten Adapter scheitern. Diese
  Betriebsfehler kommen von Bleak/BlueZ und sollten bis zur GUI als Statussignal
  transportiert werden.

## Fazit

Der passende Kernweg lautet:
`BaseScanner` -> Victron-Manufacturer-Data -> `detect_device_type()` ->
Geräteklasse mit Advertisement Key -> `Device.parse()` ->
`SolarChargerData`-Getter. Für die spätere Desktop-App sollte dieser Ablauf in
einem Worker mit eigenem Standard-`asyncio`-Loop laufen und Messwert-Snapshots
per Qt-Signal an den Hauptthread liefern.
