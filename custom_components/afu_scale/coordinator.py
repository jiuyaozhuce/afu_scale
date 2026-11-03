"""AFU 体脂秤 BLE 连接协调器。

负责：
 - 通过 HA 蓝牙集成（含 ESP32 蓝牙代理）获取可连接设备
 - 主动连接体脂秤，订阅 0xFFB2 通知
 - 解析 0xAC 报文（体重/稳定/阻抗/电量/充电），并计算 BIA 指标
 - 连接断开后自动重连

"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    CONNECT_ATTEMPTS,
    CONNECT_TIMEOUT,
    DOMAIN,
    HANDSHAKE,
    NOTIFY_CHAR_UUID,
    PACKET_MAGIC,
    RECONNECT_DELAY,
    SERVICE_UUID,
    STABLE_FLAG,
    WRITE_CHAR_UUID,
)

if TYPE_CHECKING:
    from .sensor import AfuSensor

_LOGGER = logging.getLogger(__name__)


def parse_packet(data: bytes):
    """解析 0xAC 体重报文，返回 (weight_kg, is_stable, impedance, battery, charging) 或 None。

    逆向自官方 App（com.afu.afudevicemanager / aijk_hardware_sdk）：
    原始 20 字节报文 = IC V27 分帧（[18] 报文类型、[19] 校验和），实测示例：
    ac 29 80 68 f4 56 02 00 05 40 00 64 00 00 00 00 00 29 d5 1b
    [0] 0xAC 魔数
    [1] device_type
    [3] 体重高位 (-0x68 偏移，0x68 为状态位)
    [4:6] 体重中低位
    [6] 0x02 表示测量/锁定稳定（IC 协议中即 mode 字节）
    [7] mode==1 时的电量字节（bit7=充电中，低 7 位=电量%）
    [8:10] 阻抗 (Big Endian)
    [11] mode==2 时的电量字节（4 字节 support_funs 之后，bit7=充电中，低 7 位=电量%）
    """
    if len(data) < 10 or data[0] != PACKET_MAGIC:
        return None
    raw_weight = (data[3] - 0x68) * 65536 + data[4] * 256 + data[5]
    if raw_weight < 0:
        return None
    weight_kg = raw_weight / 1000.0
    is_stable = data[6] == STABLE_FLAG
    impedance = (data[8] << 8) | data[9]
    # 过滤无效读数：体重<=0（称重结束）或阻抗过低（人已离开）
    if weight_kg <= 0.0 or impedance < 500.0:
        return None
    battery = None
    charging = False
    # 电量位置取决于 mode 字节：1 在 data[7]，2 在 data[11]
    idx = 11 if data[6] == 0x02 else 7
    if len(data) > idx:
        raw = data[idx] & 0xFF
        level = raw & 0x7F
        # 0x7F/0xFF 为"未知/无电量数据"，超过 100 的取值同样无效
        if 0 < level <= 100:
            battery = level
            charging = bool(raw & 0x80)
    return weight_kg, is_stable, impedance, battery, charging


class AfuScaleCoordinator:
    """管理与体脂秤的 BLE 连接并分发测量数据。"""

    def __init__(self, hass: HomeAssistant, address: str, height_cm: float,
                 sex: str, age: int) -> None:
        self.hass = hass
        self.address = address
        self.height_cm = height_cm
        self.male = sex == "male"
        self.age = age

        self._client: BleakClient | None = None
        self._connect_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._shutdown = False
        self.entities: dict[str, AfuSensor] = {}

        # 测量中状态：收到有效数据包置 True，无数据超时后置 False
        self.measuring = False
        self._last_data_at: float = 0.0
        self._idle_handle: asyncio.TimerHandle | None = None
        self.measuring_entity = None
        self.charging_entity = None
        self.raw_data_entity = None

    def _set_measuring(self, value: bool) -> None:
        if self.measuring == value:
            return
        self.measuring = value
        if self.measuring_entity is not None:
            self.measuring_entity.async_update_state(value)

    def _set_charging(self, value: bool) -> None:
        if self.charging_entity is not None:
            self.charging_entity.async_update_state(value)

    @callback
    def register_entity(self, key: str, entity: AfuSensor) -> None:
        self.entities[key] = entity
        if key == "measuring":
            self.measuring_entity = entity
        elif key == "charging":
            self.charging_entity = entity

    def start(self) -> None:
        self._shutdown = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._shutdown = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def _run(self) -> None:
        while not self._shutdown:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("AFU Scale 连接异常: %s", exc)
                if self._shutdown:
                    break
                await asyncio.sleep(RECONNECT_DELAY)

    async def _connect_and_listen(self) -> None:
        """连接设备并监听通知。"""
        # 通过 HA 蓝牙集成获取 BLEDevice
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address.upper(), connectable=True
        )
        if ble_device is None:
            _LOGGER.warning("未找到蓝牙设备 %s，等待重试...", self.address)
            await asyncio.sleep(RECONNECT_DELAY)
            return

        async with self._connect_lock:
            try:
                self._client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self.address,
                    timeout=CONNECT_TIMEOUT,
                    attempts=CONNECT_ATTEMPTS,
                )
            except BleakError as exc:
                _LOGGER.warning("建立连接失败: %s", exc)
                await asyncio.sleep(RECONNECT_DELAY)
                return

        _LOGGER.info("已连接 AFU 体脂秤 %s", self.address)

        # 发送握手包（部分固件需要）
        try:
            await self._client.write_gatt_char(WRITE_CHAR_UUID, HANDSHAKE)
        except BleakError:
            pass  # 忽略握手失败，继续尝试订阅

        # 订阅通知
        def notification_handler(_sender: int, data: bytearray) -> None:
            self.hass.loop.call_soon_threadsafe(self._handle_notification, bytes(data))

        try:
            await self._client.start_notify(NOTIFY_CHAR_UUID, notification_handler)
        except BleakError as exc:
            _LOGGER.warning("订阅通知失败: %s", exc)
            await self._disconnect()
            return

        # 保持连接，直到断开或关闭
        while not self._shutdown and self._client and self._client.is_connected:
            await asyncio.sleep(1)

        # 连接断开
        await self._disconnect()

    def _handle_notification(self, data: bytes) -> None:
        """处理体脂秤通知数据。"""
        self._last_data_at = time.monotonic()
        self._set_measuring(True)

        # 重置空闲定时器
        if self._idle_handle:
            self._idle_handle.cancel()
        self._idle_handle = self.hass.loop.call_later(15, self._set_measuring, False)

        parsed = parse_packet(data)
        if parsed is None:
            return

        weight_kg, is_stable, impedance, battery, charging = parsed

        # 更新充电状态
        self._set_charging(charging)

        # 计算 BIA 指标
        bmi = self._calc_bmi(weight_kg)
        body_fat = self._calc_body_fat(weight_kg, impedance)
        water = self._calc_water(weight_kg, impedance)
        muscle = self._calc_muscle(weight_kg, impedance)
        protein = self._calc_protein(weight_kg, impedance)
        bone = self._calc_bone(weight_kg, impedance)

        # 分发到各实体
        updates = {
            "weight": round(weight_kg, 2),
            "weight_jin": round(weight_kg * 2, 1),  # 斤 = kg * 2
            "impedance": round(impedance),
            "stable": 1 if is_stable else 0,
            "bmi": round(bmi, 1),
            "body_fat": round(body_fat, 1),
            "water": round(water, 1),
            "muscle": round(muscle, 1),
            "protein": round(protein, 1),
            "bone": round(bone, 2),
            "battery": battery,
            "timestamp": dt_util.utcnow(),
            "raw_data": data.hex().upper(),
        }

        for key, value in updates.items():
            if entity := self.entities.get(key):
                entity.async_update_state(value)

    async def _disconnect(self) -> None:
        self._set_measuring(False)
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None

    # --- BIA 计算公式（参考常用算法，仅供参考，不作为医疗依据） ---
    def _calc_bmi(self, weight_kg: float) -> float:
        h = self.height_cm / 100.0
        return weight_kg / (h * h)

    def _calc_body_fat(self, weight_kg: float, impedance: float) -> float:
        # 简化的体脂率估算公式
        h = self.height_cm / 100.0
        if self.male:
            return (1.2 * (weight_kg / (h * h)) + 0.23 * self.age - 16.2)
        return (1.2 * (weight_kg / (h * h)) + 0.23 * self.age - 5.4)

    def _calc_water(self, weight_kg: float, impedance: float) -> float:
        # 简化的水分率估算
        bf = self._calc_body_fat(weight_kg, impedance)
        return max(0, 100 - bf - 20)  # 粗略估算

    def _calc_muscle(self, weight_kg: float, impedance: float) -> float:
        # 肌肉量 ≈ 体重 - 脂肪 - 骨 - 水分(部分)
        bf_pct = self._calc_body_fat(weight_kg, impedance) / 100
        fat_mass = weight_kg * bf_pct
        return weight_kg - fat_mass - self._calc_bone(weight_kg, impedance) - 2.5

    def _calc_protein(self, weight_kg: float, impedance: float) -> float:
        # 简化的蛋白质率估算
        bf = self._calc_body_fat(weight_kg, impedance)
        return max(0, 20 - bf * 0.3)  # 粗略估算

    def _calc_bone(self, weight_kg: float, impedance: float) -> float:
        # 骨量 ≈ 体重 * 0.035 (男) / 0.03 (女) 近似
        return weight_kg * (0.035 if self.male else 0.03)