import lcmm.matchmaker
import lcmm.gamerunner
import lcmm.connections

running_games: list[lcmm.matchmaker.Game] = []
runners: list[lcmm.gamerunner.GameRunner] = []
conman = lcmm.connections.ConnectionManager()
