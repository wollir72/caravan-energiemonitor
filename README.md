# Caravan-Energiemonitor 0.2.1

Native Linux-Desktop-Anwendung für den Victron SmartSolar MPPT 100/20 und die
Berger LiFePO4 Lithium Batterie Pro 12,8 V / 200 Ah. Victron wird passiv über
verschlüsselte Instant-Readout-Advertisements gelesen. Die Berger-Batterie wird
per BLE/GATT mit zwei JBD-Lesebefehlen abgefragt.

## Voraussetzungen

- Python 3.13
- PySide6
- `victron-ble` 0.9.3
- `bleak` 3.0.2
- Linux mit BlueZ (entwickelt für openSUSE Tumbleweed/KDE Plasma)

Es werden keine zusätzlichen Laufzeitpakete benötigt. Die Oberfläche verwendet
die Qt-Systempalette und passt sich damit dem hellen oder dunklen Plasma-Theme an.

## Konfiguration

Im Projektverzeichnis:

```bash
cp config.example.toml config.toml
chmod 600 config.toml
```

`config.toml` ist durch `.gitignore` ausgeschlossen. Der Advertisement Key muss
genau 32 Hex-Zeichen enthalten und wird weder protokolliert noch auf stdout
ausgegeben. Die Anwendung verändert Dateirechte nicht automatisch.

Die Gerätekonfiguration ist getrennt in `[victron]` und `[berger]`. Eine bereits
vorhandene Datei mit `[device]` wird für Victron übergangsweise weiter gelesen
und nicht automatisch verändert. Ergänzt werden muss darin mindestens:

```toml
[berger]
enabled = true
name = "Berger LiFePO4 200 Ah Pro"
bluetooth_name = "11608062501619"
address = "A5:C2:37:63:DC:37"
capacity_ah = 200
```

Für die neue Struktur sollte außerdem `[device]` manuell in `[victron]`
umbenannt werden. Mit `enabled = false` läuft die Anwendung ohne Berger-Verbindung.

`solar.installed_power_watts` ist die dokumentierte PV-Nennleistung in Wp.
`display.maximum_solar_power` skaliert ausschließlich das Rundinstrument; es
verändert oder begrenzt keine Messdaten und darf über der Nennleistung liegen.
Bei 200 Wp und einer 250-W-Skala liegt die Referenzmarke bei 80 %. Leistungen
oberhalb von 200 W sind gültig.

## Start

Direkt aus dem Projektverzeichnis:

```bash
PYTHONPATH=src python -m caravan_energiemonitor
```

Nach Installation des Projekts steht zusätzlich zur Verfügung:

```bash
caravan-energiemonitor
```

Die Anwendung erwartet `config.toml` im aktuellen Arbeitsverzeichnis.

## Logdatei

Unter Linux schreibt die Anwendung Laufzeitmeldungen zusätzlich zum Terminal in
`~/.local/state/caravan-energiemonitor/caravan-energiemonitor.log`. Wenn
`XDG_STATE_HOME` gesetzt ist, wird stattdessen
`$XDG_STATE_HOME/caravan-energiemonitor/caravan-energiemonitor.log` verwendet.
Die Datei rotiert automatisch.

Die letzten Meldungen beziehungsweise neue Meldungen in Echtzeit zeigt man mit:

```bash
tail -n 200 ~/.local/state/caravan-energiemonitor/caravan-energiemonitor.log
tail -f ~/.local/state/caravan-energiemonitor/caravan-energiemonitor.log
```

## Architektur und Status

`SmartSolarScanner` ist eine eigene Unterklasse von
`victron_ble.scanner.BaseScanner`. Beim ersten passenden Advertisement wird mit
`detect_device_type()` der Parser erzeugt. Messwerte stammen ausschließlich aus
den öffentlichen Gettern von `SolarChargerData`.

`VictronWorker` ist ein dedizierter `QThread`. Er besitzt einen normalen
`asyncio`-Event-Loop, startet und stoppt dort den Bleak-Scanner und überträgt
immutable `SolarSnapshot`-Objekte per Qt-Signal. Nur Slots im Qt-Hauptthread
aktualisieren Widgets. Beim Schließen wird der Scan im `finally`-Block gestoppt,
bevor Event-Loop und Thread beendet werden.

`BergerWorker` besitzt unabhängig davon einen eigenen `QThread` und
`asyncio`-Event-Loop. Er sucht zuerst explizit nach der konfigurierten
MAC-Adresse, verbindet anschließend mit dem gefundenen `BLEDevice`, aktiviert
Notifications auf FF01 und sendet ausschließlich die bekannten Basisdaten- und
Zellspannungsabfragen über FF02 (`response=False`). Fragmentierte Notifications
werden gepuffert. Abbrüche lösen einen begrenzten Wiederverbindungs-Backoff aus;
ein nicht erreichbares Gerät hält das andere nicht auf. Eine Bluetooth-Kopplung
ist nicht erforderlich.

Die Berger-Stromanzeige verwendet `positiv = Laden` und `negativ = Entladen`.
Sie ist ausdrücklich vom Victron-Ladestrom getrennt. Die Batterieleistung ist
BMS-Spannung mal BMS-Gesamtstrom.

`BaseScanner` unterdrückt bytegleiche Advertisements. Deshalb meldet die eigene
Scanner-Unterklasse passende Zielgeräte-Aktivität bereits vor dieser
Deduplizierung. Diese Aktivität hält den Zustand „Online“, auch wenn sich die
Messwerte nicht geändert haben. Bleibt sie länger als
`stale_after_seconds` aus, erscheint „Keine aktuellen Daten“, während die
letzten Werte sichtbar bleiben. Zusätzlich sendet der Worker jede Sekunde ein
internes Scanner-Lebenszeichen. Fehlt dieses, wird ein ausgefallener Worker als
„Bluetooth-Fehler“ statt als unveränderter Messwert erkannt.

## Tests

Die Tests benötigen weder Bluetooth-Hardware noch BlueZ:

```bash
./venv-python -m pytest -v
./venv-python -m compileall src
```

Der Wrapper verwendet standardmäßig
`~/bin/venvs/caravan-energiemonitor/bin/python`; das venv muss nicht manuell
aktiviert werden. Mit `CARAVAN_PYTHON` kann ein anderer Interpreter gewählt
werden.

## Bekannte Einschränkungen

- Empfang setzt einen eingeschalteten Bluetooth-Adapter, passende
  BlueZ-/D-Bus-Berechtigungen und Instant Readout am Victron-Gerät voraus.
- Der Status „Online“ bewertet den passiven Advertisement-Empfang, nicht eine
  aktive Verbindung (eine solche wird absichtlich nicht aufgebaut).
- Version 0.2.1 zeigt den aktuellen Zustand; Verlaufsspeicherung und Diagramme
  sind nicht enthalten.
- Die herstellerspezifischen Zusatzbytes der Berger-Basisantwort werden im
  Rohtelegramm erhalten, aber mangels verifizierter Bedeutung nicht als
  Messwerte interpretiert.
- Die erfassten Berger-Antworten bilden die JBD-Prüfsumme ohne Registerbyte,
  während eine weitere dokumentierte Variante das Register einschließt. Der
  Parser prüft beide Varianten strikt und akzeptiert keine ungeprüfte Summe.
- Das Versionsbyte `0x29` wird entsprechend dem bestätigten Gerätewert als 41
  Zehntel (`4.1`) ausgewertet; eine Nibble-Auslegung würde für diesen echten
  Datensatz widersprüchlich `2.9` ergeben.
