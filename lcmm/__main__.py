import asyncio
import logging

import lcmm.matchmaker

logging.basicConfig(
    encoding="utf-8", level=logging.DEBUG, format="%(asctime)s %(message)s"
)


asyncio.run(lcmm.matchmaker.run())
