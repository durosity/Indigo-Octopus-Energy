# EV Charging Quick Start Guide

This guide gets you charging your EV optimally with Octopus Energy rates in under 15 minutes.

## Step 1: Install & Configure (5 minutes)

1. **Install Plugin**
   - Double-click `OctopusEnergy.indigoPlugin`
   - Indigo will install it automatically

2. **Add API Key**
   - Go to **Indigo → Preferences → Plugins → Octopus Energy**
   - Get your API key from: https://octopus.energy/dashboard/new/accounts/personal-details/api-access
   - Paste it in and click OK

3. **Create Account Device**
   - **Devices → New → Octopus Energy → Octopus Account**
   - Enter your account number (find it on your bill, format: A-XXXXXXXX)
   - Save

## Step 2: Discover Your Meters (2 minutes)

1. **Run Discovery**
   - **Plugins → Octopus Energy → Discover Meters...**
   - Enter your account number
   - Check the Indigo Event Log

2. **Note Your Details**
   You'll see something like:
   ```
   Discovered 2 meters:
     Electricity: MPAN=1234567890123, Serial=12A3456789
     Gas: MPRN=9876543210, Serial=GH12345678
   ```

## Step 3: Create Electricity Meter (5 minutes)

1. **Create Device**
   - **Devices → New → Octopus Energy → Electricity Meter**
   - Name it: "House Electricity" or "EV Meter"

2. **Enter Basic Info**
   - **MPAN**: Your 13-digit number from discovery
   - **Serial Number**: From discovery

3. **Enter Tariff Info** (IMPORTANT for EV charging)
   
   **If you're on Octopus Go:**
   - Product Code: `GO-VAR-22-10-14`
   - Tariff Code: `E-1R-GO-VAR-22-10-14-{YOUR_REGION}`
   - Cheap Rate Threshold: `7.5` (Go rate is 7.5p/kWh overnight)
   
   **If you're on Agile:**
   - Product Code: `AGILE-FLEX-22-11-25`
   - Tariff Code: `E-1R-AGILE-FLEX-22-11-25-{YOUR_REGION}`
   - Cheap Rate Threshold: `10.0` (adjust to your preference, typical: 5-15p)
   
   **If you're on Intelligent Go:**
   - Product Code: `INTELLI-VAR-22-10-14`
   - Tariff Code: `E-1R-INTELLI-VAR-22-10-14-{YOUR_REGION}`
   - Cheap Rate Threshold: `7.5`

4. **Replace {YOUR_REGION}** with your region code:
   - Common regions: C (London), H (South Wales), A (Eastern), etc.
   - If unsure, check your Octopus dashboard or the discovery log

5. **Save** and wait 1 minute for first update

## Step 4: Create Your First EV Charging Trigger (3 minutes)

### Basic Setup: Charge During Cheap Period

1. **Create Trigger**
   - **Events → New Event → Octopus Energy → Cheap Rate Period Starting**
   - Select your electricity meter device
   - Name it: "EV Charging - Start"

2. **Add Actions**
   - Click "Actions" tab
   - Add action: **Device Actions → Turn On** (your EV charger plug/relay)
   - Optional: Add notification

3. **Create Stop Trigger**
   - **Events → New Event → Octopus Energy → Cheap Rate Period Ending**
   - Same meter device
   - Name it: "EV Charging - Stop"
   - Action: Turn Off EV charger

That's it! Your EV will now charge automatically during cheap rate periods.

## Advanced: Smarter Charging

### Only Charge If Battery Low
Add a condition to your "Start" trigger:
```
Conditions:
  - Variable "EV Battery Level" < 80
```

### Charge Only at Night
Add a condition:
```
Conditions:
  - Current time is between 23:00 and 07:00
```

### Agile: Only Charge Below 5p/kWh
Create a custom trigger:
```
Trigger: Rate Below Specific Value
  Threshold: 5.0 p/kWh
Actions:
  - Turn On EV Charger
```

### Pause Charging During Price Spikes
```
Trigger: Rate Above Specific Value
  Threshold: 20.0 p/kWh
Conditions:
  - EV Charger is on
Actions:
  - Turn Off EV Charger
  - Set variable "Paused Due To Price" = true
```

## Monitoring Your Setup

### Check Device States
Look at your electricity meter device in Indigo:
- **Current Rate**: What you're paying right now
- **In Cheap Period**: True/false indicator
- **Cheap Period Start/End**: When next cheap period is
- **Today's Total**: How much energy used today
- **Today's Cost**: What it cost you

### Check Event Log
Look for these messages:
- "Updated House Electricity: 2.45 kWh, Today: 15.32 kWh"
- "House Electricity: Entering cheap rate period"
- "Updated rates for House Electricity: 7.50 p/kWh"

## Troubleshooting

### "No Data" Status
- **Wait 30 minutes** - smart meters have a delay
- Check MPAN and serial number are correct
- Verify your smart meter is working in Octopus dashboard

### Triggers Not Firing
1. Check your tariff codes are correct
2. Verify "Cheap Rate Threshold" is set
3. Enable debug logging (Plugin Preferences)
4. Watch Event Log during a rate change

### Rate Shows as 0.00
- Tariff codes not configured
- Run discovery again to confirm codes
- Check product/tariff codes match your actual tariff

## Example Automation Flow

Here's a complete EV charging automation:

**7:00 PM** - Set variable "EV Needs Charging" = true (if plugged in)

**11:30 PM** - Agile rates drop below 10p/kWh
  → Trigger fires: "Cheap Rate Period Starting"
  → Check: "EV Needs Charging" = true
  → Action: Turn on EV charger
  → Notification: "EV charging started at 9.5p/kWh"

**4:30 AM** - Rates rise above 10p/kWh
  → Trigger fires: "Cheap Rate Period Ending"
  → Action: Turn off EV charger
  → Notification: "EV charging stopped, charged 40kWh"

**7:00 AM** - Set variable "EV Needs Charging" = false

## Next Steps

1. **Add Gas Meter** (optional)
   - Track gas usage and costs
   - Same process as electricity

2. **Add Export Meter** (for future solar)
   - Track generation and earnings
   - Requires separate export MPAN

3. **Create Cost Tracking**
   - Log daily costs to a variable
   - Create graphs in Indigo Touch

4. **Advanced Triggers**
   - Charge above X% only during very cheap rates
   - Pause washing machine during expensive periods
   - Smart heating control

## Tips for Agile Users

- Rates published around 4 PM for next day
- Plugin fetches 48 hours ahead
- Set threshold conservatively (10-12p for normal charging)
- Use lower threshold (5-7p) for opportunistic top-ups
- Winter rates more volatile - adjust threshold seasonally

## Tips for Go/Intelligent Go Users

- Cheap period: 00:30 - 04:30 GMT (7.5p/kWh)
- Set threshold slightly above rate (8p) to catch early
- Intelligent Go handles car integration - this plugin adds home automation
- Use "Rate Change" trigger to detect period start/end precisely

## Support Resources

- **Plugin README**: Full documentation in plugin folder
- **Octopus API**: https://developer.octopus.energy/
- **Your Dashboard**: https://octopus.energy/dashboard
- **Debug Logging**: Enable in plugin preferences for troubleshooting

---

**You're all set!** Your EV will now charge automatically during the cheapest rate periods. Monitor the first few days to fine-tune your threshold settings.
