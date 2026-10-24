"""One fake per port (SPEC §8; CONVENTIONS §4, §11). Each is checked against its port's contract suite in
`tests/contract/` and against the Protocol by mypy (the `_: Port = Fake()` lines at the bottom of each module)."""
