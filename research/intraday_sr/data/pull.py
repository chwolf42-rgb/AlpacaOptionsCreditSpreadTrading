"""Alpaca pull. D2-1 owns pagination, partial-session re-pulls, and the write refusal."""

from __future__ import annotations


def pull_gaps(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def pull_lock(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def append_request_log(*_args, **_kwargs):
    raise NotImplementedError("D2-1")
