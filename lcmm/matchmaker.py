from typing import Optional
import uuid
import asyncio
from asyncio import Event, Queue
import logging

import lcmm.gamerunner
from lcmm.connections import ConnectionManager, PlayerConnection, ConnectionKey
import lcmm


MAX_RUNNING_GAMES = 10
HOST, PORT = "0.0.0.0", 57910


class Game:
    def __init__(
        self,
        player_one: PlayerConnection,
        player_two: PlayerConnection,
        runner: lcmm.gamerunner.GameRunner,
    ):
        self.players: list[PlayerConnection] = [player_one, player_two]
        self.runner = runner
        self.game_id = str(uuid.uuid4())
        self.acceptance = [False, False]
        self.all_accepted = Event()
        self.next_turn_index: Optional[int] = None
        self.bot_message_queue: Queue[tuple[ConnectionKey, str]] = Queue()

    async def start(self):
        lcmm.running_games.append(self)
        await self.runner.lock.acquire()
        for player in self.players:
            player.games_in_progress += 1
        # remainder init task
        self._task = asyncio.create_task(self._start())

    async def _start(self):
        logging.debug(f"Starting game {self.game_id}")
        for conn in self.players:
            conn.add_listener(self.bot_response)

            conn.send_json(
                {"$type": "new_game", "id": self.game_id, "max_turn_time": 10.0}
            )

        await self.all_accepted.wait()
        self._task = asyncio.create_task(self.play())

    async def play(self):
        while True:
            res = await self.runner.query_runner()
            if res.next_turn_index is None:
                break
            logging.debug(f"{self.game_id}: next turn")
            self.next_turn_index = res.next_turn_index
            self.players[self.next_turn_index].send_json(
                {"$type": "turn", "id": self.game_id, "view_json": res.payload}
            )
            _, action_json = await self.bot_message_queue.get()
            self.runner.send_response(action_json)

        for i, player in enumerate(self.players, 1):
            player.send_json(
                {
                    "$type": "end_game",
                    "id": self.game_id,
                    "player": i,
                    "result_json": res.payload,
                }
            )
            player.games_in_progress -= 1

        lcmm.running_games.remove(self)
        self.runner.lock.release()

    async def bot_response(self, conn_key: ConnectionKey, msg: dict):
        if msg["id"] != self.game_id:
            # logging.warn(f"game received response for unexpected game id {msg['id']}")
            return
        elif msg["$type"] == "accept_game":
            if not msg["accepted"]:
                return
            for idx, conn in enumerate(self.players):
                if conn.key == conn_key:
                    if not self.acceptance[idx]:
                        self.acceptance[idx] = True
                        if all(self.acceptance):
                            self.all_accepted.set()
        elif (
            msg["$type"] == "action"
            and self.next_turn_index is not None
            and self.players[self.next_turn_index].key == conn_key
        ):
            self.bot_message_queue.put_nowait((conn_key, msg["action_json"]))
            logging.debug(f"game received message from {conn_key}")
        else:
            logging.warn(f"ignoring unhandled message {msg}")


async def run():
    # def cb(reader, writer):
    #     conman.register(reader, writer)

    conman = ConnectionManager()

    end = Event()

    async def periodic():
        while True:
            if end.is_set():
                break
            async with asyncio.TaskGroup() as tg:
                tg.create_task(conman.matchmaking())
                tg.create_task(asyncio.sleep(1))

    async with asyncio.TaskGroup() as tg:
        for _ in range(10):
            logging.debug("starting runner")
            runner = lcmm.gamerunner.GameRunner()
            lcmm.runners.append(runner)
            tg.create_task(runner.start())

    logging.debug("all runners started")

    task = asyncio.create_task(periodic())

    server = await asyncio.start_server(conman.on_new_connection, HOST, PORT)
    await server.serve_forever()

    end.set()
    await task
