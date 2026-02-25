#!/usr/bin/env python3
"""
ACI Bulk EPG Delete Script
===========================
Remove EPG static bindings from existing ports.

Features:
- TWO MODES:
  1. CSV Mode: Provide SWITCH, PORT, and VLANS in CSV to delete specific bindings
  2. Query Mode: Provide SWITCH and PORT only — script queries ALL EPG bindings
     on the port and presents a multi-select list to choose which to delete
- Preview all deletions before executing
- Dry-run mode
- Batch deletion
- Multi-select EPG removal from queried port

Input CSV Format (VLANS optional):
Switch,Port,VLANS
EDCLEAFACC1501,1/68,"32,64-67"
EDCLEAFNSM2163,1/5,
EDCLEAFACC1502,1/10

When VLANS is blank, the script queries the port and lets you pick interactively.

Author: Network Automation Script
Version: 2.0.0 — Added query-and-select mode
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

# Import shared utilities
from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_port,
    prompt_input
)


# =============================================================================
# CONFIGURATION - Update these values for your environment
# =============================================================================

APIC_URLS = {
    "D1": "",  # <-- UPDATE THIS (ACC switches)
    "D2": "",  # <-- UPDATE THIS (SDC switches)
    "D3": ""   # <-- UPDATE THIS (NSM switches)
}

# Multiple tenants per datacenter
TENANTS = {
    "D1": ["BLU", "GWC", "GWS"],
    "D2": ["BLU", "GWC", "GWS"],
    "D3": ["NSM_BLU", "NSM_BRN", "NSM_GLD", "NSM_GRN"]
}

POD_ID = "1"
DEPLOYMENT_FILE = "epg_delete.csv"


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


def find_epg_binding(session, apic_url, tenant, app_profile, epg_name, path_dn):
    """Find if an EPG binding exists and return its details."""
    try:
        url = (f"{apic_url}/api/mo/uni/tn-{tenant}/ap-{app_profile}/epg-{epg_name}.json"
               f"?query-target=children&target-subtree-class=fvRsPathAtt")
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


def query_all_bindings_on_port(session, apic_url, node_id, port, pod_id="1"):
    """
    Query ALL EPG static bindings currently deployed on a specific port.

    This is the core of the new query-and-select feature.
    Searches fvRsPathAtt class for any binding matching this port's path DN.

    Returns list of dicts with: tenant, app_profile, epg_name, vlan, mode, dn

    CCIE Automation Note:
    This uses the ACI REST API fvRsPathAtt class query with a tDn filter —
    a common pattern for auditing deployed static paths on the fabric.
    """
    eth_port = f"eth{port}" if not port.startswith("eth") else port
    path_dn = f"topology/pod-{pod_id}/paths-{node_id}/pathep-[{eth_port}]"

    bindings = []

    try:
        # Query all fvRsPathAtt objects that reference this port's path DN
        url = (f"{apic_url}/api/class/fvRsPathAtt.json"
               f"?query-target-filter=eq(fvRsPathAtt.tDn,\"{path_dn}\")")
        response = session.get(url, verify=False, timeout=30)

        if response.status_code != 200:
            print(f"    [ERROR] Failed to query bindings: HTTP {response.status_code}")
            return []

        for item in response.json().get("imdata", []):
            attrs = item.get("fvRsPathAtt", {}).get("attributes", {})
            dn = attrs.get("dn", "")
            encap = attrs.get("encap", "")  # e.g., "vlan-32"
            mode = attrs.get("mode", "")

            # Extract VLAN number from encap
            vlan_match = re.search(r'vlan-(\d+)', encap)
            vlan_id = int(vlan_match.group(1)) if vlan_match else 0

            # Extract tenant, app_profile, epg from the DN
            # DN format: uni/tn-{tenant}/ap-{app_profile}/epg-{epg}/rspathAtt-[...]
            tenant_match = re.search(r'/tn-([^/]+)/', dn)
            ap_match = re.search(r'/ap-([^/]+)/', dn)
            epg_match = re.search(r'/epg-([^/]+)/', dn)

            if tenant_match and ap_match and epg_match:
                bindings.append({
                    "tenant": tenant_match.group(1),
                    "app_profile": ap_match.group(1),
                    "epg_name": epg_match.group(1),
                    "vlan": vlan_id,
                    "mode": mode,
                    "encap": encap,
                    "binding_dn": dn
                })

    except Exception as e:
        print(f"    [ERROR] Failed to query port bindings: {e}")

    # Sort by VLAN number
    bindings.sort(key=lambda x: x['vlan'])
    return bindings


def display_binding_selection(bindings, switch, port):
    """
    Display all EPG bindings on a port and let user multi-select which to delete.

    Returns list of selected binding dicts, or empty list to skip.
    """
    if not bindings:
        print(f"\n  [INFO] No EPG bindings found on {switch} port {port}")
        return []

    print(f"\n  EPG Bindings on {switch} port {port}:")
    print(f"  Found {len(bindings)} binding(s)")
    print("  " + "-" * 76)
    print(f"  {'#':>4}  {'VLAN':<6} {'EPG':<28} {'App Profile':<18} {'Tenant':<12} {'Mode'}")
    print("  " + "-" * 76)

    for i, b in enumerate(bindings, 1):
        mode_display = "trunk" if b['mode'] == "regular" else "access" if b['mode'] == "untagged" else b['mode']
        print(f"  [{i:>2}]  {b['vlan']:<6} {b['epg_name']:<28} {b['app_profile']:<18} {b['tenant']:<12} {mode_display}")

    print("  " + "-" * 76)
    print("  [A] Select ALL bindings")
    print("  [S] Skip this port")
    print()
    print("  Enter numbers separated by commas (e.g., 1,3,5) or ranges (e.g., 1-4):")

    while True:
        choice = prompt_input("\n  Select bindings to delete: ").strip().upper()

        if choice == 'S':
            return []

        if choice == 'A' or choice == 'ALL':
            print(f"  [SELECTED] All {len(bindings)} binding(s)")
            return list(bindings)

        # Parse comma-separated numbers and ranges
        selected = []
        try:
            for part in choice.split(","):
                part = part.strip()
                if "-" in part:
                    start, end = part.split("-", 1)
                    for idx in range(int(start), int(end) + 1):
                        if 1 <= idx <= len(bindings):
                            selected.append(bindings[idx - 1])
                else:
                    idx = int(part)
                    if 1 <= idx <= len(bindings):
                        selected.append(bindings[idx - 1])

            if selected:
                # Deduplicate
                seen = set()
                unique = []
                for s in selected:
                    key = s['binding_dn']
                    if key not in seen:
                        seen.add(key)
                        unique.append(s)
                print(f"  [SELECTED] {len(unique)} binding(s)")
                return unique
        except (ValueError, IndexError):
            pass

        print("  [ERROR] Invalid selection. Use numbers like 1,3,5 or 1-4 or A for all")


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
    """Load EPG delete deployment CSV file.

    VLANS column is now optional — if blank, script enters query mode for that row.
    """
    try:
        with open(filename, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            deployments = []
            for row in reader:
                normalized = {k.strip().upper(): v.strip() if v else "" for k, v in row.items() if k}
                deployments.append({
                    "switch": normalized.get("SWITCH", ""),
                    "port": parse_port(normalized.get("PORT", "")),
                    "vlans": normalized.get("VLANS", "")  # May be empty — triggers query mode
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
    print(" ACI BULK EPG DELETE SCRIPT v2.0")
    print("=" * 70)

    # Check configuration
    missing_urls = [dc for dc, url in APIC_URLS.items() if not url]
    if missing_urls:
        print(f"\n[ERROR] APIC URLs not configured for: {', '.join(missing_urls)}")
        sys.exit(1)

    # Select input mode
    print("\n" + "-" * 70)
    print(" INPUT MODE")
    print("-" * 70)
    print("\n  [1] CSV File - Load switch/port/VLANs from file")
    print("  [2] Interactive - Enter switch and port, query EPGs live")

    while True:
        input_mode = prompt_input("\nSelect mode (1/2): ").strip()
        if input_mode in ['1', '2']:
            break

    if input_mode == '1':
        # CSV mode
        print(f"\n[INFO] Default deployment file: {DEPLOYMENT_FILE}")
        custom_file = prompt_input("Press Enter to use default, or enter filename: ").strip()
        deployment_file = custom_file if custom_file else DEPLOYMENT_FILE

        print(f"\n[INFO] Loading from: {deployment_file}")
        deployments = load_epg_delete_csv(deployment_file)
        if not deployments:
            sys.exit(1)

        # Report what we found
        csv_mode_count = sum(1 for d in deployments if d['vlans'].strip())
        query_mode_count = sum(1 for d in deployments if not d['vlans'].strip())
        print(f"[INFO] Loaded {len(deployments)} entry/entries")
        if csv_mode_count:
            print(f"       {csv_mode_count} with specific VLANs (targeted delete)")
        if query_mode_count:
            print(f"       {query_mode_count} without VLANs (will query port for EPGs)")

    else:
        # Interactive mode — build deployments from user input
        deployments = []
        print("\n[INFO] Interactive mode — enter switch/port pairs (blank line to finish)")
        while True:
            switch = prompt_input("\n  Switch name (or Enter to finish): ").strip()
            if not switch:
                break
            port = prompt_input("  Port (e.g., 1/68): ").strip()
            if not port:
                break
            deployments.append({
                "switch": switch,
                "port": parse_port(port),
                "vlans": ""  # Always query mode in interactive
            })
            print(f"  [ADDED] {switch} port {parse_port(port)} (will query EPGs)")

        if not deployments:
            print("\n[INFO] No entries. Exiting.")
            sys.exit(0)

        print(f"\n[INFO] {len(deployments)} port(s) to query")

    # Select run mode
    print("\n" + "-" * 70)
    print(" RUN MODE")
    print("-" * 70)
    print("\n  [1] Normal - Delete EPG bindings")
    print("  [2] Dry-Run - Validate only, don't delete")

    while True:
        mode_choice = prompt_input("\nSelect mode (1/2): ").strip()
        if mode_choice in ['1', '2']:
            break
    dry_run = (mode_choice == '2')

    # Get credentials
    print("\n" + "-" * 70)
    print(" AUTHENTICATION")
    print("-" * 70)
    username = prompt_input("\nUsername: ").strip()
    sys.stdout.write("Password: ")
    sys.stdout.flush()
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
    ap_tenant_selections = {}

    for idx, dep in enumerate(deployments, 1):
        print(f"\n[{idx}/{len(deployments)}] {dep['switch']} port {dep['port']}")

        env = detect_environment(dep['switch'])
        if not env or env not in sessions:
            print(f"  [SKIP] Unknown environment or not authenticated")
            continue

        session = sessions[env]
        apic_url = APIC_URLS[env]
        tenants_list = TENANTS[env]
        node_id = extract_node_id(dep['switch'])

        if not node_id:
            print(f"  [SKIP] Cannot extract node ID from switch name")
            continue

        # Build path DN
        eth_port = f"eth{dep['port']}" if not dep['port'].startswith("eth") else dep['port']
        path_dn = f"topology/pod-{POD_ID}/paths-{node_id}/pathep-[{eth_port}]"

        # ======================================================================
        # QUERY MODE: No VLANs specified — query port and let user select
        # ======================================================================
        if not dep['vlans'].strip():
            print(f"  Querying all EPG bindings on port...")
            port_bindings = query_all_bindings_on_port(
                session, apic_url, node_id, dep['port'], POD_ID
            )

            if not port_bindings:
                print(f"  [INFO] No EPG bindings found on this port")
                continue

            # Display multi-select
            selected = display_binding_selection(port_bindings, dep['switch'], dep['port'])

            for b in selected:
                bindings_to_delete.append({
                    "switch": dep['switch'],
                    "port": dep['port'],
                    "node_id": node_id,
                    "vlan": b['vlan'],
                    "env": env,
                    "tenant": b['tenant'],
                    "app_profile": b['app_profile'],
                    "epg_name": b['epg_name'],
                    "binding_dn": b['binding_dn'],
                    "mode": b['mode']
                })

            continue

        # ======================================================================
        # CSV MODE: Specific VLANs provided — targeted delete (original behavior)
        # ======================================================================
        vlans = parse_vlans(dep['vlans'])
        print(f"  Searching for {len(vlans)} VLAN binding(s) (across {len(tenants_list)} tenants)...")

        for vlan in vlans:
            # Find EPG(s) for this VLAN across ALL tenants
            epg_results = get_epg_app_profiles_all_tenants(session, apic_url, tenants_list, vlan)

            if not epg_results:
                print(f"    VLAN {vlan}: [WARNING] No EPG found")
                not_found.append({
                    "switch": dep['switch'],
                    "port": dep['port'],
                    "vlan": vlan,
                    "reason": "No EPG found for this VLAN"
                })
                continue

            # Handle multiple app profiles
            if len(epg_results) > 1:
                key = f"{env}:{vlan}"
                if key in ap_tenant_selections:
                    selection = ap_tenant_selections[key]
                else:
                    print(f"\n    VLAN {vlan} found in multiple locations:")
                    print("    " + "-" * 60)
                    for i, e in enumerate(epg_results, 1):
                        print(f"    [{i}] {e['tenant']} / {e['app_profile']} -> {e['epg_name']}")
                    print(f"    [A] All of the above")
                    print("    " + "-" * 60)

                    while True:
                        sel = prompt_input("    Select: ").strip().upper()
                        if sel == 'A':
                            selection = "ALL"
                            break
                        try:
                            si = int(sel) - 1
                            if 0 <= si < len(epg_results):
                                selection = (epg_results[si]['app_profile'], epg_results[si]['tenant'])
                                break
                        except ValueError:
                            pass
                        print("    [ERROR] Invalid selection")

                    ap_tenant_selections[key] = selection

                if selection != "ALL":
                    ap, tn = selection
                    epg_results = [e for e in epg_results if e['app_profile'] == ap and e.get('tenant') == tn]

            # Find actual bindings
            for epg in epg_results:
                epg_tenant = epg.get('tenant', tenants_list[0])
                binding = find_epg_binding(session, apic_url, epg_tenant, epg['app_profile'], epg['epg_name'], path_dn)

                if binding:
                    bindings_to_delete.append({
                        "switch": dep['switch'],
                        "port": dep['port'],
                        "node_id": node_id,
                        "vlan": vlan,
                        "env": env,
                        "tenant": epg_tenant,
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

    confirm = prompt_input("\nConfirm deletion (type 'YES' to confirm): ").strip().upper()

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
