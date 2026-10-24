// SkyRC MC3000 <-> ESP32-C6 BLE bridge - Phase 1 (connect, decode, control).
//
// This is a standalone PlatformIO/Arduino sketch, not (yet) an ESPHome
// component - see ../../README.md for why, and for the plan to turn this into
// an ESPHome external_component once the protocol is fully validated.
//
// Serial commands (115200 baud):
//   status            - print last known state of all 4 channels + basic data
//   poll              - force an immediate poll of basic data + all channels
//   start <1-4>       - start charging on the given channel
//   stop  <1-4>       - stop charging on the given channel
//   raw <hex bytes>   - send an arbitrary 20-byte-or-less packet (auto zero
//                       padded + checksummed) and print the raw hex reply.
//                       Use this to probe not-yet-decoded commands.
//   program <ch> <type> <mode> <capacity_mAh> <chgI_mA> <dischgI_mA>
//           <chgCutoff_mV> <dischgCutoff_mV> <chgEndI_mA> [cycleCount] [cycleMode]
//           cycleMode: 0=C>D 1=C>D>C 2=D>C 3=D>C>D (only meaningful if mode=4/Cycle)
//                     - set a channel's charge program (CMD_SET_PROGRAM /
//                       0x11, see docs/PROTOCOL.md). Advanced fields not
//                       listed here (cycle count/mode, NiMH -dV/dt, temp/time
//                       cutoffs, ...) are left at 0/off - extend ChargeProgram
//                       in Mc3000Protocol.h and this command if you need them.
//                       type/mode are the same enums as in a channel-data
//                       reply (0=LiIon..8=LTO / 0=Charge..4=Cycle).
//   help              - show this list

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <esp_log.h>

#include "Mc3000BleLink.h"
#include "Mc3000Protocol.h"

namespace {

Mc3000BleLink g_link;

mc3000::ChannelData g_channels[mc3000::CHANNEL_COUNT];
bool g_haveChannel[mc3000::CHANNEL_COUNT] = {false, false, false, false};
mc3000::BasicData g_basic;
bool g_haveBasic = false;
mc3000::VersionInfo g_version;
bool g_haveVersion = false;

uint32_t g_lastPollMs = 0;
constexpr uint32_t kPollIntervalMs = 3000;

void printHelp() {
    Serial.println();
    Serial.println("Commands: status | poll | start <1-4> | stop <1-4> | raw <hex bytes> | program ... | help");
}

void printChannel(uint8_t ch) {
    if (!g_haveChannel[ch]) {
        Serial.printf("  ch%d: (no data yet)\n", ch + 1);
        return;
    }
    const mc3000::ChannelData& c = g_channels[ch];
    Serial.printf(
        "  ch%d: %-16s mode=%-16s status=%-28s %5.3fV %6.3fA cap=%4umAh T=%3dC R=%4umOhm cycles=%u\n",
        ch + 1, mc3000::batteryTypeName(c.batteryType), mc3000::channelModeName(c.mode),
        mc3000::channelStatusName(c.status), c.millivolts / 1000.0f, c.milliamps / 1000.0f, c.capacityMah,
        c.temperatureC, c.resistanceMilliohm, c.cycleCount);
}

void printStatus() {
    Serial.println();
    if (g_haveVersion) {
        Serial.printf("MC3000 firmware %u.%u, hardware %u.%u\n", g_version.fwMajor, g_version.fwMinor,
                       g_version.hwVersionRaw / 10, g_version.hwVersionRaw % 10);
    }
    if (g_haveBasic) {
        Serial.printf("Input: %.3fV  beep=%d  fan_mode=%u  display_mode=%u\n", g_basic.inputMillivolts / 1000.0f,
                       g_basic.beep, g_basic.coolingFanMode, g_basic.displayMode);
    }
    for (uint8_t ch = 0; ch < mc3000::CHANNEL_COUNT; ch++) {
        printChannel(ch);
    }
}

bool sendSimple(uint8_t command, const uint8_t* payload, size_t payloadLen, uint8_t* response) {
    uint8_t packet[mc3000::PACKET_SIZE];
    if (!mc3000::buildPacket(command, payload, payloadLen, packet, sizeof(packet))) {
        Serial.println("buildPacket failed (payload too long?)");
        return false;
    }
    return g_link.transact(packet, response, mc3000::PACKET_SIZE);
}

void pollAll() {
    if (!g_link.isReady()) {
        Serial.println("Not connected yet.");
        return;
    }

    uint8_t resp[mc3000::PACKET_SIZE];

    if (!g_haveVersion) {
        if (sendSimple(mc3000::CMD_GET_VERSION_INFO, nullptr, 0, resp)) {
            // Version replies have a known-bad checksum - decode unconditionally.
            g_haveVersion = mc3000::decodeVersionInfo(resp, sizeof(resp), g_version);
        }
    }

    if (sendSimple(mc3000::CMD_GET_BASIC_DATA, nullptr, 0, resp)) {
        if (mc3000::checksumValid(resp, sizeof(resp)) && mc3000::decodeBasicData(resp, sizeof(resp), g_basic)) {
            g_haveBasic = true;
        }
    }

    for (uint8_t ch = 0; ch < mc3000::CHANNEL_COUNT; ch++) {
        uint8_t payload[1] = {ch};
        if (sendSimple(mc3000::CMD_GET_CHANNEL_DATA, payload, 1, resp)) {
            if (mc3000::checksumValid(resp, sizeof(resp)) && mc3000::decodeChannelData(resp, sizeof(resp), g_channels[ch])) {
                g_haveChannel[ch] = true;
            } else {
                Serial.printf("ch%d: bad checksum/decode on channel-data reply\n", ch + 1);
            }
        }
    }

    printStatus();
}

void doStartStop(uint8_t command, int channel1based) {
    if (channel1based < 1 || channel1based > mc3000::CHANNEL_COUNT) {
        Serial.println("Channel must be 1-4.");
        return;
    }
    uint8_t mask = mc3000::channelBit(static_cast<uint8_t>(channel1based - 1));
    uint8_t resp[mc3000::PACKET_SIZE];
    if (sendSimple(command, &mask, 1, resp)) {
        Serial.printf("%s channel %d: ", command == mc3000::CMD_START_CHARGE ? "start" : "stop", channel1based);
        for (size_t i = 0; i < mc3000::PACKET_SIZE; i++) {
            Serial.printf("%02X ", resp[i]);
        }
        Serial.println();
    }
}

void doRaw(const String& hexArg) {
    uint8_t payload[mc3000::PAYLOAD_SIZE] = {0};
    size_t payloadLen = 0;
    uint8_t commandByte = 0;
    bool haveCommand = false;

    String token;
    for (size_t i = 0; i <= static_cast<size_t>(hexArg.length()); i++) {
        char c = (i < static_cast<size_t>(hexArg.length())) ? hexArg[i] : ' ';
        if (c == ' ' || c == '\t') {
            if (token.length() > 0) {
                uint8_t byteVal = static_cast<uint8_t>(strtoul(token.c_str(), nullptr, 16));
                if (!haveCommand) {
                    commandByte = byteVal;
                    haveCommand = true;
                } else if (payloadLen < mc3000::PAYLOAD_SIZE) {
                    payload[payloadLen++] = byteVal;
                }
                token = "";
            }
        } else {
            token += c;
        }
    }

    if (!haveCommand) {
        Serial.println("Usage: raw <command-byte-hex> [payload bytes, hex, space separated]");
        Serial.println("Example: raw 55 00        (same as polling channel 0)");
        return;
    }

    uint8_t resp[mc3000::PACKET_SIZE];
    if (sendSimple(commandByte, payload, payloadLen, resp)) {
        Serial.print("reply: ");
        for (size_t i = 0; i < mc3000::PACKET_SIZE; i++) {
            Serial.printf("%02X ", resp[i]);
        }
        Serial.printf(" (checksum %s)\n", mc3000::checksumValid(resp, sizeof(resp)) ? "OK" : "INVALID");
    }
}

// Pops the next whitespace-separated token off the front of `s` (mutating
// it) and returns it as a long; returns false if no token remains.
bool popLongToken(String& s, long& outValue) {
    s.trim();
    if (s.length() == 0) {
        return false;
    }
    int spaceIdx = s.indexOf(' ');
    String token = (spaceIdx == -1) ? s : s.substring(0, spaceIdx);
    s = (spaceIdx == -1) ? String("") : s.substring(spaceIdx + 1);
    outValue = strtol(token.c_str(), nullptr, 10);
    return true;
}

void doProgram(String args) {
    long channel1based, type, mode, capacity, chgI, dischgI, chgCutV, dischgCutV, chgEndI;
    long cycleCount = 0, cycleMode = 0;
    if (!popLongToken(args, channel1based) || !popLongToken(args, type) || !popLongToken(args, mode) ||
        !popLongToken(args, capacity) || !popLongToken(args, chgI) || !popLongToken(args, dischgI) ||
        !popLongToken(args, chgCutV) || !popLongToken(args, dischgCutV) || !popLongToken(args, chgEndI)) {
        Serial.println("Usage: program <ch 1-4> <type> <mode> <capacity_mAh> <chgI_mA> <dischgI_mA>"
                        " <chgCutoff_mV> <dischgCutoff_mV> <chgEndI_mA> [cycleCount] [cycleMode]");
        return;
    }
    // Optional trailing args - absent means "not a cycle program" (0/0).
    popLongToken(args, cycleCount);
    popLongToken(args, cycleMode);
    if (channel1based < 1 || channel1based > mc3000::CHANNEL_COUNT) {
        Serial.println("Channel must be 1-4.");
        return;
    }
    if (!g_link.isReady()) {
        Serial.println("Not connected yet.");
        return;
    }

    mc3000::ChargeProgram program;
    program.batteryType = static_cast<mc3000::BatteryType>(type);
    program.mode = static_cast<mc3000::ChannelMode>(mode);
    program.capacityMah = static_cast<uint16_t>(capacity);
    program.chargeCurrentMa = static_cast<uint16_t>(chgI);
    program.dischargeCurrentMa = static_cast<uint16_t>(dischgI);
    program.chargeCutoffMillivolts = static_cast<uint16_t>(chgCutV);
    program.dischargeCutoffMillivolts = static_cast<uint16_t>(dischgCutV);
    program.chargeEndCurrentMa = static_cast<uint16_t>(chgEndI);
    program.cycleCount = static_cast<uint8_t>(cycleCount);
    program.cycleMode = static_cast<uint8_t>(cycleMode);

    uint8_t packet1[mc3000::PACKET_SIZE];
    uint8_t packet2[mc3000::PACKET_SIZE];
    uint8_t mask = mc3000::channelBit(static_cast<uint8_t>(channel1based - 1));
    mc3000::buildSetProgramPackets(program, mask, packet1, packet2);

    if (!g_link.writeRaw(packet1, mc3000::PACKET_SIZE)) {
        Serial.println("Failed to send program packet 1/2.");
        return;
    }
    delay(50); // matches the gap the official app leaves between the two packets

    uint8_t resp[mc3000::PACKET_SIZE];
    if (g_link.transact(packet2, resp, sizeof(resp))) {
        Serial.print("program reply: ");
        for (size_t i = 0; i < mc3000::PACKET_SIZE; i++) {
            Serial.printf("%02X ", resp[i]);
        }
        Serial.println();
    } else {
        Serial.println("No reply to program packet 2/2 (device may not have applied it).");
    }
}

void handleLine(String line) {
    line.trim();
    if (line.length() == 0) {
        return;
    }

    int firstSpace = line.indexOf(' ');
    String cmd = (firstSpace == -1) ? line : line.substring(0, firstSpace);
    String rest = (firstSpace == -1) ? String("") : line.substring(firstSpace + 1);
    rest.trim();
    cmd.toLowerCase();

    if (cmd == "status") {
        printStatus();
    } else if (cmd == "poll") {
        pollAll();
    } else if (cmd == "start") {
        doStartStop(mc3000::CMD_START_CHARGE, rest.toInt());
    } else if (cmd == "stop") {
        doStartStop(mc3000::CMD_STOP_CHARGE, rest.toInt());
    } else if (cmd == "raw") {
        doRaw(rest);
    } else if (cmd == "program") {
        doProgram(rest);
    } else if (cmd == "help" || cmd == "?") {
        printHelp();
    } else {
        Serial.printf("Unknown command '%s'.\n", cmd.c_str());
        printHelp();
    }
}

} // namespace

void setup() {
    Serial.begin(115200);
    delay(300);

    // NimBLE's own component logging is extremely verbose at the default
    // level and will flood/overflow the USB-CDC buffer, pushing out our own
    // status output. We print our own [BLE] lines explicitly where useful,
    // so silence the rest.
    esp_log_level_set("*", ESP_LOG_WARN);

    Serial.println();
    Serial.println("=== SkyRC MC3000 BLE bridge - Phase 1 ===");
    printHelp();

    NimBLEDevice::init("MC3000-Bridge");
    NimBLEDevice::setMTU(256);
    g_link.beginScan();
}

void loop() {
    g_link.update();

    static bool wasReady = false;
    bool ready = g_link.isReady();
    if (ready && !wasReady) {
        Serial.println("[BLE] Link ready, polling initial state...");
        pollAll();
        g_lastPollMs = millis();
    }
    wasReady = ready;

    if (ready && (millis() - g_lastPollMs > kPollIntervalMs)) {
        pollAll();
        g_lastPollMs = millis();
    }

    if (Serial.available() > 0) {
        String line = Serial.readStringUntil('\n');
        handleLine(line);
    }
}
