"""Owned per-session protocol and application counters."""

from dataclasses import fields

from .observations import Statistics


class _StatisticsState:
    """Record ordered observations, preserving detached public snapshots."""

    def __init__(self):
        self.totals = {
            field.name: 0
            for field in fields(Statistics)
            if not field.name.startswith(("connected_bytes_", "packet_counts_", "play_packet_counts_"))
        }
        self.packet_counts_in = [0] * 1024
        self.packet_counts_out = [0] * 1024
        self.play_packet_counts_in = [0] * 1024
        self.play_packet_counts_out = [0] * 1024

    def increment(self, name, amount=1):
        self.totals[name] += amount

    def incoming_wire(self, size):
        self.totals["batch_bytes_in"] += size

    def incoming_batch(self, count, headers):
        self.totals["packets_in"] += count
        for header in headers:
            self.packet_counts_in[header.packet_id] += 1

    def incoming_play_packet(self, packet_id):
        self.play_packet_counts_in[packet_id] += 1

    def outgoing_batch(self, wire_size, commands):
        # Account only after connection.send() returns successfully.
        self.totals["batch_bytes_out"] += wire_size
        self.totals["packets_out"] += len(commands)
        for command in commands:
            self.packet_counts_out[command.token.packet_id] += 1
            if command.play:
                self.play_packet_counts_out[command.token.packet_id] += 1

    def capture(self):
        return Statistics(
            **self.totals,
            packet_counts_in=self.packet_counts_in,
            packet_counts_out=self.packet_counts_out,
            play_packet_counts_in=self.play_packet_counts_in,
            play_packet_counts_out=self.play_packet_counts_out,
        )
