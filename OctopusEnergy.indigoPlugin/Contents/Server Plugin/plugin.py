#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Octopus Energy Plugin for Indigo
Optimized for EV charging with time-of-use tariff support
"""

import indigo
import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import ConnectionError, Timeout
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import pytz
import logging
import time

# Constants
API_BASE_URL = "https://api.octopus.energy/v1"
GRAPHQL_URL = "https://api.octopus.energy/v1/graphql/"
DEFAULT_POLL_INTERVAL = 1800  # 30 minutes for consumption
RATE_POLL_INTERVAL = 900      # 15 minutes for tariff rates
MAX_RETRIES = 3
RETRY_DELAY = 5
HOME_MINI_POLL_INTERVAL = 300   # 5 minutes for live Home Mini telemetry
HOME_MINI_LOOKUP_RETRY = 3600   # retry device ID discovery hourly if not found
HOME_MINI_ERROR_BACKOFF = 900   # back off 15 minutes after a failed telemetry query

# States owned by the Home Mini updater when it is active
TODAY_STATE_KEYS = {'kWhToday', 'costToday', 'costTodayWithStanding', 'todayTotal', 'todayCost'}

# ---------------------------------------------------------------------------
# Timezone helpers (DST-safe)
#
# pytz timezones carry a fixed UTC offset once attached to a datetime. Calling
# .replace() or adding a timedelta keeps that offset, so a boundary computed
# from "now" in BST and applied to a GMT day (or vice versa) ends up an hour
# out. Always build local boundaries from a calendar date via localize().
# ---------------------------------------------------------------------------
UK_TZ = pytz.timezone('Europe/London')


def uk_midnight(d):
    """Aware datetime for 00:00 UK local time on calendar date d (offset correct for that date)."""
    return UK_TZ.localize(datetime(d.year, d.month, d.day))


def month_shift(year, month, delta):
    """Return (year, month) shifted by delta months."""
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def parse_api_time(value):
    """Parse an Octopus API timestamp ('...Z' or '...+01:00') into an aware datetime."""
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def uk_str(dt, fmt='%Y-%m-%d %H:%M:%S'):
    """Format an aware datetime in UK local time."""
    return dt.astimezone(UK_TZ).strftime(fmt)

class Plugin(indigo.PluginBase):
    
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super(Plugin, self).__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = pluginPrefs.get("showDebugInfo", False)
        
        # Session for HTTP requests
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json'
        })
        
        # Retry transient failures (timeouts, 429/5xx) with backoff. The Octopus
        # API is often slow just after midnight when every integration polls.
        retry = Retry(
            total=3, connect=3, read=2, status=3,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(['GET', 'POST']),
            respect_retry_after_header=True,
            raise_on_status=False
        )
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        
        # Unit-rate cache for cost calculations: key -> (expiry_ts, results)
        self.unit_rate_cache = {}
        
        # Device update tracking
        self.next_consumption_update = {}
        self.next_rate_update = {}
        
        # Home Mini (live telemetry) tracking
        self.home_mini_device_ids = {}    # meter serial -> smart device ID (EUI64)
        self.home_mini_lookup_after = 0   # earliest time to retry device ID discovery
        self.next_live_update = {}        # dev.id -> next telemetry poll time
        
        # Cache for tariff rates to detect changes
        self.rate_cache = {}
        
        # Cache for consumption logging (only log once per day when data changes)
        self.consumption_log_cache = {}  # dev.id -> {'date': 'YYYY-MM-DD', 'yesterday_kwh': float}
        
        # GraphQL authentication
        self.graphql_token = None
        self.graphql_token_expiry = None
        self.graphql_refresh_token = None
        
        # Saving Sessions tracking for triggers
        self.saving_session_state = {}  # dev.id -> {'active': bool, 'count': int}
        
        self.logger.info(f"{pluginDisplayName} initialized")

    def startup(self):
        self.logger.info(f"Octopus Energy Plugin v{self.pluginVersion} starting up")
        self.debugLog("Debug logging enabled")

    def shutdown(self):
        self.logger.info("Octopus Energy Plugin shutting down")
    
    def _is_network_error(self, exception):
        """Check if an exception is a network-related error"""
        error_str = str(exception).lower()
        return any(keyword in error_str for keyword in [
            'failed to resolve',
            'name resolution',
            'connection',
            'timeout',
            'timed out',
            'max retries',
            'nodename nor servname'
        ])
    
    def _log_error_gracefully(self, dev_name, operation, exception):
        """Log errors gracefully - simple message for network errors, full trace for others"""
        if self._is_network_error(exception):
            self.logger.warning(f"{dev_name}: {operation} failed - Network unavailable (will retry)")
            dev_name_for_lookup = dev_name
            # Find device and update status
            for dev in indigo.devices.iter("self"):
                if dev.name == dev_name:
                    dev.updateStateOnServer('status', 'Network Error')
                    break
        else:
            # For non-network errors, log the full traceback
            self.logger.error(f"{operation} error for {dev_name}: {str(exception)}")
            import traceback
            self.logger.error(traceback.format_exc())

    ####################
    # Config Methods
    ####################
    
    def validatePrefsConfigUi(self, valuesDict):
        errorDict = indigo.Dict()
        
        api_key = valuesDict.get('apiKey', '').strip()
        if not api_key:
            errorDict['apiKey'] = "API key is required"
            return (False, valuesDict, errorDict)
        
        # Basic format check - Octopus API keys start with sk_live_
        if not api_key.startswith('sk_live_'):
            self.logger.warning("API key doesn't start with 'sk_live_' - this may not be valid")
        
        # Note: We don't validate the API key here because the /v1/accounts/ endpoint
        # requires a specific account number. The key will be validated when you create
        # an account device and use it to fetch data.
        
        self.logger.info("API key saved - will be validated when you create an account device")
        
        return (True, valuesDict)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if not userCancelled:
            self.debug = valuesDict.get("showDebugInfo", False)
            # Re-run Home Mini discovery straight away after any prefs change
            self.home_mini_device_ids = {}
            self.home_mini_lookup_after = 0
            self.next_live_update = {}
            if valuesDict.get("hasHomeMini", False):
                self.logger.info("Home Mini live data enabled")
            if self.debug:
                self.logger.info("Debug logging enabled")
            else:
                self.logger.info("Debug logging disabled")

    ####################
    # Device Methods
    ####################
    
    def deviceStartComm(self, dev):
        self.debugLog(f"Starting device: {dev.name}")
        
        # Initialize device states based on type
        if dev.deviceTypeId == "accountDevice":
            self._init_account_device(dev)
        elif dev.deviceTypeId == "electricityMeter":
            self._init_electricity_meter(dev)
        elif dev.deviceTypeId == "gasMeter":
            self._init_gas_meter(dev)
        elif dev.deviceTypeId == "exportMeter":
            self._init_export_meter(dev)
        elif dev.deviceTypeId in ["electricityMeterHistoric", "gasMeterHistoric", "exportMeterHistoric"]:
            self._init_historic_meter(dev)
        
        # Force immediate update by setting next update to now
        self.next_consumption_update[dev.id] = 0
        self.next_rate_update[dev.id] = 0
        
        self.debugLog(f"Device {dev.name} will update within 60 seconds")
        
        dev.stateListOrDisplayStateIdChanged()

    def deviceStopComm(self, dev):
        self.logger.info(f"Stopping device: {dev.name}")
        if dev.id in self.next_consumption_update:
            del self.next_consumption_update[dev.id]
        if dev.id in self.next_rate_update:
            del self.next_rate_update[dev.id]
        if dev.id in self.rate_cache:
            del self.rate_cache[dev.id]
        if dev.id in self.consumption_log_cache:
            del self.consumption_log_cache[dev.id]
        self.next_live_update.pop(dev.id, None)

    def _init_account_device(self, dev):
        """Initialize account device states"""
        states = [
            {'key': 'status', 'value': 'Initializing'},
            {'key': 'accountNumber', 'value': ''},
            {'key': 'accountBalance', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'paymentMethod', 'value': 'Unknown'},
            {'key': 'nextPaymentDate', 'value': ''},
            {'key': 'nextPaymentAmount', 'value': 0.0, 'uiValue': ''},
            {'key': 'meterCount', 'value': 0},
            {'key': 'savingSessionActive', 'value': False},
            {'key': 'savingSessionStart', 'value': ''},
            {'key': 'savingSessionEnd', 'value': ''},
            {'key': 'savingSessionReward', 'value': 0.0},
            {'key': 'nextSavingSession', 'value': ''},
            {'key': 'savingSessionsCount', 'value': 0},
            {'key': 'lastUpdate', 'value': ''},
        ]
        dev.updateStatesOnServer(states)

    def _init_electricity_meter(self, dev):
        """Initialize electricity meter device states"""
        states = [
            {'key': 'status', 'value': 'Initializing'},
            {'key': 'currentRate', 'value': 0.0, 'uiValue': '0.00 p/kWh'},
            {'key': 'nextRateChange', 'value': ''},
            {'key': 'nextRate', 'value': 0.0, 'uiValue': '0.00 p/kWh'},
            {'key': 'inCheapPeriod', 'value': False},
            {'key': 'cheapPeriodStart', 'value': ''},
            {'key': 'cheapPeriodEnd', 'value': ''},
            {'key': 'isDualRate', 'value': False},
            {'key': 'currentPeriod', 'value': 'Standard'},
            {'key': 'dayRate', 'value': 0.0, 'uiValue': '0.00 p/kWh'},
            {'key': 'nightRate', 'value': 0.0, 'uiValue': '0.00 p/kWh'},
            {'key': 'isNegativeRate', 'value': False},
            {'key': 'currentConsumption', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'costToday', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'costTodayWithStanding', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'kWhToday', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'costYesterday', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'costYesterdayWithStanding', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'kWhYesterday', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'costMonthToDate', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'costMonthToDateWithStanding', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'kWhMonthToDate', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'todayTotal', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'todayCost', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'standingCharge', 'value': 0.0, 'uiValue': '0.00 p/day'},
            {'key': 'currentTariff', 'value': ''},
            {'key': 'tariffValidFrom', 'value': ''},
            {'key': 'tariffValidTo', 'value': ''},
            {'key': 'tariffEndingSoon', 'value': False},
            {'key': 'upcomingTariff', 'value': ''},
            {'key': 'tariffChanged', 'value': False},
            {'key': 'lastUpdate', 'value': ''},
        ]
        dev.updateStatesOnServer(states)

    def _init_gas_meter(self, dev):
        """Initialize gas meter device states"""
        states = [
            {'key': 'status', 'value': 'Initializing'},
            {'key': 'meterTypeDetected', 'value': 'Unknown'},
            {'key': 'currentRate', 'value': 0.0, 'uiValue': '0.00 p/kWh'},
            {'key': 'currentConsumption', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'costToday', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'costTodayWithStanding', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'kWhToday', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'costYesterday', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'costYesterdayWithStanding', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'kWhYesterday', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'costMonthToDate', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'costMonthToDateWithStanding', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'kWhMonthToDate', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'todayTotal', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'todayCost', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'standingCharge', 'value': 0.0, 'uiValue': '0.00 p/day'},
            {'key': 'currentTariff', 'value': ''},
            {'key': 'tariffValidFrom', 'value': ''},
            {'key': 'tariffValidTo', 'value': ''},
            {'key': 'tariffEndingSoon', 'value': False},
            {'key': 'upcomingTariff', 'value': ''},
            {'key': 'tariffChanged', 'value': False},
            {'key': 'lastUpdate', 'value': ''},
        ]
        dev.updateStatesOnServer(states)

    def _init_export_meter(self, dev):
        """Initialize export meter device states"""
        states = [
            {'key': 'status', 'value': 'Initializing'},
            {'key': 'currentRate', 'value': 0.0, 'uiValue': '0.00 p/kWh'},
            {'key': 'currentExport', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'todayTotal', 'value': 0.0, 'uiValue': '0.00 kWh'},
            {'key': 'todayEarnings', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'lastUpdate', 'value': ''},
        ]
        dev.updateStatesOnServer(states)
    
    def _init_historic_meter(self, dev):
        """Initialize historic meter device states (electricity, gas, or export)"""
        states = [
            {'key': 'status', 'value': 'Initializing'},
            {'key': 'monthName', 'value': ''},
            {'key': 'totalCost', 'value': 0.0, 'uiValue': '£0.00'},
            {'key': 'totalKwh', 'value': 0.0},
        ]
        
        # Add all 31 days of cost/kWh states
        for day in range(1, 32):
            day_str = f"{day:02d}"
            states.append({'key': f'day{day_str}cost', 'value': 0.0})
            states.append({'key': f'day{day_str}kWh', 'value': 0.0})
        
        states.append({'key': 'lastUpdate', 'value': ''})
        dev.updateStatesOnServer(states)

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        errorDict = indigo.Dict()
        
        if typeId == "accountDevice":
            account_num = valuesDict.get('accountNumber', '').strip()
            if not account_num:
                errorDict['accountNumber'] = "Account number is required"
            elif not account_num.startswith('A-'):
                errorDict['accountNumber'] = "Account number should start with 'A-' (e.g., A-12345678)"
            elif len(account_num) < 8:
                errorDict['accountNumber'] = "Account number seems too short (format: A-XXXXXXXX)"
        
        elif typeId in ["electricityMeter", "gasMeter", "exportMeter"]:
            mpan_mprn = valuesDict.get('mpanMprn', '').strip()
            serial = valuesDict.get('serialNumber', '').strip()
            
            if not mpan_mprn:
                errorDict['mpanMprn'] = "MPAN/MPRN is required"
            if not serial:
                errorDict['serialNumber'] = "Serial number is required"
            
            # For electricity meters, validate tariff info if provided
            if typeId == "electricityMeter":
                product_code = valuesDict.get('productCode', '').strip()
                tariff_code = valuesDict.get('tariffCode', '').strip()
                
                if product_code and not tariff_code:
                    errorDict['tariffCode'] = "Tariff code required if product code specified"
                elif tariff_code and not product_code:
                    errorDict['productCode'] = "Product code required if tariff code specified"
        
        if len(errorDict) > 0:
            return (False, valuesDict, errorDict)
        
        return (True, valuesDict)

    ####################
    # Main Loop
    ####################
    
    def runConcurrentThread(self):
        try:
            while True:
                current_time = time.time()
                
                # Update all devices
                for dev in indigo.devices.iter("self"):
                    if not dev.enabled:
                        continue
                    
                    try:
                        if dev.deviceTypeId == "accountDevice":
                            self._update_account(dev, current_time)
                        elif dev.deviceTypeId == "electricityMeter":
                            self._update_electricity_meter(dev, current_time)
                        elif dev.deviceTypeId == "gasMeter":
                            self._update_gas_meter(dev, current_time)
                        elif dev.deviceTypeId == "exportMeter":
                            self._update_export_meter(dev, current_time)
                        elif dev.deviceTypeId in ["electricityMeterHistoric", "gasMeterHistoric", "exportMeterHistoric"]:
                            self._update_historic_meter(dev, current_time)
                    
                    except Exception as e:
                        self.logger.error(f"Error updating {dev.name}: {str(e)}")
                        dev.updateStateOnServer('status', 'Error')
                
                self.sleep(60)  # Check every minute
        
        except self.StopThread:
            pass

    ####################
    # GraphQL Methods
    ####################
    
    def _get_graphql_token(self, api_key=None):
        """Get or refresh GraphQL authentication token using the Octopus API key.

        Authenticates against the Kraken GraphQL API via the APIKey input on
        obtainKrakenToken. This avoids email/password (which now requires a
        CAPTCHA response) and reuses the same API key already configured for
        the REST API at the plugin level.
        """
        # Check if we have a valid token
        if self.graphql_token and self.graphql_token_expiry:
            if datetime.now() < self.graphql_token_expiry - timedelta(minutes=5):
                return self.graphql_token

        # Need an API key
        if not api_key:
            self.logger.debug("GraphQL API key not provided - skipping GraphQL features")
            return None

        try:
            # Use refresh token if available
            if self.graphql_refresh_token:
                return self._refresh_graphql_token(api_key)

            # Otherwise obtain new token using the API key
            mutation = """
            mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
                obtainKrakenToken(input: $input) {
                    token
                    refreshToken
                    refreshExpiresIn
                }
            }
            """

            variables = {
                "input": {
                    "APIKey": api_key
                }
            }

            response = self.session.post(
                GRAPHQL_URL,
                json={'query': mutation, 'variables': variables},
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                if 'errors' in data:
                    self.logger.error(f"GraphQL auth error: {data['errors']}")
                    return None

                token_data = data.get('data', {}).get('obtainKrakenToken', {})
                self.graphql_token = token_data.get('token')
                self.graphql_refresh_token = token_data.get('refreshToken')

                # Tokens typically expire in 60 minutes for API-key auth
                self.graphql_token_expiry = datetime.now() + timedelta(minutes=55)

                self.debugLog("GraphQL authentication successful (API key)")
                return self.graphql_token
            else:
                self.logger.error(f"GraphQL auth failed: HTTP {response.status_code}")
                return None

        except Exception as e:
            self.logger.error(f"GraphQL authentication error: {str(e)}")
            return None

    def _refresh_graphql_token(self, api_key):
        """Refresh GraphQL token using refresh token, falling back to API key auth"""
        try:
            mutation = """
            mutation refreshToken($input: ObtainJSONWebTokenInput!) {
                obtainKrakenToken(input: $input) {
                    token
                    refreshToken
                    refreshExpiresIn
                }
            }
            """

            variables = {
                "input": {
                    "refreshToken": self.graphql_refresh_token
                }
            }

            response = self.session.post(
                GRAPHQL_URL,
                json={'query': mutation, 'variables': variables},
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                if 'errors' in data:
                    self.logger.debug("Refresh token expired, re-authenticating with API key")
                    self.graphql_refresh_token = None
                    return self._get_graphql_token(api_key)

                token_data = data.get('data', {}).get('obtainKrakenToken', {})
                self.graphql_token = token_data.get('token')
                self.graphql_refresh_token = token_data.get('refreshToken')
                self.graphql_token_expiry = datetime.now() + timedelta(minutes=55)

                return self.graphql_token
            else:
                self.debugLog(f"Token refresh failed (HTTP {response.status_code}), re-authenticating")
                self.graphql_refresh_token = None
                return self._get_graphql_token(api_key)

        except Exception as e:
            self.logger.error(f"Token refresh error: {str(e)}")
            self.graphql_refresh_token = None
            return self._get_graphql_token(api_key)

    def _graphql_query(self, query, variables=None, api_key=None, silent_schema_error=False):
        """Execute a GraphQL query with authentication.

        If silent_schema_error is True, schema-related errors (missing fields)
        are logged at debug level instead of error, so optional/deprecated
        queries don't spam the log.
        """
        token = self._get_graphql_token(api_key)
        if not token:
            return None

        try:
            headers = {
                'Authorization': token,
                'Content-Type': 'application/json'
            }

            response = self.session.post(
                GRAPHQL_URL,
                json={'query': query, 'variables': variables or {}},
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                if 'errors' in data:
                    self.logger.error(f"GraphQL query error: {data['errors']}")
                    return None
                return data.get('data')
            elif response.status_code == 400 and silent_schema_error:
                # Schema mismatch (field not in schema, etc.) - log quietly
                self.debugLog(f"GraphQL query unsupported (HTTP 400): {response.text[:200]}")
                return None
            else:
                self.logger.error(f"GraphQL query failed: HTTP {response.status_code}")
                self.logger.error(f"GraphQL response body: {response.text}")
                self.logger.debug(f"GraphQL query was: {query[:200]}...")
                self.logger.debug(f"GraphQL variables: {variables}")
                return None

        except Exception as e:
            self.logger.error(f"GraphQL query error: {str(e)}")
            return None

    ####################
    # Update Methods
    ####################
    
    def _update_account(self, dev, current_time):
        """Update account device with balance, payment info, and Saving Sessions"""
        # Account updates are less frequent - once per day
        if current_time < self.next_consumption_update.get(dev.id, 0):
            return
        
        api_key = self.pluginPrefs.get('apiKey', '')
        account_num = dev.pluginProps.get('accountNumber', '')
        
        if not api_key or not account_num:
            return
        
        try:
            # PART 1: REST API for meter discovery and tariff changes
            auth = HTTPBasicAuth(api_key, '')
            response = self.session.get(
                f"{API_BASE_URL}/accounts/{account_num}/",
                auth=auth,
                timeout=10
            )
            
            if response.status_code != 200:
                self.logger.error(f"Account API error: {response.status_code}")
                dev.updateStateOnServer('status', 'Error')
                return
            
            data = response.json()
            properties = data.get('properties', [])
            
            # Count meters
            meter_count = 0
            if properties:
                for prop in properties:
                    meter_count += len(prop.get('electricity_meter_points', []))
                    meter_count += len(prop.get('gas_meter_points', []))
            
            # Check for tariff changes
            if properties:
                self._check_tariff_changes(auth, properties)
            
            # PART 2: GraphQL for balance, payment info, and Saving Sessions
            balance_pounds = 0.0
            payment_method = "Not Available (GraphQL not configured)"
            next_payment_date = ""
            next_payment_amount = 0.0
            
            # Saving Sessions defaults
            session_active = False
            session_start = ""
            session_end = ""
            session_reward = 0.0
            next_session = ""
            sessions_count = 0
            
            # Try to get GraphQL data if credentials configured
            graphql_data = self._query_account_graphql(account_num, api_key)
            if graphql_data:
                # Extract balance
                balance = graphql_data.get('balance')
                if balance is not None:
                    # Balance is in pence, convert to pounds
                    balance_pounds = float(Decimal(str(balance / 100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
                    payment_method = "Direct Debit"  # Simplified - we have balance so GraphQL is working
                
                # Extract payment schedules
                payment_schedules = graphql_data.get('paymentSchedules', {}).get('edges', [])
                if payment_schedules:
                    schedule = payment_schedules[0]['node']
                    payment_day = schedule.get('paymentDay', '')
                    payment_amount = schedule.get('paymentAmount')
                    
                    # Only use payment data if it's non-zero (API often returns 0 for variable DD)
                    if payment_day and payment_amount and payment_amount > 0:
                        next_payment_date = f"Day {payment_day} of each month"
                        # Payment amount is in pence, convert to pounds
                        next_payment_amount = float(Decimal(str(payment_amount / 100)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
                    else:
                        # Variable direct debit or data not available
                        next_payment_date = "Variable (based on usage)"
                        next_payment_amount = 0.0

                
                # Extract Saving Sessions
                sessions_data = self._query_saving_sessions_graphql(account_num, api_key)
                if sessions_data:
                    now = datetime.now(pytz.timezone('Europe/London'))
                    upcoming_sessions = []
                    
                    for session in sessions_data:
                        start_str = session.get('startAt')
                        end_str = session.get('endAt')
                        
                        if not start_str or not end_str:
                            continue
                        
                        start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                        
                        # Check if session is currently active
                        if start_dt <= now <= end_dt:
                            session_active = True
                            session_start = uk_str(start_dt, '%Y-%m-%d %H:%M')
                            session_end = uk_str(end_dt, '%Y-%m-%d %H:%M')
                            # Note: Reward data not available in joinedEvents API
                            session_reward = 0.0
                        
                        # Collect upcoming sessions
                        if start_dt > now:
                            upcoming_sessions.append((start_dt, end_dt, session))
                    
                    # Sort and get next session
                    if upcoming_sessions:
                        upcoming_sessions.sort(key=lambda x: x[0])
                        sessions_count = len(upcoming_sessions)
                        next_start, next_end, next_node = upcoming_sessions[0]
                        next_session = f"{uk_str(next_start, '%Y-%m-%d %H:%M')} - {uk_str(next_end, '%H:%M')}"
            
            # Update all states
            states = [
                {'key': 'status', 'value': 'Online'},
                {'key': 'accountNumber', 'value': data.get('number', '')},
                {'key': 'accountBalance', 'value': balance_pounds,
                 'uiValue': f'£{balance_pounds:.2f}'},
                {'key': 'paymentMethod', 'value': payment_method},
                {'key': 'nextPaymentDate', 'value': next_payment_date},
                {'key': 'nextPaymentAmount', 'value': next_payment_amount,
                 'uiValue': f'£{next_payment_amount:.2f}' if next_payment_amount > 0 else ''},
                {'key': 'meterCount', 'value': meter_count},
                {'key': 'savingSessionActive', 'value': session_active},
                {'key': 'savingSessionStart', 'value': session_start},
                {'key': 'savingSessionEnd', 'value': session_end},
                {'key': 'savingSessionReward', 'value': session_reward},
                {'key': 'nextSavingSession', 'value': next_session},
                {'key': 'savingSessionsCount', 'value': sessions_count},
                {'key': 'lastUpdate', 'value': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            ]
            dev.updateStatesOnServer(states)
            
            self.debugLog(f"Updated account {account_num}:")
            self.debugLog(f"  Balance: £{balance_pounds:.2f}")
            self.debugLog(f"  Payment: {payment_method}")
            if next_payment_date:
                self.debugLog(f"  Next payment: {next_payment_date} - £{next_payment_amount:.2f}")
            self.debugLog(f"  Meter count: {meter_count}")
            if sessions_count > 0:
                self.debugLog(f"  Saving Sessions: {sessions_count} upcoming, next: {next_session}")
            if session_active:
                self.logger.info(f"  ** SAVING SESSION ACTIVE NOW ** ({session_start} to {session_end}, {session_reward:.1f}p/kWh reward)")
            
            # Fire Saving Sessions triggers
            self._check_saving_session_triggers(dev, session_active, sessions_count)
            
            # Schedule next update in 24 hours
            self.next_consumption_update[dev.id] = current_time + 86400
        
        except Exception as e:
            self._log_error_gracefully(dev.name, "Account update", e)
            dev.updateStateOnServer('status', 'Error' if not self._is_network_error(e) else 'Network Error')
    
    def _query_account_graphql(self, account_num, api_key):
        """Query account balance and payment info via GraphQL"""
        query = """
        query GetAccount($accountNumber: String!) {
            account(accountNumber: $accountNumber) {
                balance
                paymentSchedules(first: 1) {
                    edges {
                        node {
                            paymentDay
                            paymentAmount
                        }
                    }
                }
            }
        }
        """
        
        variables = {"accountNumber": account_num}
        data = self._graphql_query(query, variables, api_key)
        
        if data and 'account' in data:
            return data['account']
        return None
    
    def _query_saving_sessions_graphql(self, account_num, api_key):
        """Query upcoming Saving Sessions via GraphQL.
        
        Note: the top-level `savingSessions` query was removed from Octopus's
        schema in late 2025/early 2026. This call now silently fails on
        schema errors so it doesn't spam the log. Saving Sessions tracking
        will return no data until the plugin is updated to use the newer
        customerFlexibilityCampaignEvents query (which requires per-meter
        MPAN supply point identifiers).
        """
        query = """
        query GetSavingSessions($accountNumber: String!) {
            savingSessions {
                account(accountNumber: $accountNumber) {
                    hasJoinedCampaign
                    joinedEvents {
                        eventId
                        startAt
                        endAt
                    }
                }
            }
        }
        """
        
        variables = {"accountNumber": account_num}
        data = self._graphql_query(query, variables, api_key, silent_schema_error=True)
        
        if data and 'savingSessions' in data:
            account_data = data['savingSessions'].get('account', {})
            return account_data.get('joinedEvents', [])
        return None
    
    def _check_saving_session_triggers(self, dev, session_active, sessions_count):
        """Check and fire Saving Sessions triggers"""
        dev_id = dev.id
        
        # Get previous state
        prev_state = self.saving_session_state.get(dev_id, {'active': False, 'count': 0})
        prev_active = prev_state['active']
        prev_count = prev_state['count']
        
        # Check for session starting
        if session_active and not prev_active:
            self.logger.info(f"🎉 Saving Session STARTED for {dev.name}")
            indigo.trigger.fire('savingSessionStarting', dev=dev)
        
        # Check for session ending
        if not session_active and prev_active:
            self.logger.info(f"✓ Saving Session ENDED for {dev.name}")
            indigo.trigger.fire('savingSessionEnding', dev=dev)
        
        # Check for new session announced
        if sessions_count > prev_count:
            new_sessions = sessions_count - prev_count
            self.logger.info(f"📢 {new_sessions} new Saving Session(s) announced for {dev.name}")
            indigo.trigger.fire('newSavingSession', dev=dev)
        
        # Update tracking
        self.saving_session_state[dev_id] = {'active': session_active, 'count': sessions_count}

    def _update_electricity_meter(self, dev, current_time):
        """Update electricity meter with consumption and rates"""
        api_key = self.pluginPrefs.get('apiKey', '')
        mpan = dev.pluginProps.get('mpanMprn', '')
        serial = dev.pluginProps.get('serialNumber', '')
        
        if not api_key or not mpan or not serial:
            return
        
        # Update rates FIRST (so we have current rate for cost calculations)
        if current_time >= self.next_rate_update.get(dev.id, 0):
            self._update_tariff_rates(dev, api_key)
            self.next_rate_update[dev.id] = current_time + RATE_POLL_INTERVAL
        
        # THEN update consumption (uses the rate we just fetched)
        if current_time >= self.next_consumption_update.get(dev.id, 0):
            self._update_consumption(dev, api_key, mpan, serial, 'electricity')
            self.next_consumption_update[dev.id] = current_time + DEFAULT_POLL_INTERVAL
        
        # Live "today" data from the Home Mini (if enabled)
        if self._home_mini_enabled():
            self._update_home_mini(dev, 'electricity', current_time)

    def _update_gas_meter(self, dev, current_time):
        """Update gas meter with consumption and rates"""
        api_key = self.pluginPrefs.get('apiKey', '')
        mprn = dev.pluginProps.get('mpanMprn', '')
        serial = dev.pluginProps.get('serialNumber', '')
        
        if not api_key or not mprn or not serial:
            return
        
        # Update gas rates FIRST if tariff info is configured
        product_code = dev.pluginProps.get('productCode', '')
        tariff_code = dev.pluginProps.get('tariffCode', '')
        
        if product_code and tariff_code and current_time >= self.next_rate_update.get(dev.id, 0):
            self._update_gas_rates(dev, api_key, product_code, tariff_code)
            self.next_rate_update[dev.id] = current_time + RATE_POLL_INTERVAL
        
        # THEN update consumption (uses the rate we just fetched)
        if current_time >= self.next_consumption_update.get(dev.id, 0):
            self._update_consumption(dev, api_key, mprn, serial, 'gas')
            self.next_consumption_update[dev.id] = current_time + DEFAULT_POLL_INTERVAL
        
        # Live "today" data from the Home Mini (if enabled)
        if self._home_mini_enabled():
            self._update_home_mini(dev, 'gas', current_time)

    def _update_export_meter(self, dev, current_time):
        """Update export meter with generation data and rates"""
        api_key = self.pluginPrefs.get('apiKey', '')
        mpan = dev.pluginProps.get('mpanMprn', '')
        serial = dev.pluginProps.get('serialNumber', '')
        product_code = dev.pluginProps.get('productCode', '')
        tariff_code = dev.pluginProps.get('tariffCode', '')
        
        if not api_key or not mpan or not serial:
            return
        
        # Update export rates FIRST (so we have current rate for earnings calculations)
        if product_code and tariff_code and current_time >= self.next_rate_update.get(dev.id, 0):
            self._update_export_rates(dev, api_key, product_code, tariff_code)
            self.next_rate_update[dev.id] = current_time + RATE_POLL_INTERVAL
        
        # THEN update export consumption
        if current_time >= self.next_consumption_update.get(dev.id, 0):
            self._update_export(dev, api_key, mpan, serial)
            self.next_consumption_update[dev.id] = current_time + DEFAULT_POLL_INTERVAL
    
    def _update_export_rates(self, dev, api_key, product_code, tariff_code):
        """Fetch export tariff rates"""
        try:
            auth = HTTPBasicAuth(api_key, '')
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            # Fetch current export rate
            url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
            
            params = {
                'period_from': now.isoformat(),
                'page_size': 10
            }
            
            response = self.session.get(url, auth=auth, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                
                if results:
                    current_rate = results[0]
                    rate_value = round(current_rate['value_inc_vat'], 2)
                    
                    dev.updateStateOnServer('currentRate', rate_value, uiValue=f'{rate_value:.2f} p/kWh')
                    self.logger.info(f"Updated export rate for {dev.name}: {rate_value:.2f} p/kWh")
            elif response.status_code == 404:
                self.logger.warning(f"Export tariff not found for {dev.name}")
        
        except Exception as e:
            self.logger.error(f"Export rate update error for {dev.name}: {str(e)}")
    
    def _convert_gas_to_kwh(self, dev, results):
        """Convert gas consumption from m³ to kWh if needed (SMETS2 meters)
        
        Returns: tuple of (converted_results, meter_type_detected)
        """
        if not results:
            return results, "Unknown"
        
        # Get meter type preference and calorific value
        meter_type_pref = dev.pluginProps.get('meterType', 'auto')
        calorific_value = float(dev.pluginProps.get('calorificValue', 40.0))
        
        # Calculate average consumption from non-zero values across all data
        # (gas meters often have zero readings overnight)
        non_zero_readings = [r.get('consumption', 0) for r in results if r.get('consumption', 0) > 0]
        
        if non_zero_readings:
            avg_consumption = sum(non_zero_readings) / len(non_zero_readings)
        else:
            # All zeros - probably no consumption this period
            avg_consumption = 0
        
        # Auto-detect logic: 
        # SMETS2 meters report in m³ (typically 0.1-5 m³ per reading)
        # SMETS1 meters report in kWh (typically 1-50 kWh per reading)
        # Threshold: if average non-zero reading < 10, it's likely m³ (SMETS2)
        if meter_type_pref == 'auto':
            if len(non_zero_readings) > 0 and avg_consumption < 10:
                detected_type = 'SMETS2'
                needs_conversion = True
            else:
                detected_type = 'SMETS1'
                needs_conversion = False
        elif meter_type_pref == 'smets2':
            detected_type = 'SMETS2'
            needs_conversion = True
        else:  # smets1
            detected_type = 'SMETS1'
            needs_conversion = False
        
        # Convert if needed: kWh = m³ × 1.02264 × calorific_value ÷ 3.6
        if needs_conversion:
            conversion_factor = 1.02264 * calorific_value / 3.6
            converted_results = []
            for reading in results:
                m3_value = reading.get('consumption', 0)
                kwh_value = m3_value * conversion_factor
                converted_reading = reading.copy()
                converted_reading['consumption'] = kwh_value
                converted_reading['original_m3'] = m3_value  # Keep original for reference
                converted_results.append(converted_reading)
            
            self.debugLog(f"{dev.name}: Converting from m³ to kWh (CV={calorific_value}, factor={conversion_factor:.4f})")
            return converted_results, detected_type
        else:
            return results, detected_type

    def _update_consumption(self, dev, api_key, identifier, serial, fuel_type):
        """Fetch consumption data from API - API only provides data up to YESTERDAY"""
        try:
            auth = HTTPBasicAuth(api_key, '')
            
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            # Get tariff info for rate lookups
            product_code = dev.pluginProps.get('productCode', '')
            tariff_code = dev.pluginProps.get('tariffCode', '')
            
            # CRITICAL: Octopus API only provides data up to the previous day
            # Today values will always be 0 (no real-time data without Home Mini)
            # Yesterday = most recent complete day available
            
            # Get yesterday (most recent complete day available)
            today_start = uk_midnight(now.date())
            yesterday_start = uk_midnight(now.date() - timedelta(days=1))
            yesterday_end = today_start
            
            # Get month to date (up to end of yesterday)
            month_start = uk_midnight(now.date().replace(day=1))
            
            endpoint = f"{fuel_type}-meter-points/{identifier}/meters/{serial}/consumption/"
            url = f"{API_BASE_URL}/{endpoint}"
            
            # === FETCH YESTERDAY'S DATA (most recent complete day) ===
            params_yesterday = {
                'period_from': yesterday_start.isoformat(),
                'period_to': yesterday_end.isoformat(),
                'page_size': 100,
                'order_by': 'period'
            }
            
            response_yesterday = self.session.get(url, auth=auth, params=params_yesterday, timeout=10)
            
            kwh_yesterday = 0.0
            cost_yesterday = 0.0
            current_consumption = 0.0
            
            if response_yesterday.status_code == 200:
                data_yesterday = response_yesterday.json()
                results_yesterday = data_yesterday.get('results', [])
                
                # For gas meters, convert m³ to kWh if needed (SMETS2 meters)
                if fuel_type == 'gas' and results_yesterday:
                    results_yesterday, meter_type = self._convert_gas_to_kwh(dev, results_yesterday)
                    dev.updateStateOnServer('meterTypeDetected', meter_type)
                
                # Debug: Log first few readings to see what we're getting
                if results_yesterday:
                    self.debugLog(f"{dev.name}: Sample readings from yesterday:")
                    for i, reading in enumerate(results_yesterday[:3]):
                        self.debugLog(f"  Reading {i+1}: {reading.get('consumption')} kWh at {reading.get('interval_start')}")
                
                if results_yesterday and product_code and tariff_code:
                    # Check if yesterday falls before a tariff change
                    yesterday_product = product_code
                    yesterday_tariff = tariff_code
                    
                    tariff_change_date_str = dev.states.get('tariffChangeDate', '')
                    if tariff_change_date_str:
                        try:
                            change_date = datetime.strptime(tariff_change_date_str, '%Y-%m-%d')
                            change_date = uk_tz.localize(change_date.replace(hour=0, minute=0, second=0))
                            
                            if yesterday_start < change_date:
                                # Yesterday was before the change - use previous tariff
                                yesterday_product = dev.states.get('previousProductCode', product_code)
                                yesterday_tariff = dev.states.get('previousTariffCode', tariff_code)
                                self.debugLog(f"{dev.name}: Using previous tariff for yesterday (before change on {tariff_change_date_str})")
                        except:
                            pass
                    
                    # Calculate cost properly by fetching rates for this period
                    cost_yesterday = self._calculate_cost_for_period(
                        auth, results_yesterday, yesterday_start, yesterday_end, 
                        yesterday_product, yesterday_tariff, fuel_type
                    )
                    kwh_yesterday = round(sum(r.get('consumption', 0) for r in results_yesterday), 2)
                    current_consumption = round(results_yesterday[0].get('consumption', 0), 2)
                    
                    self.debugLog(f"{dev.name}: Yesterday: {len(results_yesterday)} readings, {kwh_yesterday:.2f} kWh, £{cost_yesterday:.2f}")
                elif results_yesterday:
                    # No tariff info - just use current rate as fallback
                    kwh_yesterday = round(sum(r.get('consumption', 0) for r in results_yesterday), 2)
                    current_rate = float(dev.states.get('currentRate', 0))
                    cost_yesterday = round((kwh_yesterday * current_rate) / 100, 2)
                    current_consumption = round(results_yesterday[0].get('consumption', 0), 2)
            else:
                self.logger.warning(f"{dev.name}: No yesterday data available (HTTP {response_yesterday.status_code})")
            
            # === FETCH MONTH TO DATE DATA (up to end of yesterday) ===
            params_mtd = {
                'period_from': month_start.isoformat(),
                'period_to': yesterday_end.isoformat(),
                'page_size': 1500,
                'order_by': 'period'
            }
            
            response_mtd = self.session.get(url, auth=auth, params=params_mtd, timeout=10)
            
            kwh_mtd = 0.0
            cost_mtd = 0.0
            
            if response_mtd.status_code == 200:
                data_mtd = response_mtd.json()
                results_mtd = data_mtd.get('results', [])
                
                # For gas meters, convert m³ to kWh if needed (SMETS2 meters)
                if fuel_type == 'gas' and results_mtd:
                    results_mtd, _ = self._convert_gas_to_kwh(dev, results_mtd)
                
                if results_mtd and product_code and tariff_code:
                    # Check if tariff changed this month
                    tariff_change_date_str = dev.states.get('tariffChangeDate', '')
                    previous_tariff = dev.states.get('previousTariffCode', '')
                    previous_product = dev.states.get('previousProductCode', '')
                    
                    # Parse tariff change date if it exists
                    tariff_changed_this_month = False
                    change_date = None
                    
                    if tariff_change_date_str and previous_tariff and previous_product:
                        try:
                            change_date = datetime.strptime(tariff_change_date_str, '%Y-%m-%d')
                            change_date = uk_tz.localize(change_date.replace(hour=0, minute=0, second=0))
                            
                            # Check if change happened this month
                            if change_date.year == month_start.year and change_date.month == month_start.month:
                                tariff_changed_this_month = True
                        except:
                            pass
                    
                    if tariff_changed_this_month and change_date:
                        # Split cost calculation at tariff change boundary
                        self.debugLog(f"{dev.name}: Splitting MTD cost at tariff change ({change_date.strftime('%Y-%m-%d')})")
                        
                        # Split consumption into before/after periods
                        consumption_before = []
                        consumption_after = []
                        
                        for reading in results_mtd:
                            reading_time = datetime.fromisoformat(reading['interval_start'].replace('Z', '+00:00'))
                            if reading_time < change_date:
                                consumption_before.append(reading)
                            else:
                                consumption_after.append(reading)
                        
                        # Calculate cost for period before change (using old tariff)
                        cost_before = 0.0
                        if consumption_before:
                            cost_before = self._calculate_cost_for_period(
                                auth, consumption_before, month_start, change_date,
                                previous_product, previous_tariff, fuel_type
                            )
                            self.debugLog(f"{dev.name}: Before change: {len(consumption_before)} readings, £{cost_before:.2f}")
                        
                        # Calculate cost for period after change (using new tariff)
                        cost_after = 0.0
                        if consumption_after:
                            cost_after = self._calculate_cost_for_period(
                                auth, consumption_after, change_date, yesterday_end,
                                product_code, tariff_code, fuel_type
                            )
                            self.debugLog(f"{dev.name}: After change: {len(consumption_after)} readings, £{cost_after:.2f}")
                        
                        # Sum the costs
                        cost_mtd = cost_before + cost_after
                        kwh_mtd = round(sum(r.get('consumption', 0) for r in results_mtd), 2)
                        
                        self.debugLog(f"{dev.name}: Month to date (split): {kwh_mtd:.2f} kWh, £{cost_mtd:.2f}")
                    else:
                        # No tariff change this month - use simple calculation
                        cost_mtd = self._calculate_cost_for_period(
                            auth, results_mtd, month_start, yesterday_end,
                            product_code, tariff_code, fuel_type
                        )
                        kwh_mtd = round(sum(r.get('consumption', 0) for r in results_mtd), 2)
                        
                        self.debugLog(f"{dev.name}: Month to date: {len(results_mtd)} readings, {kwh_mtd:.2f} kWh, £{cost_mtd:.2f}")
                elif results_mtd:
                    kwh_mtd = round(sum(r.get('consumption', 0) for r in results_mtd), 2)
                    current_rate = float(dev.states.get('currentRate', 0))
                    cost_mtd = round((kwh_mtd * current_rate) / 100, 2)
            
            # === UPDATE ALL STATES ===
            
            # Get standing charge (in pence per day)
            standing_charge_pence = float(dev.states.get('standingCharge', 0))
            standing_charge_pounds = standing_charge_pence / 100
            
            # Calculate number of days for MTD
            days_in_month_so_far = (yesterday_end - month_start).days
            
            # Calculate costs with standing charge
            # Today is always 0 (no real-time data)
            cost_today_with_standing = 0.0
            cost_yesterday_with_standing = round(cost_yesterday + standing_charge_pounds, 2)
            cost_mtd_with_standing = round(cost_mtd + (standing_charge_pounds * days_in_month_so_far), 2)
            
            states = [
                {'key': 'status', 'value': 'Online'},
                {'key': 'currentConsumption', 'value': current_consumption, 
                 'uiValue': f'{current_consumption:.2f} kWh'},
                
                # "Today" = Always 0 (no real-time data without Home Mini)
                {'key': 'kWhToday', 'value': 0.0,
                 'uiValue': '0.00 kWh'},
                {'key': 'costToday', 'value': 0.0,
                 'uiValue': '£0.00'},
                {'key': 'costTodayWithStanding', 'value': 0.0,
                 'uiValue': '£0.00'},
                
                # "Yesterday" = Most recent complete day
                {'key': 'kWhYesterday', 'value': kwh_yesterday,
                 'uiValue': f'{kwh_yesterday:.2f} kWh'},
                {'key': 'costYesterday', 'value': cost_yesterday,
                 'uiValue': f'£{cost_yesterday:.2f}'},
                {'key': 'costYesterdayWithStanding', 'value': cost_yesterday_with_standing,
                 'uiValue': f'£{cost_yesterday_with_standing:.2f}'},
                
                # Month to date (up to end of yesterday)
                {'key': 'kWhMonthToDate', 'value': kwh_mtd,
                 'uiValue': f'{kwh_mtd:.2f} kWh'},
                {'key': 'costMonthToDate', 'value': cost_mtd,
                 'uiValue': f'£{cost_mtd:.2f}'},
                {'key': 'costMonthToDateWithStanding', 'value': cost_mtd_with_standing,
                 'uiValue': f'£{cost_mtd_with_standing:.2f}'},
                
                # Legacy states (for backwards compatibility)
                {'key': 'todayTotal', 'value': 0.0,
                 'uiValue': '0.00 kWh'},
                {'key': 'todayCost', 'value': 0.0,
                 'uiValue': '£0.00'},
                
                {'key': 'lastUpdate', 'value': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            ]
            
            # When the Home Mini is supplying live data, it owns the "today" states
            if self._home_mini_enabled() and dev.pluginProps.get('serialNumber', '') in self.home_mini_device_ids:
                states = [st for st in states if st['key'] not in TODAY_STATE_KEYS]
            
            dev.updateStatesOnServer(states)
            
            # Only log consumption updates once per day when new data arrives
            today_date = now.date().isoformat()
            cached = self.consumption_log_cache.get(dev.id, {})
            cached_date = cached.get('date')
            cached_kwh = cached.get('yesterday_kwh', 0.0)
            
            # Log if: (1) Yesterday has data (>0), AND (2) It's a new day OR yesterday's value changed
            should_log = kwh_yesterday > 0 and (cached_date != today_date or abs(kwh_yesterday - cached_kwh) > 0.01)
            
            if should_log:
                self.logger.info(f"Updated {dev.name}: Yesterday={kwh_yesterday:.2f} kWh (£{cost_yesterday_with_standing:.2f} inc standing), "
                               f"MTD={kwh_mtd:.2f} kWh (£{cost_mtd_with_standing:.2f})")
                
                # Cache this log
                self.consumption_log_cache[dev.id] = {
                    'date': today_date,
                    'yesterday_kwh': kwh_yesterday
                }
            else:
                # Still log at DEBUG level for troubleshooting
                self.debugLog(f"Updated {dev.name}: Yesterday={kwh_yesterday:.2f} kWh (£{cost_yesterday_with_standing:.2f} inc standing), "
                             f"MTD={kwh_mtd:.2f} kWh (£{cost_mtd_with_standing:.2f})")
            
            # Check consumption triggers
            self._check_consumption_triggers(dev)
                
        except Exception as e:
            self._log_error_gracefully(dev.name, "Consumption update", e)
            dev.updateStateOnServer('status', 'Error' if not self._is_network_error(e) else 'Network Error')
    
    ####################
    # Home Mini (live telemetry via GraphQL)
    ####################
    
    def _home_mini_enabled(self):
        return bool(self.pluginPrefs.get('hasHomeMini', False))
    
    def _discover_home_mini_devices(self, api_key):
        """Map meter serial numbers to smart device IDs for every account device.
        
        The Home Mini reports via the meter's ESME/GSME device ID, which is only
        exposed through GraphQL (not the REST API).
        """
        query = """
        query($accountNumber: String!) {
            account(accountNumber: $accountNumber) {
                electricityAgreements(active: true) {
                    meterPoint { meters { serialNumber smartDevices { deviceId } } }
                }
                gasAgreements(active: true) {
                    meterPoint { meters { serialNumber smartDevices { deviceId } } }
                }
            }
        }
        """
        found = {}
        for acc_dev in indigo.devices.iter("self.accountDevice"):
            if not acc_dev.enabled:
                continue
            account_num = acc_dev.pluginProps.get('accountNumber', '') or acc_dev.states.get('accountNumber', '')
            if not account_num:
                continue
            
            data = self._graphql_query(query, {'accountNumber': account_num}, api_key=api_key)
            account = (data or {}).get('account') or {}
            
            for agreements_key in ('electricityAgreements', 'gasAgreements'):
                for agreement in account.get(agreements_key) or []:
                    meter_point = agreement.get('meterPoint') or {}
                    for meter in meter_point.get('meters') or []:
                        serial = meter.get('serialNumber', '')
                        for smart_dev in meter.get('smartDevices') or []:
                            device_id = smart_dev.get('deviceId')
                            if serial and device_id:
                                found.setdefault(serial, device_id)
        
        self.debugLog(f"Home Mini discovery: {found}")
        return found
    
    def _get_home_mini_device_id(self, dev, api_key):
        serial = dev.pluginProps.get('serialNumber', '')
        if serial in self.home_mini_device_ids:
            device_id = self.home_mini_device_ids[serial]
            # Meters matched by another meter's discovery call log here instead
            if dev.states.get('homeMiniDeviceId', '') != device_id:
                self.logger.info(f"{dev.name}: Home Mini telemetry found (device {device_id})")
                dev.updateStateOnServer('homeMiniDeviceId', device_id)
            return device_id
        
        if time.time() < self.home_mini_lookup_after:
            return None
        
        found = self._discover_home_mini_devices(api_key)
        if found:
            self.home_mini_device_ids.update(found)
        
        if serial in self.home_mini_device_ids:
            device_id = self.home_mini_device_ids[serial]
            self.logger.info(f"{dev.name}: Home Mini telemetry found (device {device_id})")
            dev.updateStateOnServer('homeMiniDeviceId', device_id)
            return device_id
        
        self.home_mini_lookup_after = time.time() + HOME_MINI_LOOKUP_RETRY
        self.logger.warning(f"{dev.name}: No smart device ID found for meter {serial} - "
                            f"is an Account device set up? Will retry in an hour")
        return None
    
    def _update_home_mini(self, dev, fuel_type, current_time):
        """Fetch today's consumption from the Home Mini and update the today states"""
        if current_time < self.next_live_update.get(dev.id, 0):
            return
        self.next_live_update[dev.id] = current_time + HOME_MINI_POLL_INTERVAL
        
        try:
            api_key = self.pluginPrefs.get('apiKey', '')
            if not api_key:
                return
            
            device_id = self._get_home_mini_device_id(dev, api_key)
            if not device_id:
                return
            
            now = datetime.now(UK_TZ)
            today_start = uk_midnight(now.date())
            
            query = """
            query($deviceId: String!, $start: DateTime, $end: DateTime) {
                today: smartMeterTelemetry(deviceId: $deviceId, grouping: HALF_HOURLY, start: $start, end: $end) {
                    readAt
                    consumptionDelta
                    costDeltaWithTax
                }
                latest: smartMeterTelemetry(deviceId: $deviceId) {
                    readAt
                    demand
                }
            }
            """
            variables = {
                'deviceId': device_id,
                'start': today_start.isoformat(),
                'end': now.isoformat()
            }
            
            data = self._graphql_query(query, variables, api_key=api_key)
            if data is None:
                self.next_live_update[dev.id] = current_time + HOME_MINI_ERROR_BACKOFF
                return
            
            readings = data.get('today') or []
            latest = (data.get('latest') or [None])[-1] or {}
            
            # consumptionDelta is Wh per half hour; costDeltaWithTax is pence
            records = []
            kwh_today = 0.0
            api_cost_pence = 0.0
            api_cost_missing = False
            
            for r in readings:
                read_at = r.get('readAt')
                if not read_at:
                    continue
                delta_kwh = float(r.get('consumptionDelta') or 0) / 1000.0
                kwh_today += delta_kwh
                if r.get('costDeltaWithTax') is not None:
                    api_cost_pence += float(r['costDeltaWithTax'])
                else:
                    api_cost_missing = True
                records.append({'interval_start': read_at, 'consumption': delta_kwh})
            
            kwh_today = round(kwh_today, 2)
            
            # Price with our own rate lookup (same method as yesterday/MTD),
            # falling back to Octopus's cost figure, then the current rate
            product_code = dev.pluginProps.get('productCode', '')
            tariff_code = dev.pluginProps.get('tariffCode', '')
            cost_today = 0.0
            
            if records and product_code and tariff_code:
                cost_today = self._calculate_cost_for_period(
                    HTTPBasicAuth(api_key, ''), records, today_start, now,
                    product_code, tariff_code, fuel_type
                )
            if cost_today == 0.0 and kwh_today > 0:
                if records and not api_cost_missing:
                    cost_today = round(api_cost_pence / 100, 2)
                else:
                    cost_today = round(kwh_today * float(dev.states.get('currentRate', 0)) / 100, 2)
            
            standing_pounds = float(dev.states.get('standingCharge', 0)) / 100
            cost_today_with_standing = round(cost_today + standing_pounds, 2)
            
            last_read = latest.get('readAt') or (records[-1]['interval_start'] if records else '')
            
            states = [
                {'key': 'kWhToday', 'value': kwh_today, 'uiValue': f'{kwh_today:.2f} kWh'},
                {'key': 'costToday', 'value': cost_today, 'uiValue': f'£{cost_today:.2f}'},
                {'key': 'costTodayWithStanding', 'value': cost_today_with_standing,
                 'uiValue': f'£{cost_today_with_standing:.2f}'},
                {'key': 'todayTotal', 'value': kwh_today, 'uiValue': f'{kwh_today:.2f} kWh'},
                {'key': 'todayCost', 'value': cost_today, 'uiValue': f'£{cost_today:.2f}'},
                {'key': 'homeMiniDeviceId', 'value': device_id},
                {'key': 'homeMiniLastReading', 'value': uk_str(parse_api_time(last_read)) if last_read else ''},
            ]
            
            if fuel_type == 'electricity':
                demand = latest.get('demand')
                demand_w = round(float(demand)) if demand is not None else 0
                states.append({'key': 'liveDemand', 'value': demand_w, 'uiValue': f'{demand_w} W'})
            
            dev.updateStatesOnServer(states)
            self.debugLog(f"{dev.name}: Home Mini today {kwh_today:.2f} kWh, £{cost_today:.2f} "
                          f"({len(records)} half-hours)")
            
            self._check_consumption_triggers(dev)
        
        except Exception as e:
            self._log_error_gracefully(dev.name, "Home Mini update", e)
            self.next_live_update[dev.id] = current_time + HOME_MINI_ERROR_BACKOFF
    
    def _calculate_cost_for_period(self, auth, consumption_results, period_start, period_end, product_code, tariff_code, fuel_type):
        """Calculate accurate cost by fetching historical rates and matching with consumption"""
        try:
            # Fetch rates for this period
            if fuel_type == 'electricity':
                rates_url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
            else:
                rates_url = f"{API_BASE_URL}/products/{product_code}/gas-tariffs/{tariff_code}/standard-unit-rates/"
            
            # Round the window out to whole UK days so repeated calls (e.g. the
            # Home Mini every 5 minutes) hit the same cache entry
            window_start = uk_midnight(period_start.astimezone(UK_TZ).date())
            last_instant = (period_end - timedelta(seconds=1)).astimezone(UK_TZ)
            window_end = uk_midnight(last_instant.date() + timedelta(days=1))
            
            rates_results = self._get_unit_rates(auth, rates_url, window_start, window_end)
            if rates_results is None:
                return 0.0
            
            if not rates_results:
                self.debugLog("No rates returned for period")
                return 0.0
            
            # Create a map of rates by time period
            # Parse to aware datetimes. Rates come back in UTC ('Z') but consumption
            # intervals come back with the local offset ('+01:00' in BST), so the
            # old string comparison matched the wrong rate by an hour all summer.
            rate_map = {}
            for rate in rates_results:
                valid_from = parse_api_time(rate['valid_from'])
                valid_to = parse_api_time(rate['valid_to']) if rate.get('valid_to') else None
                rate_value = rate['value_inc_vat']
                rate_map[(valid_from, valid_to)] = rate_value
            
            # Match consumption with rates and calculate cost
            total_cost = 0.0
            
            for consumption_record in consumption_results:
                interval_start = parse_api_time(consumption_record['interval_start'])
                consumption = consumption_record['consumption']
                
                # Find matching rate for this interval
                matching_rate = None
                for (rate_start, rate_end), rate_value in rate_map.items():
                    # Check if consumption interval falls within rate period
                    if interval_start >= rate_start and (rate_end is None or interval_start < rate_end):
                        matching_rate = rate_value
                        break
                
                if matching_rate is not None:
                    cost_for_interval = (consumption * matching_rate) / 100  # Convert pence to pounds
                    total_cost += cost_for_interval
            
            return round(total_cost, 2)
            
        except Exception as e:
            if self._is_network_error(e):
                self.logger.warning(f"Rate lookup for cost calculation failed after retries "
                                    f"(Octopus API busy/unreachable) - will retry next poll")
            else:
                self.logger.error(f"Cost calculation error: {str(e)}")
            return 0.0
    
    def _get_unit_rates(self, auth, rates_url, window_start, window_end):
        """Fetch unit rates for a whole-day window, following pagination, with caching.
        
        Returns a list of rate dicts, or None if the request failed.
        """
        key = (rates_url, window_start.isoformat(), window_end.isoformat())
        now_ts = time.time()
        
        cached = self.unit_rate_cache.get(key)
        if cached and cached[0] > now_ts:
            return cached[1]
        
        results = []
        url = rates_url
        params = {
            'period_from': window_start.isoformat(),
            'period_to': window_end.isoformat(),
            'page_size': 1500
        }
        while url:
            response = self.session.get(url, auth=auth, params=params, timeout=20)
            if response.status_code != 200:
                self.debugLog(f"Could not fetch unit rates (HTTP {response.status_code}), using fallback")
                return None
            data = response.json()
            results.extend(data.get('results', []))
            url = data.get('next')
            params = None  # 'next' already carries the query string
        
        # Past windows won't change; today's/future can (e.g. Agile publishing)
        ttl = 86400 if window_end.timestamp() <= now_ts else 3600
        self.unit_rate_cache[key] = (now_ts + ttl, results)
        
        # Prune expired entries
        for k in [k for k, v in self.unit_rate_cache.items() if v[0] <= now_ts]:
            del self.unit_rate_cache[k]
        
        return results
        """Fetch consumption data from API - API only provides data up to YESTERDAY"""
        try:
            auth = HTTPBasicAuth(api_key, '')
            
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            # CRITICAL: Octopus API only provides data up to the previous day
            # So "today" in our context means "yesterday" (most recent complete day)
            # and "yesterday" means "day before yesterday"
            
            # Get yesterday (most recent complete day available)
            today_start = uk_midnight(now.date())
            yesterday_start = uk_midnight(now.date() - timedelta(days=1))
            yesterday_end = today_start
            
            # Get day before yesterday
            day_before_start = uk_midnight(now.date() - timedelta(days=2))
            day_before_end = yesterday_start
            
            # Get month to date (up to end of yesterday)
            month_start = uk_midnight(now.date().replace(day=1))
            
            endpoint = f"{fuel_type}-meter-points/{identifier}/meters/{serial}/consumption/"
            url = f"{API_BASE_URL}/{endpoint}"
            
            # Get current rate for cost calculation
            current_rate = float(dev.states.get('currentRate', 0))
            
            # === FETCH YESTERDAY'S DATA (displayed as "Today") ===
            params_yesterday = {
                'period_from': yesterday_start.isoformat(),
                'period_to': yesterday_end.isoformat(),
                'page_size': 100,
                'order_by': 'period'
            }
            
            response_yesterday = self.session.get(url, auth=auth, params=params_yesterday, timeout=10)
            
            if response_yesterday.status_code == 200:
                data_yesterday = response_yesterday.json()
                results_yesterday = data_yesterday.get('results', [])
                
                kwh_today = round(sum(r.get('consumption', 0) for r in results_yesterday), 2)
                cost_today = round((kwh_today * current_rate) / 100, 2) if current_rate > 0 else 0.0
                
                # Most recent reading
                current_consumption = round(results_yesterday[0].get('consumption', 0), 2) if results_yesterday else 0.0
                
                self.debugLog(f"{dev.name}: Yesterday (as 'Today'): {len(results_yesterday)} readings, {kwh_today:.2f} kWh")
            else:
                self.logger.warning(f"{dev.name}: No yesterday data available (HTTP {response_yesterday.status_code})")
                kwh_today = 0.0
                cost_today = 0.0
                current_consumption = 0.0
            
            # === FETCH DAY BEFORE YESTERDAY'S DATA (displayed as "Yesterday") ===
            params_day_before = {
                'period_from': day_before_start.isoformat(),
                'period_to': day_before_end.isoformat(),
                'page_size': 100,
                'order_by': 'period'
            }
            
            response_day_before = self.session.get(url, auth=auth, params=params_day_before, timeout=10)
            
            if response_day_before.status_code == 200:
                data_day_before = response_day_before.json()
                results_day_before = data_day_before.get('results', [])
                kwh_yesterday = round(sum(r.get('consumption', 0) for r in results_day_before), 2)
                cost_yesterday = round((kwh_yesterday * current_rate) / 100, 2) if current_rate > 0 else 0.0
                
                self.debugLog(f"{dev.name}: Day before (as 'Yesterday'): {len(results_day_before)} readings, {kwh_yesterday:.2f} kWh")
            else:
                kwh_yesterday = 0.0
                cost_yesterday = 0.0
            
            # === FETCH MONTH TO DATE DATA (up to end of yesterday) ===
            params_mtd = {
                'period_from': month_start.isoformat(),
                'period_to': yesterday_end.isoformat(),  # Up to end of yesterday, not today
                'page_size': 1500,  # Enough for a full month of half-hourly data
                'order_by': 'period'
            }
            
            response_mtd = self.session.get(url, auth=auth, params=params_mtd, timeout=10)
            
            if response_mtd.status_code == 200:
                data_mtd = response_mtd.json()
                results_mtd = data_mtd.get('results', [])
                kwh_mtd = round(sum(r.get('consumption', 0) for r in results_mtd), 2)
                cost_mtd = round((kwh_mtd * current_rate) / 100, 2) if current_rate > 0 else 0.0
                
                self.debugLog(f"{dev.name}: Month to date: {len(results_mtd)} readings, {kwh_mtd:.2f} kWh")
            else:
                kwh_mtd = 0.0
                cost_mtd = 0.0
            
            # === UPDATE ALL STATES ===
            states = [
                {'key': 'status', 'value': 'Online'},
                {'key': 'currentConsumption', 'value': current_consumption, 
                 'uiValue': f'{current_consumption:.2f} kWh'},
                
                # "Today" = Yesterday's complete data (most recent available)
                {'key': 'kWhToday', 'value': kwh_today,
                 'uiValue': f'{kwh_today:.2f} kWh'},
                {'key': 'costToday', 'value': cost_today,
                 'uiValue': f'£{cost_today:.2f}'},
                
                # "Yesterday" = Day before yesterday
                {'key': 'kWhYesterday', 'value': kwh_yesterday,
                 'uiValue': f'{kwh_yesterday:.2f} kWh'},
                {'key': 'costYesterday', 'value': cost_yesterday,
                 'uiValue': f'£{cost_yesterday:.2f}'},
                
                # Month to date (up to end of yesterday)
                {'key': 'kWhMonthToDate', 'value': kwh_mtd,
                 'uiValue': f'{kwh_mtd:.2f} kWh'},
                {'key': 'costMonthToDate', 'value': cost_mtd,
                 'uiValue': f'£{cost_mtd:.2f}'},
                
                # Legacy states (for backwards compatibility)
                {'key': 'todayTotal', 'value': kwh_today,
                 'uiValue': f'{kwh_today:.2f} kWh'},
                {'key': 'todayCost', 'value': cost_today,
                 'uiValue': f'£{cost_today:.2f}'},
                
                {'key': 'lastUpdate', 'value': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            ]
            dev.updateStatesOnServer(states)
            
            self.logger.info(f"Updated {dev.name}: Today={kwh_today:.2f} kWh (£{cost_today:.2f}), "
                           f"Yesterday={kwh_yesterday:.2f} kWh, MTD={kwh_mtd:.2f} kWh")
            
            # Check consumption triggers
            self._check_consumption_triggers(dev)
                
        except Exception as e:
            self._log_error_gracefully(dev.name, "Consumption update", e)
            dev.updateStateOnServer('status', 'Error' if not self._is_network_error(e) else 'Network Error')

    def _update_export(self, dev, api_key, mpan, serial):
        """Fetch export data from API"""
        try:
            auth = HTTPBasicAuth(api_key, '')
            
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            today_start = uk_midnight(now.date())
            
            period_from = today_start.isoformat()
            period_to = now.isoformat()
            
            url = f"{API_BASE_URL}/electricity-meter-points/{mpan}/meters/{serial}/consumption/"
            
            params = {
                'period_from': period_from,
                'period_to': period_to,
                'page_size': 100,
                'order_by': 'period'
            }
            
            response = self.session.get(url, auth=auth, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                
                if results:
                    latest = results[0]
                    current_export = latest.get('consumption', 0)
                    today_total = sum(r.get('consumption', 0) for r in results)
                    
                    # Get export rate
                    export_rate = float(dev.states.get('currentRate', 0))
                    today_earnings = (today_total * export_rate) / 100
                    
                    states = [
                        {'key': 'status', 'value': 'Online'},
                        {'key': 'currentExport', 'value': current_export,
                         'uiValue': f'{current_export:.2f} kWh'},
                        {'key': 'todayTotal', 'value': today_total,
                         'uiValue': f'{today_total:.2f} kWh'},
                        {'key': 'todayEarnings', 'value': today_earnings,
                         'uiValue': f'£{today_earnings:.2f}'},
                        {'key': 'lastUpdate', 'value': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                    ]
                    dev.updateStatesOnServer(states)
                    
            elif response.status_code == 404:
                self.logger.warning(f"No export data available for {dev.name}")
                dev.updateStateOnServer('status', 'No Data')
        
        except Exception as e:
            self.logger.error(f"Export update error for {dev.name}: {str(e)}")
            dev.updateStateOnServer('status', 'Error')
    
    def _process_dual_rate(self, dev, day_response, night_response, now):
        """Process dual rate tariff (Economy 7, etc.)"""
        try:
            day_data = day_response.json()
            night_data = night_response.json()
            
            day_results = day_data.get('results', [])
            night_results = night_data.get('results', [])
            
            if not day_results or not night_results:
                return
            
            # Get current day and night rates
            day_rate_value = round(day_results[0]['value_inc_vat'], 2)
            night_rate_value = round(night_results[0]['value_inc_vat'], 2)
            
            # Determine current period based on night rate valid times
            night_valid_from = datetime.fromisoformat(night_results[0]['valid_from'].replace('Z', '+00:00'))
            night_valid_to_str = night_results[0].get('valid_to')
            
            if night_valid_to_str:
                night_valid_to = datetime.fromisoformat(night_valid_to_str.replace('Z', '+00:00'))
                in_night_period = night_valid_from <= now < night_valid_to
            else:
                in_night_period = False
            
            current_period = "Night" if in_night_period else "Day"
            current_rate = night_rate_value if in_night_period else day_rate_value
            
            # Check for negative rate
            is_negative = current_rate < 0
            
            states = [
                {'key': 'isDualRate', 'value': True},
                {'key': 'currentPeriod', 'value': current_period},
                {'key': 'dayRate', 'value': day_rate_value, 'uiValue': f'{day_rate_value:.2f} p/kWh'},
                {'key': 'nightRate', 'value': night_rate_value, 'uiValue': f'{night_rate_value:.2f} p/kWh'},
                {'key': 'currentRate', 'value': current_rate, 'uiValue': f'{current_rate:.2f} p/kWh'},
                {'key': 'isNegativeRate', 'value': is_negative}
            ]
            
            # Set cheap period based on night rate
            cheap_threshold = float(dev.pluginProps.get('cheapRateThreshold', 10.0))
            in_cheap_period = current_rate <= cheap_threshold
            states.append({'key': 'inCheapPeriod', 'value': in_cheap_period})
            
            dev.updateStatesOnServer(states)
            
            self.logger.debug(f"Updated dual rate for {dev.name}: Day={day_rate_value:.2f}, Night={night_rate_value:.2f}, Current Period={current_period}")
            
            # Check triggers
            self._check_rate_triggers(dev, current_rate, in_cheap_period)
            
        except Exception as e:
            self.logger.error(f"Dual rate processing error for {dev.name}: {str(e)}")
    
    def _fetch_standing_charges(self, dev, auth, product_code, tariff_code, now):
        """Fetch standing charges for electricity meter"""
        try:
            # Fetch standing charges
            standing_url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/"
            standing_params = {'period_from': now.isoformat(), 'page_size': 1}
            standing_response = self.session.get(standing_url, auth=auth, params=standing_params, timeout=10)
            
            if standing_response.status_code == 200:
                standing_data = standing_response.json()
                standing_results = standing_data.get('results', [])
                
                if standing_results:
                    standing_charge = round(standing_results[0]['value_inc_vat'], 2)
                    dev.updateStateOnServer('standingCharge', standing_charge, uiValue=f'{standing_charge:.2f} p/day')
                    self.debugLog(f"Current standing charge for {dev.name}: {standing_charge:.2f} p/day")
        
        except Exception as e:
            self.logger.error(f"Standing charge fetch error for {dev.name}: {str(e)}")
    
    def _find_active_agreement(self, agreements, meter_serial, meter_type="meter"):
        """Find the currently active agreement from a list of agreements
        
        Args:
            agreements: List of agreement dictionaries from API
            meter_serial: Serial number for logging
            meter_type: Type of meter for logging (e.g. "Electricity meter", "Gas meter")
            
        Returns:
            The active agreement dict, or None if not found
        """
        if not agreements:
            return None
        
        uk_tz = pytz.timezone('Europe/London')
        now = datetime.now(uk_tz)
        
        # Sort agreements by valid_from descending (newest first)
        try:
            sorted_agreements = sorted(
                agreements,
                key=lambda a: datetime.fromisoformat(a.get('valid_from', '1970-01-01T00:00:00Z').replace('Z', '+00:00')),
                reverse=True
            )
        except Exception as e:
            self.logger.error(f"{meter_type} {meter_serial}: Error sorting agreements: {e}")
            sorted_agreements = agreements
        
        # Debug: show all agreements
        self.logger.debug(f"{meter_type} {meter_serial}: Found {len(agreements)} agreements")
        for i, agr in enumerate(sorted_agreements):
            vf = agr.get('valid_from', 'N/A')
            vt = agr.get('valid_to', 'Ongoing')
            tc = agr.get('tariff_code', 'N/A')
            self.logger.debug(f"  Agreement {i+1}: {tc} (from {vf} to {vt})")
        
        # Find active agreement (valid_from <= now and (valid_to > now or valid_to is None))
        for agreement in sorted_agreements:
            valid_from_str = agreement.get('valid_from', '')
            valid_to_str = agreement.get('valid_to')
            
            if not valid_from_str:
                continue
            
            try:
                valid_from = datetime.fromisoformat(valid_from_str.replace('Z', '+00:00'))
                
                # Check if this agreement is active now
                if valid_from <= now:
                    if valid_to_str:
                        valid_to = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00'))
                        if now < valid_to:
                            self.logger.debug(f"{meter_type} {meter_serial}: Using active agreement (in date range): {agreement.get('tariff_code')}")
                            return agreement
                    else:
                        # No end date = ongoing
                        self.logger.debug(f"{meter_type} {meter_serial}: Using active agreement (ongoing): {agreement.get('tariff_code')}")
                        return agreement
            except Exception as e:
                self.logger.error(f"{meter_type} {meter_serial}: Error parsing agreement dates: {e}")
                continue
        
        # Fallback: use most recent (first in sorted list)
        if sorted_agreements:
            self.logger.warning(f"{meter_type} {meter_serial}: No active agreement found, using most recent: {sorted_agreements[0].get('tariff_code')}")
            return sorted_agreements[0]
        
        return None

    def _check_tariff_changes(self, auth, properties):
        """Check for tariff changes on all meters"""
        try:
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            for prop in properties:
                # Check electricity meters
                for elec_point in prop.get('electricity_meter_points', []):
                    mpan = elec_point.get('mpan')
                    is_export = elec_point.get('is_export', False)
                    
                    for meter in elec_point.get('meters', []):
                        serial = meter.get('serial_number')
                        
                        # Find matching device
                        device_type = 'exportMeter' if is_export else 'electricityMeter'
                        matching_dev = None
                        for dev in indigo.devices.iter("self." + device_type):
                            if dev.pluginProps.get('serialNumber') == serial:
                                matching_dev = dev
                                break
                        
                        if not matching_dev:
                            continue
                        
                        # Get agreements
                        agreements = elec_point.get('agreements', [])
                        if agreements:
                            self._update_meter_tariff_info(matching_dev, agreements, now, 'electricity')
                
                # Check gas meters
                for gas_point in prop.get('gas_meter_points', []):
                    mprn = gas_point.get('mprn')
                    
                    for meter in gas_point.get('meters', []):
                        serial = meter.get('serial_number')
                        
                        # Find matching device
                        matching_dev = None
                        for dev in indigo.devices.iter("self.gasMeter"):
                            if dev.pluginProps.get('serialNumber') == serial:
                                matching_dev = dev
                                break
                        
                        if not matching_dev:
                            continue
                        
                        # Get agreements
                        agreements = gas_point.get('agreements', [])
                        if agreements:
                            self._update_meter_tariff_info(matching_dev, agreements, now, 'gas')
        
        except Exception as e:
            self.logger.error(f"Tariff change check error: {str(e)}")
    
    def _update_meter_tariff_info(self, dev, agreements, now, fuel_type):
        """Update meter device with tariff information and detect changes"""
        try:
            # Find active agreement
            current_agreement = None
            upcoming_agreement = None
            
            for agreement in agreements:
                valid_from = datetime.fromisoformat(agreement.get('valid_from', '').replace('Z', '+00:00'))
                valid_to_str = agreement.get('valid_to')
                
                if valid_to_str:
                    valid_to = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00'))
                    # Active agreement (currently in effect)
                    if valid_from <= now < valid_to:
                        current_agreement = agreement
                    # Upcoming agreement (starts in future)
                    elif valid_from > now:
                        if not upcoming_agreement or valid_from < datetime.fromisoformat(upcoming_agreement.get('valid_from', '').replace('Z', '+00:00')):
                            upcoming_agreement = agreement
                else:
                    # No end date = current agreement
                    if valid_from <= now:
                        current_agreement = agreement
            
            # If no active agreement found, use most recent
            if not current_agreement and agreements:
                current_agreement = agreements[0]
            
            if not current_agreement:
                return
            
            # Extract tariff info
            tariff_code = current_agreement.get('tariff_code', '')
            valid_from_str = current_agreement.get('valid_from', '')
            valid_to_str = current_agreement.get('valid_to', '')
            
            # Extract product code from tariff code
            product_code = ""
            if tariff_code:
                if fuel_type == 'electricity':
                    # Format: E-1R-PRODUCT-REGION
                    parts = tariff_code.split('-')
                    if len(parts) >= 3:
                        product_code = '-'.join(parts[2:-1])
                else:  # gas
                    # Format: G-1R-PRODUCT-REGION
                    parts = tariff_code.split('-')
                    if len(parts) >= 3:
                        product_code = '-'.join(parts[2:-1])
            
            # Check if tariff has changed
            current_device_tariff = dev.pluginProps.get('tariffCode', '')
            tariff_has_changed = (tariff_code != current_device_tariff) and current_device_tariff != ''
            
            # Format dates for display
            valid_from_display = ""
            valid_to_display = ""
            if valid_from_str:
                valid_from_dt = datetime.fromisoformat(valid_from_str.replace('Z', '+00:00'))
                valid_from_display = valid_from_dt.strftime('%Y-%m-%d')
            if valid_to_str:
                valid_to_dt = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00'))
                valid_to_display = valid_to_dt.strftime('%Y-%m-%d')
            
            # Check if ending soon (within 30 days)
            ending_soon = False
            if valid_to_str:
                valid_to_dt = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00'))
                days_until_end = (valid_to_dt - now).days
                ending_soon = 0 < days_until_end <= 30
            
            # Get upcoming tariff info
            upcoming_tariff_str = ""
            if upcoming_agreement:
                upcoming_tariff_code = upcoming_agreement.get('tariff_code', '')
                upcoming_from_str = upcoming_agreement.get('valid_from', '')
                if upcoming_from_str:
                    upcoming_from_dt = datetime.fromisoformat(upcoming_from_str.replace('Z', '+00:00'))
                    upcoming_tariff_str = f"{upcoming_tariff_code} (from {upcoming_from_dt.strftime('%Y-%m-%d')})"
            
            # Update states
            states = [
                {'key': 'currentTariff', 'value': tariff_code},
                {'key': 'tariffValidFrom', 'value': valid_from_display},
                {'key': 'tariffValidTo', 'value': valid_to_display if valid_to_display else 'Ongoing'},
                {'key': 'tariffEndingSoon', 'value': ending_soon},
                {'key': 'upcomingTariff', 'value': upcoming_tariff_str},
                {'key': 'tariffChanged', 'value': tariff_has_changed}
            ]
            dev.updateStatesOnServer(states)
            
            # Auto-populate previous tariff info if not set and there was a recent change
            # This handles cases where tariff changed before this feature was added
            if not dev.states.get('previousTariffCode') or not dev.states.get('tariffChangeDate'):
                # Look for previous agreement that ended this month
                current_month_start = uk_midnight(now.date().replace(day=1))
                
                for agreement in agreements:
                    valid_from = datetime.fromisoformat(agreement.get('valid_from', '').replace('Z', '+00:00'))
                    valid_to_str = agreement.get('valid_to')
                    
                    if valid_to_str:
                        valid_to = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00'))
                        
                        # Check if this agreement ended this month (meaning new one started this month)
                        if current_month_start <= valid_to <= now and agreement != current_agreement:
                            # This is the previous agreement
                            previous_tariff_code = agreement.get('tariff_code', '')
                            previous_product = ""
                            
                            if previous_tariff_code:
                                parts = previous_tariff_code.split('-')
                                if len(parts) >= 3:
                                    previous_product = '-'.join(parts[2:-1])
                            
                            # The new agreement's valid_from is our change date
                            if current_agreement:
                                change_date_str = current_agreement.get('valid_from', '')
                                if change_date_str:
                                    change_dt = datetime.fromisoformat(change_date_str.replace('Z', '+00:00'))
                                    change_date_display = change_dt.strftime('%Y-%m-%d')
                                    
                                    dev.updateStatesOnServer([
                                        {'key': 'previousTariffCode', 'value': previous_tariff_code},
                                        {'key': 'previousProductCode', 'value': previous_product},
                                        {'key': 'tariffChangeDate', 'value': change_date_display}
                                    ])
                                    
                                    # Fetch rates for comparison
                                    api_key = self.pluginPrefs.get('apiKey', '')
                                    rate_msg = ""
                                    if api_key and previous_product and product_code:
                                        old_rate = self._get_current_rate_for_tariff(api_key, previous_product, previous_tariff_code)
                                        new_rate = self._get_current_rate_for_tariff(api_key, product_code, tariff_code)
                                        if old_rate is not None and new_rate is not None:
                                            change_pct = ((new_rate - old_rate) / old_rate) * 100
                                            direction = "↑" if new_rate > old_rate else "↓" if new_rate < old_rate else "="
                                            rate_msg = f" ({old_rate:.2f}p → {new_rate:.2f}p, {direction}{abs(change_pct):.1f}%)"
                                    
                                    self.logger.info(f"Auto-detected tariff change for {dev.name}: {previous_tariff_code} → {tariff_code} on {change_date_display}{rate_msg}")
                            break
            
            # If tariff changed, update device properties and log
            if tariff_has_changed:
                # Fetch old and new rates for comparison
                api_key = self.pluginPrefs.get('apiKey', '')
                old_rate = None
                new_rate = None
                
                if api_key and current_product:
                    old_rate = self._get_current_rate_for_tariff(api_key, current_product, current_device_tariff)
                if api_key and product_code:
                    new_rate = self._get_current_rate_for_tariff(api_key, product_code, tariff_code)
                
                # Build rate change message
                rate_msg = ""
                if old_rate is not None and new_rate is not None:
                    change_pct = ((new_rate - old_rate) / old_rate) * 100
                    direction = "↑" if new_rate > old_rate else "↓" if new_rate < old_rate else "="
                    rate_msg = f" ({old_rate:.2f}p → {new_rate:.2f}p, {direction}{abs(change_pct):.1f}%)"
                
                self.logger.warning(f"⚠️  TARIFF CHANGE DETECTED for {dev.name}")
                self.logger.warning(f"   Old: {current_device_tariff}")
                self.logger.warning(f"   New: {tariff_code}")
                self.logger.warning(f"   Valid from: {valid_from_display}")
                if rate_msg:
                    self.logger.warning(f"   Rate change: {old_rate:.2f}p/kWh → {new_rate:.2f}p/kWh ({direction}{abs(change_pct):.1f}%)")

                
                # Store previous tariff info for split cost calculations
                current_product = dev.pluginProps.get('productCode', '')
                dev.updateStatesOnServer([
                    {'key': 'previousTariffCode', 'value': current_device_tariff},
                    {'key': 'previousProductCode', 'value': current_product},
                    {'key': 'tariffChangeDate', 'value': valid_from_display}
                ])
                
                # Update device properties
                new_props = dev.pluginProps
                new_props['productCode'] = product_code
                new_props['tariffCode'] = tariff_code
                dev.replacePluginPropsOnServer(new_props)
                
                # Trigger rate re-fetch
                if dev.id in self.next_rate_update:
                    self.next_rate_update[dev.id] = 0
                
                # Fire trigger
                for trigger in indigo.triggers.iter("self"):
                    if not trigger.enabled:
                        continue
                    if trigger.pluginTypeId == "tariffChanged":
                        trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                        if trigger_dev_id == dev.id:
                            indigo.trigger.execute(trigger)
            
            # Check for ending soon trigger
            prev_ending_soon = dev.states.get('tariffEndingSoon', False)
            if ending_soon and not prev_ending_soon:
                self.logger.warning(f"⚠️  Tariff ending soon for {dev.name}: {tariff_code} expires {valid_to_display}")
                
                for trigger in indigo.triggers.iter("self"):
                    if not trigger.enabled:
                        continue
                    if trigger.pluginTypeId == "tariffEndingSoon":
                        trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                        if trigger_dev_id == dev.id:
                            indigo.trigger.execute(trigger)
        
        except Exception as e:
            self.logger.error(f"Tariff info update error for {dev.name}: {str(e)}")

    def _update_tariff_rates(self, dev, api_key):
        """Fetch and update tariff rates and standing charges for electricity meter"""
        try:
            auth = HTTPBasicAuth(api_key, '')
            product_code = dev.pluginProps.get('productCode', '')
            tariff_code = dev.pluginProps.get('tariffCode', '')
            
            if not product_code or not tariff_code:
                return
            
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            # === CHECK FOR DUAL RATE TARIFF (Economy 7, etc.) ===
            day_url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/day-unit-rates/"
            night_url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/night-unit-rates/"
            
            params = {
                'period_from': now.isoformat(),
                'page_size': 10
            }
            
            # Try to fetch day rates
            day_response = self.session.get(day_url, auth=auth, params=params, timeout=10)
            night_response = self.session.get(night_url, auth=auth, params=params, timeout=10)
            
            is_dual_rate = (day_response.status_code == 200 and night_response.status_code == 200)
            
            if is_dual_rate:
                # Process dual rate tariff
                self._process_dual_rate(dev, day_response, night_response, now)
                
                # Still need to fetch standing charges
                self._fetch_standing_charges(dev, auth, product_code, tariff_code, now)
                return
            
            # === FETCH STANDARD RATES (Single Rate Tariff) ===
            url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
            
            params = {
                'period_from': now.isoformat(),
                'page_size': 100
            }
            
            response = self.session.get(url, auth=auth, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                
                if results:
                    # Process rates (existing logic)
                    current_rate = results[0]
                    rate_value = round(current_rate['value_inc_vat'], 2)
                    valid_from = datetime.fromisoformat(current_rate['valid_from'].replace('Z', '+00:00'))
                    valid_to_str = current_rate.get('valid_to')
                    
                    # Check for negative rate
                    is_negative = rate_value < 0
                    
                    states = [
                        {'key': 'currentRate', 'value': rate_value,
                         'uiValue': f'{rate_value:.2f} p/kWh'},
                        {'key': 'isDualRate', 'value': False},
                        {'key': 'currentPeriod', 'value': 'Standard'},
                        {'key': 'isNegativeRate', 'value': is_negative}
                    ]
                    
                    # Find next rate change
                    next_rate = None
                    next_rate_time = None
                    
                    for rate in results[1:]:
                        rate_val = round(rate['value_inc_vat'], 2)
                        if abs(rate_val - rate_value) > 0.5:
                            next_rate = rate_val
                            next_rate_time = datetime.fromisoformat(rate['valid_from'].replace('Z', '+00:00'))
                            break
                    
                    if next_rate and next_rate_time:
                        states.append({
                            'key': 'nextRate',
                            'value': next_rate,
                            'uiValue': f'{next_rate:.2f} p/kWh'
                        })
                        states.append({
                            'key': 'nextRateChange',
                            'value': uk_str(next_rate_time)
                        })
                    
                    # Check for cheap period (Octopus Go, Agile, etc.)
                    cheap_threshold = float(dev.pluginProps.get('cheapRateThreshold', 10.0))
                    in_cheap_period = rate_value <= cheap_threshold
                    
                    states.append({'key': 'inCheapPeriod', 'value': in_cheap_period})
                    
                    # Find cheap period boundaries
                    if in_cheap_period:
                        cheap_start = valid_from
                        cheap_end = None
                        
                        for rate in results:
                            rate_val = round(rate['value_inc_vat'], 2)
                            if rate_val > cheap_threshold:
                                rate_time = datetime.fromisoformat(rate['valid_from'].replace('Z', '+00:00'))
                                if rate_time > now:
                                    cheap_end = rate_time
                                    break
                        
                        if cheap_end:
                            states.append({'key': 'cheapPeriodEnd', 'value': uk_str(cheap_end)})
                    else:
                        # Find next cheap period
                        for rate in results:
                            rate_val = round(rate['value_inc_vat'], 2)
                            if rate_val <= cheap_threshold:
                                rate_time = datetime.fromisoformat(rate['valid_from'].replace('Z', '+00:00'))
                                if rate_time > now:
                                    states.append({'key': 'cheapPeriodStart', 'value': uk_str(rate_time)})
                                    
                                    # Find end of this cheap period
                                    idx = results.index(rate)
                                    for future_rate in results[idx+1:]:
                                        if round(future_rate['value_inc_vat'], 2) > cheap_threshold:
                                            end_time = datetime.fromisoformat(future_rate['valid_from'].replace('Z', '+00:00'))
                                            states.append({'key': 'cheapPeriodEnd', 'value': uk_str(end_time)})
                                            break
                                    break
                    
                    dev.updateStatesOnServer(states)
                    self.debugLog(f"Current rate for {dev.name}: {rate_value:.2f} p/kWh")
                    
                    # Trigger rate change events
                    self._check_rate_triggers(dev, rate_value, in_cheap_period)
            
            # === FETCH STANDING CHARGE ===
            self._fetch_standing_charges(dev, auth, product_code, tariff_code, now)
                
        except Exception as e:
            self._log_error_gracefully(dev.name, "Rate/standing charge update", e)
        """Fetch and analyze tariff rates - CRITICAL for EV charging optimization"""
        product_code = dev.pluginProps.get('productCode', '')
        tariff_code = dev.pluginProps.get('tariffCode', '')
        
        if not product_code or not tariff_code:
            self.debugLog(f"No tariff info configured for {dev.name}, skipping rate update")
            return
        
        try:
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            # Fetch rates for next 48 hours to catch tomorrow's rates
            period_from = now.isoformat()
            tomorrow_end = now + timedelta(hours=48)
            period_to = tomorrow_end.isoformat()
            
            url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
            
            params = {
                'period_from': period_from,
                'period_to': period_to,
                'page_size': 100
            }
            
            response = self.session.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                
                if results:
                    # Find current rate
                    current_rate = None
                    next_rate = None
                    next_rate_time = None
                    
                    for rate in reversed(results):  # Reversed because API returns newest first
                        valid_from = datetime.fromisoformat(rate['valid_from'].replace('Z', '+00:00'))
                        valid_to_str = rate.get('valid_to')
                        
                        if valid_to_str:
                            valid_to = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00'))
                        else:
                            valid_to = None
                        
                        # Check if this rate is current
                        if valid_from <= now and (valid_to is None or now < valid_to):
                            current_rate = rate
                        
                        # Find next rate change
                        if valid_from > now and next_rate is None:
                            next_rate = rate
                            next_rate_time = valid_from
                    
                    if current_rate:
                        rate_value = current_rate['value_inc_vat']
                        
                        # Analyze for cheap periods (critical for EV charging)
                        cheap_threshold = float(dev.pluginProps.get('cheapRateThreshold', 10.0))
                        self._analyze_cheap_periods(dev, results, cheap_threshold, now)
                        
                        states = [
                            {'key': 'currentRate', 'value': rate_value,
                             'uiValue': f'{rate_value:.2f} p/kWh'}
                        ]
                        
                        if next_rate and next_rate_time:
                            next_rate_value = next_rate['value_inc_vat']
                            states.extend([
                                {'key': 'nextRate', 'value': next_rate_value,
                                 'uiValue': f'{next_rate_value:.2f} p/kWh'},
                                {'key': 'nextRateChange', 'value': uk_str(next_rate_time)}
                            ])
                        
                        dev.updateStatesOnServer(states)
                        
                        # Check if rate changed significantly (for triggers)
                        cached_rate = self.rate_cache.get(dev.id, {}).get('rate')
                        if cached_rate is not None and abs(rate_value - cached_rate) > 0.5:
                            self.logger.info(f"{dev.name}: Rate changed from {cached_rate:.2f}p to {rate_value:.2f}p")
                            
                            # Fire rate change trigger
                            for trigger in indigo.triggers.iter("self"):
                                if not trigger.enabled:
                                    continue
                                if trigger.pluginTypeId == "rateChange":
                                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                                    if trigger_dev_id == dev.id:
                                        indigo.trigger.execute(trigger)
                            
                            # Check threshold triggers
                            self._check_threshold_triggers(dev, cached_rate, rate_value)
                        
                        self.rate_cache[dev.id] = {'rate': rate_value, 'time': now}
                        
                        self.debugLog(f"Current rate for {dev.name}: {rate_value:.2f} p/kWh")
            
            elif response.status_code == 404:
                self.logger.warning(f"Tariff rates not found for {dev.name}")
        
        except Exception as e:
            self._log_error_gracefully(dev.name, "Rate update", e)

    def _update_gas_rates(self, dev, api_key, product_code, tariff_code):
        """Fetch gas tariff rates"""
        try:
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            # Gas tariff codes should be: G-1R-PRODUCT-REGION
            # If user entered something like "OE-FIX-15M-24-09-24-GAS", we need to convert it
            if not tariff_code.startswith('G-'):
                self.logger.warning(f"{dev.name}: Tariff code '{tariff_code}' doesn't start with 'G-'")
                self.logger.info(f"{dev.name}: Gas tariff codes should be format: G-1R-PRODUCT-REGION (e.g. G-1R-VAR-22-11-01-F)")
                self.logger.info(f"{dev.name}: Check your bill or Octopus dashboard for the correct code")
                return
            
            # Fetch current rate
            url = f"{API_BASE_URL}/products/{product_code}/gas-tariffs/{tariff_code}/standard-unit-rates/"
            
            self.debugLog(f"{dev.name}: Fetching gas rates from: {url}")
            
            params = {
                'period_from': now.isoformat(),
                'page_size': 10
            }
            
            auth = HTTPBasicAuth(api_key, '')
            response = self.session.get(url, auth=auth, params=params, timeout=10)
            
            self.debugLog(f"{dev.name}: Gas rate API response: HTTP {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                
                if results:
                    # Gas typically has fixed rates, not half-hourly
                    current_rate = results[0]
                    rate_value = round(current_rate['value_inc_vat'], 2)
                    
                    states = [
                        {'key': 'currentRate', 'value': rate_value,
                         'uiValue': f'{rate_value:.2f} p/kWh'}
                    ]
                    
                    dev.updateStatesOnServer(states)
                    
                    # Check if rate changed significantly (for triggers)
                    cached_rate = self.rate_cache.get(dev.id, {}).get('rate')
                    if cached_rate is not None and abs(rate_value - cached_rate) > 0.01:
                        self.logger.info(f"{dev.name}: Gas rate changed from {cached_rate:.2f}p to {rate_value:.2f}p")
                    
                    self.rate_cache[dev.id] = {'rate': rate_value, 'time': now}
                    
                    self.debugLog(f"Current gas rate for {dev.name}: {rate_value:.2f} p/kWh")
                else:
                    self.logger.warning(f"{dev.name}: API returned no rate data")
            
            elif response.status_code == 404:
                self.logger.error(f"{dev.name}: Gas tariff not found (404)")
                self.logger.error(f"  Product Code: {product_code}")
                self.logger.error(f"  Tariff Code: {tariff_code}")
                self.logger.error(f"  URL: {url}")
                self.logger.info(f"  Check these codes are correct - format should be:")
                self.logger.info(f"  Product: PRODUCT-NAME (e.g. VAR-22-11-01)")
                self.logger.info(f"  Tariff: G-1R-PRODUCT-NAME-REGION (e.g. G-1R-VAR-22-11-01-F)")
            else:
                self.logger.error(f"{dev.name}: Gas rate API error: HTTP {response.status_code}")
            
            # === FETCH STANDING CHARGE ===
            standing_url = f"{API_BASE_URL}/products/{product_code}/gas-tariffs/{tariff_code}/standing-charges/"
            
            standing_params = {
                'period_from': now.isoformat(),
                'page_size': 1
            }
            
            standing_response = self.session.get(standing_url, auth=auth, params=standing_params, timeout=10)
            
            if standing_response.status_code == 200:
                standing_data = standing_response.json()
                standing_results = standing_data.get('results', [])
                
                if standing_results:
                    standing_charge = round(standing_results[0]['value_inc_vat'], 2)
                    dev.updateStateOnServer('standingCharge', standing_charge, uiValue=f'{standing_charge:.2f} p/day')
                    self.debugLog(f"Current standing charge for {dev.name}: {standing_charge:.2f} p/day")
            else:
                self.debugLog(f"Could not fetch standing charge for {dev.name} (HTTP {standing_response.status_code})")
        
        except Exception as e:
            self._log_error_gracefully(dev.name, "Gas rate update", e)
    
    def _get_current_rate_for_tariff(self, api_key, product_code, tariff_code):
        """Fetch the current rate for a given tariff - for comparison purposes"""
        try:
            auth = HTTPBasicAuth(api_key, '')
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            # Determine if this is gas or electricity based on tariff code
            if tariff_code.startswith('G-'):
                # Gas tariff
                url = f"{API_BASE_URL}/products/{product_code}/gas-tariffs/{tariff_code}/standard-unit-rates/"
            else:
                # Electricity tariff - try standard first
                url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"
            
            params = {
                'period_from': now.isoformat(),
                'page_size': 1
            }
            
            response = self.session.get(url, auth=auth, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                if results:
                    return round(results[0]['value_inc_vat'], 2)
            
            return None
            
        except:
            return None

    def _analyze_cheap_periods(self, dev, rates, threshold, now):
        """Analyze rates to find cheap periods - KEY for EV charging triggers"""
        try:
            uk_tz = pytz.timezone('Europe/London')
            
            # Find all periods below threshold in next 24 hours
            cheap_periods = []
            
            for rate in reversed(rates):
                rate_value = rate['value_inc_vat']
                valid_from = datetime.fromisoformat(rate['valid_from'].replace('Z', '+00:00'))
                valid_to_str = rate.get('valid_to')
                
                if valid_to_str:
                    valid_to = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00'))
                else:
                    valid_to = valid_from + timedelta(hours=24)
                
                # Only look at future rates within 24 hours
                if valid_to < now or valid_from > now + timedelta(hours=24):
                    continue
                
                if rate_value <= threshold:
                    cheap_periods.append({
                        'start': valid_from,
                        'end': valid_to,
                        'rate': rate_value
                    })
            
            # Check if we're currently in a cheap period
            in_cheap_period = False
            current_cheap_start = None
            current_cheap_end = None
            next_cheap_start = None
            next_cheap_end = None
            
            for period in cheap_periods:
                if period['start'] <= now < period['end']:
                    in_cheap_period = True
                    current_cheap_start = period['start']
                    current_cheap_end = period['end']
                elif period['start'] > now and next_cheap_start is None:
                    next_cheap_start = period['start']
                    next_cheap_end = period['end']
            
            # Update states
            states = [
                {'key': 'inCheapPeriod', 'value': in_cheap_period}
            ]
            
            if current_cheap_start and current_cheap_end:
                states.extend([
                    {'key': 'cheapPeriodStart', 'value': uk_str(current_cheap_start)},
                    {'key': 'cheapPeriodEnd', 'value': uk_str(current_cheap_end)}
                ])
            elif next_cheap_start and next_cheap_end:
                states.extend([
                    {'key': 'cheapPeriodStart', 'value': uk_str(next_cheap_start)},
                    {'key': 'cheapPeriodEnd', 'value': uk_str(next_cheap_end)}
                ])
            
            dev.updateStatesOnServer(states)
            
            # Trigger events for EV charging automation
            was_in_cheap_period = dev.states.get('inCheapPeriod', False)
            
            if in_cheap_period and not was_in_cheap_period:
                self.logger.info(f"{dev.name}: Entering cheap rate period")
                for trigger in indigo.triggers.iter("self"):
                    if not trigger.enabled:
                        continue
                    if trigger.pluginTypeId == "cheapPeriodStart":
                        trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                        if trigger_dev_id == dev.id:
                            indigo.trigger.execute(trigger)
            elif not in_cheap_period and was_in_cheap_period:
                self.logger.info(f"{dev.name}: Leaving cheap rate period")
                for trigger in indigo.triggers.iter("self"):
                    if not trigger.enabled:
                        continue
                    if trigger.pluginTypeId == "cheapPeriodEnd":
                        trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                        if trigger_dev_id == dev.id:
                            indigo.trigger.execute(trigger)
            
        except Exception as e:
            self.logger.error(f"Cheap period analysis error: {str(e)}")

    def _check_threshold_triggers(self, dev, old_rate, new_rate):
        """Check if rate crossed any trigger thresholds"""
        try:
            # Get all triggers for this device
            for trigger in indigo.triggers.iter("self"):
                if not trigger.enabled:
                    continue
                
                if trigger.pluginTypeId == "rateBelowThreshold":
                    # Check if device matches
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    threshold = float(trigger.pluginProps.get("thresholdRate", 10.0))
                    
                    # Rate crossed below threshold
                    if old_rate >= threshold and new_rate < threshold:
                        self.logger.info(f"{dev.name}: Rate dropped below {threshold}p/kWh")
                        indigo.trigger.execute(trigger)
                
                elif trigger.pluginTypeId == "rateAboveThreshold":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    threshold = float(trigger.pluginProps.get("thresholdRate", 20.0))
                    
                    # Rate crossed above threshold
                    if old_rate < threshold and new_rate >= threshold:
                        self.logger.info(f"{dev.name}: Rate rose above {threshold}p/kWh")
                        indigo.trigger.execute(trigger)
        
        except Exception as e:
            self.logger.error(f"Threshold trigger check error: {str(e)}")

    def _check_consumption_triggers(self, dev):
        """Check if consumption crossed any trigger thresholds"""
        try:
            today_total = float(dev.states.get('todayTotal', 0))
            
            for trigger in indigo.triggers.iter("self"):
                if not trigger.enabled:
                    continue
                
                if trigger.pluginTypeId == "highConsumption":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    threshold = float(trigger.pluginProps.get("thresholdKwh", 50.0))
                    
                    # Check if we just crossed the threshold
                    if today_total >= threshold:
                        # Only fire once per day - check if we already fired today
                        last_fire_date = trigger.globalProps.get("lastFireDate", "")
                        today_date = datetime.now().strftime('%Y-%m-%d')
                        
                        if last_fire_date != today_date:
                            self.logger.info(f"{dev.name}: Consumption exceeded {threshold} kWh today")
                            indigo.trigger.execute(trigger)
                            trigger.globalProps["lastFireDate"] = today_date
                            trigger.replaceOnServer()
        
        except Exception as e:
            self.logger.error(f"Consumption trigger check error: {str(e)}")
    
    def _check_rate_triggers(self, dev, current_rate, in_cheap_period):
        """Check if rate crossed any trigger thresholds"""
        try:
            for trigger in indigo.triggers.iter("self"):
                if not trigger.enabled:
                    continue
                
                # Check trigger type
                if trigger.pluginTypeId == "cheapPeriodStart":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    # Fire when entering cheap period
                    if in_cheap_period:
                        prev_state = trigger.globalProps.get("inCheapPeriod", False)
                        if not prev_state:
                            self.logger.info(f"{dev.name}: Cheap period started")
                            indigo.trigger.execute(trigger)
                            trigger.globalProps["inCheapPeriod"] = True
                            trigger.replaceOnServer()
                
                elif trigger.pluginTypeId == "cheapPeriodEnd":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    # Fire when leaving cheap period
                    if not in_cheap_period:
                        prev_state = trigger.globalProps.get("inCheapPeriod", True)
                        if prev_state:
                            self.logger.info(f"{dev.name}: Cheap period ended")
                            indigo.trigger.execute(trigger)
                            trigger.globalProps["inCheapPeriod"] = False
                            trigger.replaceOnServer()
                
                elif trigger.pluginTypeId == "rateChanged":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    # Fire when rate changes significantly (more than 0.5p)
                    prev_rate = float(trigger.globalProps.get("lastRate", current_rate))
                    if abs(current_rate - prev_rate) > 0.5:
                        self.logger.info(f"{dev.name}: Rate changed from {prev_rate:.2f} to {current_rate:.2f} p/kWh")
                        indigo.trigger.execute(trigger)
                        trigger.globalProps["lastRate"] = current_rate
                        trigger.replaceOnServer()
                
                elif trigger.pluginTypeId == "rateBelowThreshold":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    threshold = float(trigger.pluginProps.get("thresholdRate", 10.0))
                    if current_rate <= threshold:
                        prev_below = trigger.globalProps.get("wasBelowThreshold", False)
                        if not prev_below:
                            self.logger.info(f"{dev.name}: Rate fell below {threshold:.2f} p/kWh")
                            indigo.trigger.execute(trigger)
                            trigger.globalProps["wasBelowThreshold"] = True
                            trigger.replaceOnServer()
                    else:
                        trigger.globalProps["wasBelowThreshold"] = False
                        trigger.replaceOnServer()
                
                elif trigger.pluginTypeId == "rateAboveThreshold":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    threshold = float(trigger.pluginProps.get("thresholdRate", 25.0))
                    if current_rate >= threshold:
                        prev_above = trigger.globalProps.get("wasAboveThreshold", False)
                        if not prev_above:
                            self.logger.info(f"{dev.name}: Rate rose above {threshold:.2f} p/kWh")
                            indigo.trigger.execute(trigger)
                            trigger.globalProps["wasAboveThreshold"] = True
                            trigger.replaceOnServer()
                    else:
                        trigger.globalProps["wasAboveThreshold"] = False
                        trigger.replaceOnServer()
                
                elif trigger.pluginTypeId == "negativeRate":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    # Fire when rate goes negative
                    is_negative = current_rate < 0
                    prev_negative = trigger.globalProps.get("wasNegative", False)
                    
                    if is_negative and not prev_negative:
                        self.logger.info(f"{dev.name}: Rate gone NEGATIVE! Currently {current_rate:.2f} p/kWh")
                        indigo.trigger.execute(trigger)
                        trigger.globalProps["wasNegative"] = True
                        trigger.replaceOnServer()
                    elif not is_negative and prev_negative:
                        trigger.globalProps["wasNegative"] = False
                        trigger.replaceOnServer()
                
                elif trigger.pluginTypeId == "periodChange":
                    trigger_dev_id = int(trigger.pluginProps.get("deviceId", 0))
                    if trigger_dev_id != dev.id:
                        continue
                    
                    # Fire when period changes (Day<->Night for Economy 7)
                    current_period = dev.states.get('currentPeriod', 'Standard')
                    prev_period = trigger.globalProps.get("lastPeriod", current_period)
                    
                    if current_period != prev_period:
                        self.logger.info(f"{dev.name}: Rate period changed from {prev_period} to {current_period}")
                        indigo.trigger.execute(trigger)
                        trigger.globalProps["lastPeriod"] = current_period
                        trigger.replaceOnServer()
        
        except Exception as e:
            self.logger.error(f"Rate trigger check error: {str(e)}")

    ####################
    # Actions
    ####################
    
    def actionControlDevice(self, action, dev):
        """Handle device actions"""
        if action.deviceAction == indigo.kDeviceAction.RequestStatus:
            self.logger.info(f"Status request for {dev.name}")
            # Force immediate update
            if dev.id in self.next_consumption_update:
                self.next_consumption_update[dev.id] = 0
            if dev.id in self.next_rate_update:
                self.next_rate_update[dev.id] = 0

    def refreshRatesAction(self, pluginAction, dev):
        """Action: fetch tariff rates on the next loop pass (within a minute)"""
        self.next_rate_update[dev.id] = 0
        self.logger.info(f"{dev.name}: Rate refresh requested")

    def refreshConsumptionAction(self, pluginAction, dev):
        """Action: fetch consumption (and Home Mini data) on the next loop pass"""
        self.next_consumption_update[dev.id] = 0
        self.next_live_update.pop(dev.id, None)
        self.logger.info(f"{dev.name}: Consumption refresh requested")

    def discoverAndCreateMeters(self, pluginAction, dev):
        """Action to discover meters and automatically create devices with all details populated"""
        api_key = self.pluginPrefs.get('apiKey', '')
        account_num = dev.pluginProps.get('accountNumber', '')
        
        if not api_key or not account_num:
            self.logger.error("API key and account number required. Please configure account device first.")
            return
        
        try:
            auth = HTTPBasicAuth(api_key, '')
            response = self.session.get(
                f"{API_BASE_URL}/accounts/{account_num}/",
                auth=auth,
                timeout=10
            )
            
            if response.status_code != 200:
                self.logger.error(f"Failed to fetch account data: HTTP {response.status_code}")
                return
            
            data = response.json()
            properties = data.get('properties', [])
            
            if not properties:
                self.logger.warning("No properties found for this account")
                return
            
            created_count = 0
            default_threshold = pluginAction.props.get('cheapRateThreshold', '10.0')
            
            for prop in properties:
                # Process Electricity meters
                for elec_point in prop.get('electricity_meter_points', []):
                    mpan = elec_point.get('mpan')
                    is_export = elec_point.get('is_export', False)
                    
                    for meter in elec_point.get('meters', []):
                        serial = meter.get('serial_number')
                        
                        # Get current tariff details
                        agreements = elec_point.get('agreements', [])
                        current_tariff = self._find_active_agreement(agreements, serial, "Electricity meter")
                        
                        if not current_tariff:
                            self.logger.warning(f"Electricity meter {serial}: No agreements found in API response")
                        
                        # Extract tariff details
                        product_code = ""
                        tariff_code = ""
                        
                        if current_tariff:
                            tariff_code = current_tariff.get('tariff_code', '')
                            # Extract product code from tariff code (format: E-1R-PRODUCT-REGION)
                            if tariff_code:
                                parts = tariff_code.split('-')
                                if len(parts) >= 3:
                                    # Reconstruct product code (everything except E-1R and last part)
                                    product_code = '-'.join(parts[2:-1])
                        
                        # Create device
                        device_type = 'exportMeter' if is_export else 'electricityMeter'
                        device_name = f"{'Export' if is_export else 'Electricity'} - {serial}"
                        
                        # Check if device already exists
                        existing = None
                        for existing_dev in indigo.devices.iter("self." + device_type):
                            if existing_dev.pluginProps.get('serialNumber') == serial:
                                existing = existing_dev
                                break
                        
                        if existing:
                            self.logger.info(f"Device already exists: {device_name} (skipping)")
                            continue
                        
                        # Create new device
                        new_dev = indigo.device.create(
                            protocol=indigo.kProtocol.Plugin,
                            address=serial,
                            name=device_name,
                            description=f"Auto-created from Octopus account",
                            pluginId=self.pluginId,
                            deviceTypeId=device_type,
                            props={
                                'mpanMprn': mpan,
                                'serialNumber': serial,
                                'productCode': product_code,
                                'tariffCode': tariff_code,
                                'cheapRateThreshold': default_threshold if not is_export else '0.0',
                                'autoCreated': True
                            },
                            folder=0
                        )
                        
                        self.logger.info(f"✓ Created {device_name}")
                        self.logger.info(f"  MPAN: {mpan}")
                        self.logger.info(f"  Serial: {serial}")
                        self.logger.info(f"  Product: {product_code}")
                        self.logger.info(f"  Tariff: {tariff_code}")
                        
                        created_count += 1
                
                # Process Gas meters
                for gas_point in prop.get('gas_meter_points', []):
                    mprn = gas_point.get('mprn')
                    
                    for meter in gas_point.get('meters', []):
                        serial = meter.get('serial_number')
                        
                        # Get current tariff
                        agreements = gas_point.get('agreements', [])
                        current_tariff = self._find_active_agreement(agreements, serial, "Gas meter")
                        
                        if not current_tariff:
                            self.logger.warning(f"Gas meter {serial}: No agreements found in API response")
                        
                        product_code = ""
                        tariff_code = ""
                        
                        if current_tariff:
                            tariff_code = current_tariff.get('tariff_code', '')
                            if tariff_code:
                                parts = tariff_code.split('-')
                                if len(parts) >= 3:
                                    product_code = '-'.join(parts[2:-1])
                        
                        device_name = f"Gas - {serial}"
                        
                        # Check if exists
                        existing = None
                        for existing_dev in indigo.devices.iter("self.gasMeter"):
                            if existing_dev.pluginProps.get('serialNumber') == serial:
                                existing = existing_dev
                                break
                        
                        if existing:
                            self.logger.info(f"Device already exists: {device_name} (skipping)")
                            continue
                        
                        # Create new device
                        new_dev = indigo.device.create(
                            protocol=indigo.kProtocol.Plugin,
                            address=serial,
                            name=device_name,
                            description=f"Auto-created from Octopus account",
                            pluginId=self.pluginId,
                            deviceTypeId='gasMeter',
                            props={
                                'mpanMprn': mprn,
                                'serialNumber': serial,
                                'productCode': product_code,
                                'tariffCode': tariff_code,
                                'autoCreated': True
                            },
                            folder=0
                        )
                        
                        self.logger.info(f"✓ Created {device_name}")
                        self.logger.info(f"  MPRN: {mprn}")
                        self.logger.info(f"  Serial: {serial}")
                        self.logger.info(f"  Product: {product_code}")
                        self.logger.info(f"  Tariff: {tariff_code}")
                        
                        created_count += 1
            
            # Update account device with meter count
            states = [
                {'key': 'meterCount', 'value': created_count}
            ]
            dev.updateStatesOnServer(states)
            
            if created_count > 0:
                self.logger.info(f"✓✓✓ Successfully created {created_count} meter device(s)")
                self.logger.info("All devices are enabled and will start updating within 1 minute")
            else:
                self.logger.info("No new meters to create (all already exist)")
        
        except Exception as e:
            if self._is_network_error(e):
                self.logger.warning("Discovery failed - Network unavailable")
            else:
                self.logger.error(f"Discovery error: {str(e)}")
                import traceback
                self.logger.error(traceback.format_exc())

    ####################
    # Menu Methods
    ####################
    
    def getDeviceConfigUiValues(self, pluginProps, typeId, devId):
        """Provide default values for device config"""
        valuesDict = pluginProps
        errorsDict = indigo.Dict()
        
        return (valuesDict, errorsDict)

    def discoverAccount(self, valuesDict, typeId, devId):
        """Button callback to discover and populate account number"""
        errorsDict = indigo.Dict()
        
        api_key = self.pluginPrefs.get('apiKey', '')
        if not api_key:
            errorsDict['accountNumber'] = "Please configure your API key in Plugin Settings first"
            return (valuesDict, errorsDict)
        
        try:
            auth = HTTPBasicAuth(api_key, '')
            
            # Try to list accounts (may not be permitted for all API keys)
            response = self.session.get(
                f"{API_BASE_URL}/accounts/",
                auth=auth,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                accounts = data.get('results', [])
                
                if not accounts:
                    errorsDict['accountNumber'] = "No accounts found for this API key"
                    self.logger.warning("API returned no accounts. You may need to enter your account number manually.")
                    return (valuesDict, errorsDict)
                
                # Use first account (most users have only one)
                account = accounts[0]
                account_number = account.get('number', '')
                
                valuesDict['accountNumber'] = account_number
                
                self.logger.info(f"Account discovered: {account_number}")
                
                # Show success message
                if len(accounts) > 1:
                    self.logger.info(f"Found {len(accounts)} accounts. Using first one: {account_number}")
                
            elif response.status_code == 401:
                errorsDict['accountNumber'] = "Invalid API key"
                self.logger.error("API key authentication failed. Please check your API key in Plugin Settings.")
                return (valuesDict, errorsDict)
                
            elif response.status_code == 403:
                # Account listing not permitted - need to enter manually
                self.logger.warning("API key doesn't have permission to list accounts (403 error)")
                self.logger.info("=" * 70)
                self.logger.info("MANUAL ACCOUNT NUMBER ENTRY REQUIRED")
                self.logger.info("=" * 70)
                self.logger.info("Your API key works, but doesn't have permission to auto-discover accounts.")
                self.logger.info("")
                self.logger.info("Please find your account number manually:")
                self.logger.info("  1. Go to: https://octopus.energy/dashboard/")
                self.logger.info("  2. Your account number is shown on your dashboard")
                self.logger.info("  3. Format: A-XXXXXXXX (letter A, hyphen, 8 characters)")
                self.logger.info("  4. Enter it in the Account Number field above")
                self.logger.info("")
                self.logger.info("OR check your Octopus Energy bill for your account number.")
                self.logger.info("=" * 70)
                
                errorsDict['accountNumber'] = "Auto-discovery not permitted. Please enter account number manually (see Event Log for instructions)."
                return (valuesDict, errorsDict)
                
            else:
                errorsDict['accountNumber'] = f"API error: {response.status_code}"
                self.logger.error(f"Unexpected API response: {response.status_code}")
                return (valuesDict, errorsDict)
        
        except Exception as e:
            self.logger.error(f"Account discovery error: {str(e)}")
            errorsDict['accountNumber'] = f"Error: {str(e)}"
            return (valuesDict, errorsDict)
        
        return (valuesDict, errorsDict)

    def discoverAndCreateMetersButton(self, valuesDict, typeId, devId):
        """Button callback to discover meters and create devices - called from Account device config"""
        errorsDict = indigo.Dict()
        
        api_key = self.pluginPrefs.get('apiKey', '')
        account_num = valuesDict.get('accountNumber', '').strip()
        
        if not api_key:
            errorsDict['showAlertText'] = "Please configure your API key in Plugin Settings first."
            return (valuesDict, errorsDict)
        
        if not account_num:
            errorsDict['accountNumber'] = "Please enter your account number first"
            return (valuesDict, errorsDict)
        
        # Validate account number format
        if not account_num.startswith('A-'):
            errorsDict['accountNumber'] = "Account number should start with 'A-' (e.g., A-12345678)"
            return (valuesDict, errorsDict)
        
        try:
            auth = HTTPBasicAuth(api_key, '')
            response = self.session.get(
                f"{API_BASE_URL}/accounts/{account_num}/",
                auth=auth,
                timeout=10
            )
            
            if response.status_code != 200:
                self.logger.error(f"Failed to fetch account data: HTTP {response.status_code}")
                errorsDict['showAlertText'] = f"Failed to fetch account data: HTTP {response.status_code}. Check Event Log for details."
                return (valuesDict, errorsDict)
            
            data = response.json()
            properties = data.get('properties', [])
            
            if not properties:
                self.logger.warning("No properties found for this account")
                errorsDict['showAlertText'] = "No properties (meters) found for this account."
                return (valuesDict, errorsDict)
            
            created_count = 0
            default_threshold = valuesDict.get('cheapRateThreshold', '10.0')
            
            self.logger.info("=" * 70)
            self.logger.info("DISCOVERING AND CREATING METERS")
            self.logger.info("=" * 70)
            
            for prop in properties:
                # Process Electricity meters
                for elec_point in prop.get('electricity_meter_points', []):
                    mpan = elec_point.get('mpan')
                    is_export = elec_point.get('is_export', False)
                    
                    for meter in elec_point.get('meters', []):
                        serial = meter.get('serial_number')
                        
                        # Get current tariff details
                        agreements = elec_point.get('agreements', [])
                        current_tariff = self._find_active_agreement(agreements, serial, "Electricity meter")
                        
                        if not current_tariff:
                            self.logger.warning(f"Electricity meter {serial}: No agreements found in API response")
                        
                        # Extract tariff details
                        product_code = ""
                        tariff_code = ""
                        
                        if current_tariff:
                            tariff_code = current_tariff.get('tariff_code', '')
                            # Extract product code from tariff code (format: E-1R-PRODUCT-REGION)
                            if tariff_code:
                                parts = tariff_code.split('-')
                                if len(parts) >= 3:
                                    # Reconstruct product code (everything except E-1R and last part)
                                    product_code = '-'.join(parts[2:-1])
                        
                        # Create device
                        device_type = 'exportMeter' if is_export else 'electricityMeter'
                        device_name = f"{'Export' if is_export else 'Electricity'} - {serial}"
                        
                        # Check if device already exists
                        existing = None
                        for existing_dev in indigo.devices.iter("self." + device_type):
                            if existing_dev.pluginProps.get('serialNumber') == serial:
                                existing = existing_dev
                                break
                        
                        if existing:
                            self.logger.info(f"Device already exists: {device_name} (skipping)")
                            continue
                        
                        # Create new device
                        new_dev = indigo.device.create(
                            protocol=indigo.kProtocol.Plugin,
                            address=serial,
                            name=device_name,
                            description=f"Auto-created from Octopus account",
                            pluginId=self.pluginId,
                            deviceTypeId=device_type,
                            props={
                                'mpanMprn': mpan,
                                'serialNumber': serial,
                                'productCode': product_code,
                                'tariffCode': tariff_code,
                                'cheapRateThreshold': default_threshold if not is_export else '0.0',
                                'autoCreated': True
                            },
                            folder=0
                        )
                        
                        self.logger.info(f"✓ Created {device_name}")
                        self.logger.info(f"  MPAN: {mpan}")
                        self.logger.info(f"  Serial: {serial}")
                        self.logger.info(f"  Product: {product_code}")
                        self.logger.info(f"  Tariff: {tariff_code}")
                        
                        created_count += 1
                
                # Process Gas meters
                for gas_point in prop.get('gas_meter_points', []):
                    mprn = gas_point.get('mprn')
                    
                    for meter in gas_point.get('meters', []):
                        serial = meter.get('serial_number')
                        
                        # Get current tariff
                        agreements = gas_point.get('agreements', [])
                        current_tariff = self._find_active_agreement(agreements, serial, "Gas meter")
                        
                        if not current_tariff:
                            self.logger.warning(f"Gas meter {serial}: No agreements found in API response")
                        
                        product_code = ""
                        tariff_code = ""
                        
                        if current_tariff:
                            tariff_code = current_tariff.get('tariff_code', '')
                            if tariff_code:
                                parts = tariff_code.split('-')
                                if len(parts) >= 3:
                                    product_code = '-'.join(parts[2:-1])
                                self.logger.debug(f"Gas meter {serial}: Extracted product '{product_code}' from tariff '{tariff_code}'")
                            else:
                                self.logger.warning(f"Gas meter {serial}: Agreement has no tariff_code")
                        
                        if not tariff_code:
                            self.logger.warning(f"Gas meter {serial}: Will be created without tariff info - rates will show as 0.00")
                            self.logger.info(f"  You can manually add tariff info by editing the device later")
                        
                        device_name = f"Gas - {serial}"
                        
                        # Check if exists
                        existing = None
                        for existing_dev in indigo.devices.iter("self.gasMeter"):
                            if existing_dev.pluginProps.get('serialNumber') == serial:
                                existing = existing_dev
                                break
                        
                        if existing:
                            self.logger.info(f"Device already exists: {device_name} (skipping)")
                            continue
                        
                        # Create new device
                        new_dev = indigo.device.create(
                            protocol=indigo.kProtocol.Plugin,
                            address=serial,
                            name=device_name,
                            description=f"Auto-created from Octopus account",
                            pluginId=self.pluginId,
                            deviceTypeId='gasMeter',
                            props={
                                'mpanMprn': mprn,
                                'serialNumber': serial,
                                'productCode': product_code,
                                'tariffCode': tariff_code,
                                'autoCreated': True
                            },
                            folder=0
                        )
                        
                        self.logger.info(f"✓ Created {device_name}")
                        self.logger.info(f"  MPRN: {mprn}")
                        self.logger.info(f"  Serial: {serial}")
                        if product_code:
                            self.logger.info(f"  Product: {product_code}")
                        else:
                            self.logger.info(f"  Product: (not available from API)")
                        if tariff_code:
                            self.logger.info(f"  Tariff: {tariff_code}")
                        else:
                            self.logger.info(f"  Tariff: (not available from API - edit device to add manually)")
                        
                        created_count += 1
            
            # Update account device with meter count if this is being edited
            if devId:
                try:
                    dev = indigo.devices[devId]
                    states = [
                        {'key': 'meterCount', 'value': created_count},
                        {'key': 'accountNumber', 'value': account_num}
                    ]
                    dev.updateStatesOnServer(states)
                except:
                    pass  # Device may not exist yet
            
            self.logger.info("=" * 70)
            if created_count > 0:
                self.logger.info(f"✓✓✓ Successfully created {created_count} meter device(s)")
                self.logger.info("All devices are enabled and will start updating within 1 minute")
                errorsDict['showAlertText'] = f"Success! Created {created_count} meter device(s). Check Event Log for details."
            else:
                self.logger.info("No new meters to create (all already exist)")
                errorsDict['showAlertText'] = "No new meters created - all meters already exist."
            self.logger.info("=" * 70)
        
        except Exception as e:
            if self._is_network_error(e):
                self.logger.warning("Discovery failed - Network unavailable")
                errorsDict['showAlertText'] = "Discovery failed - Network unavailable. Please check your internet connection."
            else:
                self.logger.error(f"Discovery error: {str(e)}")
                import traceback
                self.logger.error(traceback.format_exc())
                errorsDict['showAlertText'] = f"Error during discovery: {str(e)}. Check Event Log for details."
            return (valuesDict, errorsDict)
        
        return (valuesDict, errorsDict)
        """Button callback to discover and populate account number"""
        errorsDict = indigo.Dict()
        
        api_key = self.pluginPrefs.get('apiKey', '')
        if not api_key:
            errorsDict['accountNumber'] = "Please configure your API key in Plugin Settings first"
            return (valuesDict, errorsDict)
        
        try:
            auth = HTTPBasicAuth(api_key, '')
            
            # Try to list accounts (may not be permitted for all API keys)
            response = self.session.get(
                f"{API_BASE_URL}/accounts/",
                auth=auth,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                accounts = data.get('results', [])
                
                if not accounts:
                    errorsDict['accountNumber'] = "No accounts found for this API key"
                    self.logger.warning("API returned no accounts. You may need to enter your account number manually.")
                    return (valuesDict, errorsDict)
                
                # Use first account (most users have only one)
                account = accounts[0]
                account_number = account.get('number', '')
                
                valuesDict['accountNumber'] = account_number
                
                self.logger.info(f"Account discovered: {account_number}")
                
                # Show success message
                if len(accounts) > 1:
                    self.logger.info(f"Found {len(accounts)} accounts. Using first one: {account_number}")
                
            elif response.status_code == 401:
                errorsDict['accountNumber'] = "Invalid API key"
                self.logger.error("API key authentication failed. Please check your API key in Plugin Settings.")
                return (valuesDict, errorsDict)
                
            elif response.status_code == 403:
                # Account listing not permitted - need to enter manually
                self.logger.warning("API key doesn't have permission to list accounts (403 error)")
                self.logger.info("=" * 70)
                self.logger.info("MANUAL ACCOUNT NUMBER ENTRY REQUIRED")
                self.logger.info("=" * 70)
                self.logger.info("Your API key works, but doesn't have permission to auto-discover accounts.")
                self.logger.info("")
                self.logger.info("Please find your account number manually:")
                self.logger.info("  1. Go to: https://octopus.energy/dashboard/")
                self.logger.info("  2. Your account number is shown on your dashboard")
                self.logger.info("  3. Format: A-XXXXXXXX (letter A, hyphen, 8 characters)")
                self.logger.info("  4. Click 'Cancel', edit this device, and type it in the Account Number field")
                self.logger.info("")
                self.logger.info("OR check your Octopus Energy bill for your account number.")
                self.logger.info("=" * 70)
                
                errorsDict['accountNumber'] = "Auto-discovery not permitted. Please enter account number manually (see log for instructions)."
                return (valuesDict, errorsDict)
                
            else:
                errorsDict['accountNumber'] = f"API error: {response.status_code}"
                self.logger.error(f"Unexpected API response: {response.status_code}")
                return (valuesDict, errorsDict)
        
        except Exception as e:
            self.logger.error(f"Account discovery error: {str(e)}")
            errorsDict['accountNumber'] = f"Error: {str(e)}"
            return (valuesDict, errorsDict)
        
        return (valuesDict, errorsDict)

    ####################
    # Historic Device Methods
    ####################
    
    def getElectricityMeters(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Return list of electricity meters for dropdown"""
        menu = []
        for dev in indigo.devices.iter("self.electricityMeter"):
            serial = dev.pluginProps.get('serialNumber', 'Unknown')
            menu.append((serial, dev.name))
        return menu
    
    def getGasMeters(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Return list of gas meters for dropdown"""
        menu = []
        for dev in indigo.devices.iter("self.gasMeter"):
            serial = dev.pluginProps.get('serialNumber', 'Unknown')
            menu.append((serial, dev.name))
        return menu
    
    def getExportMeters(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Return list of export meters for dropdown"""
        menu = []
        for dev in indigo.devices.iter("self.exportMeter"):
            serial = dev.pluginProps.get('serialNumber', 'Unknown')
            menu.append((serial, dev.name))
        return menu
    
    def createHistoricDevicesButton(self, valuesDict, typeId, devId):
        """Button callback to create historic devices"""
        try:
            # Get the parent meter device
            parent_dev = indigo.devices[devId]
            parent_serial = parent_dev.pluginProps.get('serialNumber', '')
            
            # Get config from dialog
            name_template = valuesDict.get('historicNameTemplate', '• Misc - Octopus History %B %Y')
            month_count = int(valuesDict.get('historicMonthCount', 6))
            
            # Validate
            if month_count < 0 or month_count > 24:
                self.logger.error("Number of months must be between 0 and 24")
                return
            
            # Determine device type
            if typeId == "electricityMeter":
                historic_type = "electricityMeterHistoric"
                fuel_type = "Electricity"
            elif typeId == "gasMeter":
                historic_type = "gasMeterHistoric"
                fuel_type = "Gas"
            elif typeId == "exportMeter":
                historic_type = "exportMeterHistoric"
                fuel_type = "Export"
            else:
                self.logger.error(f"Unknown parent device type: {typeId}")
                return
            
            # Create devices for each month
            created_count = 0
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            for months_prior in range(month_count + 1):  # +1 to include month 0 (current month)
                # Calculate target month
                # Calendar month arithmetic (30-day steps could skip or repeat months)
                t_year, t_month = month_shift(now.year, now.month, -months_prior)
                target_date = datetime(t_year, t_month, 1)
                
                # Format device name using strftime
                device_name = target_date.strftime(name_template)
                
                # Check if device already exists
                existing = None
                for dev in indigo.devices.iter(f"self.{historic_type}"):
                    if dev.pluginProps.get('linkedMeterSerial') == parent_serial:
                        if int(dev.pluginProps.get('monthsPrior', -1)) == months_prior:
                            existing = dev
                            break
                
                if existing:
                    self.logger.info(f"  Skipping {device_name} - already exists")
                    continue
                
                # Create new device
                props = {
                    'linkedMeterSerial': parent_serial,
                    'monthsPrior': str(months_prior)
                }
                
                new_dev = indigo.device.create(
                    protocol=indigo.kProtocol.Plugin,
                    address=f"{parent_serial}-M{months_prior}",
                    name=device_name,
                    description=f"{fuel_type} historic data for {target_date.strftime('%B %Y')}",
                    pluginId=self.pluginId,
                    deviceTypeId=historic_type,
                    props=props
                )
                
                self.logger.info(f"✓ Created {device_name} (monthsPrior={months_prior})")
                created_count += 1
            
            self.logger.info(f"Created {created_count} historic devices for {parent_dev.name}")
            
        except Exception as e:
            if self._is_network_error(e):
                self.logger.warning("Creating historic devices failed - Network unavailable")
            else:
                self.logger.error(f"Error creating historic devices: {str(e)}")
                import traceback
                self.logger.error(traceback.format_exc())
    
    def _update_historic_meter(self, dev, current_time):
        """Update historic meter with monthly day-by-day data"""
        try:
            # Check if we should update (daily at midnight or on first run)
            uk_tz = pytz.timezone('Europe/London')
            now = datetime.now(uk_tz)
            
            # Check if it's midnight (or within first minute of the day)
            if now.hour != 0 and dev.id in self.next_consumption_update:
                # Not midnight and we've already run - skip
                if current_time < self.next_consumption_update.get(dev.id, 0):
                    return
            
            # Get configuration
            linked_serial = dev.pluginProps.get('linkedMeterSerial', '')
            months_prior = int(dev.pluginProps.get('monthsPrior', 1))
            
            if not linked_serial:
                dev.updateStateOnServer('status', 'Error: No linked meter')
                return
            
            # Find parent meter
            parent_dev = None
            if dev.deviceTypeId == "electricityMeterHistoric":
                for d in indigo.devices.iter("self.electricityMeter"):
                    if d.pluginProps.get('serialNumber') == linked_serial:
                        parent_dev = d
                        break
            elif dev.deviceTypeId == "gasMeterHistoric":
                for d in indigo.devices.iter("self.gasMeter"):
                    if d.pluginProps.get('serialNumber') == linked_serial:
                        parent_dev = d
                        break
            elif dev.deviceTypeId == "exportMeterHistoric":
                for d in indigo.devices.iter("self.exportMeter"):
                    if d.pluginProps.get('serialNumber') == linked_serial:
                        parent_dev = d
                        break
            
            if not parent_dev:
                dev.updateStateOnServer('status', f'Error: Meter {linked_serial} not found')
                return
            
            # Calculate target month using calendar arithmetic
            t_year, t_month = month_shift(now.year, now.month, -months_prior)
            n_year, n_month = month_shift(t_year, t_month, 1)
            
            # Month boundaries as true local midnights. Each boundary gets the
            # offset that applies on ITS date (GMT or BST), not today's offset.
            first_day = uk_midnight(datetime(t_year, t_month, 1).date())
            next_month_start = uk_midnight(datetime(n_year, n_month, 1).date())
            period_end = next_month_start  # exclusive end
            
            # Last calendar day (for display/logging only)
            last_day = next_month_start - timedelta(days=1)
            
            # Calculate month name for display
            month_name = first_day.strftime('%B %Y')
            
            # If current month (monthsPrior=0), only go up to the end of yesterday
            if months_prior == 0:
                today_start = uk_midnight(now.date())
                
                # First day of the month - nothing complete yet
                if today_start <= first_day:
                    self.debugLog(f"{dev.name}: No data available yet for {month_name} (first day of month)")
                    dev.updateStateOnServer('status', 'No Data Yet')
                    dev.updateStateOnServer('monthName', month_name)
                    # Schedule next update for tomorrow
                    tomorrow = uk_midnight(now.date() + timedelta(days=1)) + timedelta(minutes=5)
                    self.next_consumption_update[dev.id] = tomorrow.timestamp()
                    return
                
                if today_start < period_end:
                    period_end = today_start
                    last_day = today_start - timedelta(days=1)
            
            dev.updateStateOnServer('monthName', month_name)
            
            self.debugLog(f"Updating {dev.name}: {month_name} ({first_day.strftime('%Y-%m-%d')} to {last_day.strftime('%Y-%m-%d')})")
            
            # Fetch data for the entire month
            api_key = self.pluginPrefs.get('apiKey', '')
            if not api_key:
                dev.updateStateOnServer('status', 'Error: No API key')
                return
            
            auth = HTTPBasicAuth(api_key, '')
            mpan_mprn = parent_dev.pluginProps.get('mpanMprn', '')
            product_code = parent_dev.pluginProps.get('productCode', '')
            tariff_code = parent_dev.pluginProps.get('tariffCode', '')
            
            if not all([mpan_mprn, product_code, tariff_code]):
                dev.updateStateOnServer('status', 'Error: Parent meter not configured')
                return
            
            # Determine fuel type and endpoint
            if dev.deviceTypeId == "electricityMeterHistoric":
                consumption_url = f"{API_BASE_URL}/electricity-meter-points/{mpan_mprn}/meters/{linked_serial}/consumption/"
            elif dev.deviceTypeId == "gasMeterHistoric":
                consumption_url = f"{API_BASE_URL}/gas-meter-points/{mpan_mprn}/meters/{linked_serial}/consumption/"
            elif dev.deviceTypeId == "exportMeterHistoric":
                consumption_url = f"{API_BASE_URL}/electricity-meter-points/{mpan_mprn}/meters/{linked_serial}/consumption/"
            
            # Fetch consumption data for entire month
            params = {
                'period_from': first_day.isoformat(),
                'period_to': period_end.isoformat(),
                'page_size': 25000,  # Max page size
                'order_by': 'period'
            }
            
            response = self.session.get(consumption_url, auth=auth, params=params, timeout=30)
            
            if response.status_code != 200:
                error_detail = ""
                try:
                    error_data = response.json()
                    error_detail = f" - {error_data.get('detail', '')}"
                except:
                    pass
                
                self.logger.error(f"API error for {dev.name}: HTTP {response.status_code}{error_detail}")
                self.logger.error(f"  URL: {consumption_url}")
                self.logger.error(f"  Period: {first_day.strftime('%Y-%m-%d')} to {last_day.strftime('%Y-%m-%d')}")
                self.logger.error(f"  MPAN/MPRN: {mpan_mprn}, Serial: {linked_serial}")
                
                dev.updateStateOnServer('status', f'API Error: {response.status_code}')
                return
            
            data = response.json()
            results = data.get('results', [])
            
            if not results:
                self.logger.info(f"No consumption data available for {month_name}")
                dev.updateStateOnServer('status', f'No Data Available')
                return
            
            # For gas meters, check if conversion needed
            if dev.deviceTypeId == "gasMeterHistoric":
                results, meter_type = self._convert_gas_to_kwh(parent_dev, results)
            
            # Group by day and calculate daily totals
            daily_consumption = {}  # day -> kWh
            daily_readings = {}     # day -> list of readings (for per-day cost)
            
            for reading in results:
                interval_start = reading.get('interval_start', '')
                consumption = reading.get('consumption', 0)
                
                if not interval_start:
                    continue
                
                # Parse date and bucket by UK local calendar day
                dt = parse_api_time(interval_start)
                if dt < first_day or dt >= period_end:
                    continue  # defensive: never let a neighbouring month leak in
                dt_local = dt.astimezone(uk_tz)
                day = dt_local.day
                
                if day not in daily_consumption:
                    daily_consumption[day] = 0.0
                    daily_readings[day] = []
                
                daily_consumption[day] += consumption
                daily_readings[day].append(reading)
            
            # Round all daily consumption values to 2 decimal places to avoid float precision issues
            for day in daily_consumption:
                daily_consumption[day] = round(daily_consumption[day], 2)
            
            # Calculate costs for each day
            daily_costs = {}  # day -> cost
            total_cost = 0.0
            total_kwh = 0.0
            
            # Determine fuel type once (doesn't change per day)
            if dev.deviceTypeId == 'electricityMeterHistoric':
                fuel_type = 'electricity'
            elif dev.deviceTypeId == 'gasMeterHistoric':
                fuel_type = 'gas'
            else:  # exportMeterHistoric
                fuel_type = 'electricity'
            
            # Check if we should include standing charges
            include_standing = dev.pluginProps.get('includeStandingCharge', False)
            standing_charge_map = {}  # date -> standing charge in pence
            previous_standing_charge_map = {}  # For days before tariff change
            
            if include_standing:
                # Fetch standing charges for current tariff
                try:
                    if fuel_type == 'electricity':
                        standing_url = f"{API_BASE_URL}/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/"
                    else:
                        standing_url = f"{API_BASE_URL}/products/{product_code}/gas-tariffs/{tariff_code}/standing-charges/"
                    
                    standing_params = {
                        'period_from': first_day.isoformat(),
                        'period_to': period_end.isoformat(),
                        'page_size': 200
                    }
                    
                    standing_response = self.session.get(standing_url, auth=auth, params=standing_params, timeout=10)
                    
                    if standing_response.status_code == 200:
                        standing_data = standing_response.json()
                        standing_results = standing_data.get('results', [])
                        
                        # Build map of standing charges by date range
                        for sc in standing_results:
                            valid_from = datetime.fromisoformat(sc['valid_from'].replace('Z', '+00:00')).astimezone(uk_tz)
                            valid_to_str = sc.get('valid_to')
                            valid_to = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00')).astimezone(uk_tz) if valid_to_str else None
                            charge = sc['value_inc_vat']  # pence per day
                            
                            standing_charge_map[(valid_from, valid_to)] = charge
                        
                        self.debugLog(f"{dev.name}: Found {len(standing_charge_map)} standing charge periods for current tariff")
                    else:
                        self.logger.warning(f"{dev.name}: Could not fetch standing charges (HTTP {standing_response.status_code})")
                        include_standing = False
                        
                    # Check if there was a tariff change this month - need to fetch previous standing charges too
                    tariff_change_date_str = parent_dev.states.get('tariffChangeDate', '')
                    if tariff_change_date_str:
                        try:
                            change_date = datetime.strptime(tariff_change_date_str, '%Y-%m-%d')
                            change_date = uk_tz.localize(change_date.replace(hour=0, minute=0, second=0))
                            
                            # Check if change happened during this month
                            if first_day <= change_date < period_end:
                                # Fetch standing charges for previous tariff
                                previous_product = parent_dev.states.get('previousProductCode', '')
                                previous_tariff = parent_dev.states.get('previousTariffCode', '')
                                
                                if previous_product and previous_tariff:
                                    if fuel_type == 'electricity':
                                        prev_standing_url = f"{API_BASE_URL}/products/{previous_product}/electricity-tariffs/{previous_tariff}/standing-charges/"
                                    else:
                                        prev_standing_url = f"{API_BASE_URL}/products/{previous_product}/gas-tariffs/{previous_tariff}/standing-charges/"
                                    
                                    prev_standing_params = {
                                        'period_from': first_day.isoformat(),
                                        'period_to': change_date.isoformat(),
                                        'page_size': 200
                                    }
                                    
                                    prev_standing_response = self.session.get(prev_standing_url, auth=auth, params=prev_standing_params, timeout=10)
                                    
                                    if prev_standing_response.status_code == 200:
                                        prev_standing_data = prev_standing_response.json()
                                        prev_standing_results = prev_standing_data.get('results', [])
                                        
                                        for sc in prev_standing_results:
                                            valid_from = datetime.fromisoformat(sc['valid_from'].replace('Z', '+00:00')).astimezone(uk_tz)
                                            valid_to_str = sc.get('valid_to')
                                            valid_to = datetime.fromisoformat(valid_to_str.replace('Z', '+00:00')).astimezone(uk_tz) if valid_to_str else None
                                            charge = sc['value_inc_vat']
                                            
                                            previous_standing_charge_map[(valid_from, valid_to)] = charge
                                        
                                        self.debugLog(f"{dev.name}: Found {len(previous_standing_charge_map)} standing charge periods for previous tariff")
                        except Exception as e:
                            self.logger.error(f"{dev.name}: Error fetching previous standing charges: {str(e)}")
                            
                except Exception as e:
                    self.logger.error(f"{dev.name}: Error fetching standing charges: {str(e)}")
                    include_standing = False
            
            for day, kwh in daily_consumption.items():
                # Calculate cost for this day
                day_cal = datetime(t_year, t_month, day).date()
                day_start = uk_midnight(day_cal)
                day_end = uk_midnight(day_cal + timedelta(days=1))  # exclusive; 23h/25h on change days
                
                # Readings for this day only (same bucketing as the kWh totals)
                day_readings = daily_readings.get(day, [])
                
                # Check if this day falls before a tariff change
                day_product = product_code
                day_tariff = tariff_code
                
                tariff_change_date_str = parent_dev.states.get('tariffChangeDate', '')
                if tariff_change_date_str:
                    try:
                        change_date = datetime.strptime(tariff_change_date_str, '%Y-%m-%d')
                        change_date = uk_tz.localize(change_date.replace(hour=0, minute=0, second=0))
                        
                        if day_start < change_date:
                            # This day was before the change - use previous tariff
                            day_product = parent_dev.states.get('previousProductCode', product_code)
                            day_tariff = parent_dev.states.get('previousTariffCode', tariff_code)
                            self.debugLog(f"{dev.name}: Day {day} before tariff change - using previous tariff")
                    except:
                        pass
                
                # Calculate cost using our existing method
                cost = self._calculate_cost_for_period(
                    auth, day_readings, day_start, day_end, 
                    day_product, day_tariff, fuel_type
                )
                
                # Convert to Decimal immediately for precise arithmetic
                dec_cost = Decimal(str(cost))
                
                # Add standing charge if enabled
                if include_standing:
                    # Determine which standing charge map to use based on tariff change
                    tariff_change_date_str = parent_dev.states.get('tariffChangeDate', '')
                    use_previous_standing = False
                    
                    if tariff_change_date_str:
                        try:
                            change_date = datetime.strptime(tariff_change_date_str, '%Y-%m-%d')
                            change_date = uk_tz.localize(change_date.replace(hour=0, minute=0, second=0))
                            
                            if day_start < change_date:
                                use_previous_standing = True
                        except:
                            pass
                    
                    # Select the appropriate standing charge map
                    active_standing_map = previous_standing_charge_map if (use_previous_standing and previous_standing_charge_map) else standing_charge_map
                    
                    # Find the standing charge for this day
                    day_standing_charge = Decimal('0.0')
                    for (valid_from, valid_to), charge in active_standing_map.items():
                        # Check if day falls within this standing charge period
                        if day_start >= valid_from and (valid_to is None or day_start < valid_to):
                            # Convert pence to pounds using Decimal
                            day_standing_charge = Decimal(str(charge)) / Decimal('100')
                            break
                    
                    # Add standing charge using Decimal arithmetic
                    dec_cost += day_standing_charge
                
                # Quantize to 2 decimal places and store
                # Round to avoid float precision issues (use Python's round which is precise for 2 decimals)
                daily_costs[day] = round(float(dec_cost), 2)
                total_cost += round(float(dec_cost), 2)
                total_kwh += kwh
            
            # Update all states
            states = []
            
            # Update 31 days of data
            for day in range(1, 32):
                day_str = f"{day:02d}"
                kwh = daily_consumption.get(day, 0.0)
                cost = daily_costs.get(day, 0.0)
                
                # Round to 2 decimal places for clean values
                kwh_clean = round(kwh, 2)
                cost_clean = round(cost, 2)
                
                states.append({'key': f'day{day_str}kWh', 'value': kwh_clean})
                states.append({'key': f'day{day_str}cost', 'value': cost_clean})
            
            # Update summary states
            total_cost_clean = round(total_cost, 2)
            total_kwh_clean = round(total_kwh, 2)
            
            states.extend([
                {'key': 'monthName', 'value': month_name},
                {'key': 'totalCost', 'value': total_cost_clean, 'uiValue': f'£{total_cost_clean:.2f}'},
                {'key': 'totalKwh', 'value': total_kwh_clean},
                {'key': 'status', 'value': 'Online'},
                {'key': 'lastUpdate', 'value': now.strftime('%Y-%m-%d %H:%M:%S')}
            ])
            
            dev.updateStatesOnServer(states)
            
            # Only log historic updates once per day when data changes
            today_date = now.date().isoformat()
            cached = self.consumption_log_cache.get(dev.id, {})
            cached_date = cached.get('date')
            cached_kwh = cached.get('total_kwh', 0.0)
            
            # Log if: (1) Total has data (>0), AND (2) It's a new day OR total changed significantly
            should_log = total_kwh > 0 and (cached_date != today_date or abs(total_kwh - cached_kwh) > 0.1)
            
            if should_log:
                self.logger.info(f"Updated {dev.name}: {total_kwh:.1f} kWh, £{total_cost:.2f}")
                
                # Cache this log
                self.consumption_log_cache[dev.id] = {
                    'date': today_date,
                    'total_kwh': total_kwh
                }
            else:
                # Still log at DEBUG level for troubleshooting
                self.debugLog(f"Updated {dev.name}: {total_kwh:.1f} kWh, £{total_cost:.2f}")
            
            # Schedule next update for tomorrow at midnight + 5 minutes
            tomorrow = uk_midnight(now.date() + timedelta(days=1)) + timedelta(minutes=5)
            self.next_consumption_update[dev.id] = tomorrow.timestamp()
            
        except Exception as e:
            self._log_error_gracefully(dev.name, "Historic meter update", e)
            dev.updateStateOnServer('status', 'Error' if not self._is_network_error(e) else 'Network Error')
