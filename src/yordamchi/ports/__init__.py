"""Ports: one module per external system, holding its Protocols and their value types (CONVENTIONS §4).

Features import ports, never adapters (C4). Every `async def` takes a keyword-only `timeout: float`; an async
iterator takes `idle_timeout: float` (A4). Behaviour values (models, languages, limits) arrive per call from the
caller's settings snapshot; adapter constructors take bootstrap values only.
"""
