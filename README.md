# Octo Bed - Home Assistant Integration

Control your Octo adjustable bed (Octo Actuators control box, e.g. with the Star2 remote) from Home Assistant via Bluetooth, using the built-in Bluetooth adapter or an ESPHome Bluetooth proxy.

## Features

- **Bed controls**: Head up/down, feet up/down, both up/down, stop
- **Position covers**: Head, Feet, Both – set a target position (0–100%) and the bed moves there; positions survive a Home Assistant restart
- **Light**: Under-bed light (RGBW color control on beds that support it)
- **Memory presets**: Recall and save the bed's hardware preset positions (when the bed reports support)
- **Two beds as one**: Pair two beds into a "Both beds" device with shared controls and position sync
- **Calibration**: Measure the real full-travel time per motor for accurate positioning
- **4-digit PIN**: Authentication with automatic keep-alive (the bed drops the connection after ~30 s without it)
- **Automatic reconnect**: Reconnects in the background when the connection drops
- **Multi-language**: English, Dutch, German and French (config flow, entity names and sensor states follow your Home Assistant language)

## Requirements

- Home Assistant 2024.11 or newer with Bluetooth support (or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html))
- Octo adjustable bed with a Bluetooth control box
- Your bed's 4-digit PIN (from the manual or the OCTO Smart Control app)

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
3. If your bed is discovered via Bluetooth, select it. Otherwise, choose **Enter Bluetooth address manually** (e.g. `F6:21:DD:DD:6F:19`)
4. Enter your 4-digit PIN when prompted
5. With a second bed you can pair both into a combined "Both beds" device

### Finding the Bluetooth address

- **With ESPHome Bluetooth proxy**: Use the **bed base** BLE MAC address, not the remote (RC2 is the name the control box advertises). You can find it in the ESPHome logs when the bed advertises, or by scanning with a BLE tool.
- **Without proxy**: The bed must be in range of Home Assistant's Bluetooth. Use the address shown during discovery.

## Entities

After setup, you'll get:

- **Covers**: Head, Feet, Both – position-based (0% = down, 100% = up). Set a position and the bed moves until it reaches it, then stops automatically.
- **Switches**: Head/Feet/Both Up & Down (hold-style movement), Synchro mode (linked drive mode, only on beds that report it; disabled by default)
- **Light**: Under-bed light (with RGBW color picker when the bed supports it)
- **Buttons**: Stop, calibration buttons, preset recall/save buttons (when the bed reports memory slots), sync-to-other-bed buttons (with two beds)
- **Diagnostic sensors**: Head/feet position, connection status, calibration status, MAC address

> **Note on presets**: after recalling a hardware preset the bed moves on its own; the integration cannot track that movement, so the shown position may drift until the next full up/down move or calibration.

### Cover configuration

The time for full travel (0% to 100%) defaults to 30 seconds per motor. Use the calibration buttons for an exact measurement, or set it manually via **Settings** → **Devices & Services** → **Octo Bed** → **Configure**.

### Calibration

1. Press **Calibrate head** (or **Calibrate feet**). The bed first drives that part fully down so the measurement always starts from 0%, then starts moving it up while a timer runs.
2. The moment the part reaches its highest point, press **Complete calibration session**. The measured time (clamped to 5–120 s) is saved as that motor's full travel time and the part returns to 0%.
3. The **Stop** button stays available during calibration and aborts the session without saving. A session that is never completed aborts itself after 3 minutes.

The calibration status sensor shows the current phase (moving to start / measuring / returning) and the elapsed measuring time.

#### Calibrating two paired beds together

When you pair two beds, the flow asks whether you want to calibrate both beds together. If enabled, the combined "Both beds" device gets the calibration buttons: one session moves **both beds simultaneously** (down to 0%, then up while measuring) and stores the same travel times for both, keeping them in sync. The per-bed calibration buttons are disabled while paired. You can toggle this later via the options of the combined device.

## Breaking changes in 2.0.0

- The under-bed light is now a **light** entity instead of a switch. Update automations that used `switch.<bed>_light` to `light.<bed>_light`.
- The "(Continuous)" buttons were removed: their captured commands turned out to decode as *feet down* in the Octo protocol and never did what the name said.

## Troubleshooting

- **Connection fails**: Ensure the bed is powered on and in range. With a Bluetooth proxy, try pressing a button on the remote to wake the bed.
- **Wrong PIN**: Double-check your 4-digit PIN from the bed's manual or app.
- **Use bed address, not proxy**: When using ESPHome Bluetooth proxy, always use the bed's BLE address, not the proxy's.
- **Entities unavailable**: The integration reconnects automatically with backoff; check the Connection status sensor and Home Assistant logs.

## Protocol

This integration speaks the standard Octo BLE protocol (service `0xFFE0`, write characteristic `0xFFE1`):

- Packets: `[0x40, cmd, cmd, len_hi, len_lo, checksum, ...data, 0x40]` with byte stuffing
- Motors are addressed with a bit mask (head `0x02`, feet `0x04`)
- PIN authentication: command `[0x20, 0x43]` + 4 PIN digit bytes, refreshed every 25 s
- Capabilities (presets, synchro, RGBW light) are discovered via `[0x20, 0x71]`

Protocol knowledge builds on the reverse-engineering work of the Home Assistant community
([forum thread](https://community.home-assistant.io/t/540790),
[ha-adjustable-bed](https://github.com/kristofferR/ha-adjustable-bed),
[smartbed-mqtt](https://github.com/richardhopton/smartbed-mqtt)).

## License

MIT
