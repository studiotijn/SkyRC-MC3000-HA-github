#pragma once

#include <cstddef>
#include <cstdint>

// Pure protocol encode/decode for the SkyRC MC3000 BLE packet format.
// Deliberately has zero BLE/Arduino dependencies so it stays easy to reason
// about (and could be unit tested on the host) independently of the radio
// link. See docs/PROTOCOL.md in the repo root for the field-level reference
// and sources this was compiled from.
namespace mc3000 {

constexpr uint8_t PACKET_MAGIC = 0x0F;
constexpr size_t PACKET_SIZE = 20;
constexpr size_t PAYLOAD_SIZE = 17; // bytes 2..18 inclusive
constexpr uint8_t CHANNEL_COUNT = 4;

enum Command : uint8_t {
    CMD_START_CHARGE = 0x05,
    CMD_SET_PROGRAM = 0x11,
    CMD_GET_CHANNEL_DATA = 0x55,
    CMD_GET_VOLTAGE_CURVE = 0x56,
    CMD_GET_VERSION_INFO = 0x57,
    CMD_GET_BASIC_DATA = 0x61,
    CMD_STOP_CHARGE = 0xFE,
};

enum class BatteryType : uint8_t {
    LiIon = 0,
    LiFe = 1,
    LiIon435 = 2,
    NiMH = 3,
    NiCd = 4,
    NiZn = 5,
    Eneloop = 6,
    RAM = 7,
    LTO = 8,
};

enum class ChannelMode : uint8_t {
    Charge = 0,
    Refresh = 1,
    StorageOrBreakIn = 2,
    Discharge = 3,
    Cycle = 4,
};

enum class ChannelStatus : uint8_t {
    Standby = 0,
    Charge = 1,
    Discharge = 2,
    Pause = 3,
    Done = 4,
    InputVoltageLow = 128,
    InputVoltageHigh = 129,
    Mcp3424_1Error = 130,
    Mcp3424_2Error = 131,
    ConnectionBreak = 132,
    CheckVoltage = 133,
    CapacityProtection = 134,
    TimeProtection = 135,
    TempTooHigh = 136,
    BatteryTempTooHigh = 137,
    BatteryShortCircuit = 138,
    ReversePolarity = 139,
};

struct ChannelData {
    uint8_t channel = 0;
    BatteryType batteryType = BatteryType::LiIon;
    ChannelMode mode = ChannelMode::Charge;
    uint8_t cycleCount = 0;
    ChannelStatus status = ChannelStatus::Standby;
    uint16_t elapsedSeconds = 0;
    uint16_t millivolts = 0;
    uint16_t milliamps = 0;
    uint16_t capacityMah = 0;
    int8_t temperatureC = 0;
    uint16_t resistanceMilliohm = 0;
    uint8_t ledMask = 0; // bits 0-3 red ch1-4, bits 4-7 green ch1-4 (all channels, any reply)

    bool isRedLed(uint8_t ch) const { return (ledMask >> ch) & 1; }
    bool isGreenLed(uint8_t ch) const { return (ledMask >> (ch + 4)) & 1; }
};

struct BasicData {
    bool fahrenheit = false;
    bool beep = false;
    uint8_t displayMode = 0;
    bool screensaver = false;
    uint8_t coolingFanMode = 0;
    uint16_t inputMillivolts = 0;
};

struct VersionInfo {
    uint8_t fwMajor = 0;
    uint8_t fwMinor = 0;
    uint8_t hwVersionRaw = 0;
};

// A channel's full charge/discharge program, as set via CMD_SET_PROGRAM
// (0x11). Reverse-engineered from the official Android app's AddActivity
// (save()) - not documented in any prior public BLE writeup. See
// docs/PROTOCOL.md for the field-by-field byte layout and provenance.
//
// Fields marked "NiMH/NiCd/Eneloop only" are only meaningful (and only ever
// non-zero in the official app) when batteryType is one of those three -
// leave them 0 for other battery types.
struct ChargeProgram {
    BatteryType batteryType = BatteryType::LiIon;
    ChannelMode mode = ChannelMode::Charge;
    uint16_t capacityMah = 0;
    uint16_t chargeCurrentMa = 0;
    uint16_t dischargeCurrentMa = 0;
    uint16_t chargeCutoffMillivolts = 0;
    uint16_t dischargeCutoffMillivolts = 0;
    uint16_t chargeEndCurrentMa = 0;    // charge termination/trickle current
    uint16_t dischargeEndCurrentMa = 0; // discharge termination current
    uint8_t chargeRestMinutes = 0;      // also used as the cycle rest time
    uint8_t cycleCount = 0;
    uint8_t cycleMode = 0;        // 0=C>D, 1=C>D>C, 2=D>C, 3=D>C>D
    uint8_t negativeVoltageIncrementMv = 0; // NiMH/NiCd/Eneloop only (-dV/dt)
    uint8_t eddyCurrentTimesTen = 0;        // NiMH/NiCd/Eneloop only; actual mA = this * 10
    uint16_t keepVoltageMillivolts = 0;     // trickle/restart-threshold voltage
    uint8_t tempCutoffC = 0;
    uint16_t timeCutoffMinutes = 0;
    uint8_t dischargeRestMinutes = 0;
};

// Human-readable names for logging/debugging.
const char* batteryTypeName(BatteryType t);
const char* channelModeName(ChannelMode m);
const char* channelStatusName(ChannelStatus s);

// Builds a full PACKET_SIZE (20) byte request: [0x0F][command][payload, zero
// padded to 18 bytes][checksum]. `payload`/`payloadLen` may be shorter than
// PAYLOAD_SIZE (the rest is zero-filled) and `payload` may be nullptr if
// payloadLen is 0. Returns false (and writes nothing) if payloadLen is too long.
bool buildPacket(uint8_t command, const uint8_t* payload, size_t payloadLen,
                  uint8_t* outPacket, size_t outPacketCapacity);

// Convenience helper: builds the start/stop-charge payload, a single byte
// bitmask with bit N set for channel N (0-3).
inline uint8_t channelBit(uint8_t channel) { return static_cast<uint8_t>(1u << channel); }

// Checksum = sum(packet[0..19]) & 0xFF, compared against packet[20].
// Per the protocol docs, CMD_GET_VERSION_INFO replies are known to carry an
// invalid checksum due to a firmware bug - callers should not reject those
// packets based on this check alone.
bool checksumValid(const uint8_t* packet, size_t len);

// Decoders. All return false if `len` is too short or `packet[1]` doesn't
// match the expected command - they do NOT check the checksum themselves,
// callers should do that explicitly (see note above about CMD_GET_VERSION_INFO).
bool decodeChannelData(const uint8_t* packet, size_t len, ChannelData& out);
bool decodeBasicData(const uint8_t* packet, size_t len, BasicData& out);
bool decodeVersionInfo(const uint8_t* packet, size_t len, VersionInfo& out);

// Builds the two 20-byte packets that make up a CMD_SET_PROGRAM (0x11)
// write for the given channel(s). Unlike every other command, this one is a
// single logical 40-byte structure split across two consecutive
// write-without-response packets - the checksum (sum of all 39 preceding
// bytes, mod 256) lives in the *second* packet's last byte, and that second
// packet has no 0x0F/command header of its own; it's a raw continuation.
// The device buffers packet 1 and only acts once packet 2 arrives, so the
// two writes must be sent close together (the official app leaves ~50ms
// between them) without another command in between.
void buildSetProgramPackets(const ChargeProgram& program, uint8_t channelMask, uint8_t outPacket1[PACKET_SIZE],
                             uint8_t outPacket2[PACKET_SIZE]);

} // namespace mc3000
