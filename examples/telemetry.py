"""Usage: python examples/telemetry.py DEVICE"""
import asyncio
from contextlib import aclosing
import sys
from profitec_ble import ProfitecClient, decode

async def main():
    async with ProfitecClient(sys.argv[1]) as machine:
        async with aclosing(machine.events(60)) as events:
            async for packet in events:
                print(decode(packet["uuid"], bytes.fromhex(packet["hex"])))

asyncio.run(main())
