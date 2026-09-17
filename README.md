# LEDMatrix NASCAR Race Leaderboard

Live NASCAR race status plugin for [ChuckBuilds/LEDMatrix](https://github.com/ChuckBuilds/LEDMatrix).

Author: Crazzybrad/BRSStore

It uses NASCAR's public, unauthenticated timing data to show:

- Cup Series, NASCAR O'Reilly Auto Parts Series, and Craftsman Truck Series
- Current lap out of total laps
- Separate favorite drivers for each series by car number, driver id, or name
- Rotating leaderboard positions when no favorite is selected
- Next selected NASCAR race when no selected series is live
- Cached green, yellow, red, and car-number badge icons
- Cached upcoming event logos with generated series badges as a fallback

## Install

From the LEDMatrix web interface, open **Plugins**, then install from a custom GitHub URL

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

Each favorite accepts a car/truck number, NASCAR driver id, or driver name fragment.
Only the active series' favorite is used, so overlapping numbers stay separate.
In `auto` mode, a series with no favorite configured shows the leaderboard.
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
