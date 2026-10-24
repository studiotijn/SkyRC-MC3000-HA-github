import esphome.codegen as cg
from esphome.components import switch
import esphome.config_validation as cv

from .. import MC3000_CHANNEL_SCHEMA, mc3000_ns, register_mc3000_channel

Mc3000ChargeSwitch = mc3000_ns.class_("Mc3000ChargeSwitch", switch.Switch, cg.Component)

CONFIG_SCHEMA = switch.switch_schema(
    Mc3000ChargeSwitch, icon="mdi:battery-charging-100"
).extend(MC3000_CHANNEL_SCHEMA)


async def to_code(config):
    hub, channel = await register_mc3000_channel(config)
    var = await switch.new_switch(config)
    await cg.register_component(var, config)
    cg.add(var.set_hub(hub))
    cg.add(var.set_channel(channel))
    cg.add(hub.set_charge_switch(channel, var))
