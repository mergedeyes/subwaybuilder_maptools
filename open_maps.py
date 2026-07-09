import json
import subprocess
import os
import sys
import signal
from dotenv import load_dotenv
# Ensure Ctrl+C always works and kills this script immediately
def signal_handler(sig, frame):
    print("\n[!] Exiting...")
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)

load_dotenv()

# Load configuration from environment
CITY_CODE = os.getenv("CITY_CODE")
CUSTOM_HUBS_JSON = f"raw_map_files/{CITY_CODE}/custom_hubs.json"

def open_url_detached(url):
    """
    Opens a URL using xdg-open in a fully detached process.
    This prevents the terminal from waiting for the browser to close
    and avoids deadlocks caused by icon theme or bookmark lock errors.
    """
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True
        )
    except Exception as e:
        print(f"  [ERROR] Could not launch browser: {e}")

def open_airports_in_browser():
    # 1. Validate the hub configuration file exists
    if not os.path.exists(CUSTOM_HUBS_JSON):
        print(f"Error: '{CUSTOM_HUBS_JSON}' not found.")
        print("Please run the demand generation script ('generate_demand_qzm_local.py') ")
        print("at least once to auto-generate the configuration template.")
        return

    # 2. Load the hubs configuration
    with open(CUSTOM_HUBS_JSON, 'r') as f:
        data = json.load(f)

    airports = data.get("airports", {})
    
    if not airports:
        print("No airports found in custom_hubs.json.")
        return

    print(f"Opening {len(airports)} airport locations to their default centers...")
    print("Use these tabs to find your subway terminal coordinates for the overrides.")
    
    # 3. Iterate through each airport and open its default center
    for ap_id, ap_data in airports.items():
        lat = ap_data.get("default_center_lat")
        lon = ap_data.get("default_center_lon")
        
        if lat is None or lon is None:
            print(f" -> {ap_id}: Missing default coordinates.")
            continue
            
        print(f" -> Opening {ap_id}: ({lat}, {lon})")
        
        # Google Maps search URL (most stable API)
        url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        
        # Launch detached
        open_url_detached(url)
    
    print("\nDone. Check your browser.")
    print("Once you have the coordinates, update 'override_lat' and 'override_lon'")
    print(f"in '{CUSTOM_HUBS_JSON}' and run the demand script again.")

if __name__ == "__main__":
    open_airports_in_browser()