from pathlib import Path
import sys
import asyncio
from asyncio import subprocess


HOST, PORT = "localhost", 57910
BOT = Path("../lost-cities/Examples/bin/Debug/net7.0/Greedy.exe")
CLIENT = Path("../lost-cities/LostCities.Client/bin/Debug/net7.0/LostCities.Client.exe")


async def run_bot(name):
    proc: subprocess.Process = await subprocess.create_subprocess_exec(
        str(CLIENT),
        "--hostname",
        HOST,
        "--token",
        name,
        "--player",
        str(BOT.absolute()),
        cwd=str(CLIENT.parent),
    )
    await proc.wait()


async def main():
    async with asyncio.TaskGroup() as tg:
        tg.create_task(run_bot("bot1"))
        tg.create_task(run_bot("bot2"))


asyncio.run(main())
