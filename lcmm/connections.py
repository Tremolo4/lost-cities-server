import json
from typing import Awaitable, Callable, Optional
import uuid
import asyncio
from asyncio import StreamReader, StreamWriter
from dataclasses import dataclass, field
import logging

import lcmm.util


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

        self._listen_task = lcmm.util.create_task(self._listen())

    async def _listen(self):
        while True:
            try:
                msg = json.loads(await self.reader.readline())
            except ConnectionError as e:
                logging.info(
                    f"Bot {self.id_token}: Listen task failed: %s: %s",
                    type(e).__name__,
                    str(e),
                )
                logging.info(f"Kicking {self.id_token}")
                lcmm.conman.delete(self.key)
                break
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
            task = lcmm.util.create_task(listener(self.key, msg))
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

    def delete(self, key: ConnectionKey):
        try:
            self.connections.pop(key)
            logging.debug(f"Removed connection {key}")
        except KeyError:
            logging.debug(f"Could not remove connection {key}, maybe already removed.")
