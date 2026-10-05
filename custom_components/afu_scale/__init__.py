"""AFU 体脂秤集成入口"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AGE,
    CONF_HEIGHT_CM,
    CONF_MAX_DELTA_KG,
    CONF_SEX,
    DOMAIN,
)
from .coordinator import AfuScaleCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    address = entry.data[CONF_ADDRESS].upper()
    coordinator = AfuScaleCoordinator(
        hass,
        address,
        entry.data[CONF_HEIGHT_CM],
        entry.data[CONF_SEX],
        entry.data[CONF_AGE],
        entry.data[CONF_MAX_DELTA_KG],
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # 注册 reset_baseline service（多实例时只在第一个注册）
    if not hass.services.has_service(DOMAIN, "reset_baseline"):
        async def handle_reset_baseline(call) -> None:
            """清掉所有实例的 baseline，下次 stable 报文作为新 baseline。"""
            for coord in hass.data[DOMAIN].values():
                coord.reset_baseline()

        hass.services.async_register(
            DOMAIN, "reset_baseline", handle_reset_baseline
        )

    coordinator.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.stop()
    result = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # 最后一个实例卸载时移除 service
    if result and not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, "reset_baseline")
    return result


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """options 变更时把新值同步到 coordinator。"""
    coordinator: AfuScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    opts = entry.options
    if CONF_HEIGHT_CM in opts:
        coordinator.height_cm = opts[CONF_HEIGHT_CM]
    if CONF_SEX in opts:
        coordinator.male = opts[CONF_SEX] == "male"
    if CONF_AGE in opts:
        coordinator.age = opts[CONF_AGE]
    if CONF_MAX_DELTA_KG in opts:
        coordinator.max_delta_kg = opts[CONF_MAX_DELTA_KG]
    _LOGGER.info("AFU Scale: 选项已更新")