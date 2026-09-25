"""Underlying-bar replay for credit-spread research.

The live bot has no option-tape archive. This package replays the locked
daily + timing-bar entry on stock bars and prices the vertical with a
Black-Scholes credit and a bid/ask fill. It does not submit orders.
"""
