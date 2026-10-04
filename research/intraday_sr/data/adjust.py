"""Adjustment factors. D2-1 owns the loader. S0 does not default a factor to 1."""

from __future__ import annotations


def load_factors(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def load_adj_factors(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def factor_for(*_args, **_kwargs):
    raise NotImplementedError("D2-1")


def as_traded(*_args, **_kwargs):
    raise NotImplementedError("D2-1")
