#ifdef USE_ESP32

#include "mc3000.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <cstdio>

namespace esphome {
namespace mc3000 {

static const char *const TAG = "mc3000";

bool Mc3000Hub::discover_characteristic_() {
  auto *chr = this->parent_->get_characteristic(MC3000_SERVICE_UUID, MC3000_CHAR_UUID);
  if (chr == nullptr) {
    ESP_LOGW(TAG, "No ffe0/ffe1 service/characteristic found - not an MC3000?");
    return false;
  }
  this->char_handle_ = chr->handle;

  auto *descr = this->parent_->get_config_descriptor(this->char_handle_);
  if (descr == nullptr) {
    ESP_LOGW(TAG, "No config descriptor for handle 0x%x - won't be able to receive notifications", this->char_handle_);
    return false;
  }
  this->config_descr_ = descr->handle;
  return true;
}

void Mc3000Hub::write_packet_(const uint8_t *data, size_t len, bool gap) {
  if (!this->is_connected() || this->char_handle_ == 0)
    return;
  // The charger drops packets that arrive back to back (Write Without
  // Response, no flow control), so keep a minimum gap between ALL writes -
  // queue commands, start-check reads and the IR test's reads alike.
  static const uint32_t MIN_GAP_MS = 150;
  const uint32_t since = millis() - this->last_write_ms_;
  if (gap && this->last_write_ms_ != 0 && since < MIN_GAP_MS)
    delay(MIN_GAP_MS - since);
  this->last_write_ms_ = millis();
  // Confirmed against real hardware: the ffe1 characteristic only supports
  // Write Without Response - a Write Request silently never gets a reply.
  esp_ble_gattc_write_char(this->parent_->get_gattc_if(), this->parent_->get_conn_id(), this->char_handle_, len,
                           const_cast<uint8_t *>(data), ESP_GATT_WRITE_TYPE_NO_RSP, ESP_GATT_AUTH_REQ_NONE);
}

void Mc3000Hub::write_notify_config_descriptor_(bool enable) {
  if (this->config_descr_ == 0)
    return;
  uint16_t notify_en = enable ? 1 : 0;
  esp_ble_gattc_write_char_descr(this->parent_->get_gattc_if(), this->parent_->get_conn_id(), this->config_descr_,
                                 sizeof(notify_en), (uint8_t *) &notify_en, ESP_GATT_WRITE_TYPE_RSP,
                                 ESP_GATT_AUTH_REQ_NONE);
}

void Mc3000Hub::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                    esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_DISCONNECT_EVT: {
      this->has_basic_ = false;
      for (uint8_t i = 0; i < ::mc3000_protocol::CHANNEL_COUNT; i++) this->has_channel_[i] = false;
      this->status_set_warning();
      break;
    }
    case ESP_GATTC_SEARCH_CMPL_EVT: {
      if (this->discover_characteristic_()) {
        ESP_LOGI(TAG, "Discovered ffe1 characteristic (handle 0x%x), subscribing...", this->char_handle_);
        this->node_state = espbt::ClientState::ESTABLISHED;
        esp_ble_gattc_register_for_notify(this->parent_->get_gattc_if(), this->parent_->get_remote_bda(),
                                          this->char_handle_);
        this->status_clear_warning();
      } else {
        this->parent()->set_enabled(false);
        this->status_set_warning();
      }
      break;
    }
    case ESP_GATTC_REG_FOR_NOTIFY_EVT: {
      if (param->reg_for_notify.handle != this->char_handle_)
        break;
      // Same reasoning as the bedjet component: we already set node_state to
      // ESTABLISHED above (before this event arrives), which means the
      // parent BLEClient will have purged the descriptor table by the time
      // it would normally auto-write the CCCD - so we do it ourselves.
      this->write_notify_config_descriptor_(true);
      break;
    }
    case ESP_GATTC_NOTIFY_EVT: {
      if (param->notify.handle != this->char_handle_)
        break;
      this->handle_reply_(param->notify.value, param->notify.value_len);
      break;
    }
    default:
      break;
  }
}

void Mc3000Hub::handle_reply_(const uint8_t *data, size_t len) {
  if (len < ::mc3000_protocol::PACKET_SIZE)
    return;

  uint8_t command = data[1];

  if (command == ::mc3000_protocol::CMD_GET_CHANNEL_DATA) {
    if (!::mc3000_protocol::checksumValid(data, len))
      return;
    ::mc3000_protocol::ChannelData channel;
    if (!::mc3000_protocol::decodeChannelData(data, len, channel) || channel.channel >= ::mc3000_protocol::CHANNEL_COUNT)
      return;
    this->channel_data_[channel.channel] = channel;
    this->has_channel_[channel.channel] = true;
    this->channel_rx_ms_[channel.channel] = millis();
    this->publish_channel_(channel.channel);
  } else if (command == ::mc3000_protocol::CMD_GET_BASIC_DATA) {
    if (!::mc3000_protocol::checksumValid(data, len))
      return;
    if (::mc3000_protocol::decodeBasicData(data, len, this->basic_data_))
      this->has_basic_ = true;
  } else if (command == ::mc3000_protocol::CMD_GET_VERSION_INFO) {
    // Known firmware bug: version replies carry an invalid checksum - decode unconditionally.
    if (::mc3000_protocol::decodeVersionInfo(data, len, this->version_))
      this->has_version_ = true;
  }
  // CMD_START_CHARGE / CMD_STOP_CHARGE / CMD_SET_PROGRAM acks: no extra
  // action needed here - the next channel-data poll will reflect the new
  // status/program via the normal publish_channel_() path above.
}

void Mc3000Hub::publish_channel_(uint8_t idx) {
  if (idx >= ::mc3000_protocol::CHANNEL_COUNT || !this->has_channel_[idx])
    return;
  const auto &c = this->channel_data_[idx];

#ifdef USE_SENSOR
  if (this->voltage_sensor_[idx] != nullptr)
    this->voltage_sensor_[idx]->publish_state(c.millivolts / 1000.0f);
  if (this->current_sensor_[idx] != nullptr) {
    // The charger reports the magnitude only; sign it so discharge current is
    // negative (into the cell = +, out of the cell = -), same as a shunt would.
    float amps = c.milliamps / 1000.0f;
    if (c.status == ::mc3000_protocol::ChannelStatus::Discharge)
      amps = -amps;
    this->current_sensor_[idx]->publish_state(amps);
  }
  if (this->capacity_sensor_[idx] != nullptr)
    this->capacity_sensor_[idx]->publish_state(c.capacityMah);
  if (this->temperature_sensor_[idx] != nullptr)
    this->temperature_sensor_[idx]->publish_state(c.temperatureC);
  if (this->resistance_sensor_[idx] != nullptr)
    this->resistance_sensor_[idx]->publish_state(c.resistanceMilliohm);
#endif
#ifdef USE_TEXT_SENSOR
  if (this->status_text_sensor_[idx] != nullptr)
    this->status_text_sensor_[idx]->publish_state(::mc3000_protocol::channelStatusName(c.status));
#endif
#ifdef USE_SWITCH
  if (this->charge_switch_[idx] != nullptr) {
    bool working = c.status == ::mc3000_protocol::ChannelStatus::Charge || c.status == ::mc3000_protocol::ChannelStatus::Discharge ||
                   c.status == ::mc3000_protocol::ChannelStatus::Pause;
    this->charge_switch_[idx]->publish_state(working);
  }
#endif
}

void Mc3000Hub::request_next_() {
  uint8_t packet[::mc3000_protocol::PACKET_SIZE];

  // Step 0: version (only until we have it). Step 1: basic data. Steps 2-5:
  // channel data for channel 0-3. One request per update() tick is plenty -
  // replies are self-identifying (command [+channel] byte), so there's no
  // need to track which request is "in flight".
  if (this->poll_step_ == 0 && !this->has_version_) {
    ::mc3000_protocol::buildPacket(::mc3000_protocol::CMD_GET_VERSION_INFO, nullptr, 0, packet, sizeof(packet));
    this->write_packet_(packet, sizeof(packet));
  } else if (this->poll_step_ == 1) {
    ::mc3000_protocol::buildPacket(::mc3000_protocol::CMD_GET_BASIC_DATA, nullptr, 0, packet, sizeof(packet));
    this->write_packet_(packet, sizeof(packet));
  } else {
    uint8_t ch = this->poll_step_ - 2;
    if (ch < ::mc3000_protocol::CHANNEL_COUNT) {
      uint8_t payload[1] = {ch};
      ::mc3000_protocol::buildPacket(::mc3000_protocol::CMD_GET_CHANNEL_DATA, payload, 1, packet, sizeof(packet));
      this->write_packet_(packet, sizeof(packet));
    }
  }

  this->poll_step_ = (this->poll_step_ + 1) % 6;
}

void Mc3000Hub::update() {
  if (!this->is_connected()) {
    ESP_LOGV(TAG, "Not connected, skipping poll");
    return;
  }
  // Keep the link quiet while a program is being applied; the apply job
  // polls its own channel.
  if (this->apply_busy())
    return;
  this->request_next_();
}

void Mc3000Hub::request_channel_(uint8_t idx) {
  uint8_t packet[::mc3000_protocol::PACKET_SIZE];
  uint8_t payload[1] = {idx};
  ::mc3000_protocol::buildPacket(::mc3000_protocol::CMD_GET_CHANNEL_DATA, payload, 1, packet, sizeof(packet));
  this->write_packet_(packet, sizeof(packet));
  this->last_req_ms_ = millis();
}

void Mc3000Hub::apply_program(uint8_t channel_1based, const ::mc3000_protocol::ChargeProgram &program, bool start,
                              bool fast) {
  if (channel_1based < 1 || channel_1based > ::mc3000_protocol::CHANNEL_COUNT)
    return;
  // A newer request for the same channel replaces a queued one.
  for (auto it = this->jobs_.begin(); it != this->jobs_.end();) {
    if (it->ch == channel_1based && !it->stop_only)
      it = this->jobs_.erase(it);
    else
      ++it;
  }
  ApplyJob job;
  job.ch = channel_1based;
  job.program = program;
  job.start = start;
  job.fast = fast;
  this->checks_[channel_1based - 1].active = false;  // a new apply supersedes a pending check
  this->jobs_.push_back(job);
  ESP_LOGI(TAG, "Ch%u: program queued (%u in queue)", channel_1based, (unsigned) this->jobs_.size());
  this->enable_loop();
}

void Mc3000Hub::queue_stop(uint8_t channel_1based) {
  if (channel_1based < 1 || channel_1based > ::mc3000_protocol::CHANNEL_COUNT)
    return;
  ApplyJob job;
  job.ch = channel_1based;
  job.start = false;
  job.stop_only = true;
  this->checks_[channel_1based - 1].active = false;  // stopping on purpose: don't retry the start
  this->jobs_.push_back(job);
  this->enable_loop();
}

static bool may_discharge(::mc3000_protocol::ChannelMode m) {
  using ::mc3000_protocol::ChannelMode;
  return m == ChannelMode::Discharge || m == ChannelMode::Cycle || m == ChannelMode::Refresh ||
         m == ChannelMode::StorageOrBreakIn;
}

// Discharge limits: 0.05-2.00 A per slot, and at most 15 W of discharge power
// for the whole charger, shared by every slot that is (or is about to be)
// discharging. Power is estimated at the charge cutoff voltage (worst case:
// a full cell at the start of the discharge).
void Mc3000Hub::limit_discharge_(uint8_t idx, ::mc3000_protocol::ChargeProgram &program, char *note,
                                 size_t note_len) {
  using ::mc3000_protocol::ChannelStatus;
  static const uint32_t MIN_MA = 50, MAX_MA = 2000, BUDGET_MW = 15000;
  note[0] = 0;
  uint32_t ma = program.dischargeCurrentMa;
  if (ma < MIN_MA) ma = MIN_MA;
  if (ma > MAX_MA) ma = MAX_MA;
  if (may_discharge(program.mode)) {
    uint8_t n = 1;  // this channel
    for (uint8_t i = 0; i < ::mc3000_protocol::CHANNEL_COUNT; i++) {
      if (i == idx)
        continue;
      bool counts = false;
      const auto &c = this->channel_data_[i];
      const bool running = this->has_channel_[i] && (c.status == ChannelStatus::Charge ||
                                                     c.status == ChannelStatus::Discharge ||
                                                     c.status == ChannelStatus::Pause);
      if (running && may_discharge(c.mode))
        counts = true;
      if (this->checks_[i].active)
        counts = true;
      for (const auto &j : this->jobs_)
        if (j.ch == i + 1 && !j.stop_only && j.start && may_discharge(j.program.mode))
          counts = true;
      if (counts)
        n++;
    }
    const uint32_t mv = program.chargeCutoffMillivolts > 2000 ? program.chargeCutoffMillivolts : 4200;
    const uint32_t cap = (BUDGET_MW * 1000UL) / (n * mv);  // mA
    if (ma > cap) {
      ma = cap < MIN_MA ? MIN_MA : cap;
      snprintf(note, note_len, ", ontladen begrensd op %.2f A (15 W / %u sleuven)", ma / 1000.0f, n);
    }
  } else if (ma != program.dischargeCurrentMa) {
    snprintf(note, note_len, ", ontlaadstroom op %.2f A gezet", ma / 1000.0f);
  }
  if (ma != program.dischargeCurrentMa)
    ESP_LOGI(TAG, "Ch%u: discharge current %u -> %u mA", idx + 1, program.dischargeCurrentMa, (unsigned) ma);
  program.dischargeCurrentMa = (uint16_t) ma;
  this->applied_discharge_ma_[idx] = may_discharge(program.mode) ? (uint16_t) ma : 0;
}

void Mc3000Hub::set_step_(ApplyStep step) {
  this->step_ = step;
  this->step_ms_ = millis();
}

void Mc3000Hub::finish_job_(bool ok, const std::string &msg) {
  char buf[160];
  snprintf(buf, sizeof(buf), "Ch%u: %s", this->job_.ch, msg.c_str());
  if (!this->job_.stop_only || !ok)  // a successful plain stop doesn't overwrite the last apply outcome
    this->apply_result_ = buf;
  if (ok)
    ESP_LOGI(TAG, "%s", buf);
  else
    ESP_LOGW(TAG, "%s", buf);
  this->job_active_ = false;
  this->idle_since_ms_ = millis();
}

void Mc3000Hub::loop() {
  if (!this->apply_busy()) {
    this->disable_loop();
    return;
  }
  // Queued jobs first (they keep the start-to-start gap short); deferred start
  // checks run once the queue is empty, so their reads don't slow it down.
  if (this->job_active_ || !this->jobs_.empty())
    this->process_apply_();
  else
    this->process_checks_();
}

void Mc3000Hub::process_checks_() {
  using ::mc3000_protocol::ChannelStatus;
  static const uint32_t REQ_GAP_MS = 700;  // between reads (any channel)
  const uint32_t now = millis();
  if (!this->is_connected())
    return;

  // Evaluate every pending channel that has a reply newer than its last read.
  for (uint8_t i = 0; i < ::mc3000_protocol::CHANNEL_COUNT; i++) {
    auto &k = this->checks_[i];
    if (!k.active)
      continue;
    const auto &c = this->channel_data_[i];
    const bool fresh = k.req_ms != 0 && this->channel_rx_ms_[i] > k.req_ms && this->channel_rx_ms_[i] > k.sent_ms;
    char buf[96];
    if (fresh && (c.status == ChannelStatus::Charge || c.status == ChannelStatus::Discharge ||
                  c.status == ChannelStatus::Pause) && c.mode != k.mode) {
      // Running its previous program instead of ours: stop it, and send the
      // program again through the queue (it then sees the channel running and
      // waits for the stop to settle) until max_attempts.
      this->stop_charge(i + 1);
      k.active = false;
      if (k.tries < this->max_attempts_) {
        ESP_LOGW(TAG, "Ch%u: charger kept its previous program, retry %u", i + 1, k.tries + 1);
        ApplyJob job;
        job.ch = i + 1;
        job.program = k.program;
        job.start = true;
        job.fast = true;
        job.tries = k.tries + 1;
        this->jobs_.push_back(job);
        this->enable_loop();
      } else {
        snprintf(buf, sizeof(buf), "Ch%u: NIET toegepast na %u pogingen - kanaal gestopt", i + 1, k.tries);
        this->apply_result_ = buf;
        ESP_LOGW(TAG, "%s", buf);
      }
    } else if (fresh && (c.status == ChannelStatus::Charge || c.status == ChannelStatus::Discharge ||
                         c.status == ChannelStatus::Pause)) {
      snprintf(buf, sizeof(buf), "Ch%u: gestart in %.1f s (poging %u)", i + 1, (now - k.first_ms) / 1000.0f, k.attempts);
      this->apply_result_ = buf;
      ESP_LOGI(TAG, "%s", buf);
      k.active = false;
    } else if (fresh && (uint8_t) c.status >= 128) {
      snprintf(buf, sizeof(buf), "Ch%u: NIET gestart - %s", i + 1, ::mc3000_protocol::channelStatusName(c.status));
      this->apply_result_ = buf;
      ESP_LOGW(TAG, "%s", buf);
      k.active = false;
    } else if (now - k.sent_ms >= this->start_timeout_ms_) {
      if (k.attempts < this->max_attempts_) {
        k.attempts++;
        ESP_LOGD(TAG, "Ch%u: not running yet, start again (attempt %u)", i + 1, k.attempts);
        this->start_charge(i + 1);
        k.sent_ms = now;
        k.req_ms = 0;
        this->check_req_ms_ = now;  // keep the link quiet briefly after a command
        return;
      }
      snprintf(buf, sizeof(buf), "Ch%u: NIET gestart na %u pogingen (status %s)", i + 1, k.attempts,
               ::mc3000_protocol::channelStatusName(c.status));
      this->apply_result_ = buf;
      ESP_LOGW(TAG, "%s", buf);
      k.active = false;
    }
  }

  // One read at a time, round-robin over the channels still pending.
  if (now - this->check_req_ms_ < REQ_GAP_MS)
    return;
  for (uint8_t n = 0; n < ::mc3000_protocol::CHANNEL_COUNT; n++) {
    uint8_t i = (this->check_rr_ + n) % ::mc3000_protocol::CHANNEL_COUNT;
    auto &k = this->checks_[i];
    if (!k.active || now - k.sent_ms < 800)
      continue;
    this->request_channel_(i);
    k.req_ms = millis();
    this->check_req_ms_ = k.req_ms;
    this->check_rr_ = (i + 1) % ::mc3000_protocol::CHANNEL_COUNT;
    return;
  }
}

void Mc3000Hub::process_apply_() {
  using ::mc3000_protocol::ChannelStatus;
  static const uint32_t GAP_MS = 750;         // between channels
  static const uint32_t STOP_SETTLE_MS = 1500;
  static const uint32_t FAST_GAP_MS = 250;     // fast jobs: between channels
  static const uint32_t FAST_PROGRAM_MS = 800; //   program -> start
  static const uint32_t FIRST_READ_MS = 800;  // give the charger time to act before reading back
  static const uint32_t REQ_EVERY_MS = 1500;
  // The charger reports the *previous* type/mode until the channel starts, so a
  // changed program usually can't be confirmed before the start: try briefly,
  // don't resend, and let the started-status check decide.
  static const uint32_t PROGRAM_VERIFY_MS = 2500;
  static const uint32_t NO_LINK_FAIL_MS = 60000;
  const uint32_t now = millis();

  if (!this->job_active_) {
    if (this->jobs_.empty())
      return;
    if (now - this->idle_since_ms_ < (this->jobs_.front().fast ? FAST_GAP_MS : GAP_MS))
      return;
    this->job_ = this->jobs_.front();
    this->jobs_.pop_front();
    this->job_active_ = true;
    this->job_ms_ = now;
    this->prog_attempt_ = 0;
    this->start_attempt_ = 0;
    this->program_ok_ = false;
    this->last_req_ms_ = 0;
    this->set_step_(ApplyStep::STOP);
    this->step_ms_ = 0;  // send the stop immediately once connected
    if (!this->job_.stop_only) {
      char buf[48];
      snprintf(buf, sizeof(buf), "Ch%u: bezig...", this->job_.ch);
      this->apply_result_ = buf;
    }
  }

  const uint8_t ch = this->job_.ch;
  const uint8_t idx = idx_(ch);

  if (!this->is_connected()) {
    if (now - this->job_ms_ > NO_LINK_FAIL_MS) {
      this->finish_job_(false, "NIET toegepast - geen BLE-verbinding");
    }
    return;
  }

  switch (this->step_) {
    case ApplyStep::STOP:
      if (this->step_ms_ == 0) {
        const auto &cd = this->channel_data_[idx];
        const bool idle_known = !this->job_.stop_only && this->has_channel_[idx] &&
                                now - this->channel_rx_ms_[idx] < 15000 &&
                                cd.status != ::mc3000_protocol::ChannelStatus::Charge &&
                                cd.status != ::mc3000_protocol::ChannelStatus::Discharge &&
                                cd.status != ::mc3000_protocol::ChannelStatus::Pause;
        if (idle_known) {
          // Nothing to stop. Skipping it also avoids sending the program right
          // after a stop, which the charger then sometimes ignores.
          this->set_step_(ApplyStep::PROGRAM);
          break;
        }
        this->stop_charge(ch);  // a running charge never continues on the old program
        this->set_step_(ApplyStep::STOP);
      } else if (this->job_.stop_only) {
        // Verify the stop like a start: read back, resend if still running.
        const auto &c = this->channel_data_[idx];
        const bool fresh = this->last_req_ms_ != 0 && this->channel_rx_ms_[idx] > this->last_req_ms_;
        const bool running = c.status == ::mc3000_protocol::ChannelStatus::Charge ||
                             c.status == ::mc3000_protocol::ChannelStatus::Discharge ||
                             c.status == ::mc3000_protocol::ChannelStatus::Pause;
        if (fresh && !running) {
          this->finish_job_(true, "gestopt");
        } else if (now - this->step_ms_ >= 3000) {
          if (this->start_attempt_ + 1 >= this->max_attempts_) {
            this->finish_job_(false, "NIET gestopt!");
          } else {
            this->start_attempt_++;  // reused as stop-attempt counter
            ESP_LOGW(TAG, "Ch%u: still running after stop, sending stop again", ch);
            this->stop_charge(ch);
            this->set_step_(ApplyStep::STOP);
            this->last_req_ms_ = 0;
          }
        } else if (now - this->step_ms_ >= 600 && now - this->last_req_ms_ >= 1000) {
          this->request_channel_(idx);
        }
      } else if (now - this->step_ms_ >= STOP_SETTLE_MS) {
        this->set_step_(ApplyStep::PROGRAM);
      }
      break;

    case ApplyStep::PROGRAM:
      this->prog_attempt_++;
      if (this->prog_attempt_ == 1)
        this->limit_discharge_(idx, this->job_.program, this->limit_note_, sizeof(this->limit_note_));
      ESP_LOGD(TAG, "Ch%u: sending program (attempt %u)", ch, this->prog_attempt_);
      this->set_program(ch, this->job_.program);
      this->last_req_ms_ = 0;
      this->set_step_(ApplyStep::VERIFY_PROGRAM);
      break;

    case ApplyStep::VERIFY_PROGRAM: {
      const uint32_t age = now - this->step_ms_;
      if (this->job_.fast) {
        // No read-back: start shortly after the program, check later.
        if (age >= FAST_PROGRAM_MS) {
          this->start_charge(ch);
          auto &k = this->checks_[idx];
          k.active = true;
          k.sent_ms = k.first_ms = now;
          k.req_ms = 0;
          k.attempts = 1;
          k.mode = this->job_.program.mode;
          k.program = this->job_.program;
          k.tries = this->job_.tries;
          ESP_LOGD(TAG, "Ch%u: started (fast), check deferred", ch);
          this->job_active_ = false;
          this->idle_since_ms_ = now;
        }
        break;
      }
      const auto &c = this->channel_data_[idx];
      const bool fresh = this->channel_rx_ms_[idx] > this->step_ms_ + FIRST_READ_MS / 2 &&
                         this->last_req_ms_ != 0;
      if (fresh && c.batteryType == this->job_.program.batteryType && c.mode == this->job_.program.mode) {
        this->program_ok_ = true;
      } else if (age >= PROGRAM_VERIFY_MS) {
        // Not confirmed before the start (normal for a changed program); the
        // started-status check below decides.
        ESP_LOGD(TAG, "Ch%u: program not confirmed by read-back before start", ch);
      } else {
        if (age >= FIRST_READ_MS && now - this->last_req_ms_ >= REQ_EVERY_MS)
          this->request_channel_(idx);
        break;
      }
      if (!this->job_.start) {
        this->finish_job_(this->program_ok_, this->program_ok_ ? "programma ingesteld" : "programma NIET bevestigd");
        break;
      }
      this->set_step_(ApplyStep::START);
      break;
    }

    case ApplyStep::START:
      this->start_attempt_++;
      ESP_LOGD(TAG, "Ch%u: start (attempt %u)", ch, this->start_attempt_);
      this->start_charge(ch);
      this->last_req_ms_ = 0;
      this->set_step_(ApplyStep::VERIFY_START);
      break;

    case ApplyStep::VERIFY_START: {
      const uint32_t age = now - this->step_ms_;
      const auto &c = this->channel_data_[idx];
      const bool fresh = this->channel_rx_ms_[idx] > this->step_ms_ + FIRST_READ_MS / 2 &&
                         this->last_req_ms_ != 0;
      if (fresh) {
        if (c.status == ChannelStatus::Charge || c.status == ChannelStatus::Discharge ||
            c.status == ChannelStatus::Pause) {
          char msg[150];
          const bool prog_ok = c.batteryType == this->job_.program.batteryType && c.mode == this->job_.program.mode;
          if (!prog_ok) {
            // Running, but the charger kept its previous program: never leave
            // that running. Stop, then send the program again.
            ESP_LOGW(TAG, "Ch%u: running mode %u, expected %u - resending program", ch, (unsigned) c.mode,
                     (unsigned) this->job_.program.mode);
            if (this->prog_attempt_ < this->max_attempts_) {
              this->start_attempt_ = 0;
              this->set_step_(ApplyStep::STOP);
              this->step_ms_ = 0;  // sends the stop right away
              break;
            }
            this->stop_charge(ch);
            this->finish_job_(false, "NIET toegepast - lader houdt ander programma, kanaal gestopt");
            break;
          }
          snprintf(msg, sizeof(msg), "gestart in %.1f s (poging %u)%s", (now - this->job_ms_) / 1000.0f,
                   this->start_attempt_, this->limit_note_);
          this->finish_job_(true, msg);
          break;
        }
        if ((uint8_t) c.status >= 128) {
          this->finish_job_(false, std::string("NIET gestart - ") + ::mc3000_protocol::channelStatusName(c.status));
          break;
        }
      }
      if (age >= this->start_timeout_ms_) {
        if (this->start_attempt_ < this->max_attempts_) {
          this->set_step_(ApplyStep::START);
        } else {
          char msg[80];
          snprintf(msg, sizeof(msg), "NIET gestart na %u pogingen (status %s)", this->start_attempt_,
                   ::mc3000_protocol::channelStatusName(c.status));
          this->finish_job_(false, msg);
        }
        break;
      }
      if (age >= FIRST_READ_MS && now - this->last_req_ms_ >= REQ_EVERY_MS)
        this->request_channel_(idx);
      break;
    }
  }
}

void Mc3000Hub::start_charge(uint8_t channel_1based) {
  if (channel_1based < 1 || channel_1based > ::mc3000_protocol::CHANNEL_COUNT)
    return;
  uint8_t mask = ::mc3000_protocol::channelBit(idx_(channel_1based));
  uint8_t packet[::mc3000_protocol::PACKET_SIZE];
  ::mc3000_protocol::buildPacket(::mc3000_protocol::CMD_START_CHARGE, &mask, 1, packet, sizeof(packet));
  this->write_packet_(packet, sizeof(packet));
}

void Mc3000Hub::stop_charge(uint8_t channel_1based) {
  if (channel_1based < 1 || channel_1based > ::mc3000_protocol::CHANNEL_COUNT)
    return;
  uint8_t mask = ::mc3000_protocol::channelBit(idx_(channel_1based));
  uint8_t packet[::mc3000_protocol::PACKET_SIZE];
  ::mc3000_protocol::buildPacket(::mc3000_protocol::CMD_STOP_CHARGE, &mask, 1, packet, sizeof(packet));
  this->write_packet_(packet, sizeof(packet));
}

void Mc3000Hub::set_program(uint8_t channel_1based, const ::mc3000_protocol::ChargeProgram &program) {
  if (channel_1based < 1 || channel_1based > ::mc3000_protocol::CHANNEL_COUNT)
    return;
  uint8_t mask = ::mc3000_protocol::channelBit(idx_(channel_1based));
  uint8_t packet1[::mc3000_protocol::PACKET_SIZE];
  uint8_t packet2[::mc3000_protocol::PACKET_SIZE];
  ::mc3000_protocol::buildSetProgramPackets(program, mask, packet1, packet2);
  this->write_packet_(packet1, sizeof(packet1));
  // The two halves belong together: the charger discards the program when
  // the second half comes too late (seen with a 150 ms gap - it then keeps
  // running its previous program). Keep the official app's ~50 ms here and
  // skip the general minimum gap for this one packet.
  delay(50);
  this->write_packet_(packet2, sizeof(packet2), false);
}

void Mc3000Hub::dump_config() {
  ESP_LOGCONFIG(TAG, "MC3000:");
  LOG_UPDATE_INTERVAL(this);
  if (this->has_version_) {
    ESP_LOGCONFIG(TAG, "  Firmware: %u.%u  Hardware: %u.%u", this->version_.fwMajor, this->version_.fwMinor,
                 this->version_.hwVersionRaw / 10, this->version_.hwVersionRaw % 10);
  }
}

}  // namespace mc3000
}  // namespace esphome

#endif
