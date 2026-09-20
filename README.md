# LEDMatrix NASCAR Race Leaderboard

Live NASCAR race status plugin for [ChuckBuilds/LEDMatrix](https://github.com/ChuckBuilds/LEDMatrix).

Author: Crazzybrad

It uses NASCAR's public, unauthenticated timing data to show:

- Cup Series, NASCAR O'Reilly Auto Parts Series, and Craftsman Truck Series
- Current lap out of total laps
- Separate favorite drivers for each series by car number, driver id, or name
- Rotating leaderboard positions when no favorite is selected
- Next selected NASCAR race when no selected series is live
- Flag-colored live headers with cached green, yellow, red, and checkered icons
- Cached upcoming event logos with generated series badges as a fallback
- A race-final sequence that shows the winner for 15 seconds, then favorite-driver finishing positions

## Install

From the LEDMatrix web interface, open **Plugins**, then install from a custom GitHub URL once this folder is published as its own repo.

For local development against an LEDMatrix checkout:

```bash
cd /path/to/LEDMatrix
./scripts/dev/dev_plugin_setup.sh link ledmatrix-nascar /path/to/ledmatrix-nascar
```

The LEDMatrix production default scans `plugin-repos/`. For development, either switch `plugin_system.plugins_directory` to `plugins` in the web UI or copy/link this plugin into `plugin-repos/`.

## Configure

The generated plugin tab exposes:

- Series toggles for Cup, O'Reilly, and Trucks
- `display_mode`: `auto`, `favorite`, or `leaderboard`
- `favorite_cup_driver`: favorite Cup driver
- `favorite_oreilly_driver`: favorite O'Reilly driver
- `favorite_truck_driver`: favorite Truck driver
- Live and idle update intervals
- Leaderboard size and text colors
- Optional `event_logo_url_template` for a race logo service or hosted image

Each favorite accepts comma-separated car/truck numbers, NASCAR driver ids, or
driver name fragments. For example, O'Reilly favorites Jesse Love and Austin
Hill can be entered as `2, 21` or `2 - J. Love, 21 - A. Hill`. The combined
labels are accepted, but entering just the numbers is simplest.
Only the active series' favorite is used, so overlapping numbers stay separate.
In `auto` mode, a series with no favorite configured shows the leaderboard.
Favorite mode centers the selected driver's position, number badge, and name on
the middle row. The bottom row rotates through the remaining field as compact
position and car-number pairs. NASCAR's `(C)` Chase marker is omitted from the
displayed driver name.
During a live race, the series and lap header uses the current flag color along
with its matching flag icon. At the finish, the plugin shows a checkered flag,
the winning car number and driver for 15 seconds, then the configured favorites'
final positions for the remainder of the display cycle.
When upgrading from the single `favorite_driver` setting, enter your favorites
in the new series fields; the old shared setting is no longer used.

Flag icons are generated as small pixel graphics and stored as PNG data through
LEDMatrix's persistent cache. Number badges are keyed by series and car number,
so a number reused in another national series gets a separate cached badge.
Upcoming event images are decoded, resized, and cached as PNG data. NASCAR's
basic schedule feed does not consistently publish event artwork, so the plugin
uses a cached Cup, O'Reilly, or Truck event badge when no image URL is present.

## Data Sources

- Live timing: `https://cf.nascar.com/live/feeds/live-feed.json`
- Schedule fallback:
  - Cup: `https://cf.nascar.com/cacher/{year}/1/race_list_basic.json`
  - O'Reilly and Trucks: `https://cf.nascar.com/cacher/{year}/race_list_basic.json`

NASCAR currently identifies national series as Cup `1`, O'Reilly `2`, and Trucks `3`.

If the live endpoint cannot be reached, the matrix shows `LIVE FEED ERROR`
instead of presenting a stale scheduled race as upcoming. Leaving
`live_feed_url` blank safely restores the default NASCAR endpoint.
