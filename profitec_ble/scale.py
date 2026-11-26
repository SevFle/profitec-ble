"""Eureka Precisa Bluetooth scale. Protocol facts (names, UUIDs, tare, weight layout) were taken from
Beanconqueror's public source and reimplemented here; see NOTICE.md. Not verified with every firmware."""
import asyncio
import math
from .client import positive

PRECISA_NAMES = ("CFS-9002", "LSJ-001")
WEIGHT_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
COMMAND_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
TARE = bytes.fromhex("aa023131")


def decode_weight(data: bytes) -> float:
    """Grams: byte 6 is a sign flag, bytes 7-8 an unsigned little-endian count of 0.1 g."""
    data = bytes(data)
    if len(data) != 11:
        raise ValueError("Precisa weight requires 11 bytes")
    grams = int.from_bytes(data[7:9], "little") / 10
    return -grams if data[6] else grams


class PrecisaScale:
    def __init__(self, device: str, *, timeout: float = 20):
        self.device = device
        self.timeout = positive(timeout)
        self.weight = None
        self.updated = None
        self.errors = 0
        self._client = None

    async def __aenter__(self):
        from bleak import BleakClient
        client = BleakClient(self.device, timeout=self.timeout)
        await client.connect()
        try:
            await client.start_notify(WEIGHT_UUID, self._receive)
        except BaseException:
            await client.disconnect()
            raise
        self._client = client
        return self

    async def __aexit__(self, *exc):
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()

    def _receive(self, sender, data):
        try:
            self.weight = decode_weight(data)
        except ValueError:
            self.errors += 1
            return
        self.updated = asyncio.get_running_loop().time()

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def age(self) -> float:
        """Seconds since the last valid reading; infinite when none or disconnected."""
        if self.updated is None or not self.connected:
            return math.inf
        return asyncio.get_running_loop().time() - self.updated

    async def tare(self):
        if not self.connected:
            raise ConnectionError("Scale not connected")
        await self._client.write_gatt_char(COMMAND_UUID, TARE, response=False)
