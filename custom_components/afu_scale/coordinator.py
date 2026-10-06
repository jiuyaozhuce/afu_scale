"""AFU 体脂秤 BLE 连接协调器。

负责：
  - 通过 HA 蓝牙集成（含 ESP32 蓝牙代理）获取可连接设备
  - 主动连接体脂秤，订阅 0xFFB2 通知
  - 解析 0xAC 报文（体重/稳定/阻抗），并计算 BIA 指标
  - 连接断开后自动重连
  - 体重跳变过滤：同一测量会话内两次稳定读数差值过大时丢弃新数据
  - 蓝牙连接暂停/恢复：让位给手机 app

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
    FAST_RECONNECT_DELAY,
    FAST_RECONNECT_WINDOW,
    HANDSHAKE,
    MAX_VALID_IMPEDANCE,
    MAX_VALID_WEIGHT_KG,
    MIN_VALID_IMPEDANCE,
    MIN_VALID_WEIGHT_KG,
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
    """解析 0xAC 体重报文，返回 (weight_kg, is_stable, impedance) 或 None。
      [0]  0xAC 魔数
      [3]  体重高位 (-0x68 偏移)
      [4:6] 体重中低位
      [6]  0x02 表示数值锁定稳定
      [8:10] 阻抗 (Big Endian)
    """
    if len(data) < 10 or data[0] != PACKET_MAGIC:
        return None
    raw_weight = (data[3] - 0x68) * 65536 + data[4] * 256 + data[5]
    if raw_weight < 0:
        return None
    # 该秤原始单位 50g (0.05kg)/步
    weight_kg = raw_weight * 0.05
    is_stable = data[6] == STABLE_FLAG
    impedance = (data[8] << 8) | data[9]
    # 物理范围过滤：上/下秤瞬态、字节错位、阻抗测量阶段的非体重帧
    # 都会产生离谱值（如 2742kg），在源头丢弃，防止污染 baseline
    if weight_kg < MIN_VALID_WEIGHT_KG or weight_kg > MAX_VALID_WEIGHT_KG:
        return None
    if impedance < MIN_VALID_IMPEDANCE or impedance > MAX_VALID_IMPEDANCE:
        return None
    return weight_kg, is_stable, impedance


class AfuScaleCoordinator:
    """管理与体脂秤的 BLE 连接并分发测量数据。"""

    def __init__(self, hass: HomeAssistant, address: str, height_cm: float,
                 sex: str, age: int, max_delta_kg: float) -> None:
        self.hass = hass
        self.address = address
        self.height_cm = height_cm
        self.male = sex == "male"
        self.age = age
        self.max_delta_kg = max_delta_kg

        self._client: BleakClient | None = None
        self._connect_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._shutdown = False
        self._paused = False  # True 时停止重连，让位给手机 app
        self.entities: dict[str, AfuSensor] = {}

        # 测量中状态：收到有效数据包置 True，无数据超时后置 False
        self.measuring = False
        self._last_data_at: float = 0.0
        self._idle_handle: asyncio.TimerHandle | None = None
        self.measuring_entity = None

        # 体重跳变过滤：上一次接受的稳定体重（跨会话保留，单人长期使用）
        self._last_accepted_weight: float | None = None

        # 上次连接成功时间（用于断开后快速重连窗口）
        self._last_connected_at: float = 0.0

    def _set_measuring(self, value: bool) -> None:
        if self.measuring == value:
            return
        self.measuring = value
        if self.measuring_entity is not None:
            self.measuring_entity.async_update_state(value)

    @callback
    def register_entity(self, key: str, entity: AfuSensor) -> None:
        self.entities[key] = entity

    def seed_baseline(self, weight_kg: float) -> None:
        """从恢复的上次体重播种 baseline（仅当尚无 baseline 时）。

        HA 重启后内存 baseline 为空，第一个稳定读数会被无条件接受；
        用上次已接受的体重做种子，重启后跳变过滤立即生效。
        """
        if self._last_accepted_weight is None and MAX_VALID_WEIGHT_KG >= weight_kg >= MIN_VALID_WEIGHT_KG:
            self._last_accepted_weight = weight_kg
            _LOGGER.info(
                "AFU Scale %s: baseline 从上次体重播种 %.1fkg",
                self.address, weight_kg,
            )

    def start(self) -> None:
        self._shutdown = False
        self._task = asyncio.create_task(self._run())

    def _update_connection_status(self, status: str) -> None:
        """更新连接状态传感器"""
        entity = self.entities.get("connection_status")
        if entity:
            entity.async_update_state(status)

    def reset_baseline(self) -> None:
        """重置 baseline：下次 stable 报文会无条件接受为新 baseline。

        适用场景：
        - 测试时把错误值（如 8kg 物体）误当成了 baseline
        - 体重真的发生了大幅变化（> 阈值）导致所有读数都被卡住
        - 想强制重新校准
        """
        if self._last_accepted_weight is not None:
            _LOGGER.info(
                "AFU Scale %s: baseline 已重置（之前 %.1fkg）",
                self.address, self._last_accepted_weight,
            )
        else:
            _LOGGER.info("AFU Scale %s: baseline 已重置（之前未设置）", self.address)
        self._last_accepted_weight = None

    async def stop(self) -> None:
        self._shutdown = True
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
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
            if self._paused:
                # 用户主动暂停：每 5s 检查一次是否恢复
                await asyncio.sleep(5)
                continue
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("AFU Scale 连接异常: %s", exc)
            if self._shutdown:
                break
            # 刚断开（秤唤醒踢连接、测量会话窗口短暂）→ 快速重连窗口
            delay = RECONNECT_DELAY
            if self._last_connected_at and (
                time.monotonic() - self._last_connected_at < FAST_RECONNECT_WINDOW
            ):
                delay = FAST_RECONNECT_DELAY
            await asyncio.sleep(delay)

    async def set_paused(self, paused: bool) -> None:
        """控制 BLE 连接：paused=True 时主动断开并不再重连。

        适用场景：让位给手机 app（如 Mi Fitness）连接秤。
        - 关闭开关：主动断开现有连接 + 停止重连尝试
        - 开启开关：恢复重连尝试
        """
        if paused == self._paused:
            return
        self._paused = paused
        if paused:
            _LOGGER.info(
                "AFU Scale %s: 已暂停 BLE 连接（让位给手机 app）", self.address
            )
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._client = None
        else:
            _LOGGER.info("AFU Scale %s: 已恢复 BLE 连接", self.address)

    async def _connect_and_listen(self) -> None:
        async with self._connect_lock:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is None:
                self._update_connection_status("device_not_found")
                _LOGGER.debug("AFU Scale: 设备未在蓝牙集成中出现，等待重试")
                return
            self._update_connection_status("connecting")
            try:
                client = await establish_connection(
                    BleakClient,
                    device,
                    self.address,
                    timeout=CONNECT_TIMEOUT,
                    max_attempts=CONNECT_ATTEMPTS,
                )
            except BleakError as exc:
                self._update_connection_status("connect_failed")
                _LOGGER.warning("AFU Scale: 连接失败 %s: %s", self.address, exc)
                return
            self._client = client
            self._last_connected_at = time.monotonic()
            self._update_connection_status("connected")
            _LOGGER.info("AFU Scale: 已连接 %s", self.address)

        try:
            await self._subscribe(client)
            # 保持连接；断开会话自然结束，回到重连循环
            while client.is_connected and not self._shutdown:
                await asyncio.sleep(5)
        finally:
            self._client = None
            self._update_connection_status("disconnected")
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _subscribe(self, client: BleakClient) -> None:
        # 发送握手包（写 0xFFB1，无响应写），触发完整数据推送
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, HANDSHAKE, response=False)
            _LOGGER.debug("AFU Scale: 已发送握手包")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("AFU Scale: 握手包发送失败（忽略）: %s", exc)

        await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        _LOGGER.debug("AFU Scale: 已订阅 0xFFB2 通知")

    def _on_notify(self, _characteristic, data: bytes) -> None:
        """bleak 通知回调（在事件循环中调用）。"""
        parsed = parse_packet(bytes(data))
        if parsed is None:
            return
        weight_kg, is_stable, impedance = parsed

        # 体重跳变过滤：仅对 stable 报文、与上一次接受的稳定体重比对
        if is_stable and self._last_accepted_weight is not None:
            delta = abs(weight_kg - self._last_accepted_weight)
            if delta > self.max_delta_kg:
                _LOGGER.info(
                    "AFU Scale: 体重跳变 %.1fkg (last=%.1f, new=%.1f, "
                    "threshold=%.1f) 已丢弃",
                    delta, self._last_accepted_weight, weight_kg,
                    self.max_delta_kg,
                )
                # 人还在秤上：保持 measuring 状态和 idle 计时器
                self._touch_measuring()
                return  # 全丢：weight/impedance/BIA/timestamp 都不更新

        if is_stable:
            self._last_accepted_weight = weight_kg

        _LOGGER.debug(
            "AFU Scale: %.2fkg stable=%s impedance=%.0fΩ",
            weight_kg, is_stable, impedance,
        )
        self._touch_measuring()
        values = self._compute_bia(weight_kg, impedance)
        values["weight"] = weight_kg
        values["stable"] = 1.0 if is_stable else 0.0
        values["impedance"] = impedance
        # 最近测量时间：仅在读数稳定时更新
        if is_stable:
            values["timestamp"] = dt_util.utcnow()
        for key, entity in self.entities.items():
            if key in values:
                entity.async_update_state(values[key])

    def _touch_measuring(self) -> None:
        """刷新 measuring 状态和 idle 计时器。"""
        self._set_measuring(True)
        self._last_data_at = time.monotonic()
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = self.hass.loop.call_later(15, self._idle_timeout)

    def _idle_timeout(self) -> None:
        """连续 15 秒无新数据时，把"测量中"置 False。"""
        self._idle_handle = None
        if time.monotonic() - self._last_data_at >= 15:
            self._set_measuring(False)

    def _compute_bia(self, weight_kg: float, impedance: float) -> dict[str, float]:
        """BIA 身体指标计算"""
        if self.height_cm <= 0:
            return {}
        height_m = self.height_cm / 100.0
        bmi = weight_kg / (height_m * height_m)

        if impedance > 0:
            if self.male:
                fat = 0.18 * bmi + 0.012 * self.age + 0.018 * impedance - 3.2
            else:
                fat = 0.26 * bmi + 0.011 * self.age + 0.020 * impedance - 2.5
        else:
            sex_offset = -10.8 if self.male else 0.0
            fat = 1.20 * bmi + 0.23 * self.age + sex_offset - 5.4
        fat = max(5.0, min(55.0, fat))

        water = max(35.0, min(75.0, 69.7 - fat * 0.55))
        bone = max(1.5, min(5.5, weight_kg * (0.047 if self.male else 0.040)))
        protein = max(10.0, min(24.0, 16.0 + (water - 50.0) * 0.12))
        muscle = max(0.0, weight_kg * (1.0 - fat / 100.0) - bone)

        return {
            "bmi": bmi,
            "body_fat": fat,
            "water": water,
            "muscle": muscle,
            "protein": protein,
            "bone": bone,
        }