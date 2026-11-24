"""Adapt in-memory miner responses to the command submission interface."""

import thermal


class StubCommandSubmission:
    def submit_command(self, command, param=None):
        response = self.ascset(param) if command == "ascset" else self.cmd(command, param)
        return thermal.CommandResult.from_response(response, accept_info=thermal._accepts_info_ack(command, param))
