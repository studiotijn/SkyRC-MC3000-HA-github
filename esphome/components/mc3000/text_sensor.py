import esphome.codegen as cg
from esphome.components import text_sensor
import esphome.config_validation as cv
from esphome.const import CONF_STATUS

from . import MC3000_CHANNEL_SCHEMA, register_mc3000_channel

CONFIG_SCHEMA = MC3000_CHANNEL_SCHEMA.extend(
    {
        cv.Optional(CONF_STATUS): text_sensor.text_sensor_schema(
            icon="mdi:battery-sync"
        ),
    }
)


async def to_code(config):
    hub, channel = await register_mc3000_channel(config)

    if status_config := config.get(CONF_STATUS):
        sens = await text_sensor.new_text_sensor(status_config)
        cg.add(hub.set_status_text_sensor(channel, sens))
