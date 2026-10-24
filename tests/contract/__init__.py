"""Port contract suites (CONVENTIONS §4, §11). Each `<Port>Contract` class holds implementation-agnostic checks;
`Test<Impl>` subclasses bind it to an implementation. The fake side runs offline; a real adapter adds a subclass
(marked `live` when it needs the real service, or fed recorded payloads)."""
