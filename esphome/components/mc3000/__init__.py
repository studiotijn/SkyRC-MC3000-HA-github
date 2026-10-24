import esphome.codegen as cg
from esphome.components import ble_client
import esphome.config_validation as cv
from esphome.const import CONF_ID

CODEOWNERS = ["@tijnh"]
DEPENDENCIES = ["ble_client"]
MULTI_CONF = True

mc3000_ns = cg.esphome_ns.namespace("mc3000")
Mc3000Hub = mc3000_ns.class_("Mc3000Hub", ble_client.BLEClientNode, cg.PollingComponent)

CONF_MC3000_ID = "mc3000_id"
CONF_CHANNEL = "channel"

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(Mc3000Hub),
        }
    )
    .extend(ble_client.BLE_CLIENT_SCHEMA)
    .extend(cv.polling_component_schema("5s"))
)

MC3000_CHANNEL_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_MC3000_ID): cv.use_id(Mc3000Hub),
        cv.Required(CONF_CHANNEL): cv.int_range(min=1, max=4),
    }
)


async def register_mc3000_channel(config):
    """Shared helper for sensor/text_sensor/switch platforms: resolves the
    hub and returns (hub_var, channel) for the caller to wire up."""
    hub = await cg.get_variable(config[CONF_MC3000_ID])
    return hub, config[CONF_CHANNEL]


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)
