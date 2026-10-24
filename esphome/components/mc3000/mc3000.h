#pragma once
#ifdef USE_ESP32

#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/esp32_ble_tracker/esp32_ble_tracker.h"
#include "esphome/core/component.h"

#include "mc3000_protocol.h"

#include <esp_gattc_api.h>

#include <deque>
#include <string>

#ifdef USE_SENSOR
#include "esphome/components/sensor/sensor.h"
#endif
#ifdef USE_TEXT_SENSOR
#include "esphome/components/text_sensor/text_sensor.h"
#endif
#ifdef USE_SWITCH
#include "esphome/components/switch/switch.h"
#endif

namespace esphome {
namespace mc3000 {

namespace espbt = esphome::esp32_ble_tracker;

static const espbt::ESPBTUUID MC3000_SERVICE_UUID =
    espbt::ESPBTUUID::from_raw("0000ffe0-0000-1000-8000-00805f9b34fb");
static const espbt::ESPBTUUID MC3000_CHAR_UUID = espbt::ESPBTUUID::from_raw("0000ffe1-0000-1000-8000-00805f9b34fb");

// Hub component: owns the single BLE connection to the charger, polls it,
// decodes replies, and fans the results out to whichever sensor/switch/
// text_sensor entities were configured against it. See
// firmware/src/main.cpp for the standalone (non-ESPHome) prototype this was
// ported from, and docs/PROTOCOL.md for the wire format.
class Mc3000Hub : public esphome::ble_client::BLEClientNode, public PollingComponent {
 public:
  void setup() override {}
  void update() override;
  // loop() only runs while a program-apply job is queued (see apply_program());
  // otherwise the hub is purely polling- and GATT-callback-driven.
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::BLUETOOTH; }

  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;

  bool is_connected() { return this->node_state == espbt::ClientState::ESTABLISHED; }

  // Actions - channel is 1-4 (matching the serial firmware's convention,
  // converted to a 0-3 index / bitmask internally).
  void start_charge(uint8_t channel_1based);
  void stop_charge(uint8_t channel_1based);
  void set_program(uint8_t channel_1based, const ::mc3000_protocol::ChargeProgram &program);

  // Sequential, verified program apply. Jobs run one channel at a time:
  // stop -> send program -> read back battery type/mode -> start -> wait until
  // the channel reports Charging/Discharging/Paused, retrying the program and
  // the start up to max_attempts times within start_timeout each. Firing all
  // four channels' commands back to back (Write Without Response) made the
  // charger drop some of them.
  // fast = true (used by the IR test): stop -> program -> start in ~1 s per
  // channel without waiting for read-back, so the next channel starts within
  // ~1.5 s; whether each channel really runs is checked afterwards for all of
  // them together, with the same timeout/retries.
  void apply_program(uint8_t channel_1based, const ::mc3000_protocol::ChargeProgram &program, bool start = true,
                     bool fast = false);
  // Stop a channel through the same queue (so it can't collide with an apply).
  void queue_stop(uint8_t channel_1based);
  // Ask for one channel's data right away instead of waiting for the poll.
  void request_channel(uint8_t channel_1based) {
    if (channel_1based >= 1 && channel_1based <= ::mc3000_protocol::CHANNEL_COUNT && this->is_connected())
      this->request_channel_(idx_(channel_1based));
  }
  // Discharge current the hub actually sent for a channel (after the 0.05-2 A
  // range and the 15 W charger-wide discharge budget), 0 if none yet.
  uint16_t applied_discharge_ma(uint8_t channel_1based) const {
    return (channel_1based >= 1 && channel_1based <= ::mc3000_protocol::CHANNEL_COUNT)
               ? this->applied_discharge_ma_[channel_1based - 1]
               : 0;
  }
  bool apply_busy() const { return this->job_active_ || !this->jobs_.empty() || this->verify_pending_(); }
  const std::string &apply_result() const { return this->apply_result_; }
  void set_start_timeout(uint32_t ms) { this->start_timeout_ms_ = ms; }
  void set_max_attempts(uint8_t n) { this->max_attempts_ = n == 0 ? 1 : n; }

  // Read-only accessors for YAML template sensors (lambdas). channel is 1-4.
  // Return false / empty when no reply has been decoded yet (or after a
  // disconnect), so templates can publish "unknown" instead of stale zeros.
  bool has_channel_data(uint8_t channel_1based) const {
    return channel_1based >= 1 && channel_1based <= ::mc3000_protocol::CHANNEL_COUNT &&
           this->has_channel_[idx_(channel_1based)];
  }
  const ::mc3000_protocol::ChannelData &channel_data(uint8_t channel_1based) const {
    return this->channel_data_[idx_(channel_1based)];
  }
  bool has_basic_data() const { return this->has_basic_; }
  const ::mc3000_protocol::BasicData &basic_data() const { return this->basic_data_; }
  bool has_version_info() const { return this->has_version_; }
  const ::mc3000_protocol::VersionInfo &version_info() const { return this->version_; }

#ifdef USE_SENSOR
  void set_voltage_sensor(uint8_t channel_1based, sensor::Sensor *s) { this->voltage_sensor_[idx_(channel_1based)] = s; }
  void set_current_sensor(uint8_t channel_1based, sensor::Sensor *s) { this->current_sensor_[idx_(channel_1based)] = s; }
  void set_capacity_sensor(uint8_t channel_1based, sensor::Sensor *s) { this->capacity_sensor_[idx_(channel_1based)] = s; }
  void set_temperature_sensor(uint8_t channel_1based, sensor::Sensor *s) {
    this->temperature_sensor_[idx_(channel_1based)] = s;
  }
  void set_resistance_sensor(uint8_t channel_1based, sensor::Sensor *s) {
    this->resistance_sensor_[idx_(channel_1based)] = s;
  }
#endif
#ifdef USE_TEXT_SENSOR
  void set_status_text_sensor(uint8_t channel_1based, text_sensor::TextSensor *s) {
    this->status_text_sensor_[idx_(channel_1based)] = s;
  }
#endif
#ifdef USE_SWITCH
  void set_charge_switch(uint8_t channel_1based, switch_::Switch *s) { this->charge_switch_[idx_(channel_1based)] = s; }
#endif

 protected:
  static uint8_t idx_(uint8_t channel_1based) { return channel_1based - 1; }

  bool discover_characteristic_();
  void write_packet_(const uint8_t *data, size_t len, bool gap = true);
  void write_notify_config_descriptor_(bool enable);
  void request_next_();
  void handle_reply_(const uint8_t *data, size_t len);
  void publish_channel_(uint8_t idx);

  uint16_t char_handle_{0};
  uint32_t last_write_ms_{0};
  uint16_t applied_discharge_ma_[::mc3000_protocol::CHANNEL_COUNT] = {0, 0, 0, 0};
  void limit_discharge_(uint8_t idx, ::mc3000_protocol::ChargeProgram &program, char *note, size_t note_len);
  uint16_t config_descr_{0};

  ::mc3000_protocol::ChannelData channel_data_[::mc3000_protocol::CHANNEL_COUNT];
  bool has_channel_[::mc3000_protocol::CHANNEL_COUNT] = {false, false, false, false};
  ::mc3000_protocol::BasicData basic_data_;
  bool has_basic_{false};
  ::mc3000_protocol::VersionInfo version_;
  bool has_version_{false};

  // Simple round-robin over {version (once), basic, ch0, ch1, ch2, ch3} so
  // each update() tick sends exactly one request - replies are matched by
  // command (+channel) byte in handle_reply_(), not by request bookkeeping.
  uint8_t poll_step_{0};
  uint32_t channel_rx_ms_[::mc3000_protocol::CHANNEL_COUNT] = {0, 0, 0, 0};  // millis() of last decoded channel reply

  // ---- program-apply queue ----
  enum class ApplyStep : uint8_t { STOP, PROGRAM, VERIFY_PROGRAM, START, VERIFY_START };
  struct ApplyJob {
    uint8_t ch{1};
    ::mc3000_protocol::ChargeProgram program;
    bool start{true};
    bool stop_only{false};
    bool fast{false};
    uint8_t tries{1};  // fast jobs: how many times this program has been sent
  };
  // Deferred "did it start?" check for fast jobs, one slot per channel.
  struct StartCheck {
    bool active{false};
    uint32_t sent_ms{0};   // last start command
    uint32_t first_ms{0};  // first start command (for the reported time)
    uint32_t req_ms{0};    // last channel-data request for this check
    uint8_t attempts{0};
    ::mc3000_protocol::ChannelMode mode{::mc3000_protocol::ChannelMode::Charge};  // program sent
    ::mc3000_protocol::ChargeProgram program;  // to re-queue on a mismatch
    uint8_t tries{1};
  };
  StartCheck checks_[::mc3000_protocol::CHANNEL_COUNT];
  uint8_t check_rr_{0};
  uint32_t check_req_ms_{0};
  bool verify_pending_() const {
    for (const auto &c : this->checks_)
      if (c.active)
        return true;
    return false;
  }
  void process_checks_();
  void process_apply_();
  void request_channel_(uint8_t idx);
  void finish_job_(bool ok, const std::string &msg);
  void set_step_(ApplyStep step);

  std::deque<ApplyJob> jobs_;
  ApplyJob job_;
  bool job_active_{false};
  ApplyStep step_{ApplyStep::STOP};
  uint32_t step_ms_{0};        // when the current step began / its command was sent
  uint32_t job_ms_{0};         // when the current job began
  uint32_t last_req_ms_{0};    // last channel-data request we sent for the job
  uint32_t idle_since_ms_{0};  // end of the previous job (gap before the next)
  uint8_t prog_attempt_{0};
  uint8_t start_attempt_{0};
  bool program_ok_{false};
  uint32_t start_timeout_ms_{15000};
  uint8_t max_attempts_{3};
  std::string apply_result_;
  char limit_note_[64] = {0};

#ifdef USE_SENSOR
  sensor::Sensor *voltage_sensor_[::mc3000_protocol::CHANNEL_COUNT] = {};
  sensor::Sensor *current_sensor_[::mc3000_protocol::CHANNEL_COUNT] = {};
  sensor::Sensor *capacity_sensor_[::mc3000_protocol::CHANNEL_COUNT] = {};
  sensor::Sensor *temperature_sensor_[::mc3000_protocol::CHANNEL_COUNT] = {};
  sensor::Sensor *resistance_sensor_[::mc3000_protocol::CHANNEL_COUNT] = {};
#endif
#ifdef USE_TEXT_SENSOR
  text_sensor::TextSensor *status_text_sensor_[::mc3000_protocol::CHANNEL_COUNT] = {};
#endif
#ifdef USE_SWITCH
  switch_::Switch *charge_switch_[::mc3000_protocol::CHANNEL_COUNT] = {};
#endif
};

}  // namespace mc3000
}  // namespace esphome

#endif
