# Ukrposhta Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-ukrposhta.svg)](https://github.com/ha-parcel-integrations/ha-ukrposhta/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-ukrposhta/total.svg)](https://github.com/ha-parcel-integrations/ha-ukrposhta/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Ukrposhta](https://www.ukrposhta.ua) parcels and packages, domestic and international. No account is needed — you enter the tracking code yourself, just like on the Ukrposhta website.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Ukrposhta parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `at_pickup_point` / `delivered` / …), the carrier's latest status text and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar (stays empty: Ukrposhta reports no delivery windows)
- `ukrposhta.track_parcel` / `ukrposhta.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2024.12 or newer
- A Ukrposhta parcel and its tracking code (from the shipping
  confirmation) — no account needed

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-ukrposhta` as an **Integration**.
3. Install **Ukrposhta** and restart Home Assistant.

### Manual

Copy `custom_components/ukrposhta` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Ukrposhta**. There is nothing to fill in: the hub is created immediately (Ukrposhta tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`ukrposhta.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is on your shipping confirmation.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause).
- A small, fixed per-hub offset is added on top, so not every Ukrposhta
  hub out there polls at exactly the same second.

Ukrposhta reports no delivery window and no out-for-delivery event is mapped
yet, so every parcel in flight is checked on the normal 45-minute cadence. Requests for several tracking codes are spaced out rather
than sent together.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Ukrposhta → ⋮ → Delete**. Nothing is stored on Ukrposhta's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.ukrposhta_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.ukrposhta_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.ukrposhta_next_delivery` | Earliest expected delivery moment across all active parcels (Ukrposhta reports none, so this stays empty) |
| `sensor.ukrposhta_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.ukrposhta_last_successful_update` | Diagnostic: when Ukrposhta was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | Announced / received by Ukrposhta |
| `in_transit` | In the sorting network |
| `at_pickup_point` | Waiting at a post office |
| `delivered` | Delivered |
| `returning` | Refused or undeliverable and going back to the sender; stays here through its final scan |
| `problem` | Ukrposhta reports an exception |
| `unknown` | Not yet scanned, or a status we have not mapped yet |

The latest Ukrposhta event code and text are available as `raw_status`. This integration is still unreleased: more event codes may be added as real parcels confirm them.

## Events

The integration fires these on the event bus (also available as device triggers on the Ukrposhta device):

| Event | When |
|---|---|
| `ukrposhta_parcel_registered` | A new parcel appears in the active list |
| `ukrposhta_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `ukrposhta_parcel_delivered` | A parcel is delivered |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `ukrposhta.track_parcel` | `tracking_code` | Start tracking a parcel |
| `ukrposhta.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.ukrposhta: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Ukrposhta may not have scanned it yet, the code may be wrong, or the latest scan is an event code that is not mapped yet. It will pick up automatically once scanned.
- **All updates suddenly fail** — Ukrposhta may have changed how it serves tracking requests, which needs an integration update. Nothing needs to be re-entered or reauthenticated on your side.
- **A status logs "Unrecognised Ukrposhta status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-ukrposhta/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Ukrposhta, Home Assistant, or any other third party referenced in this project. Please don't contact Ukrposhta for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Ukrposhta's terms. Data is sent only to Ukrposhta's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
