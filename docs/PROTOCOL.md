# SkyRC MC3000 — BLE protocol reference

This is a consolidated reference for the BLE protocol spoken by the SkyRC MC3000
4-channel charger/analyzer. Nothing here was sniffed from scratch — it is compiled
from prior community reverse-engineering (see **Sources** below) and will be
extended/corrected as we validate things against the real device from this project.

## Device identification

- Advertises under one of these local names: `SimpleBLEPeripheral`, `Charger`, `HitecCharger`.
- Single GATT service: `0000ffe0-0000-1000-8000-00805f9b34fb`
- Single GATT characteristic: `0000ffe1-0000-1000-8000-00805f9b34fb`
  - Used for **both** writing commands and receiving responses (as notifications).
  - You must subscribe to notifications on this characteristic after connecting.
  - The device **never** sends data on its own — everything must be polled by
    writing a request packet and waiting for the notification reply.
  - Only one BLE central can be connected at a time — the official app and this
    project cannot talk to the charger simultaneously.

## General packet layout

Every packet (request and response) is **20 bytes**:

| Byte index | Meaning |
|---|---|
| 0 | `0x0F` — fixed start byte |
| 1 | Command |
| 2–18 | Payload (17 bytes, command-specific, zero-padded) |
| 19 | Checksum = sum of bytes 0..18, mod 256 |

The published protocol docs we drew from label this column "byte index 20",
which reads as 21 bytes total if you take it at face value - but the actual
example hex dumps in that same source, and the working reference
implementations (`mc3000.py`'s `_send_packet`, `mc3000usb.py`'s struct
layouts), are unambiguous: **20 bytes**. We hit this the hard way - a 21-byte
packet is exactly 1 byte too long to fit a single ATT write at the MTU this
charger negotiates (see "Transport note" below), so every request silently
went nowhere until we caught the off-by-one.

Exception: replies to "get version info" (`0x57`) carry an invalid checksum —
this is a known firmware quirk, not a framing error. Don't reject those replies
on checksum alone.

### Transport note (BLE-specific, not in the original docs)

- The `ffe1` characteristic only advertises the **Write Without Response**
  property (`canWrite()` is false, `canWriteNoResponse()` is true on the real
  device). Writing with a Write Request (response required) silently never
  produces a reply - it must be a write-without-response.
- This particular charger negotiates (and appears to cap) the connection at
  the default ATT MTU of 23 bytes (20 usable payload bytes for a single
  write-without-response), even when the central proposes a larger MTU. A
  correctly-sized 20-byte packet fits exactly; anything longer won't go out
  in one piece and there's no queued-write fallback for write-without-response.

## Known commands

### `0x55` — Get channel data

Request: byte 2 = channel index (0-3), rest zero.

Response:

| Bytes | Field | Notes |
|---|---|---|
| 2 | channel | 0-3 |
| 3 | battery type | 0 LiIon, 1 LiFe, 2 LiIon 4.35V, 3 NiMH, 4 NiCd, 5 NiZn, 6 Eneloop, 7 RAM, 8 LTO |
| 4 | mode | 0 Charge, 1 Refresh, 2 Storage/Break-in, 3 Discharge, 4 Cycle |
| 5 | cycle count | |
| 6 | status | 0 Standby, 1 Charge, 2 Discharge, 3 Pause, 4 Done, 128-139 = various fault codes (see below) |
| 7-8 | elapsed time (s) | big-endian u16 |
| 9-10 | voltage (mV) | big-endian u16 |
| 11-12 | current (mA) | big-endian u16 |
| 13-14 | capacity (mAh) | big-endian u16 |
| 15 | temperature (°C) | |
| 16-17 | internal resistance (mΩ) | big-endian u16 |
| 18 | LED bitmask, all 4 channels | bits 0-3 = red ch1-4, bits 4-7 = green ch1-4 |

Status fault codes: 128 input voltage low, 129 input voltage high, 130/131 MCP3424
ADC error, 132 connection break, 133 check voltage, 134 capacity protection,
135 time protection, 136 temp too high, 137 battery temp too high, 138 battery
short circuit, 139 reverse polarity.

### `0x56` — Get voltage curve

Request: byte 2 = channel. Response is the **only multi-packet reply** (246 bytes
total: channel, time, 120× u16 mV samples, checksum) — not implemented yet in
Phase 1 firmware.

### `0x57` — Get version info

Response bytes 14-15 = firmware version (major.minor), byte 16 = hardware version
(`value // 10` . `value % 10`). Checksum on this reply is known to be invalid.

### `0x61` — Get basic/system data

| Byte | Field |
|---|---|
| 2 | temperature unit (0 °C, 1 °F) |
| 3 | system beep (0/1) |
| 4 | display mode (0 off, 1 auto, 2 1min, 3 3min, 4 5min, 5 always on) |
| 5 | screensaver (0/1) |
| 6 | cooling fan mode (0 auto, 1 off, 2 on, 3-9 fixed temp thresholds 20-50°C) |
| 7-8 | input voltage (mV), big-endian u16 |

### `0x05` — Start charge

Byte 2 = bitmask of channels to start (bit0=ch1 .. bit3=ch4). Response echoes the
mask back with bytes 3-5 = `F0 FF FF` (meaning unknown/unconfirmed).

### `0xFE` — Stop charge

Same shape as start charge.

## `0x11` — Set Program (write a channel's charge program)

Not in any prior public BLE writeup we found - reverse-engineered by
decompiling the official Android app (`com.Sky_mc3000`, via `jadx`) and
reading `AddActivity.save()` / `BleThread`/`Config` in
`com.skyrc.mc3000.{activity,thread,broadcast.actions}`. The app only ever
*sends* this from `ProgramActivity`/`ProgramFragment` when the user taps
"Start" on a saved preset - it never reads a program back over BLE (presets
live in the app's own local SQLite DB, not on the device).

This is the one command that doesn't fit the general packet layout: it's a
**40-byte logical structure split across two consecutive 20-byte
write-without-response packets**. Only the *first* packet gets the normal
`0x0F`/command header - the second is a raw continuation with no header of
its own, and the checksum (sum of all 39 preceding bytes, mod 256) lives in
its last byte. The device buffers packet 1 and only acts once packet 2
arrives, so nothing else should be sent between them. The official app
leaves ~50ms between the two writes.

**Packet 1** (bytes 0-19 of the logical structure):

| Byte | Field | Notes |
|---|---|---|
| 0 | `0x0F` | magic |
| 1 | `0x11` (17) | command |
| 2 | channel mask | same bitmask as start/stop charge (bit0=ch1..bit3=ch4) |
| 3 | battery type | same enum as channel-data reads |
| 4 | mode | same enum as channel-data reads (0 Charge, 1 Refresh, 2 Storage/Break-in, 3 Discharge, 4 Cycle) |
| 5-6 | capacity (mAh) | big-endian u16, **raw value**, not an index (unlike the USB protocol's capacity table) |
| 7-8 | charge current (mA) | big-endian u16 |
| 9-10 | discharge current (mA) | big-endian u16 |
| 11-12 | charge cutoff voltage (mV) | big-endian u16 |
| 13-14 | discharge cutoff voltage (mV) | big-endian u16 |
| 15-16 | charge end/termination current (mA) | big-endian u16 - trickle/full-detect threshold |
| 17-18 | discharge end/termination current (mA) | big-endian u16 |
| 19 | charge rest time (min) | also reused as the cycle rest time in the app's own DB model |

**Packet 2** (bytes 20-39 of the logical structure, **no header of its own**):

| Byte (logical offset) | Field | Notes |
|---|---|---|
| 20 | cycle count | |
| 21 | cycle mode | 0 = C>D, 1 = C>D>C, 2 = D>C, 3 = D>C>D (break-in mode only uses 0/1) |
| 22 | negative voltage increment (mV) | NiMH/NiCd/Eneloop only (-dV/dt detection); 0 otherwise |
| 23 | eddy current ÷ 10 | NiMH/NiCd/Eneloop only; actual mA = this byte × 10; 0 otherwise |
| 24-25 | keep/restart voltage (mV) | big-endian u16 |
| 26 | temperature cutoff (°C) | |
| 27-28 | time cutoff (min) | big-endian u16; app sends 0 in break-in mode |
| 29 | discharge rest time (min) | |
| 30-38 | unused | zero |
| 39 | checksum | sum of bytes 0-38 (across **both** packets), mod 256 |

The device's reply echoes command byte `0x11` with the channel mask in byte
2 - `BleThread.analyAvailable()` in the app treats that reply as a cue to
immediately send `startCharge` for that mask, i.e. the official app always
auto-starts charging right after setting a program. That's an app-level
choice, not something the firmware has to replicate - our `program` serial
command just reports the raw reply.

Implemented in `firmware/include/Mc3000Protocol.h` (`ChargeProgram`,
`buildSetProgramPackets`) and exposed via the `program` serial command - see
`README.md`. **Verified against the real charger**: `program 1 0 0 2000 500
500 4200 3000 50` (LiIon, Charge, 2000mAh, 500mA/500mA, 4.2V/3.0V cutoffs,
50mA end-current) got the expected `0F 11 01 F0 FF FF ...` ack, and starting
channel 1 afterwards made it charge at a measured 0.499-0.500A - exactly the
newly-set current, not whatever was configured before.

## Checksum

```
checksum(bytes[0..18]) = sum(bytes[0..18]) & 0xFF   # stored in byte 19
```

Same additive formula is used for both BLE and USB framing (just over different byte ranges).

## Sources

- [kroimon/skyrc-ble](https://github.com/kroimon/skyrc-ble) — `docs/protocol.rst`,
  `src/skyrc_ble/mc3000.py`, `const.py`, `models.py`. MIT-licensed Python/bleak
  library; the BLE protocol tables above are drawn from its documentation.
- [kolinger/skyrc-mc3000](https://github.com/kolinger/skyrc-mc3000) —
  `mc3000usb.py`, `shared.py`. MIT-licensed; source of the USB program
  read/write field layout (an early, ultimately unneeded lead for the `0x11`
  command below, once that was found directly).
- Official `MC3000` Android app (`com.Sky_mc3000`, v4.1.2), decompiled with
  [jadx](https://github.com/skylot/jadx) for interoperability purposes.
  Source of the `0x11` "Set Program" command - see
  `com.skyrc.mc3000.activity.AddActivity.save()`,
  `com.skyrc.mc3000.broadcast.actions.Config`, and
  `com.skyrc.mc3000.thread.BleThread`.
