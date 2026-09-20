"""
LEDMatrix NASCAR live leaderboard plugin.

The NASCAR live feed reports one active on-track session at a time across the
national series. This plugin filters that feed to the user-enabled series and
renders either a favorite driver's running position or a compact leaderboard.
"""

from __future__ import annotations

import json
import base64
import hashlib
import io
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.plugin_system.base_plugin import BasePlugin

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - LEDMatrix includes Pillow in production
    Image = ImageDraw = ImageFont = None


@dataclass
class DriverRow:
    position: int
    number: str
    name: str
    driver_id: str
    delta: Optional[float]
    laps_completed: Optional[int]
    status: int


class NascarPlugin(BasePlugin):
    """Render live NASCAR lap and leaderboard data on the LED matrix."""

    DEFAULT_LIVE_FEED_URL = "https://cf.nascar.com/live/feeds/live-feed.json"
    FINAL_WINNER_SECONDS = 15.0

    SERIES = {
        "cup": {
            "id": 1,
            "label": "CUP",
            "config_key": "show_cup_series",
            "favorite_key": "favorite_cup_driver",
            "schedule_url": "https://cf.nascar.com/cacher/{year}/1/race_list_basic.json",
        },
        "oreilly": {
            "id": 2,
            "label": "ORLY",
            "config_key": "show_oreilly_series",
            "favorite_key": "favorite_oreilly_driver",
            "schedule_url": "https://cf.nascar.com/cacher/{year}/race_list_basic.json",
        },
        "truck": {
            "id": 3,
            "label": "TRK",
            "config_key": "show_truck_series",
            "favorite_key": "favorite_truck_driver",
            "schedule_url": "https://cf.nascar.com/cacher/{year}/race_list_basic.json",
        },
    }

    ACTIVE_FLAGS = {1, 2, 3, 6, 8}
    FLAG_COLORS = {
        1: (0, 220, 60),
        2: (255, 220, 0),
        3: (235, 35, 35),
        4: (255, 255, 255),
        6: (255, 220, 0),
        8: (230, 230, 230),
    }
    FLAG_LABELS = {
        1: "GREEN",
        2: "YELLOW",
        3: "RED",
        4: "FINAL",
        6: "STOP",
        8: "WARMUP",
        9: "OFF",
    }

    def __init__(
        self,
        plugin_id: str,
        config: Dict[str, Any],
        display_manager: Any,
        cache_manager: Any,
        plugin_manager: Any,
    ) -> None:
        super().__init__(plugin_id, config, display_manager, cache_manager, plugin_manager)
        self.live_feed: Dict[str, Any] = dict(config.get("mock_live_feed") or {})
        self.next_races: Dict[str, Dict[str, Any]] = {}
        self.last_error: Optional[str] = None
        self.last_update_ts: Optional[float] = None
        self._leaderboard_index = 0
        self._leaderboard_seen = 0
        self._favorite_index = 0
        self._favorite_seen = 0
        self._field_index = 0
        self._field_seen = 0
        self._final_race_key: Optional[str] = None
        self._final_started_at: Optional[float] = None
        self._final_favorite_index = 0
        self._icon_memory: Dict[str, Any] = {}

    def validate_config(self) -> bool:
        return bool(self._enabled_series_ids())

    def update(self) -> None:
        if not self.config.get("enabled", True):
            return

        self.last_error = None
        mock_feed = self.config.get("mock_live_feed")
        if isinstance(mock_feed, dict) and mock_feed:
            self.live_feed = mock_feed
        else:
            try:
                live_feed_url = str(self.config.get("live_feed_url") or self.DEFAULT_LIVE_FEED_URL).strip()
                feed = self._fetch_json(live_feed_url)
                if not isinstance(feed, dict):
                    raise ValueError("NASCAR live feed did not return a JSON object")
                self.live_feed = feed
                self._cache_set("live_feed", feed)
            except Exception as exc:  # noqa: BLE001 - update must not kill display rotation
                self.last_error = str(exc)
                cached = self._cache_get("live_feed", max_age=180)
                if isinstance(cached, dict):
                    self.live_feed = cached
                self.logger.warning("NASCAR live feed update failed: %s", exc)

        if not self.has_live_content():
            self._refresh_schedules()

        self.last_update_ts = time.time()

    def display(self, force_clear: bool = False) -> Optional[bool]:
        if not self.config.get("enabled", True):
            return False

        if force_clear:
            self.display_manager.clear()
        else:
            self.display_manager.clear()

        if self._is_selected_live_feed(self.live_feed):
            self._draw_live_feed()
        elif self._is_selected_final_feed(self.live_feed):
            self._draw_final_feed()
        else:
            self._draw_idle()

        self.display_manager.update_display()
        return True

    def has_live_content(self) -> bool:
        return self._is_selected_live_feed(self.live_feed) or self._is_selected_final_feed(self.live_feed)

    def get_live_modes(self) -> List[str]:
        return ["nascar"]

    def get_update_interval(self) -> Optional[float]:
        if self.has_live_content():
            return float(self.config.get("live_update_interval", 15))
        return float(self.config.get("idle_update_interval", 300))

    def get_display_duration(self) -> float:
        return float(self.config.get("display_duration", 12))

    def supports_dynamic_duration(self) -> bool:
        if not self.has_live_content():
            return False
        if self._is_selected_final_feed(self.live_feed):
            return True
        if self._use_leaderboard_mode():
            return True
        favorites = self._favorite_rows()
        return not favorites or len(favorites) > 1 or bool(self._favorite_field_rows(favorites))

    def reset_cycle_state(self) -> None:
        self._leaderboard_index = 0
        self._leaderboard_seen = 0
        self._favorite_index = 0
        self._favorite_seen = 0
        self._field_index = 0
        self._field_seen = 0

    def is_cycle_complete(self) -> bool:
        if self._is_selected_final_feed(self.live_feed):
            self._sync_final_state()
            if self._final_started_at is None:
                return False
            favorite_seconds = max(1.0, self.get_display_duration())
            return time.monotonic() - self._final_started_at >= self.FINAL_WINNER_SECONDS + favorite_seconds
        if self._use_favorite_mode():
            favorites = self._favorite_rows()
            if favorites:
                field_count = len(self._favorite_field_rows(favorites))
                favorites_complete = len(favorites) <= 1 or self._favorite_seen >= len(favorites)
                field_complete = field_count == 0 or self._field_seen >= field_count
                return favorites_complete and field_complete
        rows = self._leaderboard_rows()
        limit = min(len(rows), int(self.config.get("leaderboard_limit", 5)))
        return limit <= 1 or self._leaderboard_seen >= limit

    def get_cycle_duration(self, display_mode: Optional[str] = None) -> Optional[float]:
        del display_mode
        if self._is_selected_final_feed(self.live_feed):
            return self.FINAL_WINNER_SECONDS + max(1.0, self.get_display_duration())
        return None

    def on_config_change(self, new_config: Dict[str, Any]) -> None:
        self.config = new_config
        self.enabled = new_config.get("enabled", True)
        self._leaderboard_index = 0
        self._leaderboard_seen = 0
        self._favorite_index = 0
        self._favorite_seen = 0
        self._field_index = 0
        self._field_seen = 0
        self._final_favorite_index = 0

    def get_info(self) -> Dict[str, Any]:
        return {
            "series": self._series_label(self.live_feed.get("series_id")),
            "live": self.has_live_content(),
            "run_name": self.live_feed.get("run_name"),
            "lap": self.live_feed.get("lap_number"),
            "laps": self.live_feed.get("laps_in_race"),
            "vehicles": len(self.live_feed.get("vehicles") or []),
            "last_error": self.last_error,
        }

    def _draw_live_feed(self) -> None:
        width = int(getattr(self.display_manager.matrix, "width", 128))
        height = int(getattr(self.display_manager.matrix, "height", 32))
        primary = self._rgb("primary_color", (255, 255, 255))
        muted = self._rgb("muted_color", (110, 110, 110))

        header = self._header_text()
        self._draw_flag_header(header, 0, self._flag_color(), width)

        if self._use_favorite_mode():
            favorites = self._favorite_rows()
            if favorites:
                index = self._favorite_index % len(favorites)
                self._draw_favorite(favorites[index], favorites, width, height, primary, muted)
                self._favorite_index = (index + 1) % len(favorites)
                self._favorite_seen += 1
            else:
                self._draw_line("FAV NOT FOUND", self._row_y(1, height), primary, width)
                self._draw_leaderboard_page(width, height, primary, muted, start_y=self._row_y(2, height))
        else:
            self._draw_leaderboard_page(width, height, primary, muted, start_y=self._row_y(1, height))

    def _draw_final_feed(self) -> None:
        self._sync_final_state()
        width = int(getattr(self.display_manager.matrix, "width", 128))
        height = int(getattr(self.display_manager.matrix, "height", 32))
        primary = self._rgb("primary_color", (255, 255, 255))
        muted = self._rgb("muted_color", (110, 110, 110))
        series = self._series_label(self.live_feed.get("series_id"))
        self._draw_flag_header(f"{series} FINAL", 0, self._flag_color(), width)

        elapsed = time.monotonic() - (self._final_started_at or time.monotonic())
        if elapsed < self.FINAL_WINNER_SECONDS:
            rows = self._leaderboard_rows()
            winner = next((row for row in rows if row.position == 1), rows[0] if rows else None)
            if winner:
                self._draw_winner_row(winner, self._row_y(1, height), primary, width)
                self._draw_line("WINNER", self._row_y(2, height), muted, width)
            else:
                self._draw_line("WINNER PENDING", self._row_y(1, height), primary, width)
            return

        favorites = self._favorite_rows()
        if not favorites:
            self._draw_line("NO FAVORITES", self._row_y(1, height), primary, width)
            return

        max_rows = 2 if height <= 32 else 4
        page_start = self._final_favorite_index % len(favorites)
        page = favorites[page_start:page_start + max_rows]
        if len(page) < min(max_rows, len(favorites)):
            page.extend(favorites[: min(max_rows, len(favorites)) - len(page)])
        for offset, row in enumerate(page):
            color = primary if offset == 0 else muted
            self._draw_driver_row(row, self._row_y(offset + 1, height), color, width, include_delta=False)
        self._final_favorite_index = (page_start + len(page)) % len(favorites)

    def _draw_winner_row(
        self,
        row: DriverRow,
        y: int,
        color: Tuple[int, int, int],
        width: int,
    ) -> None:
        """Draw a centered car-number badge followed by the winning driver."""
        name = self._short_name(row.name)
        icon = self._number_icon(row)
        icon_width = int(getattr(icon, "width", 0))
        gap = 2 if icon_width else 0
        fitted_name = self._fit_text(name, max(1, width - icon_width - gap))
        text_width = self._text_width(fitted_name)
        start_x = max(0, (width - icon_width - gap - text_width) // 2)
        self._paste_icon(icon, start_x, y)
        self.display_manager.draw_text(
            fitted_name,
            x=start_x + icon_width + gap,
            y=max(0, int(y)),
            color=color,
            small_font=True,
        )

    def _draw_favorite(
        self,
        row: DriverRow,
        favorites: List[DriverRow],
        width: int,
        height: int,
        primary: Tuple[int, int, int],
        muted: Tuple[int, int, int],
    ) -> None:
        self._draw_driver_row(row, self._row_y(1, height), primary, width, include_delta=False)
        self._draw_field_ticker(favorites, self._row_y(2, height), muted, width)

    @staticmethod
    def _driver_identity(row: DriverRow) -> str:
        return row.driver_id or f"{row.number}:{row.name}"

    def _favorite_field_rows(self, favorites: List[DriverRow]) -> List[DriverRow]:
        favorite_ids = {self._driver_identity(row) for row in favorites}
        return [row for row in self._leaderboard_rows() if self._driver_identity(row) not in favorite_ids]

    def _draw_field_ticker(
        self,
        favorites: List[DriverRow],
        y: int,
        color: Tuple[int, int, int],
        width: int,
    ) -> None:
        rows = self._favorite_field_rows(favorites)
        if not rows:
            self._draw_line("NO OTHER CARS", y, color, width)
            return

        start = self._field_index % len(rows)
        page = rows[start:]
        items: List[str] = []
        used_width = 0
        separator = "  "
        separator_width = self._text_width(separator)
        for candidate in page:
            item = f"P{candidate.position} #{candidate.number}"
            item_width = self._text_width(item)
            added_width = item_width + (separator_width if items else 0)
            if items and used_width + added_width > width:
                break
            items.append(item)
            used_width += added_width

        if not items:
            items = [f"P{rows[start].position} #{rows[start].number}"]
        advance = len(items)
        self._field_index = (start + advance) % len(rows)
        self._field_seen += advance
        self._draw_line(separator.join(items), y, color, width)

    def _draw_leaderboard_page(
        self,
        width: int,
        height: int,
        primary: Tuple[int, int, int],
        muted: Tuple[int, int, int],
        start_y: int,
    ) -> None:
        rows = self._leaderboard_rows()
        if not rows:
            self._draw_line("NO CARS", start_y, primary, width)
            return

        max_rows = 1 if height <= 32 else 3
        configured_limit = int(self.config.get("leaderboard_limit", 5))
        limited = rows[: max(1, configured_limit)]
        page_start = self._leaderboard_index % len(limited)
        page = limited[page_start:page_start + max_rows]
        if len(page) < max_rows:
            page.extend(limited[: max_rows - len(page)])

        for offset, row in enumerate(page):
            y = start_y + offset * self._row_spacing(height)
            color = primary if offset == 0 else muted
            self._draw_driver_row(row, y, color, width, include_delta=width >= 96)

        advance = min(max_rows, len(limited))
        self._leaderboard_index = (page_start + advance) % len(limited)
        self._leaderboard_seen += advance

    def _draw_idle(self) -> None:
        width = int(getattr(self.display_manager.matrix, "width", 128))
        height = int(getattr(self.display_manager.matrix, "height", 32))
        primary = self._rgb("primary_color", (255, 255, 255))
        accent = self._rgb("accent_color", (255, 215, 0))
        muted = self._rgb("muted_color", (110, 110, 110))

        if self.last_error:
            self._draw_line("NASCAR", 0, accent, width)
            self._draw_line("LIVE FEED ERROR", self._row_y(1, height), primary, width)
            self._draw_line("CHECK CONNECTION", self._row_y(2, height), muted, width)
            return

        next_race = self._soonest_next_race()
        if next_race:
            label = self._series_label(next_race.get("series_id"))
            logo = self._event_logo(next_race, height)
            logo_width = int(getattr(logo, "width", 0))
            lines = [
                f"{label} NEXT",
                str(next_race.get("race_name") or "Race"),
                self._format_start(next_race.get("_start_utc") or next_race.get("race_date"))
                or str(next_race.get("track_name") or ""),
            ]
            if height >= 64:
                lines.append(str(next_race.get("track_name") or ""))
            available = max(1, width - logo_width - (3 if logo_width else 0))
            fitted_lines = [self._fit_text(line, available) for line in lines]
            text_width = max((self._text_width(line) for line in fitted_lines), default=0)
            total_width = logo_width + (3 if logo_width and text_width else 0) + text_width
            block_x = max(0, (width - total_width) // 2)
            self._paste_icon(logo, block_x, 0)
            text_x = block_x + logo_width + (3 if logo_width else 0)
            self._draw_line(f"{label} NEXT", 0, accent, width, x=text_x)
            self._draw_line(str(next_race.get("race_name") or "Race"), self._row_y(1, height), primary, width, x=text_x)
            self._draw_line(lines[2], self._row_y(2, height), muted, width, x=text_x)
            if height >= 64:
                self._draw_line(str(next_race.get("track_name") or ""), self._row_y(3, height), muted, width, x=text_x)
            return

        self._draw_line("NASCAR", 0, accent, width)
        self._draw_line("NO LIVE RACE", self._row_y(1, height), primary, width)
        self._draw_line(self._enabled_series_text(), self._row_y(2, height), muted, width)

    def _header_text(self) -> str:
        series = self._series_label(self.live_feed.get("series_id"))
        if self.config.get("show_lap_info", True):
            lap = self.live_feed.get("lap_number")
            total = self.live_feed.get("laps_in_race")
            if lap is not None and total:
                return f"{series} L{lap}/{total}"
        run_name = str(self.live_feed.get("run_name") or "NASCAR")
        return f"{series} {run_name}"

    def _leaderboard_rows(self) -> List[DriverRow]:
        rows: List[DriverRow] = []
        for vehicle in self.live_feed.get("vehicles") or []:
            row = self._vehicle_to_row(vehicle)
            if row and row.position > 0:
                rows.append(row)
        return sorted(rows, key=lambda item: item.position)

    def _vehicle_to_row(self, vehicle: Dict[str, Any]) -> Optional[DriverRow]:
        try:
            driver = vehicle.get("driver") or {}
            full_name = str(
                driver.get("full_name")
                or f"{driver.get('first_name', '')} {driver.get('last_name', '')}".strip()
                or "Driver"
            )
            return DriverRow(
                position=int(vehicle.get("running_position") or 0),
                number=str(vehicle.get("vehicle_number") or "").strip(),
                name=full_name,
                driver_id=str(driver.get("driver_id") or "").strip(),
                delta=self._float_or_none(vehicle.get("delta")),
                laps_completed=self._int_or_none(vehicle.get("laps_completed")),
                status=int(vehicle.get("status") or 0),
            )
        except (TypeError, ValueError):
            return None

    def _favorite_driver(self) -> str:
        series_id = self._int_or_none(self.live_feed.get("series_id"))
        for info in self.SERIES.values():
            if info["id"] == series_id:
                return str(self.config.get(info["favorite_key"]) or "").strip()
        return ""

    def _favorite_entries(self) -> List[List[str]]:
        raw = self._favorite_driver()
        entries: List[List[str]] = []
        for item in raw.replace(";", ",").replace("\n", ",").split(","):
            item = item.strip().lower()
            if not item:
                continue
            aliases = [part.strip() for part in item.split(" - ", 1) if part.strip()]
            entries.append(aliases or [item])
        return entries

    @staticmethod
    def _row_matches(row: DriverRow, aliases: List[str]) -> bool:
        for needle in aliases:
            if needle == row.number.lower() or needle == row.driver_id.lower():
                return True
            if needle in row.name.lower():
                return True
        return False

    def _favorite_rows(self) -> List[DriverRow]:
        rows = self._leaderboard_rows()
        matches: List[DriverRow] = []
        seen = set()
        for aliases in self._favorite_entries():
            match = next((row for row in rows if self._row_matches(row, aliases)), None)
            identity = (match.driver_id or f"{match.number}:{match.name}") if match else ""
            if match and identity not in seen:
                matches.append(match)
                seen.add(identity)
        return matches

    def _favorite_row(self) -> Optional[DriverRow]:
        rows = self._favorite_rows()
        return rows[0] if rows else None

    def _use_favorite_mode(self) -> bool:
        mode = str(self.config.get("display_mode", "auto")).lower()
        favorite = self._favorite_driver()
        return mode == "favorite" or (mode == "auto" and bool(favorite))

    def _use_leaderboard_mode(self) -> bool:
        return not self._use_favorite_mode()

    def _is_selected_live_feed(self, feed: Dict[str, Any]) -> bool:
        if not isinstance(feed, dict):
            return False
        series_id = self._int_or_none(feed.get("series_id"))
        if series_id not in self._enabled_series_ids():
            return False
        vehicles = feed.get("vehicles") or []
        flag_state = self._int_or_none(feed.get("flag_state"))
        return bool(vehicles) and flag_state in self.ACTIVE_FLAGS

    def _is_selected_final_feed(self, feed: Dict[str, Any]) -> bool:
        if not isinstance(feed, dict):
            return False
        series_id = self._int_or_none(feed.get("series_id"))
        if series_id not in self._enabled_series_ids():
            return False
        return bool(feed.get("vehicles") or []) and self._int_or_none(feed.get("flag_state")) == 4

    def _sync_final_state(self) -> None:
        race_key = ":".join(
            str(self.live_feed.get(key) or "")
            for key in ("series_id", "race_id", "run_id", "run_name", "laps_in_race")
        )
        if race_key != self._final_race_key:
            self._final_race_key = race_key
            self._final_started_at = time.monotonic()
            self._final_favorite_index = 0
        elif self._final_started_at is None:
            self._final_started_at = time.monotonic()

    def _enabled_series_ids(self) -> List[int]:
        enabled = []
        for info in self.SERIES.values():
            if self.config.get(info["config_key"], True):
                enabled.append(int(info["id"]))
        return enabled

    def _enabled_series_text(self) -> str:
        labels = [
            str(info["label"])
            for info in self.SERIES.values()
            if self.config.get(info["config_key"], True)
        ]
        return "/".join(labels) if labels else "NO SERIES"

    def _refresh_schedules(self) -> None:
        now_year = datetime.now(timezone.utc).year
        cache_seconds = int(self.config.get("schedule_cache_seconds", 1800))
        for key, info in self.SERIES.items():
            if not self.config.get(info["config_key"], True):
                self.next_races.pop(key, None)
                continue

            cache_key = f"schedule_{now_year}_{key}"
            races = self._cache_get(cache_key, max_age=cache_seconds)
            if not isinstance(races, list):
                try:
                    url = str(info["schedule_url"]).format(year=now_year)
                    payload = self._fetch_json(url)
                    races = self._extract_schedule_list(payload, int(info["id"]))
                    self._cache_set(cache_key, races)
                except Exception as exc:  # noqa: BLE001 - schedule fallback is best effort
                    self.logger.debug("NASCAR schedule update failed for %s: %s", key, exc)
                    races = []

            next_race = self._next_race(races)
            if next_race:
                self.next_races[key] = next_race

    def _extract_schedule_list(self, payload: Any, series_id: int) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [race for race in payload if race.get("series_id") == series_id]
        if isinstance(payload, dict):
            key = f"series_{series_id}"
            races = payload.get(key) or []
            if isinstance(races, list):
                return [race for race in races if race.get("series_id") == series_id]
        return []

    def _next_race(self, races: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        candidates: List[Tuple[datetime, Dict[str, Any]]] = []
        for race in races:
            start = self._race_start(race)
            if start and start >= now:
                item = dict(race)
                item["_start_utc"] = start.isoformat()
                candidates.append((start, item))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1]

    def _race_start(self, race: Dict[str, Any]) -> Optional[datetime]:
        schedule = race.get("schedule") or []
        for event in schedule:
            if event.get("run_type") == 3 and event.get("start_time_utc"):
                parsed = self._parse_datetime(event.get("start_time_utc"))
                if parsed:
                    return parsed
        return self._parse_datetime(race.get("race_date"))

    def _soonest_next_race(self) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        candidates = []
        for race in self.next_races.values():
            start = self._parse_datetime(race.get("_start_utc") or race.get("race_date"))
            if start and start >= now:
                candidates.append((start, race))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1]

    def _fetch_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "LEDMatrix NASCAR Plugin/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - configured public data URL
            raw = response.read()
        return json.loads(raw.decode("utf-8"))

    def _cache_get(self, suffix: str, max_age: int) -> Any:
        try:
            return self.cache_manager.get(f"{self.plugin_id}_{suffix}", max_age=max_age)
        except Exception:  # noqa: BLE001 - cache manager differs in tests/core versions
            return None

    def _cache_set(self, suffix: str, value: Any) -> None:
        try:
            self.cache_manager.set(f"{self.plugin_id}_{suffix}", value)
        except Exception:  # noqa: BLE001
            pass

    def _draw_line(
        self,
        text: str,
        y: int,
        color: Tuple[int, int, int],
        width: int,
        x: Optional[int] = None,
    ) -> None:
        available = width if x is None else max(1, width - x)
        fitted = self._fit_text(text, available)
        if x is None:
            x = max(0, (width - self._text_width(fitted)) // 2)
        self.display_manager.draw_text(fitted, x=x, y=max(0, int(y)), color=color, small_font=True)

    def _flag_icon(self) -> Any:
        state = self._int_or_none(self.live_feed.get("flag_state")) or 0
        key = "flag_checkered" if state == 4 else f"flag_{state}"
        return self._get_icon(key, lambda: self._make_flag_icon(state))

    def _flag_color(self) -> Tuple[int, int, int]:
        state = self._int_or_none(self.live_feed.get("flag_state")) or 0
        return self.FLAG_COLORS.get(state, self._rgb("accent_color", (255, 215, 0)))

    def _draw_flag_icon(self, x: int, y: int) -> int:
        """Draw the current race flag from a persistent cached PNG."""
        return self._paste_icon(self._flag_icon(), x, y)

    def _draw_flag_header(
        self,
        text: str,
        y: int,
        color: Tuple[int, int, int],
        width: int,
    ) -> None:
        icon = self._flag_icon()
        icon_width = int(getattr(icon, "width", 0))
        gap = 2 if icon_width else 0
        fitted = self._fit_text(text, max(1, width - icon_width - gap))
        text_width = self._text_width(fitted)
        start_x = max(0, (width - icon_width - gap - text_width) // 2)
        self._paste_icon(icon, start_x, y)
        self.display_manager.draw_text(
            fitted,
            x=start_x + icon_width + gap,
            y=max(0, int(y)),
            color=color,
            small_font=True,
        )

    def _number_icon(self, row: DriverRow) -> Any:
        series_id = self._int_or_none(self.live_feed.get("series_id")) or 0
        key = f"number_{series_id}_{row.number}"
        return self._get_icon(key, lambda: self._make_number_icon(row.number, series_id))

    def _draw_number_icon(self, row: DriverRow, x: int, y: int) -> int:
        """Draw a compact car-number badge keyed to series and number."""
        return self._paste_icon(self._number_icon(row), x, y)

    def _draw_driver_row(
        self,
        row: DriverRow,
        y: int,
        color: Tuple[int, int, int],
        width: int,
        include_delta: bool,
    ) -> None:
        """Draw a centered ``position, number badge, driver`` row."""
        position = f"P{row.position}"
        name = self._short_name(row.name)
        if include_delta and self.config.get("show_delta", True) and row.delta not in (None, 0):
            name = f"{name} +{row.delta:.1f}"

        icon = self._number_icon(row)
        icon_width = int(getattr(icon, "width", 0))
        position_width = self._text_width(position)
        first_gap = 2
        second_gap = 2 if icon_width else 0
        fixed_width = position_width + first_gap + icon_width + second_gap
        fitted_name = self._fit_text(name, max(1, width - fixed_width))
        name_width = self._text_width(fitted_name)
        total_width = fixed_width + name_width
        start_x = max(0, (width - total_width) // 2)

        self.display_manager.draw_text(
            position,
            x=start_x,
            y=max(0, int(y)),
            color=color,
            small_font=True,
        )
        icon_x = start_x + position_width + first_gap
        self._paste_icon(icon, icon_x, y)
        self.display_manager.draw_text(
            fitted_name,
            x=icon_x + icon_width + second_gap,
            y=max(0, int(y)),
            color=color,
            small_font=True,
        )

    def _event_logo(self, race: Dict[str, Any], height: int) -> Any:
        """Load a cached upcoming event logo or generated event badge."""
        series_id = self._int_or_none(race.get("series_id")) or 0
        race_id = str(race.get("race_id") or race.get("race_name") or "event")
        size = 30 if height >= 64 else 22
        identity = f"{series_id}:{race_id}:{self._event_logo_url(race) or 'generated'}:{size}"
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
        key = f"event_logo_{digest}"
        return self._get_icon(key, lambda: self._load_event_logo(race, size))

    def _draw_event_logo(self, race: Dict[str, Any], x: int, y: int, height: int) -> int:
        """Draw and cache an upcoming event logo or generated event badge."""
        return self._paste_icon(self._event_logo(race, height), x, y)

    def _event_logo_url(self, race: Dict[str, Any]) -> str:
        template = str(self.config.get("event_logo_url_template") or "").strip()
        if template:
            try:
                return template.format(
                    race_id=race.get("race_id", ""),
                    series_id=race.get("series_id", ""),
                    year=datetime.now(timezone.utc).year,
                )
            except (KeyError, ValueError):
                self.logger.warning("Invalid NASCAR event logo URL template")

        for key in ("event_logo_url", "race_logo_url", "logo_url", "image_url"):
            value = race.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for parent_key in ("event", "race", "logo", "image"):
            parent = race.get(parent_key)
            if isinstance(parent, dict):
                for key in ("url", "image_url", "logo_url"):
                    value = parent.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return ""

    def _load_event_logo(self, race: Dict[str, Any], size: int) -> Any:
        if Image is None:
            return None
        url = self._event_logo_url(race)
        if url:
            image = self._fetch_image(url, size)
            if image is not None:
                return image
        return self._make_event_badge(self._series_label(race.get("series_id")), size)

    def _fetch_image(self, url: str, size: int) -> Any:
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "image/png,image/jpeg,image/webp,image/svg+xml", "User-Agent": "LEDMatrix NASCAR Plugin/0.2"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310 - configured public data URL
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("event logo exceeds 4 MB")
            image = Image.open(io.BytesIO(raw)).convert("RGBA")
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            return image
        except Exception as exc:  # noqa: BLE001 - generated badge is the fallback
            self.logger.debug("Could not load NASCAR event logo %s: %s", url, exc)
            return None

    def _make_event_badge(self, label: str, size: int) -> Any:
        icon = Image.new("RGBA", (size, size), (12, 12, 12, 255))
        draw = ImageDraw.Draw(icon)
        border = {"CUP": (235, 235, 235, 255), "ORLY": (60, 170, 255, 255), "TRK": (255, 150, 40, 255)}.get(
            label, (180, 180, 180, 255)
        )
        draw.rectangle((0, 0, size - 1, size - 1), outline=border)
        draw.rectangle((2, size - 5, size - 3, size - 3), fill=border)
        draw.rectangle((2, size - 5, 4, size - 3), fill=(255, 255, 255, 255))
        draw.rectangle((6, size - 5, 8, size - 3), fill=(255, 255, 255, 255))
        font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), label, font=font)
        text_width = bbox[2] - bbox[0]
        draw.text(((size - text_width) // 2, max(1, (size // 2) - 4)), label, font=font, fill=(255, 255, 255, 255))
        return icon

    def _get_icon(self, key: str, factory: Any) -> Any:
        if Image is None:
            return None
        if key in self._icon_memory:
            return self._icon_memory[key]

        encoded = self._cache_get(f"icon_{key}", max_age=2592000)
        if isinstance(encoded, str):
            try:
                icon = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGBA")
                self._icon_memory[key] = icon
                return icon
            except Exception:  # noqa: BLE001 - regenerate a damaged cache entry
                pass

        icon = factory()
        if icon is None:
            return None
        self._icon_memory[key] = icon
        try:
            buffer = io.BytesIO()
            icon.save(buffer, format="PNG", optimize=True)
            self._cache_set(f"icon_{key}", base64.b64encode(buffer.getvalue()).decode("ascii"))
        except Exception:  # noqa: BLE001 - display remains useful without disk cache
            self.logger.debug("Could not cache NASCAR icon %s", key, exc_info=True)
        return icon

    def _make_flag_icon(self, state: int) -> Any:
        if Image is None:
            return None
        colors = {
            1: (0, 220, 60, 255),
            2: (255, 220, 0, 255),
            3: (235, 35, 35, 255),
            6: (255, 220, 0, 255),
            8: (230, 230, 230, 255),
        }
        color = colors.get(state, (110, 110, 110, 255))
        icon = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        draw.rectangle((1, 1, 2, 7), fill=(255, 255, 255, 255))
        if state == 4:
            for y in range(1, 6):
                for x in range(3, 8):
                    square = (255, 255, 255, 255) if (x + y) % 2 else (0, 0, 0, 255)
                    draw.point((x, y), fill=square)
        else:
            draw.polygon(((3, 1), (7, 2), (3, 4)), fill=color)
        draw.rectangle((0, 7, 4, 7), fill=(255, 255, 255, 255))
        return icon

    def _make_number_icon(self, number: str, series_id: int) -> Any:
        if Image is None:
            return None
        number = number or "?"
        width = max(12, min(22, 6 + len(number) * 6))
        # Series-specific border colors keep identical numbers distinguishable
        # when a feed or replay contains multiple national series.
        border = {1: (235, 235, 235, 255), 2: (60, 170, 255, 255), 3: (255, 150, 40, 255)}.get(
            series_id, (180, 180, 180, 255)
        )
        icon = Image.new("RGBA", (width, 9), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        draw.rounded_rectangle((0, 0, width - 1, 8), radius=1, fill=(20, 20, 20, 255), outline=border)
        font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), number, font=font)
        text_width = bbox[2] - bbox[0]
        draw.text(((width - text_width) // 2, -1), number, font=font, fill=(255, 255, 255, 255))
        return icon

    def _paste_icon(self, icon: Any, x: int, y: int) -> int:
        if icon is None:
            return 0
        width = int(icon.width)
        canvas = getattr(self.display_manager, "image", None)
        if canvas is not None and hasattr(canvas, "paste"):
            try:
                canvas.paste(icon, (int(x), int(y)), icon)
            except Exception:  # noqa: BLE001 - text fallback keeps older cores working
                self.logger.debug("Could not draw NASCAR icon", exc_info=True)
        return width

    def _fit_text(self, text: str, width: int) -> str:
        text = " ".join(str(text).split())
        if self._text_width(text) <= width:
            return text
        max_chars = max(3, int(width) // 8)
        candidate = text[:max_chars - 1].rstrip() + "~"
        while len(candidate) > 1 and self._text_width(candidate) > width:
            candidate = candidate[:-2].rstrip() + "~"
        return candidate

    def _text_width(self, text: str) -> int:
        getter = getattr(self.display_manager, "get_text_width", None)
        font = getattr(self.display_manager, "small_font", None)
        if callable(getter) and font is not None:
            try:
                return int(getter(text, font))
            except Exception:  # noqa: BLE001 - fall back to a safe estimate
                pass
        return len(text) * 8

    def _row_y(self, row: int, height: int) -> int:
        return row * self._row_spacing(height)

    def _row_spacing(self, height: int) -> int:
        if height <= 32:
            return 10
        if height <= 48:
            return 11
        return 12

    def _series_label(self, series_id: Any) -> str:
        series_id_int = self._int_or_none(series_id)
        for info in self.SERIES.values():
            if info["id"] == series_id_int:
                return str(info["label"])
        return "NASCAR"

    def _flag_label(self) -> str:
        flag = self._int_or_none(self.live_feed.get("flag_state"))
        return self.FLAG_LABELS.get(flag, "LIVE")

    def _short_name(self, name: str) -> str:
        clean_name = re.sub(r"\s*\(C\)\s*", " ", str(name), flags=re.IGNORECASE).strip()
        parts = [part for part in clean_name.replace(".", "").split() if part]
        if not parts:
            return "Driver"
        if len(parts) == 1:
            return parts[0].upper()
        return f"{parts[0][0].upper()} {parts[-1].upper()}"

    def _format_start(self, value: Any) -> str:
        parsed = self._parse_datetime(value)
        if not parsed:
            return ""
        return parsed.astimezone().strftime("%a %-I:%M %p")

    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        if not value:
            return None
        text = str(value)
        if text.startswith("1900-01-01"):
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _rgb(self, key: str, fallback: Tuple[int, int, int]) -> Tuple[int, int, int]:
        value = self.config.get(key, fallback)
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            try:
                return tuple(max(0, min(255, int(part))) for part in value[:3])  # type: ignore[return-value]
            except (TypeError, ValueError):
                pass
        return fallback

    def _float_or_none(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _int_or_none(self, value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
