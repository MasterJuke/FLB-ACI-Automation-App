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

def prompt_input(prompt_text):
    """Print prompt and get input - ensures prompt is visible in web UI."""
    print(prompt_text, end="", flush=True)
    return input()


def detect_environment(switch_name):
    """Detect data center from switch name. Priority: NSM > SDC > ACC"""
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
            except ValueError:
                pass
        else:
            try:
                vlans.append(int(part))
            except ValueError:
                pass
    return sorted(list(set(vlans)))


def parse_interface(interface_string):
    """Parse interface string to standard format."""
    if not interface_string:
        return None
    cleaned = interface_string.strip().lower().replace("ethernet", "").replace("eth", "").replace("e", "").strip()
    if "/" not in cleaned:
        try:
            return f"1/{int(cleaned)}"
        except ValueError:
            return None
    parts = cleaned.split("/")
    if len(parts) != 2:
        return None
    try:
        return f"{int(parts[0])}/{int(parts[1])}"
    except ValueError:
        return None


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


def validate_single_port(session, apic_url, node_id, port):
    """
    Validate a single port for policy group and EPG bindings.
    Used by ThreadPoolExecutor for parallel validation.
    """
    port_num = port['interface'].split('/')[-1]
    
    # Check for policy group (port selector) on this port
    try:
        url = f"{apic_url}/api/class/infraPortBlk.json?query-target-filter=and(eq(infraPortBlk.fromPort,\"{port_num}\"),eq(infraPortBlk.toPort,\"{port_num}\"))"
        response = session.get(url, verify=False, timeout=15)
        if response.status_code == 200:
            data = response.json().get("imdata", [])
            for item in data:
                dn = item.get("infraPortBlk", {}).get("attributes", {}).get("dn", "")
                if node_id in dn:
                    port["valid"] = False
                    port["issues"].append("Policy group assigned")
                    return port
    except:
        pass
    
    # Check for EPG bindings
    try:
        path_dn = f"topology/pod-{POD_ID}/paths-{node_id}/pathep-[{port['port']}]"
        url = f"{apic_url}/api/class/fvRsPathAtt.json?query-target-filter=eq(fvRsPathAtt.tDn,\"{path_dn}\")"
        response = session.get(url, verify=False, timeout=15)
        if response.status_code == 200:
            data = response.json().get("imdata", [])
            if data:
                port["valid"] = False
                port["issues"].append("EPG deployed")
                return port
    except:
        pass
    
    return port


def get_validated_available_ports(session, apic_url, node_id):
    """
    Get available ports with full validation using parallel queries.
    Returns list of ports that pass all 4 checks:
    1. Usage = 'discovery'
    2. No description
    3. No policy group assigned
    4. No EPG deployed
    
    Uses ThreadPoolExecutor for faster validation (~5-10x speedup).
    """
    # First get all physical interfaces
    url = f"{apic_url}/api/class/topology/pod-{POD_ID}/node-{node_id}/l1PhysIf.json"
    
    try:
        response = session.get(url, verify=False, timeout=60)
        if response.status_code != 200:
            return []
        
        ports = []
        for item in response.json().get("imdata", []):
            attrs = item.get("l1PhysIf", {}).get("attributes", {})
            
            usage = attrs.get("usage", "").lower()
            description = attrs.get("descr", "").strip()
            admin_state = attrs.get("adminSt", "")
            
            # Extract port from DN
            dn = attrs.get("dn", "")
            port_match = re.search(r'phys-\[(.+?)\]', dn)
            if not port_match:
                continue
            
            port = port_match.group(1)
            interface = parse_interface(port)
            if not interface:
                continue
            
            # Initial filter: discovery usage, no description, admin up
            if usage == "discovery" and not description and admin_state == "up":
                ports.append({
                    "port": port,
                    "interface": interface,
                    "speed": attrs.get("speed", "inherit"),
                    "usage": usage,
                    "description": description,
                    "valid": True,
                    "issues": []
                })
        
        # Sort ports
        ports.sort(key=lambda x: (
            int(re.search(r'eth(\d+)/', x['port']).group(1)) if re.search(r'eth(\d+)/', x['port']) else 0,
            int(re.search(r'/(\d+)$', x['port']).group(1)) if re.search(r'/(\d+)$', x['port']) else 0
        ))
        
        # Validate ports in parallel using ThreadPoolExecutor
        # Max 10 workers to avoid overwhelming the APIC
        validated_ports = []
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            # Submit all validation tasks
            future_to_port = {
                executor.submit(validate_single_port, session, apic_url, node_id, port.copy()): port 
                for port in ports
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_port):
                try:
                    result = future.result()
                    if result["valid"]:
                        validated_ports.append(result)
                except Exception as e:
                    pass
        
        # Re-sort after parallel processing
        validated_ports.sort(key=lambda x: (
            int(re.search(r'eth(\d+)/', x['port']).group(1)) if re.search(r'eth(\d+)/', x['port']) else 0,
            int(re.search(r'/(\d+)$', x['port']).group(1)) if re.search(r'/(\d+)$', x['port']) else 0
        ))
        
        return validated_ports
    
    except Exception as e:
        print(f"    [ERROR] Failed to query ports: {e}")
        return []


def get_epg_app_profile(session, apic_url, tenant, vlan_id):
    """Find which Application Profile(s) contain the EPG for a given VLAN."""
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
                results.append((ap_match.group(1), epg_name))
        return results
    except:
        return []


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

def display_validated_ports(ports, node_label):
    """Display validated available ports for VPC."""
    if not ports:
        print(f"\n  [WARNING] No validated available ports on {node_label}")
        return None
    
    print(f"\n  Validated available ports on {node_label}:")
    print("  " + "-" * 60)
    print(f"  {'#':>3}  {'Port':<15} {'Speed':<10} Status")
    print("  " + "-" * 60)
    
    for i, port in enumerate(ports, 1):
        status = "OK" if port['valid'] else f"INVALID: {', '.join(port['issues'])}"
        print(f"  [{i:>2}] {port['port']:<15} {port['speed']:<10} {status}")
    
    print("  " + "-" * 60)
    print("  [S] Skip this deployment")
    print("  [Q] Quit script")
    
    while True:
        choice = prompt_input("\n  Select port number: ").strip().upper()
        if choice == 'S':
            return "SKIP"
        elif choice == 'Q':
            return "QUIT"
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(ports):
                return ports[idx]
        except ValueError:
            pass
        print("  [ERROR] Invalid selection")


def display_app_profile_choice(options, vlan_id):
    """Display Application Profile options and let user select."""
    print(f"\n  VLAN {vlan_id} exists in multiple Application Profiles:")
    print("  " + "-" * 50)
    for i, (app_profile, epg_name) in enumerate(options, 1):
        print(f"  [{i}] {app_profile} -> {epg_name}")
    print("  " + "-" * 50)
    
    while True:
        choice = prompt_input("\n  Select (applies to ALL VLANs in this deployment): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        except ValueError:
            pass
        print("  [ERROR] Invalid selection")


def find_common_validated_ports(ports1, ports2):
    """Find ports that are validated and available on both switches."""
    # Create dict by interface for easy lookup
    ports1_dict = {p['interface']: p for p in ports1 if p.get('valid', True)}
    ports2_dict = {p['interface']: p for p in ports2 if p.get('valid', True)}
    
    common_interfaces = set(ports1_dict.keys()) & set(ports2_dict.keys())
    
    # Return ports from ports1 that are in common (they have same interface on both)
    common_ports = []
    for interface in common_interfaces:
        port = ports1_dict[interface].copy()
        common_ports.append(port)
    
    # Sort by port number
    common_ports.sort(key=lambda x: (
        int(re.search(r'eth(\d+)/', x['port']).group(1)) if re.search(r'eth(\d+)/', x['port']) else 0,
        int(re.search(r'/(\d+)$', x['port']).group(1)) if re.search(r'/(\d+)$', x['port']) else 0
    ))
    
    return common_ports


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
        print(f"    VLAN {binding['vlan']:>4} -> {binding['epg']} ({binding['app_profile']})")
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
    
    # 1. Set Port Description on both nodes
    print(f"\n  [1/4] Setting port description on both nodes: {port_description}")
    interface_eth = f"eth{config['interface']}"
    
    success1, _ = set_port_description(session, apic_url, config['node1'], config['interface'], port_description)
    print(f"        Node {config['node1']} {interface_eth}: {'[SUCCESS]' if success1 else '[WARNING]'}")
    
    success2, _ = set_port_description(session, apic_url, config['node2'], config['interface'], port_description)
    print(f"        Node {config['node2']} {interface_eth}: {'[SUCCESS]' if success2 else '[WARNING]'}")
    
    results["description"] = success1 and success2
    
    # 2. Create VPC Policy Group
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
    
    # 3. Create Access Port Selector
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
        success, _ = deploy_static_binding(session, apic_url, config['tenant'], binding['app_profile'], 
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
    print("Press Enter to use default, or enter filename: ", end="", flush=True)
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
        print("\nSelect mode (1/2): ", end="", flush=True)
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
        print("\nSelect (1/2) [default=1]: ", end="", flush=True)
        flow_choice = input().strip()
        if flow_choice in ["", "1"]:
            flow_control = "default"
            break
        elif flow_choice == "2":
            flow_control = "FLOW-CONTROL-ON"
            break
    
    # Get credentials
    print("\n" + "-" * 70)
    print(" AUTHENTICATION")
    print("-" * 70)
    print("\nUsername: ", end="", flush=True)
    username = input().strip()
    print("Password: ", end="", flush=True)
    # Check if running in web UI mode (PTY) - getpass doesn't work with PTY
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
        tenant = TENANTS[env]
        aep = global_settings["aep"].get(env)
        
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
        
        print(f"\n  Environment: {env} ({tenant})")
        print(f"  vPC Leaf Switch Pair: {pair_key}")
        print(f"  Work Order: {dep['work_order']}")
        
        # Get validated available ports on both switches
        print(f"\n  === PORT VALIDATION ===")
        print(f"  Querying and validating ports (checking 4 criteria)...")
        print(f"    1. Usage = 'discovery'")
        print(f"    2. No description")
        print(f"    3. No policy group assigned")
        print(f"    4. No EPG deployed")
        
        ports1 = get_validated_available_ports(session, apic_url, node1)
        ports2 = get_validated_available_ports(session, apic_url, node2)
        
        print(f"\n    Node {node1}: {len(ports1)} validated available")
        print(f"    Node {node2}: {len(ports2)} validated available")
        
        # Find common validated ports
        common_ports = find_common_validated_ports(ports1, ports2)
        print(f"    Common: {len(common_ports)} validated available on both")
        
        if not common_ports:
            print(f"  [SKIP] No validated matching ports on both switches")
            skipped += 1
            continue
        
        # Select port
        selected_port = display_validated_ports(common_ports, f"nodes {node1} & {node2}")
        if selected_port == "SKIP":
            skipped += 1
            continue
        elif selected_port == "QUIT":
            break
        
        interface = selected_port['interface']
        print(f"\n  Selected: {selected_port['port']} -> {interface}")
        
        # Get EPGs
        vlans = parse_vlans(dep['vlans'])
        print(f"\n  Processing {len(vlans)} VLANs...")
        
        epg_bindings = []
        selected_app_profile = None
        
        for vlan in vlans:
            results = get_epg_app_profile(session, apic_url, tenant, vlan)
            if not results:
                print(f"    [WARNING] No EPG for VLAN {vlan}")
                continue
            if len(results) > 1 and not selected_app_profile:
                selected_app_profile = display_app_profile_choice(results, vlan)
                results = [(ap, epg) for ap, epg in results if ap == selected_app_profile]
            elif selected_app_profile:
                results = [(ap, epg) for ap, epg in results if ap == selected_app_profile]
            if results:
                epg_bindings.append({"vlan": vlan, "app_profile": results[0][0], "epg": results[0][1]})
        
        if not epg_bindings:
            print(f"  [SKIP] No valid EPG bindings")
            skipped += 1
            continue
        
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
            "tenant": tenant, 
            "hostname": dep['hostname'],
            "work_order": dep['work_order'],
            "node1": node1, 
            "node2": node2, 
            "interface": interface,
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
