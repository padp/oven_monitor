"""Thin pylogix wrapper: batched reads, explicit connect/close.

One instance per oven - each owns its own IP and its own socket, so a
reconnect on one oven doesn't disturb the other.

Reconnect-on-repeated-failure is handled by the caller (collector.py),
which owns the retry/backoff loop and can swap in a fresh PlcClient.
"""
from pylogix import PLC


class PlcClient:
    def __init__(self, ip_address: str):
        self.ip_address = ip_address
        self._plc = PLC()
        self._plc.IPAddress = ip_address

    def read_all(self, tag_names: list) -> dict:
        """Read a list of tags in one batched call.

        Returns {tag_name: value}. A tag-level failure (e.g. a momentarily
        unavailable tag, or one of the UDT member paths that turns out to
        be program-scoped) yields None for that tag rather than raising,
        so one bad tag doesn't take down the whole poll. A
        connection-level failure (PLC unreachable) raises, for the
        caller's retry/backoff loop to handle.
        """
        if not tag_names:
            return {}
        results = self._plc.Read(tag_names)
        # pylogix returns a bare Response (not a list) for a single tag.
        if not isinstance(results, list):
            results = [results]
        return {r.TagName: (r.Value if r.Status == "Success" else None) for r in results}

    def close(self):
        self._plc.Close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
