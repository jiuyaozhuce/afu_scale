"""AFU 体脂秤集成配置流"""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_AGE,
    CONF_HEIGHT_CM,
    CONF_MAX_DELTA_KG,
    CONF_SEX,
    DEFAULT_AGE,
    DEFAULT_HEIGHT_CM,
    DEFAULT_MAX_DELTA_KG,
    DEFAULT_SEX,
    DOMAIN,
    MAX_MAX_DELTA_KG,
    MIN_MAX_DELTA_KG,
)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ADDRESS): str,
        vol.Optional(CONF_HEIGHT_CM, default=DEFAULT_HEIGHT_CM): vol.All(
            vol.Coerce(float), vol.Range(min=50, max=250)
        ),
        vol.Optional(CONF_SEX, default=DEFAULT_SEX): vol.In(["male", "female"]),
        vol.Optional(CONF_AGE, default=DEFAULT_AGE): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=120)
        ),
        vol.Optional(CONF_MAX_DELTA_KG, default=DEFAULT_MAX_DELTA_KG): NumberSelector(
            NumberSelectorConfig(
                min=MIN_MAX_DELTA_KG,
                max=MAX_MAX_DELTA_KG,
                step=0.5,
                mode=NumberSelectorMode.SLIDER,
                unit_of_measurement="kg",
            )
        ),
    }
)


class AfuScaleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """AFU 体脂秤配置流"""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            address = str(user_input[CONF_ADDRESS]).strip().upper()
            if not self._valid_mac(address):
                errors[CONF_ADDRESS] = "invalid_mac"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"AFU 体脂秤 {address}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def _valid_mac(address: str) -> bool:
        parts = address.split(":")
        return len(address) == 17 and len(parts) == 6 and all(
            len(p) == 2 for p in parts
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return AfuScaleOptionsFlow(config_entry)


class AfuScaleOptionsFlow(config_entries.OptionsFlow):
    """AFU 体脂秤选项流：在线修改身高/年龄/性别/跳变阈值。"""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_HEIGHT_CM,
                    default=opts.get(CONF_HEIGHT_CM, DEFAULT_HEIGHT_CM),
                ): vol.All(vol.Coerce(float), vol.Range(min=50, max=250)),
                vol.Optional(
                    CONF_SEX,
                    default=opts.get(CONF_SEX, DEFAULT_SEX),
                ): vol.In(["male", "female"]),
                vol.Optional(
                    CONF_AGE,
                    default=opts.get(CONF_AGE, DEFAULT_AGE),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
                vol.Optional(
                    CONF_MAX_DELTA_KG,
                    default=opts.get(CONF_MAX_DELTA_KG, DEFAULT_MAX_DELTA_KG),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_MAX_DELTA_KG,
                        max=MAX_MAX_DELTA_KG,
                        step=0.5,
                        mode=NumberSelectorMode.SLIDER,
                        unit_of_measurement="kg",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)