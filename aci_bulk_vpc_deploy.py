#!/usr/bin/env python3
"""
ACI Bulk VPC Deployment Script
===============================
Reads a deployment CSV and provisions VPCs in ACI.

Features:
- Live queries to APIC (no dependency files needed)
- Auto-detects environment from switch name (NSM > SDC > ACC)
- PRE-FLIGHT validation: Checks AEP, Interface Profiles, Link Level policies
- Auto-finds interface profile matching {node1}-{node2} pattern
- Queries available ports (discovery state, no description)
- For VPCs: Finds same port available on BOTH switches
- Auto-detects Application Profile from VLAN/EPG lookup
- Creates VPC Policy Group with proper naming
- Shows full configuration preview before each deployment
- Edit interface configuration before deployment
- Confirms every deployment
- Dry-run mode available

Input CSV Format:
Hostname,Switch1,Switch2,Speed,VLANS,WorkOrder
MEDHVIOP173_SEA_PROD,EDCLEAFACC1501,EDCLEAFACC1502,25G,"32,64-67,92-95,1035",WO123456

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

# Shared utilities (consolidated from duplicated helpers)
from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, find_common_ports_with_status,
    display_vpc_port_selection, display_vpc_independent_port_selection,
    get_validated_available_ports, find_common_validated_ports,
    cleanup_port_for_redeployment, cleanup_vpc_port_for_redeployment,
    query_existing_vpc_policy_groups, display_policy_group_selection,
    query_all_bindings_on_port,
    ensure_token_fresh, reauth_apic
)


# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# =============================================================================
# CONFIGURATION - Update these values for your environment
# =============================================================================

APIC_URLS = {
    "D1": "https://edcapic01.gwnsm.guidewell.net/",  # <-- UPDATE THIS (BLU Tenant - ACC switches)
    "D2": "https://sdcapic01.gwnsm.guidewell.net/",  # <-- UPDATE THIS (BLU Tenant - SDC switches)
    "D3": "https://edcnsmapic01.gwnsm.guidewell.net/"   # <-- UPDATE THIS (NSM_BLU Tenant - NSM switches)
}

# Multiple tenants per datacenter
TENANTS = {
    "D1": ["BLU", "GWC", "GWS"],
    "D2": ["BLU", "GWC", "GWS", "SDCFLB", "SDCGWS"],
    "D3": ["NSM_BLU", "NSM_BRN", "NSM_GLD", "NSM_GRN"]
}

# Default deployment input file
DEPLOYMENT_FILE = "vpc_deployments.csv"

# Pod ID
POD_ID = "1"

# Deployment Immediacy
DEPLOYMENT_IMMEDIACY = "immediate"

# =============================================================================
# DEFAULT POLICIES
# =============================================================================

# Default AEP per datacenter
DEFAULT_AEP = {
    "D1": "edcflb",
    "D2": "flb",
    "D3": "edcnsm"
}

CDP_POLICY = "cdp-disabled"
LLDP_POLICY = "lldp-enabled"
PORT_CHANNEL_POLICY = "lacp-active"
MCP_POLICY = "mcdp-enabled"
STORM_CONTROL_POLICY = "STORMCONTROL_5"

# =============================================================================
# SPEED MAPPING
# =============================================================================

SPEED_MAPPING = {
    "25G": "25GB",
    "1G": "1g",
    "10G": "10g",
    "40G": "40g",
    "100G": "100g"
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

# prompt_input() — moved to aci_port_utils.py

# detect_environment() — moved to aci_port_utils.py

# extract_node_id() — moved to aci_port_utils.py

# parse_vlans() — moved to aci_port_utils.py

# parse_interface() — moved to aci_port_utils.py

# =============================================================================
# API FUNCTIONS
# =============================================================================

def login_to_apic(session, apic_url, username, password):
    """Authenticate to APIC."""
    try:
        response = session.post(
            f"{apic_url}/api/aaaLogin.json",
            json={"aaaUser": {"attributes": {"name": username, "pwd": password}}},
            verify=False, timeout=30
        )
        return response.status_code == 200
    except:
        return False


def check_aep_exists(session, apic_url, aep_name):
    """Check if an AEP exists."""
    try:
        response = session.get(f"{apic_url}/api/node/mo/uni/infra/attentp-{aep_name}.json", verify=False, timeout=30)
        if response.status_code == 200:
            return bool(response.json().get("imdata"))
        return False
    except:
        return False


def get_all_aeps(session, apic_url):
    """Get all available AEPs."""
    try:
        response = session.get(f"{apic_url}/api/class/infraAttEntityP.json", verify=False, timeout=30)
        if response.status_code == 200:
            return sorted([item.get("infraAttEntityP", {}).get("attributes", {}).get("name", "") 
                          for item in response.json().get("imdata", [])])
        return []
    except:
        return []


def get_interface_profiles(session, apic_url):
    """Get all interface profiles."""
    try:
        response = session.get(f"{apic_url}/api/class/infraAccPortP.json", verify=False, timeout=30)
        if response.status_code == 200:
            return sorted([item.get("infraAccPortP", {}).get("attributes", {}).get("name", "") 
                          for item in response.json().get("imdata", [])])
        return []
    except:
        return []


def get_link_level_policies(session, apic_url):
    """Get all Link Level (Fabric HIF) Policies."""
    try:
        response = session.get(f"{apic_url}/api/class/fabricHIfPol.json", verify=False, timeout=30)
        if response.status_code == 200:
            policies = []
            for item in response.json().get("imdata", []):
                attrs = item.get("fabricHIfPol", {}).get("attributes", {})
                policies.append(attrs.get("name", ""))
            return sorted(policies)
        return []
    except:
        return []


def find_interface_profile_for_nodes(profiles, node1, node2):
    """Find interface profile containing both nodes (e.g., '1501-1502' pattern)."""
    pattern1 = f"{node1}-{node2}"
    pattern2 = f"{node2}-{node1}"
    
    for profile in profiles:
        if pattern1 in profile or pattern2 in profile:
            return profile
    return None


def check_vpc_policy_group_exists(session, apic_url, name):
    """Check if a VPC policy group already exists."""
    try:
        response = session.get(f"{apic_url}/api/node/mo/uni/infra/funcprof/accbundle-{name}.json", verify=False, timeout=30)
        if response.status_code == 200:
            return bool(response.json().get("imdata"))
        return False
    except:
        return False


def check_epg_exists(session, apic_url, tenant, app_profile, epg_name):
    """Check if an EPG exists."""
    try:
        response = session.get(f"{apic_url}/api/node/mo/uni/tn-{tenant}/ap-{app_profile}/epg-{epg_name}.json", verify=False, timeout=30)
        if response.status_code == 200:
            return bool(response.json().get("imdata"))
        return False
    except:
        return False


# validate_single_port() — moved to aci_port_utils.py

# get_validated_available_ports() — moved to aci_port_utils.py

def get_epg_app_profile(session, apic_url, tenant, vlan_id):
    """Find which Application Profile(s) contain the EPG for a given VLAN in a single tenant."""
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
                results.append((ap_match.group(1), epg_name, tenant))
        return results
    except:
        return []


def get_epg_app_profile_all_tenants(session, apic_url, tenants, vlan_id):
    """Find which Application Profile(s) contain the EPG for a given VLAN across all tenants."""
    all_results = []
    for tenant in tenants:
        results = get_epg_app_profile(session, apic_url, tenant, vlan_id)
        all_results.extend(results)
    return all_results


def set_port_description(session, apic_url, node_id, interface, description):
    """Set the description on a physical interface."""
    # Interface format: 1/5 -> eth1/5
    if not interface.startswith("eth"):
        interface = f"eth{interface}"
    
    payload = {
        "l1PhysIf": {
            "attributes": {
                "descr": description
            }
        }
    }
    try:
        response = session.post(
            f"{apic_url}/api/node/mo/topology/pod-{POD_ID}/node-{node_id}/sys/phys-[{interface}].json",
            json=payload, verify=False, timeout=30
        )
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


def create_vpc_policy_group(session, apic_url, name, link_level, flow_control, aep):
    """Create a VPC Interface Policy Group."""
    payload = {
        "infraAccBndlGrp": {
            "attributes": {"name": name, "lagT": "node"},
            "children": [
                {"infraRsAttEntP": {"attributes": {"tDn": f"uni/infra/attentp-{aep}"}}},
                {"infraRsCdpIfPol": {"attributes": {"tnCdpIfPolName": CDP_POLICY}}},
                {"infraRsHIfPol": {"attributes": {"tnFabricHIfPolName": link_level}}},
                {"infraRsLldpIfPol": {"attributes": {"tnLldpIfPolName": LLDP_POLICY}}},
                {"infraRsLacpPol": {"attributes": {"tnLacpLagPolName": PORT_CHANNEL_POLICY}}},
                {"infraRsMcpIfPol": {"attributes": {"tnMcpIfPolName": MCP_POLICY}}},
                {"infraRsStormctrlIfPol": {"attributes": {"tnStormctrlIfPolName": STORM_CONTROL_POLICY}}}
            ]
        }
    }
    if flow_control != "default":
        payload["infraAccBndlGrp"]["children"].append(
            {"infraRsQosIngressDppIfPol": {"attributes": {"tnQosDppPolName": flow_control}}}
        )
    try:
        response = session.post(f"{apic_url}/api/node/mo/uni/infra/funcprof/accbundle-{name}.json", 
                               json=payload, verify=False, timeout=30)
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


def create_port_selector(session, apic_url, interface_profile, selector_name, interface_id, policy_group_name):
    """Create an Access Port Selector under an Interface Profile for VPC."""
    if "/" in interface_id:
        parts = interface_id.split("/")
        from_card, from_port = parts[0], parts[1]
    else:
        from_card, from_port = "1", interface_id
    
    payload = {
        "infraHPortS": {
            "attributes": {"name": selector_name, "type": "range"},
            "children": [
                {"infraPortBlk": {"attributes": {"name": "block2", "fromCard": from_card, "toCard": from_card, "fromPort": from_port, "toPort": from_port}}},
                {"infraRsAccBaseGrp": {"attributes": {"tDn": f"uni/infra/funcprof/accbundle-{policy_group_name}"}}}
            ]
        }
    }
    try:
        response = session.post(f"{apic_url}/api/node/mo/uni/infra/accportprof-{interface_profile}/hports-{selector_name}-typ-range.json",
                               json=payload, verify=False, timeout=30)
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


def deploy_static_binding(session, apic_url, tenant, app_profile, epg_name, vlan_id, mode, path):
    """Deploy a static port binding to an EPG."""
    payload = {
        "fvRsPathAtt": {
            "attributes": {"tDn": path, "encap": f"vlan-{vlan_id}", "mode": mode, "instrImedcy": DEPLOYMENT_IMMEDIACY}
        }
    }
    try:
        response = session.post(f"{apic_url}/api/node/mo/uni/tn-{tenant}/ap-{app_profile}/epg-{epg_name}.json",
                               json=payload, verify=False, timeout=30)
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


# =============================================================================
# PRE-FLIGHT VALIDATION
# =============================================================================

def run_preflight_checks(sessions, deployments):
    """
    Run pre-flight validation for all VPC deployments.
    Returns global_settings dict with resolved AEPs, Interface Profiles, and Link Level policies.
    """
    print("\n" + "=" * 70)
    print(" PRE-FLIGHT VALIDATION")
    print("=" * 70)
    
    global_settings = {
        "aep": {},              # {env: aep_name}
        "int_profiles": {},     # {env: {node_pair: profile_name}}
        "link_level": {}        # {env: {speed: link_level_policy}}
    }
    
    # Group deployments by environment and collect node pairs and speeds
    envs_data = {}  # {env: {"deps": [], "node_pairs": set(), "speeds": set()}}
    
    for dep in deployments:
        env = detect_environment(dep['switch1'])
        if env and env in sessions:
            if env not in envs_data:
                envs_data[env] = {"deps": [], "node_pairs": set(), "speeds": set()}
            envs_data[env]["deps"].append(dep)
            
            node1 = extract_node_id(dep['switch1'])
            node2 = extract_node_id(dep['switch2'])
            if node1 and node2:
                # Ensure consistent ordering
                if int(node1) > int(node2):
                    node1, node2 = node2, node1
                envs_data[env]["node_pairs"].add((node1, node2))
            
            if dep['speed']:
                envs_data[env]["speeds"].add(dep['speed'].upper())
    
    for env, data in envs_data.items():
        session = sessions[env]
        apic_url = APIC_URLS[env]
        
        print(f"\n  [{env}] Checking environment ({len(data['deps'])} VPCs)...")
        
        # 1. Check AEP (per datacenter default)
        default_aep = DEFAULT_AEP.get(env, "edcflb")
        print(f"\n    Checking AEP '{default_aep}'...")
        if check_aep_exists(session, apic_url, default_aep):
            print(f"      [FOUND] {default_aep}")
            global_settings["aep"][env] = default_aep
        else:
            print(f"      [NOT FOUND] {default_aep}")
            aeps = get_all_aeps(session, apic_url)
            if not aeps:
                print(f"      [ERROR] No AEPs found in {env}. Cannot continue.")
                return None
            
            print(f"\n    Available AEPs in {env}:")
            print("    " + "-" * 50)
            for i, aep in enumerate(aeps, 1):
                print(f"    [{i:>3}] {aep}")
            print("    " + "-" * 50)
            
            while True:
                choice = prompt_input(f"\n    Select AEP for ALL {env} VPCs: ").strip()
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(aeps):
                        global_settings["aep"][env] = aeps[idx]
                        print(f"      [SELECTED] {aeps[idx]} (applies to all {env} VPCs)")
                        break
                except ValueError:
                    pass
                print("    [ERROR] Invalid selection")
        
        # 2. Check Interface Profiles for each node pair
        global_settings["int_profiles"][env] = {}
        all_profiles = get_interface_profiles(session, apic_url)
        
        print(f"\n    Checking Interface Profiles for {len(data['node_pairs'])} node pair(s)...")
        
        for node1, node2 in sorted(data['node_pairs']):
            pair_key = f"{node1}-{node2}"
            print(f"\n      Looking for profile matching '{pair_key}'...")
            
            found_profile = find_interface_profile_for_nodes(all_profiles, node1, node2)
            
            if found_profile:
                print(f"        [FOUND] {found_profile}")
                
                # Confirm or allow change
                confirm = prompt_input(f"        Use this profile for all {pair_key} VPCs? (yes/no): ").strip().lower()
                
                if confirm in ['yes', 'y']:
                    global_settings["int_profiles"][env][pair_key] = found_profile
                else:
                    # Show all profiles to select
                    print(f"\n    Available Interface Profiles:")
                    print("    " + "-" * 50)
                    for i, p in enumerate(all_profiles, 1):
                        marker = " <-- suggested" if p == found_profile else ""
                        print(f"    [{i:>3}] {p}{marker}")
                    print("    " + "-" * 50)
                    
                    while True:
                        choice = prompt_input(f"\n    Select profile for {pair_key} (or 'S' to skip): ").strip().upper()
                        if choice == 'S':
                            global_settings["int_profiles"][env][pair_key] = None
                            print(f"        [SKIP] VPCs with nodes {pair_key} will be skipped")
                            break
                        try:
                            idx = int(choice) - 1
                            if 0 <= idx < len(all_profiles):
                                global_settings["int_profiles"][env][pair_key] = all_profiles[idx]
                                print(f"        [SELECTED] {all_profiles[idx]}")
                                break
                        except ValueError:
                            pass
                        print("    [ERROR] Invalid selection")
            else:
                print(f"        [NOT FOUND] No profile matching '{pair_key}'")
                print(f"\n    Available Interface Profiles:")
                print("    " + "-" * 50)
                for i, p in enumerate(all_profiles, 1):
                    print(f"    [{i:>3}] {p}")
                print("    " + "-" * 50)
                
                while True:
                    choice = prompt_input(f"\n    Select profile for {pair_key} (or 'S' to skip): ").strip().upper()
                    if choice == 'S':
                        global_settings["int_profiles"][env][pair_key] = None
                        print(f"        [SKIP] VPCs with nodes {pair_key} will be skipped")
                        break
                    try:
                        idx = int(choice) - 1
                        if 0 <= idx < len(all_profiles):
                            global_settings["int_profiles"][env][pair_key] = all_profiles[idx]
                            print(f"        [SELECTED] {all_profiles[idx]}")
                            break
                    except ValueError:
                        pass
                    print("    [ERROR] Invalid selection")
        
        # 3. Select Link Level Policies by Speed
        global_settings["link_level"][env] = {}
        all_link_levels = get_link_level_policies(session, apic_url)
        
        if data["speeds"]:
            print(f"\n    Configuring Link Level Policies for {len(data['speeds'])} speed(s)...")
            
            for speed in sorted(data["speeds"]):
                suggested = SPEED_MAPPING.get(speed, speed)
                
                # Check if suggested exists
                matching = [ll for ll in all_link_levels if suggested.lower() in ll.lower()]
                
                if matching:
                    print(f"\n      Speed {speed}: Found {len(matching)} matching Link Level policy(ies)")
                    print("      " + "-" * 40)
                    for i, ll in enumerate(matching, 1):
                        print(f"      [{i}] {ll}")
                    print(f"      [{len(matching)+1}] Show ALL Link Level policies")
                    print("      " + "-" * 40)
                    
                    while True:
                        choice = prompt_input(f"\n      Select Link Level policy for {speed} (will apply to all {speed} VPCs): ").strip()
                        try:
                            idx = int(choice) - 1
                            if 0 <= idx < len(matching):
                                global_settings["link_level"][env][speed] = matching[idx]
                                print(f"        [SELECTED] {matching[idx]}")
                                break
                            elif idx == len(matching):
                                # Show all
                                print(f"\n      All Link Level Policies:")
                                print("      " + "-" * 40)
                                for i, ll in enumerate(all_link_levels, 1):
                                    print(f"      [{i}] {ll}")
                                print("      " + "-" * 40)
                                
                                while True:
                                    choice2 = prompt_input(f"\n      Select Link Level policy for {speed}: ").strip()
                                    try:
                                        idx2 = int(choice2) - 1
                                        if 0 <= idx2 < len(all_link_levels):
                                            global_settings["link_level"][env][speed] = all_link_levels[idx2]
                                            print(f"        [SELECTED] {all_link_levels[idx2]}")
                                            break
                                    except ValueError:
                                        pass
                                    print("      [ERROR] Invalid selection")
                                break
                        except ValueError:
                            pass
                        print("      [ERROR] Invalid selection")
                else:
                    print(f"\n      Speed {speed}: No matching Link Level policies found")
                    print(f"\n      All Link Level Policies:")
                    print("      " + "-" * 40)
                    for i, ll in enumerate(all_link_levels, 1):
                        print(f"      [{i}] {ll}")
                    print("      " + "-" * 40)
                    
                    while True:
                        choice = prompt_input(f"\n      Select Link Level policy for {speed}: ").strip()
                        try:
                            idx = int(choice) - 1
                            if 0 <= idx < len(all_link_levels):
                                global_settings["link_level"][env][speed] = all_link_levels[idx]
                                print(f"        [SELECTED] {all_link_levels[idx]}")
                                break
                        except ValueError:
                            pass
                        print("      [ERROR] Invalid selection")
    
    print("\n" + "=" * 70)
    print(" PRE-FLIGHT COMPLETE")
    print("=" * 70)
    
    # Summary
    print("\n  Global Settings:")
    for env in envs_data:
        print(f"\n    [{env}]")
        print(f"      AEP: {global_settings['aep'].get(env, 'NOT SET')}")
        print(f"      Interface Profiles:")
        for pair, profile in global_settings['int_profiles'].get(env, {}).items():
            status = profile if profile else "SKIP"
            print(f"        Nodes {pair}: {status}")
        print(f"      Link Level Policies:")
        for speed, ll in global_settings['link_level'].get(env, {}).items():
            print(f"        Speed {speed}: {ll}")
    
    confirm = prompt_input("\n  Continue with these settings? (yes/no): ").strip().lower()
    if confirm not in ['yes', 'y']:
        return None
    
    return global_settings


# =============================================================================
# DISPLAY FUNCTIONS
# =============================================================================

# display_validated_ports() — moved to aci_port_utils.py

def display_app_profile_choice(options, vlan_id):
    """Display Application Profile options and let user select. Options include tenant."""
    print(f"\n  VLAN {vlan_id} exists in multiple locations:")
    print("  " + "-" * 60)
    for i, option in enumerate(options, 1):
        if len(option) == 3:
            app_profile, epg_name, tenant = option
            print(f"  [{i}] {tenant} / {app_profile} -> {epg_name}")
        else:
            app_profile, epg_name = option
            print(f"  [{i}] {app_profile} -> {epg_name}")
    print("  " + "-" * 60)
    
    while True:
        choice = prompt_input("\n  Select (applies to ALL VLANs in this deployment): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                # Return tuple of (app_profile, tenant) if tenant available
                if len(options[idx]) == 3:
                    return (options[idx][0], options[idx][2])  # (app_profile, tenant)
                return (options[idx][0], None)  # (app_profile, None)
        except ValueError:
            pass
        print("  [ERROR] Invalid selection")


# find_common_validated_ports() — moved to aci_port_utils.py

def display_deployment_preview(config, aep):
    """Display VPC deployment configuration preview."""
    print("\n" + "=" * 70)
    print(" DEPLOYMENT PREVIEW")
    print("=" * 70)
    
    # Use custom description if set
    if 'custom_description' in config and config['custom_description']:
        port_description = config['custom_description']
    else:
        port_description = f"{config['hostname']} {config['work_order']}"
    
    print(f"\n  Environment:           {config['environment']} ({config['tenant']})")
    print(f"  Hostname:              {config['hostname']}")
    print(f"  Work Order:            {config['work_order']}")
    
    print(f"\n  === INTERFACE CONFIGURATION ===")
    print(f"  [1] Node Type:              Leaf")
    print(f"  [2] Port Type:              Access")
    print(f"  [3] Interface Type:         Ethernet")
    print(f"  [4] Interface Aggregation:  VPC")
    print(f"  [5] vPC Leaf Switch Pair:   {config['node1']}-{config['node2']}")
    print(f"  [6] Interfaces For All Switches: {config['interface']}")
    print(f"  [7] Interface Profile:      {config['interface_profile']}")
    print(f"  [8] Port Description:       {port_description}")
    
    print(f"\n  === NEW VPC INTERFACE POLICY GROUP ===")
    print(f"  [9]  Name:                  {config['policy_group']}")
    print(f"       Description:           (empty)")
    print(f"  [10] AEP:                   {aep}")
    print(f"       CDP Policy:            {CDP_POLICY}")
    print(f"  [11] Link Level Policy:     {config['link_level']}")
    print(f"       LLDP Policy:           {LLDP_POLICY}")
    print(f"       Port Channel Policy:   {PORT_CHANNEL_POLICY}")
    print(f"       MCP Policy:            {MCP_POLICY}")
    print(f"       Storm Control Policy:  {STORM_CONTROL_POLICY}")
    print(f"  [12] Flow Control Policy:   {config['flow_control']}")
    
    print(f"\n  === ACCESS PORT SELECTOR (under Interface Profile) ===")
    print(f"  Interface Profile:      {config['interface_profile']}")
    print(f"  Name:                   {config['policy_group']}")
    iface2 = config.get('interface2', config['interface'])
    if config.get('asymmetric_vpc'):
        print(f"  Interface (node {config['node1']}):  eth{config['interface']}")
        print(f"  Interface (node {config['node2']}):  eth{iface2}")
    else:
        print(f"  Interface IDs:          {config['interface']}")
    print(f"  Interface Policy Group: {config['policy_group']}")
    
    print(f"\n  === STATIC EPG BINDING ===")
    print(f"  Port Type:              VPC")
    print(f"  vPC Leaf Switch Pair:   {config['node1']}-{config['node2']}")
    print(f"  Path:                   {config['policy_group']}")
    print(f"  Deployment Immediacy:   Immediate")
    print(f"  Mode:                   Trunk (Tagged)")
    print(f"  PTP:                    Disabled")
    
    print(f"\n  EPG Bindings ({len(config['epg_bindings'])} VLANs):")
    for binding in config['epg_bindings'][:10]:
        tenant_str = f" [{binding.get('tenant', config['tenant'])}]" if binding.get('tenant') else ""
        print(f"    VLAN {binding['vlan']:>4} -> {binding['app_profile']} / {binding['epg']}{tenant_str}")
    if len(config['epg_bindings']) > 10:
        print(f"    ... and {len(config['epg_bindings']) - 10} more")
    
    print("\n" + "=" * 70)


def edit_vpc_configuration(config, aep, all_profiles, all_link_levels, all_aeps, available_ports, session, apic_url):
    """
    Allow user to edit VPC interface configuration settings.
    Returns updated config, aep, and proceed flag.
    """
    while True:
        if 'custom_description' in config and config['custom_description']:
            port_description = config['custom_description']
        else:
            port_description = f"{config['hostname']} {config['work_order']}"
        
        print("\n  === EDIT VPC INTERFACE CONFIGURATION ===")
        print(f"  [1] Node Type:              Leaf (fixed)")
        print(f"  [2] Port Type:              Access (fixed)")
        print(f"  [3] Interface Type:         Ethernet (fixed)")
        print(f"  [4] Interface Aggregation:  VPC (fixed)")
        print(f"  [5] vPC Leaf Switch Pair:   {config['node1']}-{config['node2']} (from CSV)")
        print(f"  [6] Interface:              eth{config['interface']}")
        print(f"  [7] Interface Profile:      {config['interface_profile']}")
        print(f"  [8] Port Description:       {port_description}")
        print(f"\n  VPC Policy Group: {config['policy_group']}")
        print(f"  [9]  Policy Group Name:     {config['policy_group']}")
        print(f"  [10] AEP:                   {aep}")
        print(f"  [11] Link Level Policy:     {config['link_level']}")
        print(f"  [12] Flow Control Policy:   {config['flow_control']}")
        print("  " + "-" * 50)
        print("  [D] Done editing")
        print("  [C] Cancel deployment")
        
        choice = prompt_input("\n  Select option to edit: ").strip().upper()
        
        if choice == 'D':
            return config, aep, True
        
        elif choice == 'C':
            return config, aep, False
        
        elif choice in ['1', '2', '3', '4', '5']:
            print("    [INFO] This setting is fixed for this script")
        
        elif choice == '6':
            # Change interface
            print("\n    Available Interfaces (common to both switches):")
            print("    " + "-" * 50)
            for i, port in enumerate(available_ports, 1):
                current = " <-- current" if port['interface'] == config['interface'] else ""
                print(f"    [{i:>2}] eth{port['interface']:<10} {port['speed']}{current}")
            print("    " + "-" * 50)
            
            while True:
                int_choice = prompt_input("\n    Select interface (or 'B' to go back): ").strip().upper()
                if int_choice == 'B':
                    break
                try:
                    idx = int(int_choice) - 1
                    if 0 <= idx < len(available_ports):
                        config['interface'] = available_ports[idx]['interface']
                        # Update policy group name
                        config['policy_group'] = f"{config['hostname']}_e{config['interface'].split('/')[-1]}.vpc"
                        print(f"    [UPDATED] Interface: eth{config['interface']}")
                        print(f"    [UPDATED] Policy Group Name: {config['policy_group']}")
                        break
                except ValueError:
                    pass
                print("    [ERROR] Invalid selection")
        
        elif choice == '7':
            # Change interface profile
            print("\n    Available Interface Profiles:")
            print("    " + "-" * 50)
            for i, profile in enumerate(all_profiles, 1):
                current = " <-- current" if profile == config['interface_profile'] else ""
                print(f"    [{i:>3}] {profile}{current}")
            print("    " + "-" * 50)
            
            while True:
                prof_choice = prompt_input("\n    Select profile (or 'B' to go back): ").strip().upper()
                if prof_choice == 'B':
                    break
                try:
                    idx = int(prof_choice) - 1
                    if 0 <= idx < len(all_profiles):
                        config['interface_profile'] = all_profiles[idx]
                        print(f"    [UPDATED] Interface Profile: {all_profiles[idx]}")
                        break
                except ValueError:
                    pass
                print("    [ERROR] Invalid selection")
        
        elif choice == '8':
            # Edit port description
            print(f"\n    Current: {port_description}")
            print("    Enter new description (or press Enter to keep current):")
            new_desc = prompt_input("    New description: ").strip()
            if new_desc:
                config['custom_description'] = new_desc
                print(f"    [UPDATED] Port Description: {new_desc}")
            else:
                if 'custom_description' in config:
                    del config['custom_description']
                print("    [KEPT] Using default description")
        
        elif choice == '9':
            # Edit policy group name
            print(f"\n    Current: {config['policy_group']}")
            print("    Enter new name (or press Enter to keep current):")
            new_name = prompt_input("    New name: ").strip()
            if new_name:
                config['policy_group'] = new_name
                print(f"    [UPDATED] Policy Group Name: {new_name}")
        
        elif choice == '10':
            # Change AEP
            print("\n    Available AEPs:")
            print("    " + "-" * 50)
            for i, aep_option in enumerate(all_aeps, 1):
                current = " <-- current" if aep_option == aep else ""
                print(f"    [{i:>3}] {aep_option}{current}")
            print("    " + "-" * 50)
            
            while True:
                aep_choice = prompt_input("\n    Select AEP (or 'B' to go back): ").strip().upper()
                if aep_choice == 'B':
                    break
                try:
                    idx = int(aep_choice) - 1
                    if 0 <= idx < len(all_aeps):
                        aep = all_aeps[idx]
                        print(f"    [UPDATED] AEP: {all_aeps[idx]}")
                        break
                except ValueError:
                    pass
                print("    [ERROR] Invalid selection")
        
        elif choice == '11':
            # Change Link Level policy
            print("\n    Available Link Level Policies:")
            print("    " + "-" * 50)
            for i, ll in enumerate(all_link_levels, 1):
                current = " <-- current" if ll == config['link_level'] else ""
                print(f"    [{i:>3}] {ll}{current}")
            print("    " + "-" * 50)
            
            while True:
                ll_choice = prompt_input("\n    Select Link Level policy (or 'B' to go back): ").strip().upper()
                if ll_choice == 'B':
                    break
                try:
                    idx = int(ll_choice) - 1
                    if 0 <= idx < len(all_link_levels):
                        config['link_level'] = all_link_levels[idx]
                        print(f"    [UPDATED] Link Level Policy: {all_link_levels[idx]}")
                        break
                except ValueError:
                    pass
                print("    [ERROR] Invalid selection")
        
        elif choice == '12':
            # Toggle Flow Control
            print("\n    Flow Control Options:")
            print("    [1] default")
            print("    [2] FLOW-CONTROL-ON")
            print(f"    Current: {config['flow_control']}")
            
            fc_choice = prompt_input("\n    Select (1/2 or 'B' to go back): ").strip().upper()
            if fc_choice == '1':
                config['flow_control'] = 'default'
                print(f"    [UPDATED] Flow Control: default")
            elif fc_choice == '2':
                config['flow_control'] = 'FLOW-CONTROL-ON'
                print(f"    [UPDATED] Flow Control: FLOW-CONTROL-ON")
        
        else:
            print("    [ERROR] Invalid option")


# =============================================================================
# DEPLOYMENT FUNCTIONS
# =============================================================================

def load_vpc_csv(filename):
    """Load VPC deployment CSV file."""
    try:
        with open(filename, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            deployments = []
            for row in reader:
                normalized = {k.strip().upper(): v.strip() if v else "" for k, v in row.items() if k}
                deployments.append({
                    "hostname": normalized.get("HOSTNAME", ""),
                    "switch1": normalized.get("SWITCH1", ""),
                    "switch2": normalized.get("SWITCH2", ""),
                    "speed": normalized.get("SPEED", "").upper(),
                    "vlans": normalized.get("VLANS", ""),
                    "work_order": normalized.get("WORKORDER", "")
                })
            return deployments
    except FileNotFoundError:
        print(f"[ERROR] File not found: {filename}")
        return None
    except Exception as e:
        print(f"[ERROR] Failed to load CSV: {e}")
        return None


def deploy_vpc(session, apic_url, config, aep, dry_run=False):
    """Deploy a VPC configuration."""
    results = {"description": False, "policy_group": False, "port_selector": False, "bindings": []}
    
    # Use custom description if set, otherwise default
    if 'custom_description' in config and config['custom_description']:
        port_description = config['custom_description']
    else:
        port_description = f"{config['hostname']} {config['work_order']}"
    
    if dry_run:
        print("\n  [DRY-RUN] Would execute:")
        print(f"    1. Set port description on both nodes: {port_description}")
        print(f"    2. Create VPC Interface Policy Group: {config['policy_group']}")
        print(f"       - AEP: {aep}")
        print(f"       - CDP: {CDP_POLICY}")
        print(f"       - Link Level: {config['link_level']}")
        print(f"       - LLDP: {LLDP_POLICY}")
        print(f"       - Port Channel: {PORT_CHANNEL_POLICY}")
        print(f"       - MCP: {MCP_POLICY}")
        print(f"       - Storm Control: {STORM_CONTROL_POLICY}")
        print(f"       - Flow Control: {config['flow_control']}")
        iface2 = config.get('interface2', config['interface'])
        if config.get('asymmetric_vpc'):
            print(f"    3. Create Port Selectors (asymmetric VPC) on: {config['interface_profile']}")
            print(f"       - Port 1: {config['interface']} (node {config['node1']})")
            print(f"       - Port 2: {iface2} (node {config['node2']})")
        else:
            print(f"    3. Create Access Port Selector on: {config['interface_profile']}")
            print(f"       - Name: {config['policy_group']}")
            print(f"       - Interface IDs: {config['interface']}")
        print(f"       - Interface Policy Group: {config['policy_group']}")
        print(f"    4. Deploy {len(config['epg_bindings'])} static bindings")
        results["description"] = True
        results["policy_group"] = True
        results["port_selector"] = True
        for binding in config['epg_bindings']:
            results["bindings"].append({"vlan": binding['vlan'], "success": True})
        return results
    
    # 1. Set Port Description on both nodes (supports asymmetric VPC ports)
    iface1 = config['interface']
    iface2 = config.get('interface2', config['interface'])
    print(f"\n  [1/4] Setting port description on both nodes: {port_description}")
    
    success1, _ = set_port_description(session, apic_url, config['node1'], iface1, port_description)
    print(f"        Node {config['node1']} eth{iface1}: {'[SUCCESS]' if success1 else '[WARNING]'}")
    
    success2, _ = set_port_description(session, apic_url, config['node2'], iface2, port_description)
    print(f"        Node {config['node2']} eth{iface2}: {'[SUCCESS]' if success2 else '[WARNING]'}")
    
    results["description"] = success1 and success2
    
    # 2. Create or reuse VPC Policy Group
    if config.get('reuse_policy_group'):
        print(f"  [2/4] Using EXISTING VPC Policy Group: {config['policy_group']}")
        print(f"        [REUSE] Skipping creation")
        results["policy_group"] = True
    else:
        print(f"  [2/4] Creating VPC Interface Policy Group: {config['policy_group']}")
        print(f"        AEP: {aep}")
        print(f"        CDP: {CDP_POLICY}")
        print(f"        Link Level: {config['link_level']}")
        print(f"        LLDP: {LLDP_POLICY}")
        print(f"        Port Channel: {PORT_CHANNEL_POLICY}")
        print(f"        MCP: {MCP_POLICY}")
        print(f"        Storm Control: {STORM_CONTROL_POLICY}")
        print(f"        Flow Control: {config['flow_control']}")
        
        success, response = create_vpc_policy_group(session, apic_url, config['policy_group'], 
                                                     config['link_level'], config['flow_control'], aep)
        if success:
            print(f"        [SUCCESS]")
            results["policy_group"] = True
        else:
            print(f"        [FAILED] {response[:100]}")
            return results
    
    # 3. Create Access Port Selector(s) — supports asymmetric VPC ports
    iface1 = config['interface']
    iface2 = config.get('interface2', config['interface'])
    is_asymmetric = config.get('asymmetric_vpc', False)
    
    if is_asymmetric:
        sel1 = f"{config['hostname']}_e{iface1.split('/')[-1]}"
        sel2 = f"{config['hostname']}_e{iface2.split('/')[-1]}"
        print(f"  [3/4] Creating Port Selectors (asymmetric VPC):")
        print(f"        Interface Profile: {config['interface_profile']}")
        print(f"        Selector 1: {sel1} -> port {iface1} (node {config['node1']})")
        print(f"        Selector 2: {sel2} -> port {iface2} (node {config['node2']})")
        print(f"        Policy Group: {config['policy_group']}")
        
        ok1, r1 = create_port_selector(session, apic_url, config['interface_profile'],
                                        sel1, iface1, config['policy_group'])
        print(f"        Selector 1 (e{iface1.split('/')[-1]}): {'[SUCCESS]' if ok1 else '[FAILED] ' + r1[:80]}")
        
        ok2, r2 = create_port_selector(session, apic_url, config['interface_profile'],
                                        sel2, iface2, config['policy_group'])
        print(f"        Selector 2 (e{iface2.split('/')[-1]}): {'[SUCCESS]' if ok2 else '[FAILED] ' + r2[:80]}")
        
        if ok1 and ok2:
            results["port_selector"] = True
        else:
            print(f"        [FAILED] One or both selectors failed")
            return results
    else:
        print(f"  [3/4] Creating Access Port Selector: {config['policy_group']}")
        print(f"        Interface Profile: {config['interface_profile']}")
        print(f"        Interface IDs: {config['interface']}")
        print(f"        Interface Policy Group: {config['policy_group']}")
        
        success, response = create_port_selector(session, apic_url, config['interface_profile'], 
                                                 config['policy_group'], config['interface'], config['policy_group'])
        if success:
            print(f"        [SUCCESS]")
            results["port_selector"] = True
        else:
            print(f"        [FAILED] {response[:100]}")
            return results
    
    # 4. Deploy Static Bindings
    print(f"  [4/4] Deploying Static Bindings ({len(config['epg_bindings'])} VLANs, mode: regular (trunk))")
    vpc_path = f"topology/pod-{POD_ID}/protpaths-{config['node1']}-{config['node2']}/pathep-[{config['policy_group']}]"
    
    for binding in config['epg_bindings']:
        # Use tenant from binding if available, otherwise use config tenant
        binding_tenant = binding.get('tenant', config['tenant'])
        success, _ = deploy_static_binding(session, apic_url, binding_tenant, binding['app_profile'], 
                                           binding['epg'], binding['vlan'], "regular", vpc_path)
        results["bindings"].append({"vlan": binding['vlan'], "success": success})
        print(f"        VLAN {binding['vlan']}: {'OK' if success else 'FAILED'}")
    
    return results


# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    print("\n" + "=" * 70)
    print(" ACI BULK VPC DEPLOYMENT SCRIPT")
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
    deployments = load_vpc_csv(deployment_file)
    if not deployments:
        sys.exit(1)
    
    print(f"[INFO] Loaded {len(deployments)} VPC deployment(s)")
    
    # Select run mode
    print("\n" + "-" * 70)
    print(" RUN MODE")
    print("-" * 70)
    print("\n  [1] Normal - Deploy configurations")
    print("  [2] Dry-Run - Validate only, don't deploy")
    
    while True:
        sys.stdout.write("\nSelect mode (1/2): ")
        sys.stdout.flush()
        mode_choice = input().strip()
        if mode_choice in ['1', '2']:
            break
    dry_run = (mode_choice == '2')
    
    # Select Flow Control
    print("\n" + "-" * 70)
    print(" FLOW CONTROL")
    print("-" * 70)
    print("\n  [1] default")
    print("  [2] FLOW-CONTROL-ON")
    
    while True:
        sys.stdout.write("\nSelect (1/2) [default=1]: ")
        sys.stdout.flush()
        flow_choice = input().strip()
        if flow_choice in ["", "1"]:
            flow_control = "default"
            break
        elif flow_choice == "2":
            flow_control = "FLOW-CONTROL-ON"
            break
    
    # Policy Group Mode
    print("\n" + "-" * 70)
    print(" POLICY GROUP MODE")
    print("-" * 70)
    print("\n  [1] Create NEW policy group per deployment (default)")
    print("  [2] Reuse EXISTING policy group (query by link level)")
    
    while True:
        pg_mode_choice = prompt_input("\nSelect (1/2) [default=1]: ").strip()
        if pg_mode_choice in ["", "1", "2"]:
            break
    reuse_pg_mode = (pg_mode_choice == '2')
    
    # Policy Group Mode
    
    # Policy Group Mode
    
    # Policy Group Mode
    
    # Policy Group Mode
    
    # Policy Group Mode
    
    # Policy Group Mode
    
    # Policy Group Mode
    
    # Policy Group Mode
    
    # Policy Group Mode
    
    # When PG already exists with same name
    pg_exists_always_use = True
    if not reuse_pg_mode:
        print("\n" + "-" * 70)
        print(" WHEN POLICY GROUP NAME ALREADY EXISTS")
        print("-" * 70)
        print("\n  [1] Always use existing policy group (default)")
        print("  [2] Ask each time")
        pg_exists_choice = prompt_input("\nSelect (1/2) [default=1]: ").strip()
        pg_exists_always_use = (pg_exists_choice != '2')
    
    # EPG Binding Mode
    print("\n" + "-" * 70)
    print(" EPG BINDING MODE")
    print("-" * 70)
    print("\n  [1] Add - Deploy new EPG bindings (keep existing on each port)")
    print("  [2] Overwrite - Show existing EPGs, choose which to delete first")
    print("  [3] Overwrite ALL - Automatically remove ALL existing EPG bindings first")
    epg_mode_choice = prompt_input("\nSelect (1/2/3) [default=1]: ").strip()
    overwrite_mode = epg_mode_choice in ['2', '3']
    overwrite_interactive = (epg_mode_choice == '2')
    overwrite_auto = (epg_mode_choice == '3')
    if overwrite_mode:
        print("\n  [OVERWRITE] Existing EPG bindings will be wiped before deploying on every port")
    
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
    
    # Authenticate to needed environments
    sessions = {}
    needed_envs = set(detect_environment(d['switch1']) for d in deployments)
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
    import time as _token_time
    token_states = {}
    _credentials = {"username": username, "password": password}
    for _env in sessions:
        token_states[_env] = {"login_time": _token_time.time(), "lifetime": 300}
    
    # Run pre-flight checks
    global_settings = run_preflight_checks(sessions, deployments)
    if not global_settings:
        print("\n[CANCELLED]")
        sys.exit(0)
    
    # Process deployments
    print("\n" + "=" * 70)
    print(" PROCESSING VPC DEPLOYMENTS")
    print("=" * 70)
    
    successful, failed, skipped = 0, 0, 0
    
    for i, dep in enumerate(deployments, 1):
        print(f"\n{'='*70}")
        print(f" [{i}/{len(deployments)}] {dep['hostname']}")
        print(f"{'='*70}")
        
        # Get environment
        env = detect_environment(dep['switch1'])
        if not env or env not in sessions:
            print(f"  [SKIP] Environment not available")
            skipped += 1
            continue
        
        session = sessions[env]
        apic_url = APIC_URLS[env]
        aep = global_settings["aep"].get(env)
        
        # Refresh APIC token if aging (prevents 403 on long batch runs)
        if env in token_states:
            if not ensure_token_fresh(session, apic_url, token_states[env]):
                reauth_apic(session, apic_url, _credentials["username"],
                           _credentials["password"], token_states[env])
        
        # Extract node IDs
        node1 = extract_node_id(dep['switch1'])
        node2 = extract_node_id(dep['switch2'])
        
        if not node1 or not node2:
            print(f"  [SKIP] Could not extract node IDs")
            skipped += 1
            continue
        
        # Ensure consistent ordering
        if int(node1) > int(node2):
            node1, node2 = node2, node1
        
        pair_key = f"{node1}-{node2}"
        
        # Get interface profile from pre-flight
        interface_profile = global_settings["int_profiles"].get(env, {}).get(pair_key)
        
        if not interface_profile:
            print(f"  [SKIP] No interface profile configured for nodes {pair_key}")
            skipped += 1
            continue
        
        # Get link level from pre-flight
        link_level = global_settings["link_level"].get(env, {}).get(dep['speed'])
        if not link_level:
            print(f"  [SKIP] No Link Level policy configured for speed '{dep['speed']}'")
            skipped += 1
            continue
        
        tenants_str = ", ".join(TENANTS[env])
        print(f"\n  Environment: {env} (Tenants: {tenants_str})")
        print(f"  vPC Leaf Switch Pair: {pair_key}")
        print(f"  Work Order: {dep['work_order']}")
        
        # Get validated available ports on both switches
        print(f"\n  === PORT VALIDATION ===")
        print(f"  Querying all ports and checking status...")
        print(f"    Criteria: discovery usage, no description, no policy group, no EPG")
        print(f"    [AVAIL] = passes all checks  |  [IN-USE] = has existing config")
        
        ports1 = get_all_ports_with_status(session, apic_url, node1, POD_ID)
        ports2 = get_all_ports_with_status(session, apic_url, node2, POD_ID)
        
        avail1 = sum(1 for p in ports1 if p['valid'])
        avail2 = sum(1 for p in ports2 if p['valid'])
        print(f"\n    Node {node1}: {len(ports1)} total ({avail1} available)")
        print(f"    Node {node2}: {len(ports2)} total ({avail2} available)")
        
        # Find common ports (both available and in-use)
        common_ports = find_common_ports_with_status(ports1, ports2)
        avail_common = sum(1 for p in common_ports if p['valid'])
        print(f"    Common: {len(common_ports)} total ({avail_common} available on both)")
        
        # Port selection mode
        print(f"\n  Port Selection Mode:")
        print(f"    [1] Same port on both switches (common ports)")
        print(f"    [2] Different port on each switch (independent)")
        port_mode = prompt_input("\n  Select (1/2) [default=1]: ").strip()
        
        asymmetric_vpc = False
        selected_port2 = None
        
        if port_mode == '2':
            # Independent selection — different port per switch
            selected_port, selected_port2 = display_vpc_independent_port_selection(
                ports1, ports2, node1, node2
            )
            if selected_port is None or selected_port2 is None:
                print(f"  [SKIPPED by user]")
                skipped += 1
                continue
            asymmetric_vpc = (selected_port['interface'] != selected_port2['interface'])
        else:
            # Same port mode — use common ports
            if not common_ports:
                print(f"  [SKIP] No common ports found on both switches")
                print(f"  [TIP] Try mode 2 for independent port selection")
                skipped += 1
                continue
            selected_port = display_vpc_port_selection(common_ports, node1, node2)
        if selected_port == "SKIP":
            skipped += 1
            continue
        elif selected_port == "QUIT":
            break
        
        interface = selected_port['interface']
        print(f"\n  Selected: {selected_port['port']} -> {interface}")
        
        # Get EPGs - search across ALL tenants for this environment
        vlans = parse_vlans(dep['vlans'])
        tenants_list = TENANTS[env]
        print(f"\n  Processing {len(vlans)} VLANs (searching {len(tenants_list)} tenants)...")
        
        epg_bindings = []
        selected_ap_tenant = None  # Tuple of (app_profile, tenant) or just app_profile
        
        for vlan in vlans:
            # Search all tenants
            results = get_epg_app_profile_all_tenants(session, apic_url, tenants_list, vlan)
            if not results:
                print(f"    [WARNING] No EPG for VLAN {vlan}")
                continue
            
            if len(results) > 1 and not selected_ap_tenant:
                # Multiple results - user needs to select
                selected_ap_tenant = display_app_profile_choice(results, vlan)
                if isinstance(selected_ap_tenant, tuple):
                    ap, tn = selected_ap_tenant
                    results = [(a, e, t) for a, e, t in results if a == ap and t == tn]
                else:
                    results = [(a, e, t) for a, e, t in results if a == selected_ap_tenant]
            elif selected_ap_tenant:
                # Filter by previous selection
                if isinstance(selected_ap_tenant, tuple):
                    ap, tn = selected_ap_tenant
                    results = [(a, e, t) for a, e, t in results if a == ap and t == tn]
                else:
                    results = [(a, e, t) for a, e, t in results if a == selected_ap_tenant]
            
            if results:
                app_profile, epg_name, tenant_found = results[0]
                epg_bindings.append({
                    "vlan": vlan, 
                    "app_profile": app_profile, 
                    "epg": epg_name,
                    "tenant": tenant_found
                })
        
        if not epg_bindings:
            print(f"  [SKIP] No valid EPG bindings")
            skipped += 1
            continue
        
        # Use the tenant from the first EPG binding for the config
        deployment_tenant = epg_bindings[0].get("tenant", tenants_list[0])
        
        # Check policy group doesn't already exist
        policy_group_name = f"{dep['hostname']}_e{interface.split('/')[-1]}.vpc"
        if check_vpc_policy_group_exists(session, apic_url, policy_group_name):
            print(f"\n  [WARNING] Policy group '{policy_group_name}' already exists")
            use_existing = prompt_input("  Use existing policy group? (yes/no): ").strip().lower()
            if use_existing not in ['yes', 'y']:
                print(f"  [SKIP] Policy group already exists")
                skipped += 1
                continue
        
        # Build config
        config = {
            "environment": env, 
            "tenant": deployment_tenant, 
            "hostname": dep['hostname'],
            "work_order": dep['work_order'],
            "node1": node1, 
            "node2": node2, 
            "interface": interface,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,
            "policy_group": policy_group_name,
            "link_level": link_level,
            "flow_control": flow_control, 
            "interface_profile": interface_profile,
            "epg_bindings": epg_bindings
        }
        
        # Get all profiles and policies for edit functionality
        all_profiles = get_interface_profiles(session, apic_url)
        all_link_levels = get_link_level_policies(session, apic_url)
        all_aeps = get_all_aeps(session, apic_url)
        
        # Preview and confirm loop
        while True:
            # Preview
            display_deployment_preview(config, aep)
            
            # Confirm with edit option
            print("\n  [Y] Yes - Deploy")
            print("  [N] No - Skip this deployment")
            print("  [E] Edit - Modify interface configuration")
            print("  [Q] Quit - Exit script")
            
            confirm = prompt_input("\n  Choice: ").strip().upper()
            
            if confirm == 'Q':
                print("\n[INFO] Quitting...")
                skipped += 1
                break
            
            elif confirm == 'N':
                print("  [SKIPPED by user]")
                skipped += 1
                break
            
            elif confirm == 'E':
                # Full edit mode
                config, aep, proceed = edit_vpc_configuration(
                    config, aep, all_profiles, all_link_levels, all_aeps, 
                    common_ports, session, apic_url
                )
                if not proceed:
                    print("  [CANCELLED]")
                    skipped += 1
                    break
                # Loop back to show updated preview
                continue
            
            elif confirm in ['Y', 'YES']:
                # Deploy
                print("\n  Deploying..." if not dry_run else "\n  Dry-run...")
                
                # Full cleanup if overriding in-use port(s)
                need_clean1 = not selected_port.get('valid', True) if selected_port else False
                need_clean2 = not selected_port2.get('valid', True) if selected_port2 else False
                
                if not dry_run and (need_clean1 or need_clean2):
                    print("\n  [CLEANUP] Wiping existing port configuration...")
                    c_iface1 = config['interface']
                    c_iface2 = config.get('interface2', c_iface1)
                    
                    if need_clean1:
                        print(f"    Cleaning node {config['node1']} port {c_iface1}...")
                        cleanup_port_for_redeployment(
                            session, apic_url, config['node1'], c_iface1,
                            config['interface_profile'], POD_ID
                        )
                    if need_clean2:
                        print(f"    Cleaning node {config['node2']} port {c_iface2}...")
                        cleanup_port_for_redeployment(
                            session, apic_url, config['node2'], c_iface2,
                            config['interface_profile'], POD_ID
                        )
                    # Also clean VPC protpaths bindings
                    cleanup_vpc_port_for_redeployment(
                        session, apic_url, config['node1'], config['node2'],
                        c_iface1, config['interface_profile'], POD_ID
                    )
                    print(f"  [CLEANUP] Done\n")
                

                
                # Overwrite: delete ALL existing EPG bindings before Step 4
                # Overwrite: delete existing EPG bindings before deploying
                if overwrite_mode and not dry_run:
                    import time as _time
                    vpc_ow_path = f"topology/pod-{POD_ID}/protpaths-{config['node1']}-{config['node2']}/pathep-[{config['policy_group']}]"
                    print(f"\n  [OVERWRITE] Querying existing EPG bindings on VPC path...")
                    print(f"  Path: {vpc_ow_path}")
                    
                    # Use merged dual-strategy query from port_utils
                    ow_existing = query_all_bindings_on_port(
                        session, apic_url, config['node1'], config['interface'],
                        POD_ID, tenants=TENANTS.get(env, []),
                        path_type="vpc", node2=config['node2'],
                        pg_name=config['policy_group']
                    )
                    
                    ow_deleted = 0
                    if ow_existing:
                        if overwrite_interactive:
                            # Interactive: show list, let user choose
                            print(f"\n  Existing EPG bindings ({len(ow_existing)}):\n")
                            for oi, ob in enumerate(ow_existing, 1):
                                print(f"    [{oi}] VLAN {ob.get('vlan','?')} — {ob.get('epg','?')} ({ob.get('tenant','?')})")
                            sel = prompt_input(f"\n  Delete which? (numbers, 'all', 'none') [default=all]: ").strip().lower()
                            if sel in ['', 'all', 'a']:
                                to_del = ow_existing[:]
                            elif sel in ['none', 'n', '0']:
                                to_del = []
                            else:
                                sel_idx = set()
                                for part in sel.split(','):
                                    part = part.strip()
                                    if '-' in part:
                                        rp = part.split('-')
                                        for ri in range(int(rp[0]), int(rp[1]) + 1):
                                            if 1 <= ri <= len(ow_existing): sel_idx.add(ri)
                                    elif part.isdigit():
                                        ri = int(part)
                                        if 1 <= ri <= len(ow_existing): sel_idx.add(ri)
                                to_del = [ow_existing[i-1] for i in sorted(sel_idx)]
                            print(f"  -> Deleting {len(to_del)} of {len(ow_existing)} binding(s)")
                        else:
                            # Auto mode or simple overwrite
                            to_del = ow_existing[:]
                            print(f"  [AUTO] Deleting all {len(to_del)} existing binding(s)")
                        
                        for ob in to_del:
                            try:
                                del_resp = session.delete(
                                    f"{apic_url}/api/mo/{ob['dn']}.json",
                                    verify=False, timeout=30)
                                ok = del_resp.status_code == 200
                            except Exception:
                                ok = False
                            status = '[DELETED]' if ok else '[FAIL]'
                            print(f"    {status} VLAN {ob.get('vlan','?')} — {ob.get('epg','?')} ({ob.get('tenant','?')})")
                            if ok:
                                ow_deleted += 1
                        
                        # Verify
                        _time.sleep(1)
                        v_bindings = query_all_bindings_on_port(
                            session, apic_url, config['node1'], config['interface'],
                            POD_ID, tenants=TENANTS.get(env, []),
                            path_type="vpc", node2=config['node2'],
                            pg_name=config['policy_group'], verbose=False
                        )
                        if not v_bindings:
                            print(f"  [VERIFIED] VPC path is clean — 0 bindings remain")
                        else:
                            print(f"  [WARNING] {len(v_bindings)} binding(s) still remain")
                        print(f"  [OVERWRITE] Cleanup complete — {ow_deleted} removed")
                    else:
                        print(f"  [OVERWRITE] No existing bindings found (clean VPC path)")
                
                results = deploy_vpc(session, apic_url, config, aep, dry_run)
                
                ok_bindings = sum(1 for b in results['bindings'] if b['success'])
                print(f"\n  Complete: Desc={'OK' if results['description'] else 'WARN'}, "
                      f"PolicyGrp={'OK' if results['policy_group'] else 'FAIL'}, "
                      f"Selector={'OK' if results['port_selector'] else 'FAIL'}, "
                      f"Bindings={ok_bindings}/{len(results['bindings'])}")
                
                if results['policy_group'] and results['port_selector']:
                    successful += 1
                else:
                    failed += 1
                break
            
            else:
                print("  [ERROR] Invalid choice")
                continue
        
        # Check if user quit
        if confirm == 'Q':
            break
    
    # Summary
    print("\n" + "=" * 70)
    print(" COMPLETE")
    print("=" * 70)
    print(f"\n  Total: {len(deployments)}, Success: {successful}, Failed: {failed}, Skipped: {skipped}")
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
