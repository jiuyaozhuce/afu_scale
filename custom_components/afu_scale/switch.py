"""AFU 体脂秤 BLE 连接开关

关闭时主动断开 BLE 连接并停止重连，让位给手机 app（如 Mi Fitness）连接秤。
开启时恢复重连尝试。
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import AfuScaleCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AfuScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AfuScaleBluetoothSwitch(coordinator)])


class AfuScaleBluetoothSwitch(SwitchEntity):
    """控制与体脂秤的 BLE 连接。"""

    _attr_should_poll = False

    def __init__(self, coordinator: AfuScaleCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_bluetooth"
        self._attr_name = "蓝牙连接"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.address)},
            name="AFU 体脂秤",
            manufacturer="沃莱科技",
            model="AFU-WL-TZ-A1",
        )

    @property
    def is_on(self) -> bool:
        return not self._coordinator._paused

    @property
    def icon(self) -> str:
        return "mdi:bluetooth" if self.is_on else "mdi:bluetooth-off"

    async def async_turn_on(self, **kwargs) -> None:
        await self._coordinator.set_paused(False)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._coordinator.set_paused(True)
        self.async_write_ha_state()