#!/usr/bin/env python3
"""
ACI Bulk EPG Delete Script
===========================
Remove EPG static bindings from existing ports.

Features:
- Delete EPGs from ports
- Preview all deletions before executing
- Dry-run mode
- Batch deletion

Input CSV Format:
Switch,Port,VLANS
EDCLEAFACC1501,1/68,"32,64-67"
EDCLEAFNSM2163,1/5,2958

Author: Network Automation Script
"""

import csv
import os
import re
import sys
import getpass
import requests
import urllib3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =============================================================================
# CONFIGURATION - Update these values for your environment
# =============================================================================

APIC_URLS = {
    "D1": "",  # <-- UPDATE THIS (BLU Tenant - ACC switches)
    "D2": "",  # <-- UPDATE THIS (BLU Tenant - SDC switches)
    "D3": ""   # <-- UPDATE THIS (NSM_BLU Tenant - NSM switches)
}

TENANTS = {
    "D1": "BLU",
    "D2": "BLU",
    "D3": "NSM_BLU"
}

POD_ID = "1"
DEPLOYMENT_FILE = "epg_delete.csv"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def prompt_input(prompt_text):
    """Print prompt and get input - ensures prompt is visible in web UI."""
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    return input()


def detect_environment(switch_name):
    """Detect data center from switch name."""
    switch_upper = switch_name.upper()
    if "NSM" in switch_upper:
        return "D3"
    elif "SDC" in switch_upper:
        return "D2"
    elif "ACC" in switch_upper:
        return "D1"
    return None


def extract_node_id(switch_name):
    """Extract node ID from switch name (last digits)."""
    match = re.search(r'(\d+)$', switch_name)
    return match.group(1) if match else None


def parse_vlans(vlan_string):
    """Parse VLAN string into list of integers."""
    vlans = []
    vlan_string = str(vlan_string).replace(" ", "")
    for part in vlan_string.split(","):
        if "-" in part:
            try:
                start, end = part.split("-")
                vlans.extend(range(int(start), int(end) + 1))
            except:
                pass
        else:
            try:
                vlans.append(int(part))
            except:
                pass
    return sorted(set(vlans))


def parse_port(port_string):
    """Parse port string to standard format (e.g., '1/68' or 'eth1/68' -> '1/68')."""
    port_string = str(port_string).strip().lower()
    port_string = port_string.replace("eth", "").replace("ethernet", "")
    if "/" in port_string:
        return port_string
    return f"1/{port_string}"


# =============================================================================
# API FUNCTIONS
# =============================================================================

def login_to_apic(session, apic_url, username, password):
    """Login to APIC and get session."""
    try:
        payload = {"aaaUser": {"attributes": {"name": username, "pwd": password}}}
        response = session.post(f"{apic_url}/api/aaaLogin.json", json=payload, verify=False, timeout=30)
        return response.status_code == 200
    except Exception as e:
        print(f"    [ERROR] Login failed: {e}")
        return False


def get_epg_app_profiles(session, apic_url, tenant, vlan_id):
    """Find all Application Profiles containing the EPG for a given VLAN."""
    try:
        vlan_pattern = f"V{vlan_id:04d}"
        response = session.get(
            f'{apic_url}/api/class/fvAEPg.json?query-target-filter=and(wcard(fvAEPg.dn,"tn-{tenant}"),wcard(fvAEPg.name,"{vlan_pattern}"))',
            verify=False, timeout=30
        )
        if response.status_code != 200:
            return []
        
        results = []
        for item in response.json().get("imdata", []):
            attrs = item.get("fvAEPg", {}).get("attributes", {})
            dn = attrs.get("dn", "")
            epg_name = attrs.get("name", "")
            ap_match = re.search(r'/ap-([^/]+)/', dn)
            if ap_match:
                results.append({
                    "app_profile": ap_match.group(1),
                    "epg_name": epg_name,
                    "dn": dn
                })
        return results
    except:
        return []


def find_epg_binding(session, apic_url, tenant, app_profile, epg_name, path_dn):
    """Find if an EPG binding exists and return its details."""
    try:
        url = f"{apic_url}/api/mo/uni/tn-{tenant}/ap-{app_profile}/epg-{epg_name}.json?query-target=children&target-subtree-class=fvRsPathAtt"
        response = session.get(url, verify=False, timeout=15)
        if response.status_code == 200:
            for item in response.json().get("imdata", []):
                attrs = item.get("fvRsPathAtt", {}).get("attributes", {})
                if path_dn in attrs.get("tDn", ""):
                    return {
                        "dn": attrs.get("dn", ""),
                        "tDn": attrs.get("tDn", ""),
                        "encap": attrs.get("encap", ""),
                        "mode": attrs.get("mode", "")
                    }
    except:
        pass
    return None


def delete_static_binding(session, apic_url, binding_dn):
    """Delete a static binding from an EPG."""
    try:
        response = session.delete(
            f"{apic_url}/api/mo/{binding_dn}.json",
            verify=False, timeout=30
        )
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


# =============================================================================
# CSV FUNCTIONS
# =============================================================================

def load_epg_delete_csv(filename):
    """Load EPG delete deployment CSV file."""
    try:
        with open(filename, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            deployments = []
            for row in reader:
                normalized = {k.strip().upper(): v.strip() if v else "" for k, v in row.items() if k}
                deployments.append({
                    "switch": normalized.get("SWITCH", ""),
                    "port": parse_port(normalized.get("PORT", "")),
                    "vlans": normalized.get("VLANS", "")
                })
            return deployments
    except FileNotFoundError:
        print(f"[ERROR] File not found: {filename}")
        return None
    except Exception as e:
        print(f"[ERROR] Failed to load CSV: {e}")
        return None


# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    print("\n" + "=" * 70)
    print(" ACI BULK EPG DELETE SCRIPT")
    print("=" * 70)
    
    # Check configuration
    missing_urls = [dc for dc, url in APIC_URLS.items() if not url]
    if missing_urls:
        print(f"\n[ERROR] APIC URLs not configured for: {', '.join(missing_urls)}")
        sys.exit(1)
    
    # Get deployment file
    print(f"\n[INFO] Default deployment file: {DEPLOYMENT_FILE}")
    sys.stdout.write("Press Enter to use default, or enter filename: ")
    sys.stdout.flush()
    custom_file = input().strip()
    deployment_file = custom_file if custom_file else DEPLOYMENT_FILE
    
    # Load deployments
    print(f"\n[INFO] Loading from: {deployment_file}")
    deployments = load_epg_delete_csv(deployment_file)
    if not deployments:
        sys.exit(1)
    
    print(f"[INFO] Loaded {len(deployments)} deployment(s)")
    
    # Select run mode
    print("\n" + "-" * 70)
    print(" RUN MODE")
    print("-" * 70)
    print("\n  [1] Normal - Delete EPG bindings")
    print("  [2] Dry-Run - Validate only, don't delete")
    
    while True:
        sys.stdout.write("\nSelect mode (1/2): ")
        sys.stdout.flush()
        mode_choice = input().strip()
        if mode_choice in ['1', '2']:
            break
    dry_run = (mode_choice == '2')
    
    # Get credentials
    print("\n" + "-" * 70)
    print(" AUTHENTICATION")
    print("-" * 70)
    sys.stdout.write("\nUsername: ")
    sys.stdout.flush()
    username = input().strip()
    sys.stdout.write("Password: ")
    sys.stdout.flush()
    # Check if running in web UI mode - getpass doesn't work with pipes
    if os.environ.get('ACI_WEB_UI') == '1':
        password = input().strip()
    else:
        password = getpass.getpass("")
    if not username or not password:
        print("[ERROR] Credentials required")
        sys.exit(1)
    
    # Authenticate
    sessions = {}
    needed_envs = set(detect_environment(d['switch']) for d in deployments)
    needed_envs.discard(None)
    
    for env in needed_envs:
        print(f"\n[INFO] Authenticating to {env}...")
        session = requests.Session()
        if login_to_apic(session, APIC_URLS[env], username, password):
            sessions[env] = session
            print(f"       [SUCCESS]")
        else:
            print(f"       [FAILED]")
    
    if not sessions:
        print("\n[ERROR] No successful authentications.")
        sys.exit(1)
    
    # ==========================================================================
    # PHASE 1: FIND ALL BINDINGS TO DELETE
    # ==========================================================================
    
    print("\n" + "=" * 70)
    print(" PHASE 1: FINDING EPG BINDINGS")
    print("=" * 70)
    
    bindings_to_delete = []
    not_found = []
    app_profile_selections = {}
    
    for idx, dep in enumerate(deployments, 1):
        print(f"\n[{idx}/{len(deployments)}] {dep['switch']} port {dep['port']}")
        
        env = detect_environment(dep['switch'])
        if not env or env not in sessions:
            print(f"  [SKIP] Unknown environment or not authenticated")
            continue
        
        session = sessions[env]
        apic_url = APIC_URLS[env]
        tenant = TENANTS[env]
        node_id = extract_node_id(dep['switch'])
        
        if not node_id:
            print(f"  [SKIP] Cannot extract node ID from switch name")
            continue
        
        # Build path DN
        eth_port = f"eth{dep['port']}" if not dep['port'].startswith("eth") else dep['port']
        path_dn = f"topology/pod-{POD_ID}/paths-{node_id}/pathep-[{eth_port}]"
        
        # Process each VLAN
        vlans = parse_vlans(dep['vlans'])
        print(f"  Searching for {len(vlans)} VLAN binding(s)...")
        
        for vlan in vlans:
            # Find EPG(s) for this VLAN
            epg_results = get_epg_app_profiles(session, apic_url, tenant, vlan)
            
            if not epg_results:
                print(f"    VLAN {vlan}: [WARNING] No EPG found")
                not_found.append({
                    "switch": dep['switch'],
                    "port": dep['port'],
                    "vlan": vlan,
                    "reason": "No EPG found"
                })
                continue
            
            # Handle multiple Application Profiles
            if len(epg_results) > 1:
                cache_key = f"{env}:{vlan}"
                if cache_key not in app_profile_selections:
                    print(f"\n    [ALERT] VLAN {vlan} exists in multiple Application Profiles:")
                    print("    " + "-" * 40)
                    for i, opt in enumerate(epg_results, 1):
                        print(f"    [{i}] {opt['app_profile']} -> {opt['epg_name']}")
                    print("    [A] Check ALL Application Profiles")
                    print("    " + "-" * 40)
                    
                    while True:
                        sys.stdout.write(f"\n    Select for VLAN {vlan}: "); sys.stdout.flush()
                        choice = input().strip().upper()
                        if choice == 'A':
                            app_profile_selections[cache_key] = "ALL"
                            break
                        try:
                            idx = int(choice) - 1
                            if 0 <= idx < len(epg_results):
                                app_profile_selections[cache_key] = epg_results[idx]['app_profile']
                                break
                        except:
                            pass
                        print("    [ERROR] Invalid selection")
                
                selection = app_profile_selections[cache_key]
                if selection != "ALL":
                    epg_results = [e for e in epg_results if e['app_profile'] == selection]
            
            # Find actual bindings
            for epg in epg_results:
                binding = find_epg_binding(session, apic_url, tenant, epg['app_profile'], epg['epg_name'], path_dn)
                
                if binding:
                    bindings_to_delete.append({
                        "switch": dep['switch'],
                        "port": dep['port'],
                        "node_id": node_id,
                        "vlan": vlan,
                        "env": env,
                        "tenant": tenant,
                        "app_profile": epg['app_profile'],
                        "epg_name": epg['epg_name'],
                        "binding_dn": binding['dn'],
                        "mode": binding['mode']
                    })
                    print(f"    VLAN {vlan}: [FOUND] {epg['epg_name']} ({epg['app_profile']})")
                else:
                    not_found.append({
                        "switch": dep['switch'],
                        "port": dep['port'],
                        "vlan": vlan,
                        "reason": f"No binding on {epg['app_profile']}"
                    })
    
    # ==========================================================================
    # PHASE 2: PREVIEW DELETIONS
    # ==========================================================================
    
    print("\n" + "=" * 70)
    print(" PHASE 2: DELETION PREVIEW")
    print("=" * 70)
    
    if not bindings_to_delete:
        print("\n[INFO] No bindings found to delete")
        if not_found:
            print(f"\n  {len(not_found)} binding(s) not found:")
            for item in not_found[:10]:
                print(f"    - {item['switch']} port {item['port']}: VLAN {item['vlan']} - {item['reason']}")
            if len(not_found) > 10:
                print(f"    ... and {len(not_found) - 10} more")
        sys.exit(0)
    
    print(f"\n  Bindings to delete: {len(bindings_to_delete)}")
    print(f"  Not found:          {len(not_found)}")
    
    print("\n  === BINDINGS TO DELETE ===")
    print("  " + "-" * 76)
    print(f"  {'Switch':<20} {'Port':<8} {'VLAN':<6} {'EPG':<25} {'App Profile':<15}")
    print("  " + "-" * 76)
    
    for b in bindings_to_delete[:25]:
        mode = "trunk" if b['mode'] == "regular" else "access"
        print(f"  {b['switch']:<20} {b['port']:<8} {b['vlan']:<6} {b['epg_name']:<25} {b['app_profile']:<15}")
    
    if len(bindings_to_delete) > 25:
        print(f"  ... and {len(bindings_to_delete) - 25} more bindings")
    
    print("  " + "-" * 76)
    
    # ==========================================================================
    # PHASE 3: CONFIRM AND DELETE
    # ==========================================================================
    
    print("\n" + "=" * 70)
    print(" PHASE 3: DELETION")
    print("=" * 70)
    
    if dry_run:
        print("\n[DRY-RUN] Would delete the following bindings:")
        for b in bindings_to_delete:
            print(f"  - {b['switch']} port {b['port']}: VLAN {b['vlan']} from {b['epg_name']}")
        print(f"\n[DRY-RUN] {len(bindings_to_delete)} binding(s) would be deleted")
        sys.exit(0)
    
    print(f"\n[WARNING] About to delete {len(bindings_to_delete)} EPG binding(s)")
    print("         This action cannot be undone!")
    
    print("\n  [Y] Yes - Delete all bindings")
    print("  [N] No - Cancel")
    
    sys.stdout.write("\nConfirm deletion (type 'YES' to confirm): "); sys.stdout.flush()
    confirm = input().strip().upper()
    
    if confirm != 'YES':
        print("\n[CANCELLED]")
        sys.exit(0)
    
    # Delete
    print("\n[INFO] Deleting bindings...")
    
    success_count = 0
    fail_count = 0
    
    for b in bindings_to_delete:
        session = sessions[b['env']]
        apic_url = APIC_URLS[b['env']]
        
        success, response = delete_static_binding(session, apic_url, b['binding_dn'])
        
        if success:
            print(f"  [DELETED] {b['switch']} port {b['port']}: VLAN {b['vlan']}")
            success_count += 1
        else:
            print(f"  [FAIL] {b['switch']} port {b['port']}: VLAN {b['vlan']} - {response[:50]}")
            fail_count += 1
    
    # Summary
    print("\n" + "=" * 70)
    print(" COMPLETE")
    print("=" * 70)
    print(f"\n  Deleted:   {success_count}")
    print(f"  Failed:    {fail_count}")
    print(f"  Not found: {len(not_found)}")
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
