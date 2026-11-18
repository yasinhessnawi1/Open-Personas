"""Built-in ``weather`` MCP server (Spec 27 T9, D-27-2 / D-27-6).

A thin FastMCP Streamable-HTTP server exposing current weather + a short forecast
via **open-meteo** — free, no API key, no signup (D-27-6). The model supplies a
location *name* (not a URL), so there is no SSRF surface: the server only ever
calls the two fixed open-meteo hosts.

**Opt-in** (catalog ``default_enabled = false``): the operator enables it via
``PERSONA_MCP_BUILTIN_ENABLED``. open-meteo's free tier is non-commercial; a
commercial deployment needs a paid open-meteo key or met.no (documented, D-27-6).

Network/lookup failures surface as MCP tool errors (FastMCP maps a raised
exception to an ``isError`` result) — the persona degrades gracefully.

Exposed as ``mcp:weather:get_weather``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from persona.child_env import OUTBOUND_HTTPS_ENV

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["CHILD_ENV", "build", "fetch_weather"]

#: Env names this server needs beyond the shared base (R9-258). It is the one
#: built-in that calls out (HTTPS to open-meteo through httpx, which trusts the
#: env): the CA bundle names, and the proxy names so it still reaches open-meteo
#: where the operator routes egress through a proxy. A proxy URL can carry proxy
#: credentials; that is the one secret-bearing value allowed, because the
#: outbound call needs it. SSLKEYLOGFILE is deliberately left out.
CHILD_ENV: frozenset[str] = OUTBOUND_HTTPS_ENV

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_S = 10.0
_MAX_DAYS = 7

# Condensed WMO weather-code → description map (open-meteo `weather_code`).
_WMO: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def _describe(code: object) -> str:
    if isinstance(code, (int, float)) and not isinstance(code, bool):
        return _WMO.get(int(code), f"code {code}")
    return "unknown conditions"


async def _geocode(client: httpx.AsyncClient, location: str) -> tuple[float, float, str]:
    """Resolve a place name to (lat, lon, resolved_label). Raises on no match."""
    resp = await client.get(
        _GEOCODE_URL, params={"name": location, "count": 1, "language": "en", "format": "json"}
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        msg = f"location not found: {location!r}"
        raise ValueError(msg)
    top = results[0]
    label = ", ".join(p for p in (top.get("name"), top.get("country")) if p)
    return float(top["latitude"]), float(top["longitude"]), label or location


async def fetch_weather(client: httpx.AsyncClient, location: str, days: int) -> str:
    """Geocode ``location``, fetch the forecast, and format a compact report.

    Raises:
        ValueError: location not found.
        httpx.HTTPError: a network / HTTP failure (the tool maps it to is_error).
    """
    days = max(1, min(days, _MAX_DAYS))
    lat, lon, label = await _geocode(client, location)
    resp = await client.get(
        _FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "forecast_days": days,
            "timezone": "auto",
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return _format_report(label, data, days)


def _format_report(label: str, data: dict[str, Any], days: int) -> str:
    units = data.get("current_units", {})
    temp_u = units.get("temperature_2m", "°C")
    cur = data.get("current", {})
    lines = [
        f"Weather for {label}:",
        f"  Now: {cur.get('temperature_2m')}{temp_u}, {_describe(cur.get('weather_code'))}, "
        f"humidity {cur.get('relative_humidity_2m')}%, "
        f"wind {cur.get('wind_speed_10m')}{units.get('wind_speed_10m', 'km/h')}",
    ]
    daily = data.get("daily", {})
    times = daily.get("time", []) or []
    highs = daily.get("temperature_2m_max", []) or []
    lows = daily.get("temperature_2m_min", []) or []
    codes = daily.get("weather_code", []) or []
    for i in range(min(days, len(times))):
        lines.append(
            f"  {times[i]}: {lows[i]}–{highs[i]}{temp_u}, {_describe(codes[i])}"
            if i < len(highs) and i < len(lows) and i < len(codes)
            else f"  {times[i]}: (no data)"
        )
    return "\n".join(lines)


def build(host: str, port: int) -> FastMCP:
    """Build (do not run) the ``weather`` FastMCP server bound to ``host:port``."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("weather", host=host, port=port)

    @server.tool()
    async def get_weather(location: str, days: int = 1) -> str:
        """Get current weather and a short daily forecast for a place by name
        (e.g. 'Oslo' or 'Tokyo, Japan'). ``days`` is the forecast length (1-7).
        Uses open-meteo (no API key). Returns temperatures in the locale's units.
        """
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            try:
                return await fetch_weather(client, location, days)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"weather lookup failed: {exc}") from exc

    return server
