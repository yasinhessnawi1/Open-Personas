"""Untrusted document parsing, in a worker process the api can kill (D-FV0-31).

- :mod:`.untrusted`: the parse and page render, plain data in and out.
- :mod:`.worker`: the worker process's entry point.
- :mod:`.protocol`: the framed JSON wire between the two.
- :mod:`.pool`: the api side; starts, limits, queues, kills and recycles workers.

This package's ``__init__`` imports nothing, so the worker process starts light.
"""
