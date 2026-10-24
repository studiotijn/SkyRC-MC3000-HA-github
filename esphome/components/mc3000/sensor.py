import esphome.codegen as cg
from esphome.components import sensor
import esphome.config_validation as cv
from esphome.const import (
    CONF_CAPACITY,
    CONF_CURRENT,
    CONF_TEMPERATURE,
    CONF_VOLTAGE,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_VOLTAGE,
    STATE_CLASS_MEASUREMENT,
    UNIT_AMPERE,
    UNIT_CELSIUS,
    UNIT_VOLT,
)

from . import MC3000_CHANNEL_SCHEMA, register_mc3000_channel

# Not a predefined esphome.const CONF_* in this ESPHome version.
CONF_RESISTANCE = "resistance"

CONFIG_SCHEMA = MC3000_CHANNEL_SCHEMA.extend(
    {
        cv.Optional(CONF_VOLTAGE): sensor.sensor_schema(
            unit_of_measurement=UNIT_VOLT,
            device_class=DEVICE_CLASS_VOLTAGE,
            state_class=STATE_CLASS_MEASUREMENT,
            accuracy_decimals=3,
        ),
        cv.Optional(CONF_CURRENT): sensor.sensor_schema(
            unit_of_measurement=UNIT_AMPERE,
            device_class=DEVICE_CLASS_CURRENT,
            state_class=STATE_CLASS_MEASUREMENT,
            accuracy_decimals=3,
        ),
        cv.Optional(CONF_CAPACITY): sensor.sensor_schema(
            unit_of_measurement="mAh",
            state_class=STATE_CLASS_MEASUREMENT,
            accuracy_decimals=0,
            icon="mdi:battery-charging",
        ),
        cv.Optional(CONF_TEMPERATURE): sensor.sensor_schema(
            unit_of_measurement=UNIT_CELSIUS,
            device_class=DEVICE_CLASS_TEMPERATURE,
            state_class=STATE_CLASS_MEASUREMENT,
            accuracy_decimals=0,
        ),
        # Raw milliohms, as reported by the charger - not UNIT_OHM, to avoid
        # implying a x1000 unit conversion that isn't actually happening.
        cv.Optional(CONF_RESISTANCE): sensor.sensor_schema(
            unit_of_measurement="mΩ",
            state_class=STATE_CLASS_MEASUREMENT,
            accuracy_decimals=0,
            icon="mdi:omega",
        ),
    }
)


async def to_code(config):
    hub, channel = await register_mc3000_channel(config)

    if voltage_config := config.get(CONF_VOLTAGE):
        sens = await sensor.new_sensor(voltage_config)
        cg.add(hub.set_voltage_sensor(channel, sens))
    if current_config := config.get(CONF_CURRENT):
        sens = await sensor.new_sensor(current_config)
        cg.add(hub.set_current_sensor(channel, sens))
    if capacity_config := config.get(CONF_CAPACITY):
        sens = await sensor.new_sensor(capacity_config)
        cg.add(hub.set_capacity_sensor(channel, sens))
    if temperature_config := config.get(CONF_TEMPERATURE):
        sens = await sensor.new_sensor(temperature_config)
        cg.add(hub.set_temperature_sensor(channel, sens))
    if resistance_config := config.get(CONF_RESISTANCE):
        sens = await sensor.new_sensor(resistance_config)
        cg.add(hub.set_resistance_sensor(channel, sens))
