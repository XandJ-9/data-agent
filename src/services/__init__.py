"""Business use-case layer: the only place transactions are opened.

Each module orchestrates one bounded use case across storage repositories;
transport layers (api, channels, worker) call these, never raw SQL.
"""
