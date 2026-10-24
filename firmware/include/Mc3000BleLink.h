#pragma once

#include <cstddef>
#include <cstdint>

#include <NimBLEDevice.h>

#include "Mc3000Protocol.h"

// Thin NimBLE wrapper around the MC3000's single read/write/notify
// characteristic. Keeps all BLE plumbing in one place so main.cpp only deals
// with "send this command, get that reply".
class Mc3000BleLink {
public:
    enum class State {
        Idle,
        Scanning,
        Connecting,
        Discovering,
        Ready,
    };

    // Starts (or restarts) a scan for a device advertising one of the known
    // MC3000 local names. Safe to call repeatedly from loop() while idle.
    void beginScan();

    // Call frequently from loop(). Drives the scan -> connect -> discover
    // state machine and handles disconnects by re-arming the scan.
    void update();

    bool isReady() const { return state_ == State::Ready; }
    State state() const { return state_; }

    // Writes `packet` (must be Mc3000::PACKET_SIZE bytes) to the command
    // characteristic and blocks (busy-waiting, feeding the watchdog) until a
    // notification arrives or `timeoutMs` elapses. On success, copies the
    // raw reply into outResponse (capacity >= Mc3000::PACKET_SIZE) and
    // returns true.
    bool transact(const uint8_t* packet, uint8_t* outResponse, size_t outCapacity,
                   uint32_t timeoutMs = 2000);

    // Fire-and-forget write-without-response of exactly `len` bytes, with no
    // wait for a notification reply. Needed for CMD_SET_PROGRAM's first of
    // two packets, which the device does not ack on its own - only the
    // second packet (sent via transact()) gets a reply. Returns false if not
    // connected or the write itself fails.
    bool writeRaw(const uint8_t* data, size_t len);

    // Human-readable connection target, once known (set after a successful scan match).
    const char* targetName() const { return targetName_; }
    const NimBLEAddress* targetAddress() const { return hasTarget_ ? &targetAddress_ : nullptr; }

    // Called by the free-function NimBLE callback trampolines in
    // Mc3000BleLink.cpp. Not part of the intended public API, but needs
    // external linkage since NimBLE callbacks aren't class members.
    void onDisconnectPublic();
    void handleNotificationPublic(NimBLERemoteCharacteristic* chr, uint8_t* data, size_t length, bool isNotify);

private:
    static bool isKnownDeviceName(const std::string& name);

    State state_ = State::Idle;
    NimBLEClient* client_ = nullptr;
    NimBLERemoteCharacteristic* characteristic_ = nullptr;

    bool hasTarget_ = false;
    NimBLEAddress targetAddress_{};
    char targetName_[32] = {0};

    volatile bool responsePending_ = false;
    volatile bool responseReady_ = false;
    uint8_t responseBuffer_[mc3000::PACKET_SIZE] = {0};
    volatile size_t responseLength_ = 0;

    uint32_t lastScanAttemptMs_ = 0;
};
