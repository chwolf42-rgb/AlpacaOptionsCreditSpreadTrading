"""VIX history. D2-1 owns the CSV loader."""

from __future__ import annotations


def load_vix_csv(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def prior_close(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def asof_timestamp(*_args, **_kwargs):
    raise NotImplementedError("D2-1")
