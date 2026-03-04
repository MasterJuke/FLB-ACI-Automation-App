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
- Multi-port CSV expansion (e.g. "1/67, 1/68, 1/69")
- EPG Overwrite: Interactive (per-port selection) or Auto (delete all)
- Merged dual-strategy port query (class-level + per-tenant EPG subtree)
- Token auto-refresh for long batch runs

Input CSV Format:
Switch,Port,VLANS
EDCLEAFACC1501,1/68,"32,64-67"
EDCLEAFNSM2163,1/5,2958
EDCLEAFACC1602,"1/47, 1/48","92-95,2032"

Author: Network Automation Script
"""

import csv
import os
import re
import sys
import time
import getpass
import requests
import urllib3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Shared utilities
from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_port,
    parse_ports, prompt_input,
    query_all_bindings_on_port, delete_all_bindings_on_port,
    ensure_token_fresh, reauth_apic
)

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =============================================================================
# CONFIGURATION - Update these values for your environment
# =============================================================================

APIC_URLS = {
    "D1": "https://edcapic01.gwnsm.guidewell.net/",
    "D2": "https://sdcapic01.gwnsm.guidewell.net/",
    "D3": "https://edcnsmapic01.gwnsm.guidewell.net/"
}

# Multiple tenants per datacenter
TENANTS = {
    "D1": ["BLU", "GWC", "GWS"],
    "D2": ["BLU", "GWC", "GWS", "SDCFLB", "SDCGWS"],
    "D3": ["NSM_BLU", "NSM_BRN", "NSM_GLD", "NSM_GRN"]
}

POD_ID = "1"
DEPLOYMENT_FILE = "epg_add.csv"


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
    """Find all Application Profiles containing the EPG for a given VLAN in a single tenant."""
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
                    "tenant": tenant,
                    "dn": dn
                })
        return results
    except:
        return []


def get_epg_app_profiles_all_tenants(session, apic_url, tenants, vlan_id):
    """Find all Application Profiles containing the EPG for a given VLAN across all tenants."""
    all_results = []
    for tenant in tenants:
        results = get_epg_app_profiles(session, apic_url, tenant, vlan_id)
        all_results.extend(results)
    return all_results


def check_port_exists(session, apic_url, node_id, port):
    """Check if port has a policy group assigned (is configured)."""
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
# MERGED DUAL-STRATEGY PORT QUERY
# =============================================================================

# =============================================================================
# CSV FUNCTIONS
# =============================================================================

def load_epg_add_csv(filename):
    """Load EPG add deployment CSV file.
    
    Supports multi-port entries: PORT column can contain comma-separated
    ports like "1/67, 1/68, 1/69" which expand to separate deployments.
    """
    try:
        with open(filename, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            deployments = []
            for row in reader:
                normalized = {k.strip().upper(): v.strip() if v else "" for k, v in row.items() if k}
                switch = normalized.get("SWITCH", "")
                vlans = normalized.get("VLANS", "")
                raw_port = normalized.get("PORT", "")
                
                # Expand multi-port entries ("1/67, 1/68, 1/69" -> 3 rows)
                ports = parse_ports(raw_port)
                if not ports:
                    ports = [parse_port(raw_port)]
                
                for port in ports:
                    deployments.append({
                        "switch": switch,
                        "port": port,
                        "vlans": vlans
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
    
    # Show unique switch+port combos vs total rows (multi-port expansion)
    unique_ports = set((d['switch'], d['port']) for d in deployments)
    if len(unique_ports) != len(deployments):
        ports_per = len(deployments) / max(len(unique_ports), 1)
        print(f"       ({len(unique_ports)} unique switch+port combos, avg {ports_per:.0f} VLANs each)")
    
    # =========================================================================
    # RUN MODE
    # =========================================================================
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
    
    # =========================================================================
    # BINDING MODE
    # =========================================================================
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
    
    # =========================================================================
    # EPG MODE — Add / Overwrite Interactive / Overwrite ALL
    # =========================================================================
    print("\n" + "-" * 70)
    print(" EPG MODE")
    print("-" * 70)
    print("\n  [1] Add - Add new EPG bindings (keep existing)")
    print("  [2] Overwrite - Show existing EPGs per port, choose which to delete")
    print("  [3] Overwrite ALL - Automatically delete ALL existing EPGs on every port")
    
    while True:
        sys.stdout.write("\nSelect mode (1/2/3) [default=1]: ")
        sys.stdout.flush()
        epg_mode_choice = input().strip()
        if epg_mode_choice in ["", "1", "2", "3"]:
            break
    
    overwrite_mode = epg_mode_choice in ["2", "3"]
    overwrite_interactive = (epg_mode_choice == "2")
    overwrite_auto = (epg_mode_choice == "3")
    
    if overwrite_auto:
        print("\n  [OVERWRITE ALL] Every port will have ALL existing EPG bindings")
        print("                  deleted automatically before deploying new ones.")
    elif overwrite_interactive:
        print("\n  [OVERWRITE] Per port you will see existing EPG bindings and")
        print("              choose which to delete before deploying new ones.")
    
    # =========================================================================
    # AUTHENTICATION
    # =========================================================================
    print("\n" + "-" * 70)
    print(" AUTHENTICATION")
    print("-" * 70)
    sys.stdout.write("\nUsername: ")
    sys.stdout.flush()
    username = input().strip()
    sys.stdout.write("Password: ")
    sys.stdout.flush()
    # Check if running in web UI mode — getpass doesn't work with pipes
    if os.environ.get('ACI_WEB_UI') == '1':
        password = input().strip()
    else:
        password = getpass.getpass("")
    if not username or not password:
        print("[ERROR] Credentials required")
        sys.exit(1)
    
    # Authenticate to each needed APIC
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
    
    # Token state tracking for auto-refresh during batch deployments
    token_states = {}
    _credentials = {"username": username, "password": password}
    for _env in sessions:
        token_states[_env] = {"login_time": time.time(), "lifetime": 300}
    
    # =========================================================================
    # PHASE 1: ANALYZE ALL DEPLOYMENTS
    # =========================================================================
    
    print("\n" + "=" * 70)
    print(" PHASE 1: ANALYZING DEPLOYMENTS")
    print("=" * 70)
    
    all_bindings = []
    alerts = []
    ap_tenant_selections = {}  # Cache user selections: {env:vlan -> (app_profile, tenant)}
    
    for idx, dep in enumerate(deployments, 1):
        print(f"\n[{idx}/{len(deployments)}] {dep['switch']} port {dep['port']}")
        
        env = detect_environment(dep['switch'])
        if not env or env not in sessions:
            print(f"  [SKIP] Unknown environment or not authenticated")
            continue
        
        session = sessions[env]
        apic_url = APIC_URLS[env]
        tenants_list = TENANTS[env]
        
        # Refresh APIC token if aging (prevents 403 on long batch runs)
        if env in token_states:
            if not ensure_token_fresh(session, apic_url, token_states[env]):
                reauth_apic(session, apic_url, _credentials["username"],
                           _credentials["password"], token_states[env])
        
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
        print(f"  Processing {len(vlans)} VLAN(s) (searching {len(tenants_list)} tenants)...")
        
        for vlan in vlans:
            # Find EPG(s) for this VLAN across ALL tenants
            epg_results = get_epg_app_profiles_all_tenants(session, apic_url, tenants_list, vlan)
            
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
            
            # Check if VLAN exists in multiple Application Profiles/Tenants
            if len(epg_results) > 1:
                cache_key = f"{env}:{vlan}"
                if cache_key in ap_tenant_selections:
                    selected_ap, selected_tenant = ap_tenant_selections[cache_key]
                    epg_results = [e for e in epg_results if e['app_profile'] == selected_ap and e['tenant'] == selected_tenant]
                else:
                    alerts.append({
                        "type": "MULTI_AP",
                        "switch": dep['switch'],
                        "port": dep['port'],
                        "vlan": vlan,
                        "env": env,
                        "options": epg_results,
                        "message": f"VLAN {vlan} exists in {len(epg_results)} locations"
                    })
                    continue
            
            if epg_results:
                epg = epg_results[0]
                epg_tenant = epg.get('tenant', tenants_list[0])
                
                # Check if binding already exists
                already_bound = check_epg_binding_exists(
                    session, apic_url, epg_tenant, epg['app_profile'], epg['epg_name'], path_dn
                )
                
                all_bindings.append({
                    "switch": dep['switch'],
                    "port": dep['port'],
                    "node_id": node_id,
                    "vlan": vlan,
                    "env": env,
                    "tenant": epg_tenant,
                    "app_profile": epg['app_profile'],
                    "epg_name": epg['epg_name'],
                    "path_dn": path_dn,
                    "already_bound": already_bound,
                    "mode": binding_mode
                })
    
    # =========================================================================
    # PHASE 2: RESOLVE MULTI-AP ALERTS
    # =========================================================================
    
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
            print(f"\n[ALERT] VLAN {alert['vlan']} exists in multiple locations:")
            print("-" * 60)
            for i, opt in enumerate(alert['options'], 1):
                print(f"  [{i}] {opt.get('tenant', 'N/A')} / {opt['app_profile']} -> {opt['epg_name']}")
            print("-" * 60)
            
            while True:
                sys.stdout.write(f"\nSelect for VLAN {alert['vlan']}: ")
                sys.stdout.flush()
                choice = input().strip()
                try:
                    sel_idx = int(choice) - 1
                    if 0 <= sel_idx < len(alert['options']):
                        selected = alert['options'][sel_idx]
                        selected_tenant = selected.get('tenant', TENANTS[alert['env']][0])
                        ap_tenant_selections[key] = (selected['app_profile'], selected_tenant)
                        print(f"  [SELECTED] {selected_tenant} / {selected['app_profile']}")
                        
                        # Add bindings for all ports with this VLAN
                        for dep in deployments:
                            dep_env = detect_environment(dep['switch'])
                            if f"{dep_env}:{alert['vlan']}" != key:
                                continue
                            if dep_env not in sessions:
                                continue
                            
                            dep_session = sessions[dep_env]
                            dep_apic_url = APIC_URLS[dep_env]
                            dep_node_id = extract_node_id(dep['switch'])
                            
                            dep_vlans = parse_vlans(dep['vlans'])
                            if alert['vlan'] not in dep_vlans:
                                continue
                            
                            dep_eth = f"eth{dep['port']}" if not dep['port'].startswith("eth") else dep['port']
                            dep_path = f"topology/pod-{POD_ID}/paths-{dep_node_id}/pathep-[{dep_eth}]"
                            
                            already_bound = check_epg_binding_exists(
                                dep_session, dep_apic_url, selected_tenant,
                                selected['app_profile'], selected['epg_name'], dep_path
                            )
                            
                            all_bindings.append({
                                "switch": dep['switch'],
                                "port": dep['port'],
                                "node_id": dep_node_id,
                                "vlan": alert['vlan'],
                                "env": dep_env,
                                "tenant": selected_tenant,
                                "app_profile": selected['app_profile'],
                                "epg_name": selected['epg_name'],
                                "path_dn": dep_path,
                                "already_bound": already_bound,
                                "mode": binding_mode
                            })
                        break
                except:
                    pass
                print("  [ERROR] Invalid selection")
    
    # =========================================================================
    # PHASE 3: PREVIEW ALL BINDINGS
    # =========================================================================
    
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
    if overwrite_mode:
        print(f"  Already exist:  {len(existing_bindings)} (will be RE-DEPLOYED after wipe)")
        ow_ports = set((b['switch'], b['port']) for b in all_bindings)
        mode_label = "ALL auto-deleted" if overwrite_auto else "interactive selection"
        print(f"  [OVERWRITE] {len(ow_ports)} port(s) — existing bindings will be {mode_label}")
    else:
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
    
    if existing_bindings and not overwrite_mode:
        print(f"\n  === EXISTING BINDINGS (will skip) ===")
        for b in existing_bindings[:5]:
            print(f"  {b['switch']} port {b['port']}: VLAN {b['vlan']} already bound")
        if len(existing_bindings) > 5:
            print(f"  ... and {len(existing_bindings) - 5} more")
    
    # =========================================================================
    # PHASE 4: CONFIRM AND DEPLOY
    # =========================================================================
    
    print("\n" + "=" * 70)
    print(" PHASE 4: DEPLOYMENT")
    print("=" * 70)
    
    if dry_run:
        print("\n[DRY-RUN] Would deploy the following bindings:")
        for b in new_bindings:
            print(f"  - {b['switch']} port {b['port']}: VLAN {b['vlan']} -> {b['epg_name']}")
        print(f"\n[DRY-RUN] {len(new_bindings)} binding(s) would be created")
        sys.exit(0)
    
    # Confirmation prompt
    deploy_count = len(all_bindings) if overwrite_mode else len(new_bindings)
    if overwrite_auto:
        print(f"\nReady to OVERWRITE ALL: auto-delete existing + deploy {deploy_count} binding(s)")
    elif overwrite_interactive:
        print(f"\nReady to OVERWRITE (interactive): select deletions + deploy {deploy_count} binding(s)")
    else:
        print(f"\nReady to deploy {deploy_count} binding(s)")
    
    print("\n  [Y] Yes - Deploy all bindings")
    print("  [N] No - Cancel")
    
    sys.stdout.write("\nConfirm deployment: ")
    sys.stdout.flush()
    confirm = input().strip().upper()
    
    if confirm not in ['Y', 'YES']:
        print("\n[CANCELLED]")
        sys.exit(0)
    
    # =========================================================================
    # OVERWRITE: QUERY AND DELETE EXISTING EPG BINDINGS
    # =========================================================================
    
    overwrite_deleted = 0
    
    if overwrite_mode:
        print("\n" + "=" * 70)
        if overwrite_auto:
            print(" OVERWRITE ALL: REMOVING EXISTING EPG BINDINGS")
        else:
            print(" OVERWRITE: INTERACTIVE EPG BINDING REMOVAL")
        print("=" * 70)
        
        # Build unique set of switch+port combinations from CSV
        ports_to_clean = {}
        for b in all_bindings:
            key = (b['switch'], b['port'], b['node_id'], b['env'])
            if key not in ports_to_clean:
                ports_to_clean[key] = []
            ports_to_clean[key].append(b)
        
        port_num = 0
        port_total = len(ports_to_clean)
        
        for (switch, port, node_id, env), port_bindings in sorted(ports_to_clean.items()):
            port_num += 1
            print(f"\n  {'=' * 60}")
            print(f"  Port {port_num}/{port_total}: {switch} port {port}")
            print(f"  {'=' * 60}")
            
            if env not in sessions:
                print(f"  [SKIP] No session for {env}")
                continue
            
            session = sessions[env]
            apic_url = APIC_URLS[env]
            tenants_list = TENANTS.get(env, [])
            
            # =============================================================
            # 3-STRATEGY QUERY — individual + per-tenant + VPC protpaths
            # =============================================================
            existing = query_all_bindings_on_port(
                session, apic_url, node_id, port, POD_ID,
                tenants=tenants_list,
                token_state=token_states.get(env),
                credentials=_credentials
            )
            
            # No bindings found
            if not existing:
                print(f"  [INFO] No existing bindings found on this port")
                continue
            
            # =============================================================
            # INTERACTIVE MODE: show list, let user select which to delete
            # =============================================================
            to_delete = []
            
            if overwrite_interactive:
                print(f"\n  Existing EPG bindings on this port:")
                print(f"  {'-' * 70}")
                print(f"  {'#':<5} {'VLAN':<7} {'EPG':<35} {'Tenant':<12} {'App Profile'}")
                print(f"  {'-' * 70}")
                for idx, b_ex in enumerate(existing, 1):
                    print(f"  [{idx:<3}] {b_ex.get('vlan', '?'):<7} {b_ex.get('epg', '?'):<35} {b_ex.get('tenant', '?'):<12} {b_ex.get('app_profile', '?')}")
                print(f"  {'-' * 70}")
                
                print(f"\n  [A] Select ALL bindings")
                print(f"  [N] Select NONE (skip this port)")
                print(f"  Or enter numbers: 1,2,5 or ranges: 1-5,8,10-12")
                
                sys.stdout.write(f"\n  Delete which? [default=all]: ")
                sys.stdout.flush()
                selection = input().strip().lower()
                
                if selection in ["", "all", "a"]:
                    to_delete = existing[:]
                    print(f"  -> Deleting ALL {len(to_delete)} binding(s)")
                elif selection in ["none", "n", "0"]:
                    to_delete = []
                    print(f"  -> Skipping deletion on this port")
                else:
                    # Parse comma-separated indices and ranges (e.g. "1-5,8,10-12")
                    selected_indices = set()
                    try:
                        for part in selection.split(","):
                            part = part.strip()
                            if "-" in part:
                                range_parts = part.split("-")
                                start = int(range_parts[0].strip())
                                end = int(range_parts[1].strip())
                                for i in range(start, end + 1):
                                    if 1 <= i <= len(existing):
                                        selected_indices.add(i)
                            elif part.isdigit():
                                i = int(part)
                                if 1 <= i <= len(existing):
                                    selected_indices.add(i)
                        
                        to_delete = [existing[i - 1] for i in sorted(selected_indices)]
                        print(f"  -> Deleting {len(to_delete)} of {len(existing)} binding(s)")
                    except (ValueError, IndexError):
                        print(f"  [WARNING] Invalid selection, skipping this port")
                        to_delete = []
            
            # =============================================================
            # AUTO MODE: delete all automatically, no prompts
            # =============================================================
            elif overwrite_auto:
                to_delete = existing[:]
                print(f"  [AUTO] Deleting all {len(to_delete)} existing binding(s)")
            
            # =============================================================
            # DELETE SELECTED BINDINGS
            # =============================================================
            if to_delete:
                for b_del in to_delete:
                    try:
                        resp = session.delete(
                            f"{apic_url}/api/mo/{b_del['dn']}.json",
                            verify=False, timeout=30
                        )
                        ok = resp.status_code == 200
                    except Exception:
                        ok = False
                    
                    status = "[DELETED]" if ok else "[FAIL]"
                    print(f"    {status} VLAN {b_del.get('vlan', '?')} — {b_del.get('epg', '?')} ({b_del.get('tenant', '?')})")
                    if ok:
                        overwrite_deleted += 1
                    else:
                        print(f"           DN: {b_del.get('dn', '?')[:80]}")
                
                # Verify port is clean after deletion
                time.sleep(1)
                verify_bindings = query_all_bindings_on_port(
                    session, apic_url, node_id, port, POD_ID,
                    tenants=tenants_list, verbose=False,
                    token_state=token_states.get(env),
                    credentials=_credentials
                )
                if not verify_bindings:
                    print(f"  [VERIFIED] Port is clean — 0 bindings remain")
                else:
                    print(f"  [WARNING] {len(verify_bindings)} binding(s) still remain after deletion")
        
        print(f"\n  {'=' * 60}")
        print(f"  OVERWRITE COMPLETE: {overwrite_deleted} binding(s) removed")
        print(f"  {'=' * 60}")
    
    # =========================================================================
    # DEPLOY NEW BINDINGS
    # =========================================================================
    
    print("\n" + "=" * 70)
    print(" DEPLOYING EPG BINDINGS")
    print("=" * 70)
    
    success_count = 0
    fail_count = 0
    
    # In overwrite mode, deploy ALL bindings (not just "new" — we wiped ports)
    deploy_list = all_bindings if overwrite_mode else new_bindings
    
    # Track port changes for separator output
    _last_deploy_port = None
    for b in deploy_list:
        _dp_key = (b['switch'], b['port'])
        if _dp_key != _last_deploy_port:
            _last_deploy_port = _dp_key
            _dp_count = sum(1 for x in deploy_list if (x['switch'], x['port']) == _dp_key)
            print(f"\n  {'-' * 60}")
            print(f"  {b['switch']} port {b['port']} — {_dp_count} binding(s)")
            print(f"  {'-' * 60}")
        
        session = sessions[b['env']]
        apic_url = APIC_URLS[b['env']]
        
        # Refresh token if needed
        if b['env'] in token_states:
            if not ensure_token_fresh(session, apic_url, token_states[b['env']]):
                reauth_apic(session, apic_url, _credentials["username"],
                           _credentials["password"], token_states[b['env']])
        
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
    
    # =========================================================================
    # SUMMARY
    # =========================================================================
    
    print("\n" + "=" * 70)
    print(" COMPLETE")
    print("=" * 70)
    print(f"\n  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    if overwrite_mode:
        print(f"  Wiped:   {overwrite_deleted} (existing bindings removed)")
    else:
        print(f"  Skipped: {len(existing_bindings)} (already existed)")
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
