#!/usr/bin/env python3
"""
ACI Bulk Individual Port Deployment Script
============================================
Reads a deployment CSV and provisions individual (non-VPC) interfaces in ACI.

Complete Workflow:
==================

1. PORT VALIDATION
   - Check if policy group already assigned
   - Check if EPG already deployed to port
   - Verify usage = "discovery"
   - Verify no description set

2. INTERFACE CONFIGURATION (Fabric > Access Policies > Interface Configuration)
   - Node Type: Leaf (always)
   - Port Type: Access (always)
   - Interface Type: Ethernet | Fibre Channel (from Media column)
   - Interface Aggregation: Individual (always)
   - Node ID: from CSV
   - Interface: selected from validated available ports
   - Leaf Access Port Policy Group: based on speed (prompt to reuse for batch)

3. DEPLOY TO EPG (Static Binding)
   - Port Type: Port
   - Node: from CSV
   - Path: selected interface
   - VLAN: matches EPG (V0032 = VLAN 32)
   - Deployment Immediacy: Immediate
   - Primary VLAN for Micro-Seg: blank
   - Mode: Trunk | Access (Untagged) from CSV
   - PTP: Disabled

Input CSV Format:
Hostname,Switch,Type,Speed,VLANS,WorkOrder
MEDHVIOP173_MGMT,EDCLEAFNSM2163,ACCESS,1G,2958,WO123456
MEDHVIOP173_Clients,EDCLEAFNSM2163,TRUNK,25G,2704-2719,WO123456

Type: ACCESS (untagged) | TRUNK (tagged)

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
DEPLOYMENT_FILE = "individual_port_deployments.csv"

# Pod ID
POD_ID = "1"

# =============================================================================
# INTERFACE CONFIGURATION DEFAULTS (Always these values)
# =============================================================================

NODE_TYPE = "leaf"           # Always leaf
PORT_TYPE = "access"         # Always access
INTERFACE_AGGREGATION = "individual"  # Always individual for this script

# =============================================================================
# STATIC BINDING DEFAULTS
# =============================================================================

DEPLOYMENT_IMMEDIACY = "immediate"   # Always immediate
PRIMARY_VLAN_MICROSEG = ""           # Always blank
PTP_MODE = "disabled"                # Always disabled (not sent in API, just FYI)

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

# =============================================================================
# SPEED TO LINK LEVEL MAPPING (for searching similar policies)
# =============================================================================

SPEED_MAPPING = {
    "25G": "25GB",
    "1G": "1g",
    "10G": "10g",
    "40G": "40g",
    "100G": "100g"
}

# =============================================================================
# MEDIA TYPE MAPPING (CSV value -> ACI Interface Type)
# =============================================================================

MEDIA_MAPPING = {
    "COPPER": "ethernet",
    "FIBER": "fc",           # Fibre Channel
    "FIBRE": "fc",
    "ETHERNET": "ethernet",
    "FC": "fc"
}

# =============================================================================
# MODE MAPPING (CSV Type -> ACI mode)
# =============================================================================

MODE_MAPPING = {
    "TRUNK": "regular",       # Trunk (tagged)
    "ACCESS": "untagged"      # Access (untagged)
}

# =============================================================================
# SPEED MAPPING (CSV Speed -> suggested policy groups)
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
# API FUNCTIONS - AUTHENTICATION
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


# =============================================================================
# API FUNCTIONS - PRE-FLIGHT CHECKS
# =============================================================================

def check_aep_exists(session, apic_url, aep_name):
    """Check if an AEP exists."""
    try:
        response = session.get(f"{apic_url}/api/node/mo/uni/infra/attentp-{aep_name}.json", verify=False, timeout=30)
        return response.status_code == 200 and bool(response.json().get("imdata"))
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


def find_interface_profile_for_node(profiles, node_id):
    """
    Find interface profile containing the node ID.
    e.g., node_id '2163' -> 'EDCLEAFNSM2163_IntProf'
    """
    for profile in profiles:
        if node_id in profile:
            return profile
    return None


def check_interface_profile_exists(session, apic_url, profile_name):
    """Check if interface profile exists (case-insensitive)."""
    profiles = get_interface_profiles(session, apic_url)
    for p in profiles:
        if p.lower() == profile_name.lower():
            return p
    return None


# =============================================================================
# API FUNCTIONS - PORT VALIDATION
# =============================================================================

def get_port_details(session, apic_url, node_id, interface):
    """
    Get detailed port information for validation.
    Returns dict with: usage, description, policy_group, has_epg_bindings
    """
    port_info = {
        "usage": None,
        "description": None,
        "policy_group": None,
        "has_epg_bindings": False,
        "valid": False,
        "issues": []
    }
    
    # Format interface for query
    if "/" in interface:
        eth_interface = f"eth{interface}"
    else:
        eth_interface = f"eth1/{interface}"
    
    # Query physical interface
    url = f"{apic_url}/api/class/topology/pod-{POD_ID}/node-{node_id}/l1PhysIf.json?query-target-filter=wcard(l1PhysIf.dn,\"phys-[{eth_interface}]\")"
    
    try:
        response = session.get(url, verify=False, timeout=30)
        if response.status_code == 200:
            data = response.json().get("imdata", [])
            if data:
                attrs = data[0].get("l1PhysIf", {}).get("attributes", {})
                port_info["usage"] = attrs.get("usage", "").lower()
                port_info["description"] = attrs.get("descr", "").strip()
    except:
        pass
    
    # Check for existing policy group assignment
    # Query infraHPortS (port selectors) that reference this port
    url = f"{apic_url}/api/class/infraPortBlk.json?query-target-filter=and(eq(infraPortBlk.fromPort,\"{interface.split('/')[-1]}\"),eq(infraPortBlk.toPort,\"{interface.split('/')[-1]}\"))"
    
    try:
        response = session.get(url, verify=False, timeout=30)
        if response.status_code == 200:
            data = response.json().get("imdata", [])
            # Check if any of these are on our node's interface profile
            for item in data:
                dn = item.get("infraPortBlk", {}).get("attributes", {}).get("dn", "")
                if node_id in dn:
                    port_info["policy_group"] = "exists"  # Found a port selector for this port
                    break
    except:
        pass
    
    # Check for existing EPG bindings on this port
    path_dn = f"topology/pod-{POD_ID}/paths-{node_id}/pathep-[{eth_interface}]"
    url = f"{apic_url}/api/class/fvRsPathAtt.json?query-target-filter=eq(fvRsPathAtt.tDn,\"{path_dn}\")"
    
    try:
        response = session.get(url, verify=False, timeout=30)
        if response.status_code == 200:
            data = response.json().get("imdata", [])
            if data:
                port_info["has_epg_bindings"] = True
    except:
        pass
    
    # Validate
    if port_info["usage"] != "discovery":
        port_info["issues"].append(f"Usage is '{port_info['usage']}' (expected 'discovery')")
    if port_info["description"]:
        port_info["issues"].append(f"Has description: '{port_info['description']}'")
    if port_info["policy_group"]:
        port_info["issues"].append("Policy group already assigned")
    if port_info["has_epg_bindings"]:
        port_info["issues"].append("EPG already deployed to this port")
    
    port_info["valid"] = len(port_info["issues"]) == 0
    
    return port_info


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


# =============================================================================
# API FUNCTIONS - LEAF ACCESS PORT POLICY GROUPS
# =============================================================================

def get_leaf_access_port_policy_groups(session, apic_url):
    """Get all Leaf Access Port Policy Groups."""
    try:
        response = session.get(f"{apic_url}/api/class/infraAccPortGrp.json", verify=False, timeout=30)
        if response.status_code == 200:
            groups = []
            for item in response.json().get("imdata", []):
                attrs = item.get("infraAccPortGrp", {}).get("attributes", {})
                groups.append(attrs.get("name", ""))
            return sorted(groups)
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


def check_policy_group_exists(session, apic_url, name):
    """Check if a Leaf Access Port Policy Group already exists."""
    try:
        response = session.get(f"{apic_url}/api/node/mo/uni/infra/funcprof/accportgrp-{name}.json", verify=False, timeout=30)
        return response.status_code == 200 and bool(response.json().get("imdata"))
    except:
        return False


def create_leaf_access_port_policy_group(session, apic_url, name, aep, link_level):
    """
    Create a Leaf Access Port Policy Group.
    
    Settings:
    - Name: {Hostname}_e{InterfaceID}
    - Description: (empty)
    - AEP: from pre-flight selection
    - CDP: cdp-disabled
    - Link Level: from pre-flight selection (by speed)
    - LLDP: lldp-enabled
    """
    payload = {
        "infraAccPortGrp": {
            "attributes": {
                "name": name,
                "descr": ""
            },
            "children": [
                {"infraRsAttEntP": {"attributes": {"tDn": f"uni/infra/attentp-{aep}"}}},
                {"infraRsCdpIfPol": {"attributes": {"tnCdpIfPolName": CDP_POLICY}}},
                {"infraRsHIfPol": {"attributes": {"tnFabricHIfPolName": link_level}}},
                {"infraRsLldpIfPol": {"attributes": {"tnLldpIfPolName": LLDP_POLICY}}}
            ]
        }
    }
    try:
        response = session.post(f"{apic_url}/api/node/mo/uni/infra/funcprof/accportgrp-{name}.json", 
                               json=payload, verify=False, timeout=30)
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


def get_policy_group_details(session, apic_url, policy_group_name):
    """
    Query a Leaf Access Port Policy Group and return its configured policies.
    Returns dict with policy names for each type.
    """
    details = {
        "aep": None,
        "cdp": None,
        "lldp": None,
        "link_level": None,
        "mcp": None,
        "storm_control": None,
        "flow_control": None
    }
    
    # Query the policy group with children
    url = f"{apic_url}/api/node/mo/uni/infra/funcprof/accportgrp-{policy_group_name}.json?query-target=children"
    
    try:
        response = session.get(url, verify=False, timeout=30)
        if response.status_code != 200:
            return details
        
        data = response.json().get("imdata", [])
        
        for item in data:
            # AEP
            if "infraRsAttEntP" in item:
                tdn = item["infraRsAttEntP"]["attributes"].get("tDn", "")
                match = re.search(r'attentp-(.+)$', tdn)
                if match:
                    details["aep"] = match.group(1)
            
            # CDP Policy
            elif "infraRsCdpIfPol" in item:
                details["cdp"] = item["infraRsCdpIfPol"]["attributes"].get("tnCdpIfPolName", "")
            
            # LLDP Policy
            elif "infraRsLldpIfPol" in item:
                details["lldp"] = item["infraRsLldpIfPol"]["attributes"].get("tnLldpIfPolName", "")
            
            # Link Level Policy
            elif "infraRsHIfPol" in item:
                details["link_level"] = item["infraRsHIfPol"]["attributes"].get("tnFabricHIfPolName", "")
            
            # MCP Policy
            elif "infraRsMcpIfPol" in item:
                details["mcp"] = item["infraRsMcpIfPol"]["attributes"].get("tnMcpIfPolName", "")
            
            # Storm Control Policy
            elif "infraRsStormctrlIfPol" in item:
                details["storm_control"] = item["infraRsStormctrlIfPol"]["attributes"].get("tnStormctrlIfPolName", "")
            
            # Flow Control / QoS Ingress DPP Policy
            elif "infraRsQosIngressDppIfPol" in item:
                details["flow_control"] = item["infraRsQosIngressDppIfPol"]["attributes"].get("tnQosDppPolName", "")
        
        return details
    
    except:
        return details


def check_policy_group_exists(session, apic_url, name):
    """Check if an Access policy group already exists."""
    try:
        response = session.get(f"{apic_url}/api/node/mo/uni/infra/funcprof/accportgrp-{name}.json", verify=False, timeout=30)
        return response.status_code == 200 and bool(response.json().get("imdata"))
    except:
        return False


# =============================================================================
# API FUNCTIONS - EPG LOOKUP
# =============================================================================

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


def check_epg_exists(session, apic_url, tenant, app_profile, epg_name):
    """Check if an EPG exists."""
    try:
        response = session.get(f"{apic_url}/api/node/mo/uni/tn-{tenant}/ap-{app_profile}/epg-{epg_name}.json", verify=False, timeout=30)
        return response.status_code == 200 and bool(response.json().get("imdata"))
    except:
        return False


# =============================================================================
# API FUNCTIONS - CREATE INTERFACE CONFIGURATION
# =============================================================================

def set_port_description(session, apic_url, node_id, interface, description):
    """
    Set the description on a physical interface.
    Description format: {Hostname} {WorkOrder}
    """
    if "/" in interface:
        eth_interface = f"eth{interface}"
    else:
        eth_interface = f"eth1/{interface}"
    
    # The DN for a physical interface
    dn = f"topology/pod-{POD_ID}/node-{node_id}/sys/phys-[{eth_interface}]"
    
    payload = {
        "l1PhysIf": {
            "attributes": {
                "descr": description
            }
        }
    }
    
    try:
        response = session.post(f"{apic_url}/api/node/mo/{dn}.json", 
                               json=payload, verify=False, timeout=30)
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


def create_port_selector(session, apic_url, interface_profile, selector_name, interface_id, policy_group_name):
    """Create an Access Port Selector under an Interface Profile."""
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
                {"infraRsAccBaseGrp": {"attributes": {"tDn": f"uni/infra/funcprof/accportgrp-{policy_group_name}"}}}
            ]
        }
    }
    try:
        response = session.post(f"{apic_url}/api/node/mo/uni/infra/accportprof-{interface_profile}/hports-{selector_name}-typ-range.json",
                               json=payload, verify=False, timeout=30)
        return response.status_code == 200, response.text
    except Exception as e:
        return False, str(e)


# =============================================================================
# API FUNCTIONS - DEPLOY STATIC BINDING TO EPG
# =============================================================================

def deploy_static_binding_to_epg(session, apic_url, tenant, app_profile, epg_name, vlan_id, mode, node_id, interface):
    """
    Deploy Static EPG on Interface.
    
    Settings:
    - Port Type: Port
    - Node: node_id
    - Path: eth{interface}
    - VLAN: vlan_id
    - Deployment Immediacy: Immediate
    - Primary VLAN for Micro-Seg: blank
    - Mode: regular (trunk) or untagged (access)
    - PTP: Disabled (next step)
    """
    # Build path
    if "/" in interface:
        eth_interface = f"eth{interface}"
    else:
        eth_interface = f"eth1/{interface}"
    
    path = f"topology/pod-{POD_ID}/paths-{node_id}/pathep-[{eth_interface}]"
    
    payload = {
        "fvRsPathAtt": {
            "attributes": {
                "tDn": path,
                "encap": f"vlan-{vlan_id}",
                "mode": mode,
                "instrImedcy": DEPLOYMENT_IMMEDIACY
                # primaryEncap not set = blank (Micro-Seg)
                # PTP is disabled by not including it
            }
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
    """Run pre-flight validation for all deployments."""
    print("\n" + "=" * 70)
    print(" PRE-FLIGHT VALIDATION")
    print("=" * 70)
    
    global_settings = {
        "aep": {},               # {env: aep_name}
        "int_profiles": {},      # {env: {node_id: profile_name}}
        "link_level": {}         # {env: {speed: link_level_policy}}
    }
    
    # Group by environment and collect unique node IDs and speeds
    envs_data = {}
    for dep in deployments:
        env = detect_environment(dep['switch'])
        if env and env in sessions:
            if env not in envs_data:
                envs_data[env] = {"deps": [], "node_ids": set(), "speeds": set()}
            envs_data[env]["deps"].append(dep)
            
            node_id = extract_node_id(dep['switch'])
            if node_id:
                envs_data[env]["node_ids"].add(node_id)
            if dep['speed']:
                envs_data[env]["speeds"].add(dep['speed'].upper())
    
    for env, data in envs_data.items():
        session = sessions[env]
        apic_url = APIC_URLS[env]
        
        print(f"\n  [{env}] Checking environment ({len(data['deps'])} deployments)...")
        
        # 1. Check AEP
        default_aep = DEFAULT_AEP.get(env, "edcflb")
        print(f"\n    Checking AEP '{default_aep}'...")
        if check_aep_exists(session, apic_url, default_aep):
            print(f"      [FOUND] {default_aep}")
            global_settings["aep"][env] = default_aep
        else:
            print(f"      [NOT FOUND] {default_aep}")
            aeps = get_all_aeps(session, apic_url)
            if not aeps:
                print(f"      [ERROR] No AEPs found. Cannot continue.")
                return None
            
            print(f"\n    Available AEPs:")
            print("    " + "-" * 50)
            for i, aep in enumerate(aeps, 1):
                print(f"    [{i:>3}] {aep}")
            print("    " + "-" * 50)
            
            while True:
                choice = prompt_input(f"\n    Select AEP for ALL {env} deployments: ").strip()
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(aeps):
                        global_settings["aep"][env] = aeps[idx]
                        print(f"      [SELECTED] {aeps[idx]}")
                        break
                except ValueError:
                    pass
                print("    [ERROR] Invalid selection")
        
        # 2. Find Interface Profiles for each node ID
        global_settings["int_profiles"][env] = {}
        all_profiles = get_interface_profiles(session, apic_url)
        
        if data["node_ids"]:
            print(f"\n    Finding Interface Profiles for {len(data['node_ids'])} node(s)...")
            
            for node_id in sorted(data["node_ids"]):
                print(f"\n      Looking for profile matching node '{node_id}'...")
                
                found_profile = find_interface_profile_for_node(all_profiles, node_id)
                
                if found_profile:
                    print(f"        [FOUND] {found_profile}")
                    
                    # Confirm or allow change
                    confirm = prompt_input(f"        Use this profile for node {node_id}? (yes/no): ").strip().lower()
                    
                    if confirm in ['yes', 'y']:
                        global_settings["int_profiles"][env][node_id] = found_profile
                    else:
                        # Show all profiles to select
                        print(f"\n    Available Interface Profiles:")
                        print("    " + "-" * 50)
                        for i, p in enumerate(all_profiles, 1):
                            marker = " <-- suggested" if p == found_profile else ""
                            print(f"    [{i:>3}] {p}{marker}")
                        print("    " + "-" * 50)
                        
                        while True:
                            choice = prompt_input(f"\n    Select profile for node {node_id} (or 'S' to skip): ").strip().upper()
                            if choice == 'S':
                                global_settings["int_profiles"][env][node_id] = None
                                print(f"        [SKIP] Deployments for node {node_id} will be skipped")
                                break
                            try:
                                idx = int(choice) - 1
                                if 0 <= idx < len(all_profiles):
                                    global_settings["int_profiles"][env][node_id] = all_profiles[idx]
                                    print(f"        [SELECTED] {all_profiles[idx]}")
                                    break
                            except ValueError:
                                pass
                            print("    [ERROR] Invalid selection")
                else:
                    print(f"        [NOT FOUND] No profile matching node '{node_id}'")
                    print(f"\n    Available Interface Profiles:")
                    print("    " + "-" * 50)
                    for i, p in enumerate(all_profiles, 1):
                        print(f"    [{i:>3}] {p}")
                    print("    " + "-" * 50)
                    
                    while True:
                        choice = prompt_input(f"\n    Select profile for node {node_id} (or 'S' to skip): ").strip().upper()
                        if choice == 'S':
                            global_settings["int_profiles"][env][node_id] = None
                            print(f"        [SKIP] Deployments for node {node_id} will be skipped")
                            break
                        try:
                            idx = int(choice) - 1
                            if 0 <= idx < len(all_profiles):
                                global_settings["int_profiles"][env][node_id] = all_profiles[idx]
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
                        choice = prompt_input(f"\n      Select Link Level policy for {speed} (will apply to all {speed} deployments): ").strip()
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
    
    # Summary
    print("\n" + "=" * 70)
    print(" PRE-FLIGHT COMPLETE")
    print("=" * 70)
    
    print("\n  Global Settings:")
    for env in envs_data:
        print(f"\n    [{env}]")
        print(f"      AEP: {global_settings['aep'].get(env, 'NOT SET')}")
        print(f"      Interface Profiles:")
        for node_id, profile in global_settings['int_profiles'].get(env, {}).items():
            status = profile if profile else "SKIP"
            print(f"        Node {node_id}: {status}")
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

def display_validated_ports(ports, node_id):
    """Display validated available ports."""
    if not ports:
        print(f"\n  [WARNING] No validated available ports on node {node_id}")
        return None
    
    print(f"\n  Validated available ports on node {node_id}:")
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
    """Display Application Profile options."""
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


def display_deployment_preview(config, all_profiles, all_link_levels, all_aeps, available_ports):
    """Display deployment preview with Interface Configuration details."""
    print("\n" + "=" * 70)
    print(" DEPLOYMENT PREVIEW")
    print("=" * 70)
    
    mode_display = "Access (Untagged)" if config['mode'] == "untagged" else "Trunk (Tagged)"
    # Both COPPER and FIBER are Ethernet - just different physical media
    interface_type = "Ethernet"
    
    # Use custom description if set
    if 'custom_description' in config and config['custom_description']:
        port_description = config['custom_description']
    else:
        port_description = f"{config['hostname']} {config['work_order']}"
    
    print(f"\n  Environment:           {config['environment']} ({config['tenant']})")
    print(f"  Hostname:              {config['hostname']}")
    print(f"  Work Order:            {config['work_order']}")
    
    print(f"\n  === INTERFACE CONFIGURATION ===")
    print(f"  [1] Node Type:             {NODE_TYPE.capitalize()}")
    print(f"  [2] Port Type:             {PORT_TYPE.capitalize()}")
    print(f"  [3] Interface Type:        {interface_type}")
    print(f"  [4] Interface Aggregation: {INTERFACE_AGGREGATION.capitalize()}")
    print(f"  [5] Node ID:               {config['node_id']}")
    print(f"  [6] Interface:             eth{config['interface']}")
    print(f"  [7] Interface Profile:     {config['interface_profile']}")
    print(f"  [8] Port Description:      {port_description}")
    
    print(f"\n  === NEW LEAF ACCESS PORT POLICY GROUP ===")
    print(f"  [9]  Name:                  {config['policy_group_name']}")
    print(f"       Description:           (empty)")
    print(f"  [10] AEP:                   {config['aep']}")
    print(f"       CDP Policy:            {CDP_POLICY}")
    print(f"  [11] Link Level Policy:     {config['link_level']}")
    print(f"       LLDP Policy:           {LLDP_POLICY}")
    
    print(f"\n  === STATIC EPG BINDING ===")
    print(f"  Port Type:             Port")
    print(f"  Node:                  {config['node_id']}")
    print(f"  Path:                  eth{config['interface']}")
    print(f"  Deployment Immediacy:  {DEPLOYMENT_IMMEDIACY.capitalize()}")
    print(f"  Primary VLAN (uSeg):   (blank)")
    print(f"  Mode:                  {mode_display}")
    print(f"  PTP:                   {PTP_MODE.capitalize()}")
    
    print(f"\n  EPG Bindings ({len(config['epg_bindings'])} VLANs):")
    for binding in config['epg_bindings'][:10]:
        print(f"    VLAN {binding['vlan']:>4} -> {binding['epg']} ({binding['app_profile']})")
    if len(config['epg_bindings']) > 10:
        print(f"    ... and {len(config['epg_bindings']) - 10} more")
    
    print("\n" + "=" * 70)


def edit_interface_configuration(config, all_profiles, all_link_levels, all_aeps, available_ports, session, apic_url):
    """
    Allow user to edit interface configuration settings.
    Returns updated config and proceed flag.
    """
    while True:
        if 'custom_description' in config and config['custom_description']:
            port_description = config['custom_description']
        else:
            port_description = f"{config['hostname']} {config['work_order']}"
        
        print("\n  === EDIT INTERFACE CONFIGURATION ===")
        print(f"  [1] Node Type:             {NODE_TYPE.capitalize()} (fixed)")
        print(f"  [2] Port Type:             {PORT_TYPE.capitalize()} (fixed)")
        print(f"  [3] Interface Type:        Ethernet (fixed)")
        print(f"  [4] Interface Aggregation: {INTERFACE_AGGREGATION.capitalize()} (fixed)")
        print(f"  [5] Node ID:               {config['node_id']} (from CSV)")
        print(f"  [6] Interface:             eth{config['interface']}")
        print(f"  [7] Interface Profile:     {config['interface_profile']}")
        print(f"  [8] Port Description:      {port_description}")
        print(f"\n  Policy Group: {config['policy_group_name']}")
        print(f"  [9]  Policy Group Name:    {config['policy_group_name']}")
        print(f"  [10] AEP:                  {config['aep']}")
        print(f"  [11] Link Level Policy:    {config['link_level']}")
        print("  " + "-" * 50)
        print("  [D] Done editing")
        print("  [C] Cancel deployment")
        
        choice = prompt_input("\n  Select option to edit: ").strip().upper()
        
        if choice == 'D':
            return config, True
        
        elif choice == 'C':
            return config, False
        
        elif choice in ['1', '2', '3', '4', '5']:
            print("    [INFO] This setting is fixed for this script")
        
        elif choice == '6':
            # Change interface
            print("\n    Available Interfaces:")
            print("    " + "-" * 40)
            for i, port in enumerate(available_ports, 1):
                current = " <-- current" if port['interface'] == config['interface'] else ""
                print(f"    [{i:>2}] eth{port['interface']:<10} {port['speed']}{current}")
            print("    " + "-" * 40)
            
            while True:
                int_choice = prompt_input("\n    Select interface (or 'B' to go back): ").strip().upper()
                if int_choice == 'B':
                    break
                try:
                    idx = int(int_choice) - 1
                    if 0 <= idx < len(available_ports):
                        config['interface'] = available_ports[idx]['interface']
                        # Update policy group name
                        config['policy_group_name'] = f"{config['hostname']}_e{config['interface'].split('/')[-1]}"
                        print(f"    [UPDATED] Interface: eth{config['interface']}")
                        print(f"    [UPDATED] Policy Group Name: {config['policy_group_name']}")
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
            print(f"\n    Current: {config['policy_group_name']}")
            print("    Enter new name (or press Enter to keep current):")
            new_name = prompt_input("    New name: ").strip()
            if new_name:
                config['policy_group_name'] = new_name
                print(f"    [UPDATED] Policy Group Name: {new_name}")
        
        elif choice == '10':
            # Change AEP
            print("\n    Available AEPs:")
            print("    " + "-" * 50)
            for i, aep in enumerate(all_aeps, 1):
                current = " <-- current" if aep == config['aep'] else ""
                print(f"    [{i:>3}] {aep}{current}")
            print("    " + "-" * 50)
            
            while True:
                aep_choice = prompt_input("\n    Select AEP (or 'B' to go back): ").strip().upper()
                if aep_choice == 'B':
                    break
                try:
                    idx = int(aep_choice) - 1
                    if 0 <= idx < len(all_aeps):
                        config['aep'] = all_aeps[idx]
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
        
        else:
            print("    [ERROR] Invalid option")


# =============================================================================
# DEPLOYMENT FUNCTIONS
# =============================================================================

def load_individual_port_csv(filename):
    """Load individual port deployment CSV file."""
    try:
        with open(filename, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            deployments = []
            for row in reader:
                normalized = {k.strip().upper(): v.strip() if v else "" for k, v in row.items() if k}
                deployments.append({
                    "hostname": normalized.get("HOSTNAME", ""),
                    "switch": normalized.get("SWITCH", ""),
                    "type": normalized.get("TYPE", "").upper(),
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


def deploy_individual_port(session, apic_url, config, dry_run=False):
    """
    Deploy an individual port configuration.
    
    Steps:
      1. Set port description
      2. Create Leaf Access Port Policy Group
      3. Create Port Selector (links port to new policy group)
      4. Deploy Static Bindings to EPG(s)
    """
    results = {"description": False, "policy_group": False, "port_selector": False, "bindings": []}
    
    # Use custom description if set, otherwise default
    if 'custom_description' in config and config['custom_description']:
        port_description = config['custom_description']
    else:
        port_description = f"{config['hostname']} {config['work_order']}"
    
    if dry_run:
        print("\n  [DRY-RUN] Would execute:")
        print(f"    1. Set port description: {port_description}")
        print(f"    2. Create Leaf Access Port Policy Group: {config['policy_group_name']}")
        print(f"       - AEP: {config['aep']}")
        print(f"       - CDP: {CDP_POLICY}")
        print(f"       - Link Level: {config['link_level']}")
        print(f"       - LLDP: {LLDP_POLICY}")
        print(f"    3. Create Port Selector on: {config['interface_profile']}")
        print(f"    4. Deploy {len(config['epg_bindings'])} static bindings (mode: {config['mode']})")
        results["description"] = True
        results["policy_group"] = True
        results["port_selector"] = True
        for binding in config['epg_bindings']:
            results["bindings"].append({"vlan": binding['vlan'], "success": True})
        return results
    
    # Step 1: Set Port Description
    print(f"\n  [1/4] Setting port description: {port_description}")
    success, response = set_port_description(session, apic_url, config['node_id'], 
                                              config['interface'], port_description)
    if success:
        print(f"        [SUCCESS]")
        results["description"] = True
    else:
        print(f"        [WARNING] Could not set description: {response[:100]}")
        results["description"] = False
    
    # Step 2: Create Leaf Access Port Policy Group
    print(f"  [2/4] Creating Leaf Access Port Policy Group: {config['policy_group_name']}")
    print(f"        AEP: {config['aep']}")
    print(f"        CDP: {CDP_POLICY}")
    print(f"        Link Level: {config['link_level']}")
    print(f"        LLDP: {LLDP_POLICY}")
    
    success, response = create_leaf_access_port_policy_group(
        session, apic_url, 
        config['policy_group_name'], 
        config['aep'], 
        config['link_level']
    )
    if success:
        print(f"        [SUCCESS]")
        results["policy_group"] = True
    else:
        print(f"        [FAILED] {response[:100]}")
        return results
    
    # Step 3: Create Port Selector
    selector_name = f"{config['hostname']}_e{config['interface'].split('/')[-1]}"
    print(f"  [3/4] Creating Port Selector: {selector_name}")
    print(f"        Interface Profile: {config['interface_profile']}")
    print(f"        Policy Group: {config['policy_group_name']}")
    success, response = create_port_selector(session, apic_url, config['interface_profile'], 
                                             selector_name, config['interface'], config['policy_group_name'])
    if success:
        print(f"        [SUCCESS]")
        results["port_selector"] = True
    else:
        print(f"        [FAILED] {response[:100]}")
        return results
    
    # Step 4: Deploy Static Bindings to EPGs
    mode_display = "untagged" if config['mode'] == "untagged" else "regular (trunk)"
    print(f"  [4/4] Deploying Static Bindings ({len(config['epg_bindings'])} VLANs, mode: {mode_display})")
    
    for binding in config['epg_bindings']:
        success, response = deploy_static_binding_to_epg(
            session, apic_url, config['tenant'],
            binding['app_profile'], binding['epg'],
            binding['vlan'], config['mode'],
            config['node_id'], config['interface']
        )
        results["bindings"].append({"vlan": binding['vlan'], "success": success})
        print(f"        VLAN {binding['vlan']}: {'OK' if success else 'FAILED'}")
    
    return results


# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    print("\n" + "=" * 70)
    print(" ACI BULK INDIVIDUAL PORT DEPLOYMENT SCRIPT")
    print("=" * 70)
    
    # Check configuration
    missing_urls = [dc for dc, url in APIC_URLS.items() if not url]
    if missing_urls:
        print(f"\n[ERROR] APIC URLs not configured for: {', '.join(missing_urls)}")
        sys.exit(1)
    
    # Get deployment file
    print(f"\n[INFO] Default deployment file: {DEPLOYMENT_FILE}")
    custom_file = prompt_input("Press Enter to use default, or enter filename: ").strip()
    deployment_file = custom_file if custom_file else DEPLOYMENT_FILE
    
    # Load deployments
    print(f"\n[INFO] Loading from: {deployment_file}")
    deployments = load_individual_port_csv(deployment_file)
    if not deployments:
        sys.exit(1)
    
    access_count = sum(1 for d in deployments if d['type'] == 'ACCESS')
    trunk_count = sum(1 for d in deployments if d['type'] == 'TRUNK')
    print(f"[INFO] Loaded {len(deployments)} deployment(s): {access_count} ACCESS, {trunk_count} TRUNK")
    
    # Select run mode
    print("\n" + "-" * 70)
    print(" RUN MODE")
    print("-" * 70)
    print("\n  [1] Normal - Deploy configurations")
    print("  [2] Dry-Run - Validate only, don't deploy")
    
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
    print("Password: ", end="", flush=True)
    # Check if running in web UI mode (PTY) - getpass doesn't work with PTY
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
    
    # Run pre-flight checks
    global_settings = run_preflight_checks(sessions, deployments)
    if not global_settings:
        print("\n[CANCELLED]")
        sys.exit(0)
    
    # Process deployments
    print("\n" + "=" * 70)
    print(" PROCESSING INDIVIDUAL PORT DEPLOYMENTS")
    print("=" * 70)
    
    successful, failed, skipped = 0, 0, 0
    
    for i, dep in enumerate(deployments, 1):
        print(f"\n{'='*70}")
        print(f" [{i}/{len(deployments)}] {dep['hostname']}")
        print(f"{'='*70}")
        
        # Validate type
        if dep['type'] not in ['ACCESS', 'TRUNK']:
            print(f"  [SKIP] Invalid type: {dep['type']}")
            skipped += 1
            continue
        
        # Get environment
        env = detect_environment(dep['switch'])
        if not env or env not in sessions:
            print(f"  [SKIP] Environment not available")
            skipped += 1
            continue
        
        session = sessions[env]
        apic_url = APIC_URLS[env]
        tenant = TENANTS[env]
        
        # Extract node ID
        node_id = extract_node_id(dep['switch'])
        if not node_id:
            print(f"  [SKIP] Could not extract node ID")
            skipped += 1
            continue
        
        # Get AEP from pre-flight
        aep = global_settings["aep"].get(env)
        if not aep:
            print(f"  [SKIP] No AEP configured for {env}")
            skipped += 1
            continue
        
        # Get Link Level policy from pre-flight
        link_level = global_settings["link_level"].get(env, {}).get(dep['speed'])
        if not link_level:
            print(f"  [SKIP] No Link Level policy configured for speed '{dep['speed']}'")
            skipped += 1
            continue
        
        # Get interface profile by node ID (for Port Selector)
        interface_profile = global_settings["int_profiles"].get(env, {}).get(node_id)
        if not interface_profile:
            print(f"  [SKIP] No interface profile for node {node_id}")
            skipped += 1
            continue
        
        print(f"\n  Environment: {env} ({tenant})")
        print(f"  Node: {node_id}, Type: {dep['type']}, Speed: {dep['speed']}")
        print(f"  Work Order: {dep['work_order']}")
        
        # PORT VALIDATION - Get validated available ports
        print(f"\n  === PORT VALIDATION ===")
        print(f"  Querying and validating ports (checking 4 criteria)...")
        print(f"    1. Usage = 'discovery'")
        print(f"    2. No description")
        print(f"    3. No policy group assigned")
        print(f"    4. No EPG deployed")
        
        ports = get_validated_available_ports(session, apic_url, node_id)
        print(f"\n  Found {len(ports)} validated available ports")
        
        if not ports:
            print(f"  [SKIP] No validated available ports")
            skipped += 1
            continue
        
        # Select port
        selected_port = display_validated_ports(ports, node_id)
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
        
        # Build policy group name: {Hostname}_e{InterfaceID}
        policy_group_name = f"{dep['hostname']}_e{interface.split('/')[-1]}"
        
        # Check if policy group already exists
        if check_policy_group_exists(session, apic_url, policy_group_name):
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
            "node_id": node_id, 
            "interface": interface,
            "interface_profile": interface_profile,
            "policy_group_name": policy_group_name,
            "aep": aep,
            "link_level": link_level,
            "type": dep['type'],
            "mode": MODE_MAPPING.get(dep['type'], "regular"),
            "epg_bindings": epg_bindings
        }
        
        # Get all profiles and link level policies for edit functionality
        all_profiles = get_interface_profiles(session, apic_url)
        all_link_levels = get_link_level_policies(session, apic_url)
        all_aeps = get_all_aeps(session, apic_url)
        
        # Preview and confirm loop
        while True:
            # Preview
            display_deployment_preview(config, all_profiles, all_link_levels, all_aeps, ports)
            
            # Confirm with edit option
            print("\n  [Y] Yes - Deploy")
            print("  [N] No - Skip this deployment")
            print("  [E] Edit - Modify interface configuration")
            print("  [Q] Quit - Exit script")
            
            confirm = prompt_input("\n  Choice: ").strip().upper()
            
            if confirm == 'Q':
                print("\n[INFO] Quitting...")
                # Exit the outer loop
                skipped += 1
                break
            
            elif confirm == 'N':
                print("  [SKIPPED by user]")
                skipped += 1
                break
            
            elif confirm == 'E':
                # Edit mode
                config, proceed = edit_interface_configuration(
                    config, all_profiles, all_link_levels, all_aeps, ports, session, apic_url
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
                results = deploy_individual_port(session, apic_url, config, dry_run)
                
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
