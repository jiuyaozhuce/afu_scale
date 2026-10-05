"""AFU 体脂秤传感器实体"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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


class AfuSensor(SensorEntity):
    """AFU 体脂秤传感器基类"""

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
        # 图标
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

    @callback
    def async_update_state(self, value) -> None:
        # 将 kg 转换为斤（体重、肌肉量、骨量）
        if self._key in ("weight", "muscle", "bone") and value is not None:
            value = round(value * 2, self._def.get("precision", 1))
        self._attr_native_value = value
        self.async_write_ha_state()


class AfuTimestampSensor(AfuSensor):
    """记录最近一次测量时间"""

    def __init__(self, coordinator: AfuScaleCoordinator) -> None:
        super().__init__(coordinator, "weight")
        self._key = "timestamp"
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_timestamp"
        self._attr_name = "AFU 体脂秤最近测量时间"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_native_unit_of_measurement = None
        self._attr_state_class = None
        self._attr_suggested_display_precision = None
        self._attr_icon = "mdi:clock-outline"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AfuScaleCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [AfuSensor(coordinator, key) for key in SENSOR_DEFS]
    entities.append(AfuTimestampSensor(coordinator))
    async_add_entities(entities)
    for entity in entities:
        coordinator.register_entity(entity._key, entity)