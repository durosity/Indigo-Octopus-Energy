# Octopus Energy Plugin for Indigo Domotics

Monitor your Octopus Energy consumption, rates, account balance, and tariff changes in real-time from Indigo. Built with EV charging automation in mind for time-of-use tariffs like Agile and Intelligent Go.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Indigo](https://img.shields.io/badge/Indigo-2025.1%2B-blue.svg)](https://www.indigodomo.com/)

---

## Features

### Real-time monitoring
- Current electricity and gas rates, updated automatically
- Half-hourly consumption tracking from smart meters
- Dual-register meter support (Economy 7, Intelligent Go)
- Export meter support for solar generation
- Account balance, payment method, next payment date and amount

### EV charging optimisation
- Automatic detection of cheap rate periods below a user-defined threshold
- Triggers when rates cross thresholds, change period, or go negative
- Next-rate preview so automations can act ahead of time
- Designed for Octopus Agile and Intelligent Go

### Tariff awareness
- Detects when your tariff changes (e.g. cheaper Agile rates appearing)
- Fires triggers when a fixed tariff is ending soon

### Historic consumption
- Optional historic meter devices for trend analysis and reporting

## Requirements

- Indigo 2025.1 or later
- Octopus Energy UK account with at least one smart meter
- Octopus Energy API key (free, available from your account)

## Installation

1. Download the latest `OctopusEnergy.indigoPlugin` from the [Releases](../../releases) page
2. Double-click the file — Indigo will install and prompt you to enable it
3. Open **Plugins → Octopus Energy → Configure...** and paste your API key
4. Create your account device (see Quick Start below)

## Getting your API key

1. Log into your Octopus account in a browser
2. Go to <https://octopus.energy/dashboard/new/accounts/personal-details/api-access>
3. Copy the API key (it starts with `sk_live_`)
4. Paste it into Plugin Configuration in Indigo

## Quick start

### 1. Configure the plugin
Open **Plugins → Octopus Energy → Configure...**, paste your API key, click OK.

### 2. Create your account device
**Devices → New → Octopus Energy → Octopus Account**, enter your account number (format `A-XXXXXXXX`).

### 3. Discover your meters
On the account device's config dialog, click **Discover Meters**. The plugin will create devices for each electricity and gas meter on your account.

### 4. Set up triggers
**Triggers → New → Octopus Energy** — choose from the available trigger types and point them at your meter devices.

## Device types

| Device | Purpose |
|---|---|
| Octopus Account | Account-level info: balance, payments, meter count |
| Electricity Meter | Live rates, consumption, cheap-rate state |
| Gas Meter | Live rates and consumption |
| Export Meter | Solar export rates and generation |
| Historic (Elec/Gas/Export) | Optional devices for long-term consumption trends |

## Triggers

- Cheap period start / end
- Rate changed
- Rate above / below threshold
- High consumption
- Negative rate (Agile)
- Half-hourly period change
- Tariff changed
- Tariff ending soon

## Actions

- Discover and create meters (account device)
- Refresh rates (electricity meter)
- Refresh consumption (any meter)

## Configuration tips

**Cheap rate threshold** — set on each electricity meter to the pence-per-kWh price below which the plugin considers electricity "cheap". Cheap-period triggers fire when current rates cross this value.

**Polling intervals** — configurable in plugin preferences. The defaults are conservative to stay within Octopus's API rate limits.

**Debug logging** — toggle in plugin preferences when troubleshooting.

## Example automations

**Charge the EV when rates go cheap:**
Trigger on *Cheap Period Start* on your electricity meter → action: enable your EV charger.

**Run the dishwasher overnight on Agile:**
Trigger on *Rate Below Threshold* with threshold = 10p → action: send command to your smart plug.

**Alert when a fixed tariff is about to end:**
Trigger on *Tariff Ending Soon* → action: send a notification.

## Troubleshooting

**Plugin won't authenticate** — verify your API key is the new-style key from the API access page (starts with `sk_live_`). Re-copy it into the plugin config; leading/trailing whitespace can cause issues.

**No meters discovered** — make sure your account number is in the format `A-XXXXXXXX` (with the dash). The "Discover Meters" button will only work once a valid API key is saved.

**HTTP 400 errors related to `savingSessions`** — see Known Limitations below.

**Other issues** — enable debug logging in plugin preferences and check the Indigo log. Please [open an issue](../../issues) with the relevant log lines.

## Known limitations

- Octopus removed the top-level `savingSessions` GraphQL query in late 2025. Saving Sessions tracking will return no data until the plugin migrates to the newer `customerFlexibilityCampaignEvents` API. The plugin handles this gracefully — no log spam — but the Saving Session states won't update for now.
- UK Octopus accounts only. Other Kraken-powered suppliers (Octopus US, etc.) are not currently supported.

## Contributing

Pull requests welcome. Please:
- Use `<Name>` tags (not `<n>`) in XML
- Increment the plugin version by 0.0.1 per build
- Test against your own Octopus account before submitting

## Credits

Built by Mike (Durosity). GraphQL approach informed by [BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) and [caliston/octopus-saving-sessions](https://github.com/caliston/octopus-saving-sessions).

## License

MIT — see [LICENSE](LICENSE).
