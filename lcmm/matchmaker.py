import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Generator, Optional
import uuid
import asyncio
from asyncio import Event, Lock, Queue, StreamReader, StreamWriter, subprocess
from dataclasses import dataclass, field
import itertools
import logging


MAX_RUNNING_GAMES = 10
HOST, PORT = "0.0.0.0", 57910
RUNNER = Path("../lost-cities/LostCities.Game/bin/Debug/net7.0/LostCities.Game.exe")

running_games: list["Game"] = []
runners: list["GameRunner"] = []


class ConnectionKey(str):
    pass


@dataclass
class PlayerConnection:
    key: ConnectionKey
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    id_token: str = "unknown"
    max_games_concurrently: int = 0

    previous_enemies: set[ConnectionKey] = field(default_factory=set)
    games_in_progress: int = 0
    _listen_task: Optional[Awaitable] = None
    _event_listeners: set[Callable] = field(default_factory=set)
    _event_listener_tasks: set[Awaitable] = field(default_factory=set)

    def check_live(self):
        if self.reader.at_eof():
            return False
        return True

    def available_for_new_game(self):
        return (
            self._listen_task is not None
            and self.games_in_progress < self.max_games_concurrently
        )

    async def initial_hello(self):
        msg = json.loads(await self.reader.readline())
        if msg["$type"] != "init":
            raise ValueError(f"expected init message first, but got {msg['$type']}")

        self.id_token = msg["token"]
        self.max_games_concurrently = int(msg["max_games_concurrently"])

        logging.debug(f"Bot connected with id_token {self.id_token}")

        self._listen_task = asyncio.create_task(self._listen())

    async def _listen(self):
        while True:
            msg = json.loads(await self.reader.readline())
            self._emit(msg)
            # logging.debug(f"Received from {self.id_token}: {msg}")

    def has_played_against(self, other: "PlayerConnection"):
        return other.key in self.previous_enemies

    def send_json(self, data: dict):
        try:
            # self.writer.write("Hello\n".encode())
            msg = (json.dumps(data) + "\n").encode()
            # logging.debug(f"Sending to {self.id_token}: {msg.decode()}")
            # self.writer.writelines([msg])
            self.writer.write(msg)
        except Exception as e:
            logging.warn(
                f"error when sending message to bot {self.id_token}", exc_info=e
            )

    def add_listener(self, listener):
        self._event_listeners.add(listener)

    def remove_listener(self, listener):
        self._event_listeners.remove(listener)

    def _emit(self, msg):
        for listener in self._event_listeners:
            task = asyncio.create_task(listener(self.key, msg))
            # keep reference to background tasks to prevent GC
            self._event_listener_tasks.add(task)
            task.add_done_callback(self._event_listener_tasks.discard)


class ConnectionManager:
    def __init__(self):
        # connections waiting for an opponent
        self.connections: dict[ConnectionKey, PlayerConnection] = dict()

    async def on_new_connection(self, reader: StreamReader, writer: StreamWriter):
        key = ConnectionKey(str(uuid.uuid4()))
        conn = PlayerConnection(key, reader, writer)
        await conn.initial_hello()
        self.connections[key] = conn

    def delete(self, key):
        self.connections.pop(key)

    async def matchmaking(self):
        free_runners = [runner for runner in runners if not runner.lock.locked()]
        if not len(free_runners) > 0:
            logging.info("Matchmaking: 0 free runners.")
            return
        started_games = 0
        gen = self.potential_games()
        for runner in free_runners:
            try:
                player_one, player_two = next(gen)
                game = Game(player_one, player_two, runner)
                await game.start()
                started_games += 1
            except StopIteration:
                break
        logging.info(
            f"Matchmaking: had {len(free_runners)} free runners. Started {started_games} new games."
        )

    def potential_games(self):
        for player_one, player_two in itertools.combinations(
            self.connections.values(), 2
        ):
            while (
                player_one.available_for_new_game()
                and player_two.available_for_new_game()
            ):
                yield player_one, player_two


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
        self.proc.stdin.write(msg.encode())
        self.proc.stdin.write("\n".encode())

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
            line = (await self.proc.stdout.readline()).decode()
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


class Game:
    def __init__(
        self,
        player_one: PlayerConnection,
        player_two: PlayerConnection,
        runner: GameRunner,
    ):
        self.players: list[PlayerConnection] = [player_one, player_two]
        self.runner = runner
        self.game_id = str(uuid.uuid4())
        self.acceptance = [False, False]
        self.all_accepted = Event()
        self.next_turn_index: Optional[int] = None
        self.bot_message_queue: Queue[tuple[ConnectionKey, str]] = Queue()

    async def start(self):
        running_games.append(self)
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

        running_games.remove(self)
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
            runner = GameRunner()
            runners.append(runner)
            tg.create_task(runner.start())

    logging.debug("all runners started")

    task = asyncio.create_task(periodic())

    server = await asyncio.start_server(conman.on_new_connection, HOST, PORT)
    await server.serve_forever()

    end.set()
    await task
