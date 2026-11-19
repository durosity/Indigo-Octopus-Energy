# Octopus Energy Plugin for Indigo Domotics

Bring your Octopus Energy (UK) account into Indigo: live tariff rates, smart meter consumption and costs, live "today" usage from an Octopus Home Mini, account balance, tariff change alerts, and day-by-day historic devices. Built with EV charging automation in mind for time-of-use tariffs like Octopus Go, Intelligent Go and Agile.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Indigo](https://img.shields.io/badge/Indigo-2025.1%2B-blue.svg)](https://www.indigodomo.com/)

---

## Features

### Rates and costs
- Current and next electricity rate, with time of the next rate change
- Cheap-period detection against a per-meter threshold, with start/end times
- Negative-rate detection (Agile)
- Dual-register support (Economy 7, Intelligent Go) with day/night rates
- Gas rates and standing charges
- Costs calculated against the actual half-hourly rates in force, not a flat average

### Consumption
- **Yesterday** and **month to date** kWh and cost for electricity and gas, with and without standing charge
- **Today** kWh and cost, updated every 5 minutes, when you have an Octopus Home Mini
- Live electricity demand in watts from the Home Mini
- Export meter rates and generation for solar
- Automatic SMETS1/SMETS2 gas detection with m³ → kWh conversion

### Historic devices
- One device per month with day-by-day kWh and cost (`day01kWh` … `day31cost`) plus monthly totals
- Create as many months back as you like from the meter's config dialog
- Ideal for control pages, graphs and month-on-month comparisons

### Account and tariffs
- Account balance, payment method, next payment date and amount
- Detects tariff changes and records the previous tariff
- Warns when a fixed tariff is ending soon

### Reliability
- Correct across the GMT/BST clock changes (23 and 25 hour days are handled properly)
- Automatic retries with backoff for timeouts, rate limiting and server errors
- Rate lookups cached to keep API usage low
- Network problems logged as single warnings rather than error spam

## Requirements

- Indigo 2025.1 or later
- An Octopus Energy UK account with at least one smart meter
- An Octopus API key (free)
- Optional: an Octopus Home Mini for live "today" figures

## Installation

1. Download `OctopusEnergy_indigoPlugin.zip` from the latest [release](../../releases)
2. Unzip it and double-click `OctopusEnergy.indigoPlugin`
3. Indigo will install the plugin and offer to enable it

### Upgrading from 2.2.18 or earlier

Version 2.2.19 changed the plugin ID from `com.indigodomo.octopusenergy` to `com.durosity.indigo.octopusenergy`. Indigo treats this as a new plugin, so existing devices, triggers and plugin settings do not carry over:

1. Note any triggers, control pages and scripts that use the Octopus devices
2. Disable the old plugin, then rename or delete its devices (device names must be unique)
3. Install the new version, enter your API key and set up your devices again as described below
4. Point your triggers, control pages and scripts at the new devices

To keep your plugin settings, quit Indigo or keep the new plugin disabled and copy `com.indigodomo.octopusenergy.indiPref` to `com.durosity.indigo.octopusenergy.indiPref` in Indigo's `Preferences/Plugins` folder before first enabling it.

## Getting your API key

1. Log into your Octopus account in a browser
2. Go to <https://octopus.energy/dashboard/new/accounts/personal-details/api-access>
3. Copy the API key (it starts with `sk_live_`)

The same key is used for both the REST API and GraphQL. No email or password is needed.

## Setup

### 1. Configure the plugin
**Plugins → Octopus Energy → Configure...**

| Setting | Description |
|---|---|
| API Key | Your `sk_live_…` key |
| I have an Octopus Home Mini | Enables live "today" data and live demand (see below) |
| Enable debug logging | Verbose logging for troubleshooting |

### 2. Create the account device
**Devices → New → Octopus Energy → Octopus Account**

1. Click **Auto-Discover Account**, or type your account number (`A-XXXXXXXX`) if discovery returns a 403. You'll find it on your bill or at the top of your Octopus dashboard.
2. Set a **Default Cheap Rate Threshold** in p/kWh. Typical values are 7.5–8p for Octopus Go and 10–12p for Agile.
3. Click **Discover & Create All Meters**. The plugin creates an Electricity, Gas and/or Export Meter device for each meter on the account, with the MPAN/MPRN, serial number, product code and tariff code already filled in.

You can also run discovery later with the **Discover & Create Meters** action.

### 3. Enable the Home Mini (optional)
If you have a working Octopus Home Mini, tick **I have an Octopus Home Mini** in the plugin configuration. Within a few minutes the log will show a line like this for each meter:

```
• Energy - Octopus Electricity - 22J0000000: Home Mini telemetry found (device 00-1C-55-00-00-00-00-00)
```

From then on the meter's today states are updated every 5 minutes. The account device must exist, because the meters' smart device IDs are looked up through your account.

### 4. Create historic devices (optional)
Open a meter device's config dialog and use the **Historic Month Devices** section:

| Setting | Description |
|---|---|
| Name Template | Device name, using `strftime` codes, e.g. `Octopus Electric History %B %Y` |
| Number of Months | How many months to create, counting back from the current month |

Click **Create Historic Devices**. Each historic device has:

| Setting | Description |
|---|---|
| Linked Meter | The meter it reads from |
| Months Prior | `0` = this month (up to yesterday), `1` = last month, and so on |
| Include Standing Charge | Add the daily standing charge to each day's cost |

Historic devices refresh shortly after midnight each day.

## How "today", "yesterday" and "month to date" work

Octopus's REST API only publishes consumption once a day is complete, and often several hours later. So:

- **Yesterday** and **month to date** come from the REST API and update every 30 minutes until the data arrives. Month to date runs up to the end of yesterday.
- **Today** cannot come from the REST API. Without a Home Mini the today states stay at 0. With a Home Mini they come from Octopus's live telemetry and update every 5 minutes.
- If **yesterday** shows 0 in the morning, Octopus hasn't received that day's readings yet. It normally fills in later in the day.

## Device types

| Device | Purpose |
|---|---|
| Octopus Account | Balance, payments, meter count, Saving Sessions |
| Electricity Meter | Rates, cheap periods, consumption, costs, tariff info, live demand |
| Gas Meter | Rates, consumption, costs, tariff info |
| Export Meter | Export rate and generation |
| Electricity / Gas / Export Meter (Historic) | Day-by-day figures for one month |

### Key electricity meter states

| State | Description |
|---|---|
| `currentRate`, `nextRate`, `nextRateChange` | Current and upcoming unit rate (p/kWh) |
| `inCheapPeriod`, `cheapPeriodStart`, `cheapPeriodEnd` | Cheap-period status and times |
| `currentPeriod`, `dayRate`, `nightRate`, `isDualRate` | Dual-register details |
| `isNegativeRate` | True when the current rate is below zero |
| `kWhToday`, `costToday`, `costTodayWithStanding` | Today's usage (Home Mini) |
| `liveDemand` | Current demand in watts (Home Mini) |
| `kWhYesterday`, `costYesterday`, `costYesterdayWithStanding` | Yesterday's usage |
| `kWhMonthToDate`, `costMonthToDate`, `costMonthToDateWithStanding` | Month to date |
| `standingCharge` | Daily standing charge (p/day) |
| `currentTariff`, `tariffValidFrom`, `tariffValidTo` | Current tariff |
| `tariffEndingSoon`, `upcomingTariff` | Tariff renewal info |
| `tariffChanged`, `previousTariffCode`, `previousProductCode`, `tariffChangeDate` | Tariff change history |
| `homeMiniDeviceId`, `homeMiniLastReading` | Home Mini link and last live reading |

Gas meters have the same consumption, cost and tariff states (without the cheap-period, dual-rate and demand states), plus `meterTypeDetected`.

`todayTotal` and `todayCost` are kept for backwards compatibility and mirror `kWhToday` and `costToday`.

## Triggers

All triggers are under **Triggers → New → Octopus Energy** and are tied to a specific device.

| Trigger | Fires when |
|---|---|
| Cheap Rate Period Starting | The rate drops below the meter's cheap threshold |
| Cheap Rate Period Ending | The rate rises back above the threshold |
| Rate Changed | The unit rate changes by more than 0.5p/kWh |
| Rate Below Specific Value | The rate drops below a value you set |
| Rate Above Specific Value | The rate rises above a value you set |
| Negative Rate (Free Electricity) | The rate goes below zero |
| Rate Period Changed | A new rate period starts |
| High Consumption Detected | Today's usage passes a kWh threshold (needs Home Mini). Fires once per day |
| Tariff Changed | Your tariff changes |
| Tariff Ending Soon | A fixed tariff is nearing its end date |
| Saving Session Starting / Ending | A joined Saving Session starts or ends |
| New Saving Session Announced | A new Saving Session appears |

## Actions

| Action | Description |
|---|---|
| Discover & Create Meters | Create devices for any meters on the account that don't have one |
| Refresh Rates Now | Fetch rates for a meter on the next update pass (within a minute) |
| Refresh Consumption Now | Fetch consumption, and Home Mini data if enabled, on the next update pass |

## Update intervals

| Data | Interval |
|---|---|
| Tariff rates | Every 15 minutes |
| Yesterday / month to date | Every 30 minutes |
| Home Mini today figures | Every 5 minutes |
| Historic devices | Daily, just after midnight |

## Example automations

**Charge the EV on the cheap rate**
Trigger *Cheap Rate Period Starting* on your electricity meter → turn on the charger.
Trigger *Cheap Rate Period Ending* → turn it off.

**Run the dishwasher when Agile is cheap**
Trigger *Rate Below Specific Value* at 10p → switch on the dishwasher's smart plug.

**Daily usage alert**
Trigger *High Consumption Detected* at 25 kWh → send a notification.

**Fixed tariff renewal reminder**
Trigger *Tariff Ending Soon* → send a notification.

## Troubleshooting

**Today values stay at 0.** You need a Home Mini, the **I have an Octopus Home Mini** option ticked, and an account device. Check the log for "Home Mini telemetry found" and look at the meter's `homeMiniDeviceId` state. If nothing is found, the plugin retries hourly. Turn on debug logging and look for the "Home Mini discovery" line to see what Octopus returned.

**Yesterday shows 0.** Octopus hasn't published that day's data yet. Leave it; it fills in on a later poll.

**Two historic devices show the same figures.** Check each device's **Months Prior** value; two set to `0` will both show the current month.

**Warnings around midnight about rate lookups failing.** The Octopus API is busy just after midnight. The plugin retries automatically and tries again on the next poll, so occasional warnings are harmless.

**Auto-discover account returns 403.** Type your account number in manually.

**Authentication errors.** Re-copy the API key from the API access page; stray spaces cause failures. Keys start with `sk_live_`.

**Anything else.** Enable debug logging, reproduce the problem, and [open an issue](../../issues) with the relevant log lines. Please remove your API key and account number before posting.

## Known limitations

- **Saving Sessions:** Octopus removed the `savingSessions` GraphQL query in late 2025. The plugin handles this quietly, but Saving Session states won't update until it migrates to the newer campaign events API.
- **Home Mini gas data** arrives in half-hourly batches rather than live, and gas has no demand figure.
- **Export meters** don't use Home Mini data.
- **UK only:** Octopus suppliers in other countries are not supported.

## Privacy and security

- Your API key is stored in Indigo's plugin preferences and is never written to the log.
- All communication is with Octopus over HTTPS; nothing is sent anywhere else.
- Older versions had **Email** and **Password** fields on the account device. These are no longer used. If you filled them in, clear them, as they are stored in plain text in the device properties.

## Changelog

See [Releases](../../releases) for detailed notes on each version.

## License

MIT. See [LICENSE](LICENSE).
