from lcmm.matchmaker import ConnectionManager, run
import asyncio
import logging

logging.basicConfig(
    encoding="utf-8", level=logging.DEBUG, format="%(asctime)s %(message)s"
)

asyncio.run(run())
