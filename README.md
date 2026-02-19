# Octo Bed - Home Assistant Integration

Control your Octo adjustable bed from Home Assistant via Bluetooth, using an ESPHome Bluetooth proxy.

## Features

- **Bed controls**: Head up/down, feet up/down, both up/down, stop
- **Light control**: Under-bed light on/off
- **4-digit PIN**: Secure authentication with your bed's PIN

## Requirements

- **ESPHome Bluetooth proxy** (recommended) – flash an ESP32 with [bluetooth_proxy](https://esphome.io/components/bluetooth_proxy.html) and add it to Home Assistant. The integration uses `bluetooth_adapters` so it waits for your proxy to be ready.
- Or: Home Assistant with built-in Bluetooth
- Octo adjustable bed with Bluetooth
- Your bed's 4-digit PIN (from the manual or app)

## Installation

### Via HACS (recommended)

1. Open HACS → Integrations
2. Click the three dots (⋮) → Custom repositories
3. Add: `https://github.com/bramboe/octo-bed`
4. Search for "Octo Bed" and install
5. Restart Home Assistant

### Manual

1. Download the [latest release](https://github.com/bramboe/octo-bed/releases)
2. Copy the `custom_components/octo_bed` folder to your Home Assistant `custom_components` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for **Octo Bed**
3. If your bed is discovered via Bluetooth, select it. Otherwise, choose **Configure manually** and enter the bed's Bluetooth address (e.g. `F6:21:DD:DD:6F:19`)
4. Enter your 4-digit PIN when prompted

### Finding the Bluetooth address

- **With ESPHome Bluetooth proxy** (recommended): Use the **bed base** BLE MAC address (e.g. `F6:21:DD:DD:6F:19`), not the remote (RC2). Ensure your ESPHome device has `bluetooth_proxy:` and `esp32_ble_tracker:` in its config. Place the proxy near the bed.
- **Without proxy**: The bed must be in range of Home Assistant's Bluetooth. Use the address shown during discovery.

## Entities

After setup, you'll get:

- **Covers**: Head, Feet, Both – position-based (0% = down, 100% = up). Set a position and the bed moves until it reaches it, then stops automatically.
- **Buttons**: Both Down, Both Up, Both Up (Continuous), Feet Down, Feet Up, Head Down, Head Up, Head Up (Continuous), Stop
- **Switch**: Light (under-bed light)

### Cover configuration

The time for full travel (0% to 100%) defaults to 30 seconds. To change it: go to **Settings** → **Devices & Services** → **Octo Bed** → **Configure** and set "Full travel time (seconds)".

## Troubleshooting

- **Connection fails**: Ensure the bed is powered on and in range. With a Bluetooth proxy, try pressing a button on the remote to wake the bed.
- **Wrong PIN**: Double-check your 4-digit PIN from the bed's manual or app (e.g. 1987).
- **Use bed address, not proxy**: When using ESPHome Bluetooth proxy, always use the bed base BLE address (e.g. `F6:21:DD:DD:6F:19`), not the remote (RC2).
- **Debug logging**: Add to `configuration.yaml`:
  ```yaml
  logger:
    default: info
    logs:
      custom_components.octo_bed: debug
  ```

## Protocol

This integration uses the Octo bed BLE protocol:

- Commands are sent via ATT Write to handle `0x0011`
- Notifications on handle `0x0012` for PIN keep-alive
- PIN format: `40204300040001` + 4 PIN bytes + `40`

## License

MIT
