"""AFU 体脂秤传感器实体"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback, State
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity


from .const import DOMAIN
from .coordinator import AfuScaleCoordinator

ICON_MAP: dict[str, str] = {
    "weight": "mdi:scale-bathroom",
    "impedance": "mdi:flash",
    "stable": "mdi:checkbox-marked-circle",
    "bmi": "mdi:human-male-height",
    "body_fat": "mdi:percent",
    "water": "mdi:water-percent",
    "muscle": "mdi:weight-kilogram",
    "protein": "mdi:egg",
    "bone": "mdi:bone",
    "timestamp": "mdi:clock-outline",
    "connection_status": "mdi:bluetooth",
}

SENSOR_DEFS: dict[str, dict] = {
    "weight": {
        "name": "体重",
        "unit": "斤",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "impedance": {
        "name": "电阻抗",
        "unit": "Ω",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
    },
    "stable": {
        "name": "称重稳定",
        "unit": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
    },
    "bmi": {
        "name": "BMI",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "body_fat": {
        "name": "体脂率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "water": {
        "name": "水分率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "muscle": {
        "name": "肌肉量",
        "unit": "斤",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "protein": {
        "name": "蛋白质率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "bone": {
        "name": "骨量",
        "unit": "斤",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 2,
    },
}


class AfuSensor(SensorEntity, RestoreEntity):
    """AFU 体脂秤传感器基类：永远保持上次有效值，不设为 None/unknown"""

    def __init__(self, coordinator: AfuScaleCoordinator, key: str) -> None:
        self._coordinator = coordinator
        self._key = key
        self._def = SENSOR_DEFS[key]
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_{key}"
        self._attr_name = f"AFU 体脂秤{self._def['name']}"
        self._attr_should_poll = False
        self._attr_native_unit_of_measurement = self._def.get("unit")
        if self._def.get("device_class"):
            self._attr_device_class = self._def["device_class"]
        if self._def.get("state_class"):
            self._attr_state_class = self._def["state_class"]
        if "precision" in self._def:
            self._attr_suggested_display_precision = self._def["precision"]
        if key in ICON_MAP:
            self._attr_icon = ICON_MAP[key]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.address)},
            name="AFU 体脂秤",
            manufacturer="沃莱科技",
            model="AFU-WL-TZ-A1",
        )

    async def async_added_to_hass(self) -> None:
        """恢复上次状态，避免重启后变 unknown"""
        await super().async_added_to_hass()
        last_state: State | None = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable", ""):
            try:
                self._attr_native_value = float(last_state.state)
            except ValueError:
                self._attr_native_value = last_state.state
            # 体重实体恢复后给 baseline 播种（恢复值是斤，换回 kg）
            if self._key == "weight" and isinstance(self._attr_native_value, float):
                self._coordinator.seed_baseline(self._attr_native_value / 2.0)

    @callback
    def async_update_state(self, value) -> None:
        """收到新值才更新；value 为 None 时保持原值不变"""
        if value is None:
            return
        # 体重、肌肉量、骨量：kg → 斤
        if self._key in ("weight", "muscle", "bone"):
            value = round(value * 2, self._def.get("precision", 1))
        elif "precision" in self._def:
            value = round(value, self._def["precision"])
        self._attr_native_value = value
        self.async_write_ha_state()


class AfuTimestampSensor(SensorEntity, RestoreEntity):
    """最近一次稳定测量时间：仅在 stable=True 时更新"""

    def __init__(self, coordinator: AfuScaleCoordinator) -> None:
        self._coordinator = coordinator
        self._key = "timestamp"
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_timestamp"
        self._attr_name = "AFU 体脂秤最近测量时间"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = False
        self._attr_icon = ICON_MAP["timestamp"]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.address)},
            name="AFU 体脂秤",
            manufacturer="沃莱科技",
            model="AFU-WL-TZ-A1",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state: State | None = await self.async_get_last_state()
        if last_state is not None and last_state.state not in ("unknown", "unavailable", ""):
            self._attr_native_value = last_state.state

    @callback
    def async_update_state(self, timestamp) -> None:
        """仅在有稳定读数时更新"""
        if timestamp is None:
            return
        self._attr_native_value = timestamp
        self.async_write_ha_state()


class AfuConnectionStatusSensor(SensorEntity):
    """蓝牙连接状态：connected / disconnected / connecting"""

    def __init__(self, coordinator: AfuScaleCoordinator) -> None:
        self._coordinator = coordinator
        self._key = "connection_status"
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_connection_status"
        self._attr_name = "AFU 体脂秤连接状态"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_should_poll = False
        self._attr_icon = ICON_MAP["connection_status"]
        self._attr_native_value = "disconnected"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.address)},
            name="AFU 体脂秤",
            manufacturer="沃莱科技",
            model="AFU-WL-TZ-A1",
        )

    @callback
    def async_update_state(self, status: str) -> None:
        self._attr_native_value = status
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AfuScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [AfuSensor(coordinator, key) for key in SENSOR_DEFS]
    entities.append(AfuTimestampSensor(coordinator))
    entities.append(AfuConnectionStatusSensor(coordinator))
    async_add_entities(entities)
    for entity in entities:
        coordinator.register_entity(entity._key, entity)