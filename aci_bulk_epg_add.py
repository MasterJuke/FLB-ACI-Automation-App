#!/usr/bin/env python3
"""
ACI Bulk EPG Add Script
========================
Add EPG static bindings to existing ports.

Features:
- Add EPGs to ports that already have policy groups configured
- Handles EPGs that exist in multiple Application Profiles
- Batch preview before deployment
- Dry-run mode
- Interactive Application Profile selection

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
DEPLOYMENT_FILE = "epg_add.csv"


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


def check_port_exists(session, apic_url, node_id, port):
    """Check if port has a policy group assigned (is configured)."""
    eth_port = f"eth{port}" if not port.startswith("eth") else port
    
    # Check if port selector exists for this port
    port_num = port.split('/')[-1]
    url = f"{apic_url}/api/class/infraPortBlk.json?query-target-filter=and(eq(infraPortBlk.fromPort,\"{port_num}\"),eq(infraPortBlk.toPort,\"{port_num}\"))"
    
    try:
        response = session.get(url, verify=False, timeout=15)
        if response.status_code == 200:
            data = response.json().get("imdata", [])
            for item in data:
                dn = item.get("infraPortBlk", {}).get("attributes", {}).get("dn", "")
                if node_id in dn:
                    return True
    except:
        pass
    return False


def check_epg_binding_exists(session, apic_url, tenant, app_profile, epg_name, path_dn):
    """Check if an EPG binding already exists on a path."""
    try:
        url = f"{apic_url}/api/mo/uni/tn-{tenant}/ap-{app_profile}/epg-{epg_name}.json?query-target=children&target-subtree-class=fvRsPathAtt"
        response = session.get(url, verify=False, timeout=15)
        if response.status_code == 200:
            for item in response.json().get("imdata", []):
                attrs = item.get("fvRsPathAtt", {}).get("attributes", {})
                if path_dn in attrs.get("tDn", ""):
                    return True
    except:
        pass
    return False


def deploy_static_binding(session, apic_url, tenant, app_profile, epg_name, vlan_id, mode, path_dn):
    """Deploy a static binding to an EPG."""
    payload = {
        "fvRsPathAtt": {
            "attributes": {
                "tDn": path_dn,
                "encap": f"vlan-{vlan_id}",
                "instrImedcy": "immediate",
                "mode": mode
            }
        }
    }
    
    try:
        response = session.post(
            f"{apic_url}/api/mo/uni/tn-{tenant}/ap-{app_profile}/epg-{epg_name}.json",
            json=payload, verify=False, timeout=30
        )
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


# =============================================================================
# CSV FUNCTIONS
# =============================================================================

def load_epg_add_csv(filename):
    """Load EPG add deployment CSV file."""
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
    print(" ACI BULK EPG ADD SCRIPT")
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
    deployments = load_epg_add_csv(deployment_file)
    if not deployments:
        sys.exit(1)
    
    print(f"[INFO] Loaded {len(deployments)} deployment(s)")
    
    # Select run mode
    print("\n" + "-" * 70)
    print(" RUN MODE")
    print("-" * 70)
    print("\n  [1] Normal - Deploy EPG bindings")
    print("  [2] Dry-Run - Validate only, don't deploy")
    
    while True:
        sys.stdout.write("\nSelect mode (1/2): ")
        sys.stdout.flush()
        mode_choice = input().strip()
        if mode_choice in ['1', '2']:
            break
    dry_run = (mode_choice == '2')
    
    # Select binding mode
    print("\n" + "-" * 70)
    print(" BINDING MODE")
    print("-" * 70)
    print("\n  [1] Trunk (Tagged) - Multiple VLANs, tagged traffic")
    print("  [2] Access (Untagged) - Single VLAN, untagged traffic")
    
    while True:
        sys.stdout.write("\nSelect mode (1/2) [default=1]: ")
        sys.stdout.flush()
        binding_choice = input().strip()
        if binding_choice in ["", "1"]:
            binding_mode = "regular"  # trunk
            break
        elif binding_choice == "2":
            binding_mode = "untagged"  # access
            break
    
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
    # PHASE 1: ANALYZE ALL DEPLOYMENTS
    # ==========================================================================
    
    print("\n" + "=" * 70)
    print(" PHASE 1: ANALYZING DEPLOYMENTS")
    print("=" * 70)
    
    all_bindings = []
    alerts = []
    app_profile_selections = {}  # Cache user selections for multi-AP VLANs
    
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
        
        # Check port exists
        if not check_port_exists(session, apic_url, node_id, dep['port']):
            print(f"  [WARNING] Port may not have policy group configured")
        
        # Build path DN
        eth_port = f"eth{dep['port']}" if not dep['port'].startswith("eth") else dep['port']
        path_dn = f"topology/pod-{POD_ID}/paths-{node_id}/pathep-[{eth_port}]"
        
        # Process each VLAN
        vlans = parse_vlans(dep['vlans'])
        print(f"  Processing {len(vlans)} VLAN(s)...")
        
        for vlan in vlans:
            # Find EPG(s) for this VLAN
            epg_results = get_epg_app_profiles(session, apic_url, tenant, vlan)
            
            if not epg_results:
                print(f"    VLAN {vlan}: [WARNING] No EPG found")
                alerts.append({
                    "type": "NO_EPG",
                    "switch": dep['switch'],
                    "port": dep['port'],
                    "vlan": vlan,
                    "message": f"No EPG found for VLAN {vlan}"
                })
                continue
            
            # Check if VLAN exists in multiple Application Profiles
            if len(epg_results) > 1:
                # Check if we already have a selection for this VLAN
                cache_key = f"{env}:{vlan}"
                if cache_key in app_profile_selections:
                    selected_ap = app_profile_selections[cache_key]
                    epg_results = [e for e in epg_results if e['app_profile'] == selected_ap]
                else:
                    # Alert - will prompt user later
                    alerts.append({
                        "type": "MULTI_AP",
                        "switch": dep['switch'],
                        "port": dep['port'],
                        "vlan": vlan,
                        "env": env,
                        "options": epg_results,
                        "message": f"VLAN {vlan} exists in {len(epg_results)} Application Profiles"
                    })
                    continue
            
            if epg_results:
                epg = epg_results[0]
                
                # Check if binding already exists
                already_bound = check_epg_binding_exists(
                    session, apic_url, tenant, epg['app_profile'], epg['epg_name'], path_dn
                )
                
                all_bindings.append({
                    "switch": dep['switch'],
                    "port": dep['port'],
                    "node_id": node_id,
                    "vlan": vlan,
                    "env": env,
                    "tenant": tenant,
                    "app_profile": epg['app_profile'],
                    "epg_name": epg['epg_name'],
                    "path_dn": path_dn,
                    "already_bound": already_bound,
                    "mode": binding_mode
                })
    
    # ==========================================================================
    # PHASE 2: RESOLVE MULTI-AP ALERTS
    # ==========================================================================
    
    multi_ap_alerts = [a for a in alerts if a['type'] == 'MULTI_AP']
    
    if multi_ap_alerts:
        print("\n" + "=" * 70)
        print(" PHASE 2: RESOLVE APPLICATION PROFILE CONFLICTS")
        print("=" * 70)
        
        # Group by VLAN to avoid asking multiple times
        vlan_alerts = {}
        for alert in multi_ap_alerts:
            key = f"{alert['env']}:{alert['vlan']}"
            if key not in vlan_alerts:
                vlan_alerts[key] = alert
        
        for key, alert in vlan_alerts.items():
            print(f"\n[ALERT] VLAN {alert['vlan']} exists in multiple Application Profiles:")
            print("-" * 50)
            for i, opt in enumerate(alert['options'], 1):
                print(f"  [{i}] {opt['app_profile']} -> {opt['epg_name']}")
            print("-" * 50)
            
            while True:
                sys.stdout.write(f"\nSelect Application Profile for VLAN {alert['vlan']}: "); sys.stdout.flush()
                choice = input().strip()
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(alert['options']):
                        selected = alert['options'][idx]
                        app_profile_selections[key] = selected['app_profile']
                        print(f"  [SELECTED] {selected['app_profile']}")
                        
                        # Now add the bindings for all ports with this VLAN
                        for dep in deployments:
                            env = detect_environment(dep['switch'])
                            if f"{env}:{alert['vlan']}" != key:
                                continue
                            
                            if env not in sessions:
                                continue
                            
                            session = sessions[env]
                            apic_url = APIC_URLS[env]
                            tenant = TENANTS[env]
                            node_id = extract_node_id(dep['switch'])
                            
                            vlans = parse_vlans(dep['vlans'])
                            if alert['vlan'] not in vlans:
                                continue
                            
                            eth_port = f"eth{dep['port']}" if not dep['port'].startswith("eth") else dep['port']
                            path_dn = f"topology/pod-{POD_ID}/paths-{node_id}/pathep-[{eth_port}]"
                            
                            already_bound = check_epg_binding_exists(
                                session, apic_url, tenant, selected['app_profile'], selected['epg_name'], path_dn
                            )
                            
                            all_bindings.append({
                                "switch": dep['switch'],
                                "port": dep['port'],
                                "node_id": node_id,
                                "vlan": alert['vlan'],
                                "env": env,
                                "tenant": tenant,
                                "app_profile": selected['app_profile'],
                                "epg_name": selected['epg_name'],
                                "path_dn": path_dn,
                                "already_bound": already_bound,
                                "mode": binding_mode
                            })
                        break
                except:
                    pass
                print("  [ERROR] Invalid selection")
    
    # ==========================================================================
    # PHASE 3: PREVIEW ALL BINDINGS
    # ==========================================================================
    
    print("\n" + "=" * 70)
    print(" PHASE 3: DEPLOYMENT PREVIEW")
    print("=" * 70)
    
    if not all_bindings:
        print("\n[INFO] No valid bindings to deploy")
        sys.exit(0)
    
    # Show summary
    new_bindings = [b for b in all_bindings if not b['already_bound']]
    existing_bindings = [b for b in all_bindings if b['already_bound']]
    
    print(f"\n  Total bindings: {len(all_bindings)}")
    print(f"  New bindings:   {len(new_bindings)}")
    print(f"  Already exist:  {len(existing_bindings)} (will be skipped)")
    
    # Show alerts
    no_epg_alerts = [a for a in alerts if a['type'] == 'NO_EPG']
    if no_epg_alerts:
        print(f"\n  [WARNINGS] {len(no_epg_alerts)} VLAN(s) with no EPG found:")
        for alert in no_epg_alerts[:5]:
            print(f"    - {alert['switch']} port {alert['port']}: VLAN {alert['vlan']}")
        if len(no_epg_alerts) > 5:
            print(f"    ... and {len(no_epg_alerts) - 5} more")
    
    # Show binding details
    mode_display = "Trunk (Tagged)" if binding_mode == "regular" else "Access (Untagged)"
    
    print(f"\n  Binding Mode: {mode_display}")
    print("\n  === NEW BINDINGS TO DEPLOY ===")
    print("  " + "-" * 66)
    print(f"  {'Switch':<20} {'Port':<8} {'VLAN':<6} {'EPG':<30}")
    print("  " + "-" * 66)
    
    for b in new_bindings[:20]:
        print(f"  {b['switch']:<20} {b['port']:<8} {b['vlan']:<6} {b['epg_name']:<30}")
    
    if len(new_bindings) > 20:
        print(f"  ... and {len(new_bindings) - 20} more bindings")
    
    print("  " + "-" * 66)
    
    if existing_bindings:
        print(f"\n  === EXISTING BINDINGS (will skip) ===")
        for b in existing_bindings[:5]:
            print(f"  {b['switch']} port {b['port']}: VLAN {b['vlan']} already bound")
        if len(existing_bindings) > 5:
            print(f"  ... and {len(existing_bindings) - 5} more")
    
    # ==========================================================================
    # PHASE 4: CONFIRM AND DEPLOY
    # ==========================================================================
    
    print("\n" + "=" * 70)
    print(" PHASE 4: DEPLOYMENT")
    print("=" * 70)
    
    if dry_run:
        print("\n[DRY-RUN] Would deploy the following bindings:")
        for b in new_bindings:
            print(f"  - {b['switch']} port {b['port']}: VLAN {b['vlan']} -> {b['epg_name']}")
        print(f"\n[DRY-RUN] {len(new_bindings)} binding(s) would be created")
        sys.exit(0)
    
    print(f"\nReady to deploy {len(new_bindings)} binding(s)")
    print("\n  [Y] Yes - Deploy all bindings")
    print("  [N] No - Cancel")
    
    sys.stdout.write("\nConfirm deployment: "); sys.stdout.flush()
    confirm = input().strip().upper()
    
    if confirm not in ['Y', 'YES']:
        print("\n[CANCELLED]")
        sys.exit(0)
    
    # Deploy
    print("\n[INFO] Deploying bindings...")
    
    success_count = 0
    fail_count = 0
    
    for b in new_bindings:
        session = sessions[b['env']]
        apic_url = APIC_URLS[b['env']]
        
        success, response = deploy_static_binding(
            session, apic_url, b['tenant'], b['app_profile'],
            b['epg_name'], b['vlan'], b['mode'], b['path_dn']
        )
        
        if success:
            print(f"  [OK] {b['switch']} port {b['port']}: VLAN {b['vlan']}")
            success_count += 1
        else:
            print(f"  [FAIL] {b['switch']} port {b['port']}: VLAN {b['vlan']} - {response[:50]}")
            fail_count += 1
    
    # Summary
    print("\n" + "=" * 70)
    print(" COMPLETE")
    print("=" * 70)
    print(f"\n  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    print(f"  Skipped: {len(existing_bindings)} (already existed)")
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
