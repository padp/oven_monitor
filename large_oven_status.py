from pylogix import PLC
import time
from datetime import datetime
from enum import Enum
import os
import json
import traceback


class OvenState(Enum):
    RUNNING = "RUNNING"
    IDLE = "IDLE"
    FAULT = "FAULT"
    UNKNOWN = "UNKNOWN"


class OvenMonitor:
    def __init__(self, plc_ip_address, poll_interval=30, debug=False):
        """
        Initialize the oven monitor

        Args:
            plc_ip_address: IP address of the PLC
            poll_interval: Time in seconds between polls (default 30)
            debug: Enable debug output to see raw tag values
        """
        self.plc = PLC()
        self.plc.IPAddress = plc_ip_address
        self.poll_interval = poll_interval
        self.debug = debug
        self.previous_cycle_time = None
        self.last_running_time = None

        # Define critical tags to monitor
        self.monitoring_tags = {
            # Temperature indicators
            'Z1_ACTUAL_TEMP': 'Z1_ACTUAL_TEMP',
            'Z2_ACTUAL_TEMP': 'Z2_ACTUAL_TEMP',
            'OVEN_TEMP_SETPOINT': 'OVEN_TEMP_SETPOINT',

            # Burner activity
            'ZONE_1_BURNER_MTR': 'ZONE_1_BURNER_MTR',
            'ZONE_2_BURNER_MTR': 'ZONE_2_BURNER_MTR',

            # Cycle status
            'CYCLE_TOTAL_MINUTES_LEFT': 'CYCLE_TOTAL_MINUTES_LEFT',
            'SOAK_CYCLE_COMPLETE_COUNTER': 'SOAK_CYCLE_COMPLETE_COUNTER',

            # Mode selection
            'AUTOMATIC_MODE_SELECTED': 'AUTOMATIC_MODE_SELECTED',
            'MANUAL_MODE_SELECTED': 'MANUAL_MODE_SELECTED',

            # Primary fault indicators (most reliable)
            'CombustionFaultLock': 'CombustionFaultLock',
            'Fault_Lock': 'Fault_Lock',
            'PURGE_FAULT': 'PURGE_FAULT',

            # Safety relays (for reference only, not primary fault indicators)
            'z1_safeguard_relay': 'z1_safeguard_relay',
            'z2_safeguard_relay': 'z2_safeguard_relay',

            # Exhaust fan (indicates operation)
            'EXHAUST_FAN': 'EXHAUST_FAN',

            # Power status
            'POWER_FEED': 'POWER_FEED'
        }

        # Optional tags for detailed diagnostics
        self.diagnostic_tags = {
            'FAULTS': 'FAULTS',
            'FAULT_BITS': 'FAULT_BITS',
            'Z1_FLAME_FLAME_FAULT_CTR': 'Z1_FLAME_FLAME_FAULT_CTR',
            'Z2_FLAME_FLAME_FAULT_CTR': 'Z2_FLAME_FLAME_FAULT_CTR',
        }

    def read_tags(self):
        """Read all monitoring tags from the PLC"""
        tag_values = {}

        for tag_name, tag_address in self.monitoring_tags.items():
            try:
                result = self.plc.Read(tag_address)
                if result.Status == 'Success':
                    tag_values[tag_name] = result.Value
                    if self.debug:
                        print(f"DEBUG: {tag_name} = {result.Value} (type: {type(result.Value)})")
                else:
                    tag_values[tag_name] = None
                    if self.debug:
                        print(f"DEBUG: Could not read {tag_name}: {result.Status}")
            except Exception as e:
                if self.debug:
                    print(f"DEBUG: Error reading {tag_name}: {e}")
                tag_values[tag_name] = None

        # Optionally read diagnostic tags if debug is enabled
        if self.debug:
            print("\n--- Diagnostic Tags ---")
            for tag_name, tag_address in self.diagnostic_tags.items():
                try:
                    result = self.plc.Read(tag_address)
                    if result.Status == 'Success':
                        print(f"DEBUG: {tag_name} = {result.Value} (type: {type(result.Value)})")
                except Exception as e:
                    print(f"DEBUG: Error reading {tag_name}: {e}")
            print("--- End Diagnostics ---\n")

        return tag_values

    def safe_get_numeric(self, value, default=0):
        """
        Safely convert any value to a numeric type
        
        Args:
            value: The value to convert
            default: Default value if conversion fails
            
        Returns:
            Numeric value or default
        """
        try:
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return value
            if isinstance(value, bool):
                return 1 if value else 0
            if isinstance(value, (bytes, bytearray)):
                if len(value) == 0:
                    return default
                return int.from_bytes(value, byteorder='little', signed=False)
            if isinstance(value, str):
                # Try to parse string as number
                try:
                    return float(value)
                except:
                    return default
            return default
        except Exception as e:
            if self.debug:
                print(f"DEBUG: Error converting value {value} to numeric: {e}")
            return default

    def safe_get_bool(self, value, default=False):
        """
        Safely convert any value to boolean
        
        Args:
            value: The value to convert
            default: Default value if conversion fails
            
        Returns:
            Boolean value or default
        """
        try:
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, (bytes, bytearray)):
                if len(value) == 0:
                    return default
                return int.from_bytes(value, byteorder='little', signed=False) != 0
            if isinstance(value, str):
                return value.lower() in ('true', '1', 'yes', 'on')
            return default
        except Exception as e:
            if self.debug:
                print(f"DEBUG: Error converting value {value} to bool: {e}")
            return default

    def determine_state(self, tag_values):
        """
        Determine oven state based on tag values
        Priority: Running > Fault > Idle
        
        Returns:
            Tuple of (OvenState enum value, reason string)
        """
        try:
            # First check if running (highest priority - if actively running, it's not faulted)
            if self._check_running_condition(tag_values):
                self.last_running_time = datetime.now()
                return OvenState.RUNNING, self._get_running_reason(tag_values)
        
            # Then check for true faults (oven stopped mid-cycle)
            if self._check_fault_condition(tag_values):
                return OvenState.FAULT, self._get_fault_reason(tag_values)
        
            # Finally check idle (normal downtime between cycles)
            if self._check_idle_condition(tag_values):
                return OvenState.IDLE, self._get_idle_reason(tag_values)
                
            # If none of the above, return unknown
            return OvenState.UNKNOWN, "Unable to determine state from available data"
            
        except Exception as e:
            error_msg = f"Error in state determination: {str(e)}"
            if self.debug:
                print(f"DEBUG: {error_msg}")
                traceback.print_exc()
            return OvenState.UNKNOWN, error_msg

    def _convert_to_int(self, value):
        """Convert byte/byte array values to integer - DEPRECATED, use safe_get_numeric"""
        return int(self.safe_get_numeric(value, 0))

    def _check_running_condition(self, tags):
        """
        Check if oven is actively running
        An oven is RUNNING if it's executing a cycle, regardless of any fault bits
        """
        try:
            # Get cycle time
            cycle_time_left = self.safe_get_numeric(tags.get('CYCLE_TOTAL_MINUTES_LEFT'), 0)
            
            # Get temperatures
            z1_temp = self.safe_get_numeric(tags.get('Z1_ACTUAL_TEMP'), 0)
            z2_temp = self.safe_get_numeric(tags.get('Z2_ACTUAL_TEMP'), 0)
            
            # Get burner activity
            z1_burner = self.safe_get_numeric(tags.get('ZONE_1_BURNER_MTR'), 0)
            z2_burner = self.safe_get_numeric(tags.get('ZONE_2_BURNER_MTR'), 0)
            
            # Get exhaust fan status
            exhaust_fan = self.safe_get_bool(tags.get('EXHAUST_FAN'), False)
            
            # RUNNING criteria (all must be true for high confidence):
            # 1. Cycle time is actively counting (not zero)
            # 2. Temperatures are at or near setpoint (>200°F indicates active heating)
            # 3. At least one burner is active OR exhaust fan is running
            
            cycle_active = cycle_time_left > 0
            temps_at_process = (z1_temp > 200) or (z2_temp > 200)
            burners_or_exhaust_active = (z1_burner > 0) or (z2_burner > 0) or exhaust_fan
            
            # Track cycle progression
            if cycle_active and self.previous_cycle_time is not None:
                cycle_progressing = cycle_time_left < self.previous_cycle_time
            else:
                cycle_progressing = True  # Assume progressing on first read
            
            self.previous_cycle_time = cycle_time_left
            
            # Running if cycle is active AND either temps elevated OR equipment active
            is_running = cycle_active and (temps_at_process or burners_or_exhaust_active)
            
            return is_running
            
        except Exception as e:
            if self.debug:
                print(f"DEBUG: Error in _check_running_condition: {e}")
            return False

    def _check_fault_condition(self, tags):
        """
        Check if oven is in a TRUE FAULT state (stopped mid-cycle)
        Only flag as fault if the oven SHOULD be running but is stopped
        """
        try:
            # Check primary fault locks (these are definitive)
            if self.safe_get_bool(tags.get('CombustionFaultLock'), False):
                return True
            
            if self.safe_get_bool(tags.get('Fault_Lock'), False):
                return True
            
            if self.safe_get_bool(tags.get('PURGE_FAULT'), False):
                return True
            
            # Check for unexpected stop: cycle time exists but no activity
            cycle_time_left = self.safe_get_numeric(tags.get('CYCLE_TOTAL_MINUTES_LEFT'), 0)
            
            z1_temp = self.safe_get_numeric(tags.get('Z1_ACTUAL_TEMP'), 0)
            z2_temp = self.safe_get_numeric(tags.get('Z2_ACTUAL_TEMP'), 0)
            
            z1_burner = self.safe_get_numeric(tags.get('ZONE_1_BURNER_MTR'), 0)
            z2_burner = self.safe_get_numeric(tags.get('ZONE_2_BURNER_MTR'), 0)
            exhaust_fan = self.safe_get_bool(tags.get('EXHAUST_FAN'), False)
            
            # TRUE FAULT: Cycle time remaining but no activity and temps dropping
            cycle_should_be_active = cycle_time_left > 5  # More than 5 minutes left
            no_heating_activity = (z1_burner == 0) and (z2_burner == 0) and not exhaust_fan
            temps_below_setpoint = (z1_temp < 300) and (z2_temp < 300)  # Below typical process temp
            
            # This indicates an unexpected stop mid-cycle
            if cycle_should_be_active and no_heating_activity and temps_below_setpoint:
                return True
            
            return False
            
        except Exception as e:
            if self.debug:
                print(f"DEBUG: Error in _check_fault_condition: {e}")
            return False

    def _check_idle_condition(self, tags):
        """
        Check if oven is in normal IDLE state (downtime between loads)
        """
        try:
            cycle_time_left = self.safe_get_numeric(tags.get('CYCLE_TOTAL_MINUTES_LEFT'), 0)
            
            z1_temp = self.safe_get_numeric(tags.get('Z1_ACTUAL_TEMP'), 0)
            z2_temp = self.safe_get_numeric(tags.get('Z2_ACTUAL_TEMP'), 0)
            
            z1_burner = self.safe_get_numeric(tags.get('ZONE_1_BURNER_MTR'), 0)
            z2_burner = self.safe_get_numeric(tags.get('ZONE_2_BURNER_MTR'), 0)
            
            # IDLE: No cycle active OR cycle complete (very low time remaining)
            no_cycle = cycle_time_left == 0 or cycle_time_left <= 2
            
            # No active heating
            burners_idle = (z1_burner == 0) and (z2_burner == 0)
            
            # Temperatures either cooling down or at ambient
            # (any temp is acceptable for idle - could be cooling from previous cycle)
            
            return no_cycle and burners_idle
            
        except Exception as e:
            if self.debug:
                print(f"DEBUG: Error in _check_idle_condition: {e}")
            return False

    def _get_running_reason(self, tags):
        """Generate detailed running status"""
        try:
            cycle_time = self.safe_get_numeric(tags.get('CYCLE_TOTAL_MINUTES_LEFT'), 0)
            z1_temp = self.safe_get_numeric(tags.get('Z1_ACTUAL_TEMP'), 0)
            z2_temp = self.safe_get_numeric(tags.get('Z2_ACTUAL_TEMP'), 0)
            
            return f"Oven running cycle - {cycle_time} minutes remaining, Temps: Z1={z1_temp}°F Z2={z2_temp}°F"
        except Exception as e:
            return f"Oven running (error getting details: {str(e)})"

    def _get_fault_reason(self, tags):
        """Generate detailed fault reason"""
        try:
            reasons = []
            
            if self.safe_get_bool(tags.get('CombustionFaultLock'), False):
                reasons.append("Combustion fault lock active")
            
            if self.safe_get_bool(tags.get('Fault_Lock'), False):
                reasons.append("General fault lock active")
            
            if self.safe_get_bool(tags.get('PURGE_FAULT'), False):
                reasons.append("Purge system fault")
            
            # Check for unexpected stop
            cycle_time_left = self.safe_get_numeric(tags.get('CYCLE_TOTAL_MINUTES_LEFT'), 0)
            z1_burner = self.safe_get_numeric(tags.get('ZONE_1_BURNER_MTR'), 0)
            z2_burner = self.safe_get_numeric(tags.get('ZONE_2_BURNER_MTR'), 0)
            
            if cycle_time_left > 5 and z1_burner == 0 and z2_burner == 0:
                reasons.append(f"Unexpected stop: {cycle_time_left} min remaining but no burner activity")
            
            return "FAULT: " + "; ".join(reasons) if reasons else "FAULT: Unknown fault condition"
        except Exception as e:
            return f"FAULT: Error determining fault details: {str(e)}"

    def _get_idle_reason(self, tags):
        """Generate detailed idle status"""
        try:
            cycle_time = self.safe_get_numeric(tags.get('CYCLE_TOTAL_MINUTES_LEFT'), 0)
            
            if cycle_time <= 2 and cycle_time > 0:
                return "Cycle completing - normal downtime"
            elif self.last_running_time:
                idle_duration = datetime.now() - self.last_running_time
                minutes_idle = int(idle_duration.total_seconds() / 60)
                return f"Idle - awaiting next load ({minutes_idle} min since last cycle)"
            else:
                return "Idle - awaiting next load"
        except Exception as e:
            return f"Idle (error getting details: {str(e)})"

    def format_status_report(self, tag_values, state, reason):
        """Format a human-readable status report"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        report = f"\n{'='*60}\n"
        report += f"Oven Status Report - {timestamp}\n"
        report += f"{'='*60}\n\n"

        report += f"STATE: {state.value}\n"
        report += f"REASON: {reason}\n\n"

        report += "Key Parameters:\n"
        report += f"  Zone 1 Temp: {tag_values.get('Z1_ACTUAL_TEMP', 'N/A')}°F\n"
        report += f"  Zone 2 Temp: {tag_values.get('Z2_ACTUAL_TEMP', 'N/A')}°F\n"
        report += f"  Setpoint: {tag_values.get('OVEN_TEMP_SETPOINT', 'N/A')}°F\n"
        report += f"  Zone 1 Burner: {tag_values.get('ZONE_1_BURNER_MTR', 'N/A')}%\n"
        report += f"  Zone 2 Burner: {tag_values.get('ZONE_2_BURNER_MTR', 'N/A')}%\n"
        report += f"  Cycle Time Left: {tag_values.get('CYCLE_TOTAL_MINUTES_LEFT', 'N/A')} min\n"
        report += f"  Exhaust Fan: {tag_values.get('EXHAUST_FAN', 'N/A')}\n"
        report += f"  Auto Mode: {tag_values.get('AUTOMATIC_MODE_SELECTED', 'N/A')}\n"
        report += f"  Manual Mode: {tag_values.get('MANUAL_MODE_SELECTED', 'N/A')}\n"

        report += "\nSafety/Diagnostic Info:\n"
        report += f"  Z1 Safeguard Relay: {tag_values.get('z1_safeguard_relay', 'N/A')}\n"
        report += f"  Z2 Safeguard Relay: {tag_values.get('z2_safeguard_relay', 'N/A')}\n"
        report += f"  Combustion Fault Lock: {tag_values.get('CombustionFaultLock', 'N/A')}\n"
        report += f"  Fault Lock: {tag_values.get('Fault_Lock', 'N/A')}\n"
        report += f"  Purge Fault: {tag_values.get('PURGE_FAULT', 'N/A')}\n"
        report += f"  Power Feed: {tag_values.get('POWER_FEED', 'N/A')}\n"

        report += f"\n{'='*60}\n"

        return report

    def format_status_json(self, tag_values, state, reason):
        """Format status as JSON and save to daily file"""
        try:
            timestamp = datetime.now()
            report_data = {
                "timestamp": timestamp.isoformat(),
                "status": {
                    "state": state.value,
                    "reason": reason
                },
                "indicators": {
                    "zone1_temp": f"{tag_values.get('Z1_ACTUAL_TEMP', 'N/A')}",
                    "zone2_temp": f"{tag_values.get('Z2_ACTUAL_TEMP', 'N/A')}",
                    "setpoint": f"{tag_values.get('OVEN_TEMP_SETPOINT', 'N/A')}",
                    "zone1_burner": tag_values.get('ZONE_1_BURNER_MTR', 'N/A'),
                    "zone2_burner": tag_values.get('ZONE_2_BURNER_MTR', 'N/A'),
                    "cycle_time_left_min": tag_values.get('CYCLE_TOTAL_MINUTES_LEFT', 'N/A'),
                    "exhaust_fan_active": tag_values.get('EXHAUST_FAN', 'N/A'),
                    "auto_mode_selected": tag_values.get('AUTOMATIC_MODE_SELECTED', 'N/A'),
                    "manual_mode_selected": tag_values.get('MANUAL_MODE_SELECTED', 'N/A')
                },
                "safety_status": {
                    "z1_safeguard_relay": tag_values.get('z1_safeguard_relay', 'N/A'),
                    "z2_safeguard_relay": tag_values.get('z2_safeguard_relay', 'N/A'),
                    "combustion_fault_lock": tag_values.get('CombustionFaultLock', 'N/A'),
                    "fault_lock": tag_values.get('Fault_Lock', 'N/A'),
                    "purge_fault": tag_values.get('PURGE_FAULT', 'N/A'),
                    "power_feed": tag_values.get('POWER_FEED', 'N/A')
                }
            }
            self.save_to_json(timestamp, report_data)
        except Exception as e:
            print(f"Error formatting/saving JSON: {e}")
            if self.debug:
                traceback.print_exc()

    def save_to_json(self, timestamp, new_data):
        """Save status data to daily JSON file"""
        try:
            file_name = f'Large_Oven_Status_{timestamp:%Y-%m-%d}.json'

            # Create file if it doesn't exist
            if not os.path.exists(file_name):
                with open(file_name, 'w') as js_wrt:
                    json.dump([], js_wrt)

            # Load existing data from file
            with open(file_name, 'r') as js_read:
                data = json.load(js_read)

            # Append new data
            data.append(new_data)

            # Write updated data back to file
            with open(file_name, 'w') as js_wrt:
                json.dump(data, js_wrt, indent=2)
        except Exception as e:
            print(f"Error saving to JSON file: {e}")
            if self.debug:
                traceback.print_exc()

    def monitor_continuous(self, callback=None):
        """
        Continuously monitor the oven status

        Args:
            callback: Optional function to call with (state, reason, tag_values)
        """
        print(f"Starting oven monitor (polling every {self.poll_interval} seconds)")
        print(f"PLC IP: {self.plc.IPAddress}")
        print("Press Ctrl+C to stop\n")

        consecutive_errors = 0
        max_consecutive_errors = 10

        try:
            while True:
                try:
                    # Read all tags
                    tag_values = self.read_tags()

                    # Determine state
                    result = self.determine_state(tag_values)
                    
                    # Unpack result safely
                    if result is None or not isinstance(result, tuple) or len(result) != 2:
                        print(f"[{datetime.now():%H:%M:%S}] WARNING: Invalid state determination result")
                        state = OvenState.UNKNOWN
                        reason = "Invalid state determination result"
                    else:
                        state, reason = result

                    # Save to JSON
                    self.format_status_json(tag_values, state, reason)
                    
                    # Print brief status to console
                    print(f"[{datetime.now():%H:%M:%S}] {state.value}: {reason}")

                    # Call callback if provided
                    if callback:
                        try:
                            callback(state, reason, tag_values)
                        except Exception as e:
                            print(f"Error in callback function: {e}")
                            if self.debug:
                                traceback.print_exc()

                    # Reset error counter on successful iteration
                    consecutive_errors = 0

                except Exception as e:
                    consecutive_errors += 1
                    print(f"[{datetime.now():%H:%M:%S}] ERROR (attempt {consecutive_errors}): {e}")
                    if self.debug:
                        traceback.print_exc()
                    
                    # If too many consecutive errors, exit
                    if consecutive_errors >= max_consecutive_errors:
                        print(f"\nToo many consecutive errors ({max_consecutive_errors}). Stopping monitor.")
                        break
                    
                    # Continue after brief pause
                    time.sleep(5)
                    continue

                # Wait for next poll
                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            print("\n\nMonitoring stopped by user")
        except Exception as e:
            print(f"\n\nFatal error during monitoring: {e}")
            if self.debug:
                traceback.print_exc()
        finally:
            try:
                self.plc.Close()
            except:
                pass

    def get_single_status(self):
        """Get a single status reading"""
        try:
            tag_values = self.read_tags()
            result = self.determine_state(tag_values)
            
            # Unpack result safely
            if result is None or not isinstance(result, tuple) or len(result) != 2:
                state = OvenState.UNKNOWN
                reason = "Invalid state determination result"
            else:
                state, reason = result
                
            print(self.format_status_report(tag_values, state, reason))
            return state, reason, tag_values
        except Exception as e:
            print(f"Error getting status: {e}")
            if self.debug:
                traceback.print_exc()
            return OvenState.UNKNOWN, f"Error: {str(e)}", {}
        finally:
            try:
                self.plc.Close()
            except:
                pass


# Example usage
if __name__ == "__main__":
    # Replace with your PLC IP address
    PLC_IP = "10.4.20.93"
    POLL_INTERVAL = 30  # seconds

    # Create monitor instance
    # Set debug=True to see all raw tag values and error details
    monitor = OvenMonitor(PLC_IP, POLL_INTERVAL, debug=False)

    # Continuous monitoring
    monitor.monitor_continuous()