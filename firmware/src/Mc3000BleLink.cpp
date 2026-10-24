#include "Mc3000BleLink.h"

#include <Arduino.h>
#include <cstring>

namespace {

constexpr const char* kKnownNames[] = {"Charger", "HitecCharger", "SimpleBLEPeripheral"};
constexpr const char* kServiceUuid = "ffe0";
constexpr const char* kCharacteristicUuid = "ffe1";
constexpr uint32_t kScanTimeMs = 5000;

class LinkClientCallbacks : public NimBLEClientCallbacks {
public:
    explicit LinkClientCallbacks(Mc3000BleLink* owner) : owner_(owner) {}

    void onDisconnect(NimBLEClient* /*client*/, int /*reason*/) override {
        if (owner_ != nullptr) {
            owner_->onDisconnectPublic();
        }
    }

private:
    Mc3000BleLink* owner_;
};

LinkClientCallbacks* g_clientCallbacks = nullptr;

} // namespace

bool Mc3000BleLink::isKnownDeviceName(const std::string& name) {
    for (const char* known : kKnownNames) {
        if (name == known) {
            return true;
        }
    }
    return false;
}

void Mc3000BleLink::beginScan() {
    if (state_ == State::Scanning || state_ == State::Connecting || state_ == State::Discovering) {
        return;
    }
    lastScanAttemptMs_ = millis();
    state_ = State::Scanning;

    NimBLEScan* scan = NimBLEDevice::getScan();
    scan->setActiveScan(true);
    scan->setInterval(45);
    scan->setWindow(15);

    Serial.println("[BLE] Scanning for MC3000 (names: Charger / HitecCharger / SimpleBLEPeripheral)...");
    NimBLEScanResults results = scan->getResults(kScanTimeMs, false);

    hasTarget_ = false;
    for (int i = 0; i < results.getCount(); i++) {
        const NimBLEAdvertisedDevice* dev = results.getDevice(i);
        if (dev->haveName() && isKnownDeviceName(dev->getName())) {
            targetAddress_ = dev->getAddress();
            std::strncpy(targetName_, dev->getName().c_str(), sizeof(targetName_) - 1);
            targetName_[sizeof(targetName_) - 1] = '\0';
            hasTarget_ = true;
            Serial.printf("[BLE] Found '%s' at %s\n", targetName_, targetAddress_.toString().c_str());
            break;
        }
    }
    scan->clearResults();

    if (!hasTarget_) {
        Serial.println("[BLE] No MC3000 found this round. Make sure it's powered on and not already");
        Serial.println("[BLE] connected to the official app (only one BLE link at a time).");
    }
    // Whether or not we found it, scanning itself is done; update() will act
    // on hasTarget_ next (either connect, or wait and scan again).
    state_ = State::Idle;
}

void Mc3000BleLink::update() {
    switch (state_) {
        case State::Idle: {
            // Retry scanning periodically.
            if (millis() - lastScanAttemptMs_ > 3000) {
                beginScan();
            }
            break;
        }
        case State::Scanning: {
            // beginScan() is currently blocking; by the time we get here it has
            // already resolved to Idle (not found) or progressed further below.
            break;
        }
        case State::Ready:
        case State::Connecting:
        case State::Discovering:
            break;
    }

    if (state_ == State::Idle && hasTarget_) {
        state_ = State::Connecting;

        if (g_clientCallbacks == nullptr) {
            g_clientCallbacks = new LinkClientCallbacks(this);
        }
        client_ = NimBLEDevice::createClient();
        client_->setClientCallbacks(g_clientCallbacks, false);

        Serial.printf("[BLE] Connecting to %s ...\n", targetAddress_.toString().c_str());
        if (!client_->connect(targetAddress_)) {
            Serial.println("[BLE] Connect failed, will retry.");
            NimBLEDevice::deleteClient(client_);
            client_ = nullptr;
            state_ = State::Idle;
            return;
        }

        state_ = State::Discovering;
        NimBLERemoteService* service = client_->getService(kServiceUuid);
        if (service == nullptr) {
            Serial.println("[BLE] Service ffe0 not found - unexpected device?");
            client_->disconnect();
            state_ = State::Idle;
            return;
        }
        characteristic_ = service->getCharacteristic(kCharacteristicUuid);
        if (characteristic_ == nullptr) {
            Serial.println("[BLE] Characteristic ffe1 not found - unexpected device?");
            client_->disconnect();
            state_ = State::Idle;
            return;
        }

        Serial.printf("[BLE] ffe1 properties: write=%d writeNoResp=%d notify=%d indicate=%d\n",
                       characteristic_->canWrite(), characteristic_->canWriteNoResponse(),
                       characteristic_->canNotify(), characteristic_->canIndicate());

        if (!characteristic_->subscribe(
                true, [this](NimBLERemoteCharacteristic* chr, uint8_t* data, size_t length, bool isNotify) {
                    this->handleNotificationPublic(chr, data, length, isNotify);
                })) {
            Serial.println("[BLE] Failed to subscribe to notifications.");
            client_->disconnect();
            state_ = State::Idle;
            return;
        }

        Serial.printf("[BLE] Connected and subscribed. MTU=%u. Ready.\n", client_->getMTU());
        state_ = State::Ready;
    }
}

void Mc3000BleLink::onDisconnectPublic() {
    Serial.println("[BLE] Disconnected.");
    characteristic_ = nullptr;
    if (client_ != nullptr) {
        NimBLEDevice::deleteClient(client_);
        client_ = nullptr;
    }
    responsePending_ = false;
    responseReady_ = false;
    state_ = State::Idle;
    lastScanAttemptMs_ = millis(); // let update() re-scan shortly
}

void Mc3000BleLink::handleNotificationPublic(NimBLERemoteCharacteristic* /*chr*/, uint8_t* data, size_t length,
                                              bool /*isNotify*/) {
    if (!responsePending_) {
        return; // unsolicited/late notification, ignore
    }
    size_t copyLen = length < sizeof(responseBuffer_) ? length : sizeof(responseBuffer_);
    std::memcpy(responseBuffer_, data, copyLen);
    responseLength_ = copyLen;
    responsePending_ = false;
    responseReady_ = true;
}

bool Mc3000BleLink::transact(const uint8_t* packet, uint8_t* outResponse, size_t outCapacity, uint32_t timeoutMs) {
    if (state_ != State::Ready || characteristic_ == nullptr) {
        return false;
    }

    responseReady_ = false;
    responsePending_ = true;

    // The ffe1 characteristic only advertises the Write-Without-Response
    // property (confirmed via canWrite()/canWriteNoResponse() logging) - a
    // plain Write Request is not valid here and silently never gets a reply.
    if (!characteristic_->writeValue(packet, mc3000::PACKET_SIZE, false)) {
        responsePending_ = false;
        Serial.println("[BLE] Write failed.");
        return false;
    }

    uint32_t start = millis();
    while (!responseReady_ && (millis() - start) < timeoutMs) {
        delay(5);
    }
    responsePending_ = false;

    if (!responseReady_) {
        Serial.println("[BLE] Timed out waiting for response.");
        return false;
    }

    size_t copyLen = responseLength_ < outCapacity ? responseLength_ : outCapacity;
    std::memcpy(outResponse, responseBuffer_, copyLen);
    return true;
}

bool Mc3000BleLink::writeRaw(const uint8_t* data, size_t len) {
    if (state_ != State::Ready || characteristic_ == nullptr) {
        return false;
    }
    return characteristic_->writeValue(data, len, false);
}
