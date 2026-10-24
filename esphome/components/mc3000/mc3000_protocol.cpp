#include "mc3000_protocol.h"

#include <cstring>

namespace mc3000_protocol {

namespace {

uint16_t readBE16(const uint8_t* p) {
    return static_cast<uint16_t>((p[0] << 8) | p[1]);
}

void writeBE16(uint8_t* p, uint16_t value) {
    p[0] = static_cast<uint8_t>(value >> 8);
    p[1] = static_cast<uint8_t>(value & 0xFF);
}

uint8_t sumChecksum(const uint8_t* packet, size_t count) {
    uint32_t sum = 0;
    for (size_t i = 0; i < count; i++) {
        sum += packet[i];
    }
    return static_cast<uint8_t>(sum & 0xFF);
}

} // namespace

const char* batteryTypeName(BatteryType t) {
    switch (t) {
        case BatteryType::LiIon: return "LiIon";
        case BatteryType::LiFe: return "LiFe";
        case BatteryType::LiIon435: return "LiIon 4.35V";
        case BatteryType::NiMH: return "NiMH";
        case BatteryType::NiCd: return "NiCd";
        case BatteryType::NiZn: return "NiZn";
        case BatteryType::Eneloop: return "Eneloop";
        case BatteryType::RAM: return "RAM";
        case BatteryType::LTO: return "LTO";
        default: return "Unknown";
    }
}

const char* channelModeName(ChannelMode m) {
    switch (m) {
        case ChannelMode::Charge: return "Charge";
        case ChannelMode::Refresh: return "Refresh";
        case ChannelMode::StorageOrBreakIn: return "Storage/Break-in";
        case ChannelMode::Discharge: return "Discharge";
        case ChannelMode::Cycle: return "Cycle";
        default: return "Unknown";
    }
}

const char* channelStatusName(ChannelStatus s) {
    switch (s) {
        case ChannelStatus::Standby: return "Standby";
        case ChannelStatus::Charge: return "Charging";
        case ChannelStatus::Discharge: return "Discharging";
        case ChannelStatus::Pause: return "Paused";
        case ChannelStatus::Done: return "Done";
        case ChannelStatus::InputVoltageLow: return "FAULT: input voltage too low";
        case ChannelStatus::InputVoltageHigh: return "FAULT: input voltage too high";
        case ChannelStatus::Mcp3424_1Error: return "FAULT: MCP3424-1 error";
        case ChannelStatus::Mcp3424_2Error: return "FAULT: MCP3424-2 error";
        case ChannelStatus::ConnectionBreak: return "FAULT: connection break";
        case ChannelStatus::CheckVoltage: return "FAULT: check voltage";
        case ChannelStatus::CapacityProtection: return "FAULT: capacity protection";
        case ChannelStatus::TimeProtection: return "FAULT: time protection";
        case ChannelStatus::TempTooHigh: return "FAULT: temperature too high";
        case ChannelStatus::BatteryTempTooHigh: return "FAULT: battery temperature too high";
        case ChannelStatus::BatteryShortCircuit: return "FAULT: battery short circuit";
        case ChannelStatus::ReversePolarity: return "FAULT: reverse polarity";
        default: return "Unknown";
    }
}

bool buildPacket(uint8_t command, const uint8_t* payload, size_t payloadLen,
                  uint8_t* outPacket, size_t outPacketCapacity) {
    if (outPacketCapacity < PACKET_SIZE || payloadLen > PAYLOAD_SIZE) {
        return false;
    }

    std::memset(outPacket, 0, PACKET_SIZE);
    outPacket[0] = PACKET_MAGIC;
    outPacket[1] = command;
    if (payload != nullptr && payloadLen > 0) {
        std::memcpy(outPacket + 2, payload, payloadLen);
    }
    outPacket[PACKET_SIZE - 1] = sumChecksum(outPacket, PACKET_SIZE - 1);
    return true;
}

bool checksumValid(const uint8_t* packet, size_t len) {
    if (len < PACKET_SIZE) {
        return false;
    }
    return sumChecksum(packet, PACKET_SIZE - 1) == packet[PACKET_SIZE - 1];
}

bool decodeChannelData(const uint8_t* packet, size_t len, ChannelData& out) {
    if (len < PACKET_SIZE || packet[1] != CMD_GET_CHANNEL_DATA) {
        return false;
    }
    out.channel = packet[2];
    out.batteryType = static_cast<BatteryType>(packet[3]);
    out.mode = static_cast<ChannelMode>(packet[4]);
    out.cycleCount = packet[5];
    out.status = static_cast<ChannelStatus>(packet[6]);
    out.elapsedSeconds = readBE16(packet + 7);
    out.millivolts = readBE16(packet + 9);
    out.milliamps = readBE16(packet + 11);
    out.capacityMah = readBE16(packet + 13);
    out.temperatureC = static_cast<int8_t>(packet[15]);
    out.resistanceMilliohm = readBE16(packet + 16);
    out.ledMask = packet[18];
    return true;
}

bool decodeBasicData(const uint8_t* packet, size_t len, BasicData& out) {
    if (len < PACKET_SIZE || packet[1] != CMD_GET_BASIC_DATA) {
        return false;
    }
    out.fahrenheit = packet[2] != 0;
    out.beep = packet[3] != 0;
    out.displayMode = packet[4];
    out.screensaver = packet[5] != 0;
    out.coolingFanMode = packet[6];
    out.inputMillivolts = readBE16(packet + 7);
    return true;
}

bool decodeVersionInfo(const uint8_t* packet, size_t len, VersionInfo& out) {
    if (len < PACKET_SIZE || packet[1] != CMD_GET_VERSION_INFO) {
        return false;
    }
    out.fwMajor = packet[14];
    out.fwMinor = packet[15];
    out.hwVersionRaw = packet[16];
    return true;
}

void buildSetProgramPackets(const ChargeProgram& program, uint8_t channelMask, uint8_t outPacket1[PACKET_SIZE],
                             uint8_t outPacket2[PACKET_SIZE]) {
    uint8_t buf[40] = {0};

    buf[0] = PACKET_MAGIC;
    buf[1] = CMD_SET_PROGRAM;
    buf[2] = channelMask;
    buf[3] = static_cast<uint8_t>(program.batteryType);
    buf[4] = static_cast<uint8_t>(program.mode);
    writeBE16(buf + 5, program.capacityMah);
    writeBE16(buf + 7, program.chargeCurrentMa);
    writeBE16(buf + 9, program.dischargeCurrentMa);
    writeBE16(buf + 11, program.chargeCutoffMillivolts);
    writeBE16(buf + 13, program.dischargeCutoffMillivolts);
    writeBE16(buf + 15, program.chargeEndCurrentMa);
    writeBE16(buf + 17, program.dischargeEndCurrentMa);
    buf[19] = program.chargeRestMinutes;
    buf[20] = program.cycleCount;
    buf[21] = program.cycleMode;
    buf[22] = program.negativeVoltageIncrementMv;
    buf[23] = program.eddyCurrentTimesTen;
    writeBE16(buf + 24, program.keepVoltageMillivolts);
    buf[26] = program.tempCutoffC;
    writeBE16(buf + 27, program.timeCutoffMinutes);
    buf[29] = program.dischargeRestMinutes;
    // bytes 30..38 stay zero (unused by the official app's encoder either)
    buf[39] = sumChecksum(buf, 39);

    std::memcpy(outPacket1, buf, PACKET_SIZE);
    std::memcpy(outPacket2, buf + PACKET_SIZE, PACKET_SIZE);
}

} // namespace mc3000_protocol
