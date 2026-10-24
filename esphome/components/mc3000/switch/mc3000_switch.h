#pragma once
#ifdef USE_ESP32

#include "esphome/components/switch/switch.h"
#include "../mc3000.h"

namespace esphome {
namespace mc3000 {

// Thin switch: all the BLE work happens on the Mc3000Hub (which is the
// BLEClientNode); this just forwards write_state() into hub->start/stop and
// receives publish_state() calls back from the hub's channel-data polling.
class Mc3000ChargeSwitch : public switch_::Switch, public Component {
 public:
  void set_hub(Mc3000Hub *hub) { this->hub_ = hub; }
  void set_channel(uint8_t channel_1based) { this->channel_ = channel_1based; }

 protected:
  void write_state(bool state) override {
    if (state) {
      this->hub_->start_charge(this->channel_);
    } else {
      this->hub_->stop_charge(this->channel_);
    }
    // Don't publish_state() here - wait for the next channel-data poll to
    // confirm the charger actually changed state (see Mc3000Hub::publish_channel_).
  }

  Mc3000Hub *hub_{nullptr};
  uint8_t channel_{1};
};

}  // namespace mc3000
}  // namespace esphome

#endif
