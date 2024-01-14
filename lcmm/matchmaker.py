import itertools
from typing import Optional
import uuid
import asyncio
from asyncio import Event, Queue
import logging

import lcmm.gamerunner, lcmm.util
from lcmm.connections import ConnectionManager, PlayerConnection, ConnectionKey
import lcmm


MAX_RUNNING_GAMES = 10
HOST, PORT = "0.0.0.0", 57910
BOT_RESPONSE_TIMEOUT = 5


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
        self._task = lcmm.util.create_task(self._start())

    def _kick(self, player: PlayerConnection):
        logging.debug(f"{self.game_id}: kicking player {player.key}")
        lcmm.conman.delete(player.key)

    async def _start(self):
        logging.debug(f"Starting game {self.game_id}")
        for conn in self.players:
            conn.add_listener(self.bot_response)

            conn.send_json(
                {"$type": "new_game", "id": self.game_id, "max_turn_time": 10.0}
            )

        logging.debug(f"{self.game_id} Waiting for bots to accept")

        try:
            await asyncio.wait_for(self.all_accepted.wait(), BOT_RESPONSE_TIMEOUT)
        except asyncio.TimeoutError:
            logging.info(f"{self.game_id}: Timeout waiting for bots to accept.")
            for i, accepted in enumerate(self.acceptance):
                if not accepted:
                    self._kick(self.players[i])

            self.stop()
        else:
            self._task = lcmm.util.create_task(self.play())

    def stop(self):
        logging.debug(f"{self.game_id} Ending game")
        for i, player in enumerate(self.players, 1):
            player.games_in_progress -= 1

        lcmm.running_games.remove(self)
        self.runner.lock.release()

    async def play(self):
        while True:
            logging.debug(f"{self.game_id}: Waiting for runner")
            res = await self.runner.query_runner()
            if res.next_turn_index is None:
                break
            logging.debug(f"{self.game_id}: Next turn")
            self.next_turn_index = res.next_turn_index
            current_player = self.players[self.next_turn_index]
            current_player.send_json(
                {"$type": "turn", "id": self.game_id, "view_json": res.payload}
            )
            try:
                logging.debug(f"{self.game_id}: Waiting for bot response")
                _, action_json = await asyncio.wait_for(
                    self.bot_message_queue.get(), BOT_RESPONSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                logging.info(f"{self.game_id}: Timeout of {current_player.id_token}.")
                self.runner.send_response("")
                self._kick(current_player)
                break

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

        self.stop()

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


async def matchmaking(conman: ConnectionManager):
    free_runners = [runner for runner in lcmm.runners if not runner.lock.locked()]
    if not len(free_runners) > 0:
        logging.info("Matchmaking: 0 free runners.")
        return
    started_games = 0
    gen = potential_games(conman)
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


def potential_games(conman: ConnectionManager):
    for player_one, player_two in itertools.combinations(
        conman.connections.values(), 2
    ):
        while (
            player_one.available_for_new_game() and player_two.available_for_new_game()
        ):
            yield player_one, player_two


async def run():
    end = Event()

    async def periodic():
        while True:
            if end.is_set():
                break
            async with asyncio.TaskGroup() as tg:
                tg.create_task(matchmaking(lcmm.conman))
                tg.create_task(asyncio.sleep(1))

    async with asyncio.TaskGroup() as tg:
        for _ in range(MAX_RUNNING_GAMES):
            logging.debug("starting runner")
            runner = lcmm.gamerunner.GameRunner()
            lcmm.runners.append(runner)
            tg.create_task(runner.start())

    logging.debug("all runners started")

    task = lcmm.util.create_task(periodic())

    server = await asyncio.start_server(lcmm.conman.on_new_connection, HOST, PORT)
    await server.serve_forever()

    end.set()
    await task
