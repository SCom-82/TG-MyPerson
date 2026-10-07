"""Flat route table for tool-name resolution in middleware.

Middleware runs before routing, so the tool name must be found by matching the
request against the routes ourselves. Walking app.routes depends on FastAPI
internals: since 0.137 included routers are wrapped in a private _IncludedRouter
and the old walk silently found nothing (every write passed authz).

The table is built from our own routers' public `.routes` (plain APIRoute objects
whose paths already carry the router's own prefix) plus the prefix each router is
mounted at. Paths are compiled with Starlette's public compile_path, the same
rules the router uses, so matching does not depend on the FastAPI version.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from re import Pattern

from starlette.routing import BaseRoute, compile_path


@dataclass(frozen=True)
class RouteEntry:
    path: str
    methods: frozenset[str]
    name: str
    regex: Pattern[str]
    convertors: dict


class RouteTable:
    def __init__(self, sources: Iterable[tuple[str, Iterable[BaseRoute]]]):
        entries: list[RouteEntry] = []
        for prefix, routes in sources:
            for route in routes:
                path = getattr(route, "path", None)
                methods = getattr(route, "methods", None)
                name = getattr(route, "name", None)
                if path is None or not methods or name is None:
                    continue  # websockets / mounts: not HTTP tools
                full_path = prefix + path
                regex, _, convertors = compile_path(full_path)
                entries.append(RouteEntry(full_path, frozenset(methods), name, regex, convertors))
        self.entries: tuple[RouteEntry, ...] = tuple(entries)

    def resolve(self, method: str, path: str) -> str | None:
        """Name of the first route matching path and method (router order), or None."""
        for entry in self.entries:
            match = entry.regex.match(path)
            if match is None:
                continue
            try:
                for key, value in match.groupdict().items():
                    entry.convertors[key].convert(value)
            except (ValueError, KeyError):
                continue
            if method in entry.methods or (method == "HEAD" and "GET" in entry.methods):
                return entry.name
        return None


# Routes whose resolution proves the table works: a write that must be blocked
# on ro accounts and a read. If either fails to resolve, authz would fail open.
CRITICAL_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("POST", "/api/v1/messages", "send_message"),
    ("GET", "/api/v1/messages", "list_messages"),
)


def verify_route_table(table: RouteTable, critical=CRITICAL_ROUTES) -> None:
    """Refuse to start when key routes don't resolve (fail closed, not open)."""
    broken = [
        f"{method} {path} → {table.resolve(method, path)!r} (expected {name!r})"
        for method, path, name in critical
        if table.resolve(method, path) != name
    ]
    if broken:
        raise RuntimeError(
            "authz route table is broken, refusing to start: " + "; ".join(broken)
        )
