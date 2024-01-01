from asyncio import Lock, subprocess
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Optional


RUNNER = Path("../lost-cities/LostCities.Game/bin/Debug/net7.0/LostCities.Game.exe")


@dataclass
class RunnerResponse:
    next_turn_index: Optional[int]
    payload: Optional[str]


class GameRunner:
    def __init__(self):
        self.ready_for_new_game = True
        self.lock = Lock()

    async def start(self):
        self.proc = await subprocess.create_subprocess_exec(
            str(RUNNER),
            "--endless",
            cwd=str(RUNNER.parent),
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
        )

    def send_response(self, response: str):
        try:
            msg = json.dumps(json.loads(response))
        except Exception as e:
            logging.warn(
                "Bot sent invalid json, forwarding empty line to disqualify.",
                exc_info=e,
            )
            msg = ""
        # logging.debug(f"Sending to runner: {msg}")
        self.proc.stdin.write(msg.encode())  # type:ignore
        self.proc.stdin.write("\n".encode())  # type:ignore

    async def query_runner(self):
        turn_index, json_response = await self._read_response()

        return RunnerResponse(
            next_turn_index=turn_index,
            payload=json_response,
        )

    async def _read_response(self):
        turn_index = None
        while True:
            self.ready_for_new_game = False
            if self.proc.returncode is not None:
                logging.error("Runner stopped")
                return None, None
            line = (await self.proc.stdout.readline()).decode()  # type:ignore
            # logging.debug(f"runner says: {line}")
            field, _, value = line.partition(":")
            field = field.strip()
            value = value.strip()
            if field == "End":
                self.ready_for_new_game = True
                return None, value
            if field == "Turn":
                turn_index = int(value[-1]) - 1
            elif field == "View":
                view_json = value
                break
        return turn_index, view_json
