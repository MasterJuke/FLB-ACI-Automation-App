#!/usr/bin/env python3
"""
ACI Integration Patcher
=========================
Automatically applies all integration changes to existing deployment scripts.

What it does:
  1. Patches aci_bulk_vpc_deploy.py
     - Adds aci_port_utils import
     - Replaces duplicated helper functions with pass-through comments
     - Updates port query/display to show ALL ports with color coding

  2. Patches aci_bulk_individual_deploy.py
     - Same as VPC but for individual port deployment

  3. Patches aci_bulk_epg_add.py
     - Adds aci_port_utils import for shared helpers

  4. Patches aci_deployment_app.py
     - Adds CSS for [AVAIL]/[IN-USE] port status coloring
     - Updates JavaScript addLine() for new bracket tags

Usage:
    python apply_patches.py [--dry-run]

    --dry-run   Show what would change without modifying files
    --backup    Create .bak files before modifying (default: yes)

Author: Network Automation
Version: 1.0.0
"""

import os
import sys
import re
import shutil
from datetime import datetime


# =============================================================================
# CONFIGURATION
# =============================================================================

BACKUP = True
DRY_RUN = '--dry-run' in sys.argv
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# =============================================================================
# PATCH UTILITIES
# =============================================================================

def backup_file(filepath):
    """Create a timestamped backup of a file."""
    if not os.path.exists(filepath):
        return False
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{filepath}.{ts}.bak"
    shutil.copy2(filepath, backup_path)
    print(f"  [BACKUP] {os.path.basename(filepath)} -> {os.path.basename(backup_path)}")
    return True


def read_file(filepath):
    """Read file content."""
    with open(filepath, 'r', encoding='utf-8') as f:
        return f.read()


def write_file(filepath, content):
    """Write content to file."""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)


def find_and_replace(content, find_str, replace_str, label=""):
    """Find and replace a string. Returns (new_content, success)."""
    if find_str in content:
        content = content.replace(find_str, replace_str, 1)
        if label:
            print(f"    [OK] {label}")
        return content, True
    else:
        if label:
            print(f"    [SKIP] {label} — pattern not found")
        return content, False


def find_and_delete_function(content, func_name):
    """
    Delete an entire function definition from the source.
    Handles functions that start with 'def func_name(' and end at the next
    'def ', class, or module-level comment block.
    """
    # Pattern: find 'def func_name(' at the start of a line
    pattern = re.compile(
        r'^(def\s+' + re.escape(func_name) + r'\s*\(.*?\).*?:.*?)(?=\ndef\s|\nclass\s|\n# ={5,}|\Z)',
        re.MULTILINE | re.DOTALL
    )
    match = pattern.search(content)
    if match:
        # Replace function with a comment noting it was moved
        replacement = f"# {func_name}() — moved to aci_port_utils.py\n"
        content = content[:match.start()] + replacement + content[match.end():]
        print(f"    [REMOVED] def {func_name}()")
        return content, True
    else:
        print(f"    [SKIP] def {func_name}() — not found")
        return content, False


# =============================================================================
# IMPORT INJECTION
# =============================================================================

# Import lines for each script type
VPC_IMPORT = """
# Shared utilities (consolidated from duplicated helpers)
from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, find_common_ports_with_status,
    display_vpc_port_selection, display_vpc_independent_port_selection,
    get_validated_available_ports, find_common_validated_ports,
    cleanup_port_for_redeployment, cleanup_vpc_port_for_redeployment,
    query_existing_vpc_policy_groups, display_policy_group_selection
)
"""

INDIVIDUAL_IMPORT = """
# Shared utilities (consolidated from duplicated helpers)
from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, display_port_selection,
    get_validated_available_ports,
    cleanup_port_for_redeployment,
    query_existing_access_policy_groups, display_policy_group_selection
)
"""

EPGADD_IMPORT = """
# Shared utilities (consolidated from duplicated helpers)
from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_port,
    parse_ports, prompt_input,
    query_all_bindings_on_port, delete_all_bindings_on_port
)
"""


def inject_import(content, import_block, script_name):
    """Add import block after 'from concurrent.futures import...' or after urllib3 disable."""
    # Try after concurrent.futures import
    anchor = "from concurrent.futures import ThreadPoolExecutor, as_completed"
    if anchor in content:
        content, ok = find_and_replace(
            content,
            anchor,
            anchor + "\n" + import_block,
            f"Injected import after concurrent.futures"
        )
        if ok:
            return content

    # Fallback: after urllib3 disable warnings
    anchor2 = "urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)"
    if anchor2 in content:
        content, ok = find_and_replace(
            content,
            anchor2,
            anchor2 + "\n" + import_block,
            f"Injected import after urllib3"
        )
        if ok:
            return content

    print(f"    [WARNING] Could not find injection point in {script_name}")
    return content


# =============================================================================
# FUNCTIONS TO REMOVE PER SCRIPT
# =============================================================================

# Functions that exist in aci_port_utils and should be removed from each script
COMMON_FUNCS_TO_REMOVE = [
    "prompt_input",
    "detect_environment",
    "extract_node_id",
    "parse_vlans",
]

VPC_FUNCS_TO_REMOVE = COMMON_FUNCS_TO_REMOVE + [
    "parse_interface",
    "validate_single_port",
    "get_validated_available_ports",
    "find_common_validated_ports",
    "display_validated_ports",
]

INDIVIDUAL_FUNCS_TO_REMOVE = COMMON_FUNCS_TO_REMOVE + [
    "parse_interface",
    "validate_single_port",
    "get_validated_available_ports",
    "get_port_details",
    "display_validated_ports",
]

EPGADD_FUNCS_TO_REMOVE = COMMON_FUNCS_TO_REMOVE + [
    "parse_port",
]


# =============================================================================
# PORT DISPLAY PATCHES
# =============================================================================

def patch_vpc_port_display(content):
    """Replace VPC port query/display with asymmetric VPC support + cleanup."""

    # --- PATCH 0: Update import block if already patched with old imports ---
    # Match any previous version of the VPC import and upgrade to current
    import_patterns = [
        # v1.0 — no cleanup, no independent, no PG reuse
        """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, find_common_ports_with_status,
    display_vpc_port_selection, get_validated_available_ports,
    find_common_validated_ports
)""",
        # v1.1 — cleanup but no independent/PG reuse
        """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, find_common_ports_with_status,
    display_vpc_port_selection, get_validated_available_ports,
    find_common_validated_ports,
    cleanup_vpc_port_for_redeployment
)""",
        # v1.1b — cleanup + independent but no PG reuse
        """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, find_common_ports_with_status,
    display_vpc_port_selection, display_vpc_independent_port_selection,
    get_validated_available_ports, find_common_validated_ports,
    cleanup_port_for_redeployment, cleanup_vpc_port_for_redeployment
)""",
    ]

    current_vpc_import = """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, find_common_ports_with_status,
    display_vpc_port_selection, display_vpc_independent_port_selection,
    get_validated_available_ports, find_common_validated_ports,
    cleanup_port_for_redeployment, cleanup_vpc_port_for_redeployment,
    query_existing_vpc_policy_groups, display_policy_group_selection
)"""

    for old_imp in import_patterns:
        if old_imp in content:
            content, _ = find_and_replace(content, old_imp, current_vpc_import, "VPC: update imports")
            break

    # --- PATCH A: Validation header text ---
    old_header = '''        print(f"  Querying and validating ports (checking 4 criteria)...")
        print(f"    1. Usage = \'discovery\'")
        print(f"    2. No description")
        print(f"    3. No policy group assigned")
        print(f"    4. No EPG deployed")'''

    new_header = '''        print(f"  Querying all ports and checking status...")
        print(f"    Criteria: discovery usage, no description, no policy group, no EPG")
        print(f"    [AVAIL] = passes all checks  |  [IN-USE] = has existing config")'''

    content, _ = find_and_replace(content, old_header, new_header, "VPC validation header")

    # --- PATCH B: Port query + mode selection (same vs independent) ---
    old_query = '''        ports1 = get_validated_available_ports(session, apic_url, node1)
        ports2 = get_validated_available_ports(session, apic_url, node2)
        
        print(f"\\n    Node {node1}: {len(ports1)} validated available")
        print(f"    Node {node2}: {len(ports2)} validated available")
        
        # Find common validated ports
        common_ports = find_common_validated_ports(ports1, ports2)
        print(f"    Common: {len(common_ports)} validated available on both")
        
        if not common_ports:
            print(f"  [SKIP] No validated matching ports on both switches")
            skipped += 1
            continue
        
        # Select port
        selected_port = display_validated_ports(common_ports, f"nodes {node1} & {node2}")'''

    new_query = '''        ports1 = get_all_ports_with_status(session, apic_url, node1, POD_ID)
        ports2 = get_all_ports_with_status(session, apic_url, node2, POD_ID)
        
        avail1 = sum(1 for p in ports1 if p['valid'])
        avail2 = sum(1 for p in ports2 if p['valid'])
        print(f"\\n    Node {node1}: {len(ports1)} total ({avail1} available)")
        print(f"    Node {node2}: {len(ports2)} total ({avail2} available)")
        
        # Find common ports (both available and in-use)
        common_ports = find_common_ports_with_status(ports1, ports2)
        avail_common = sum(1 for p in common_ports if p['valid'])
        print(f"    Common: {len(common_ports)} total ({avail_common} available on both)")
        
        # Port selection mode
        print(f"\\n  Port Selection Mode:")
        print(f"    [1] Same port on both switches (common ports)")
        print(f"    [2] Different port on each switch (independent)")
        port_mode = prompt_input("\\n  Select (1/2) [default=1]: ").strip()
        
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
            selected_port = display_vpc_port_selection(common_ports, node1, node2)'''

    content, _ = find_and_replace(content, old_query, new_query, "VPC port query + mode selection")

    # --- PATCH C: Store interface2 + asymmetric flag in config ---
    old_cfg = '''            "interface": interface,'''
    new_cfg = '''            "interface": interface,
            "interface2": selected_port2['interface'] if asymmetric_vpc else interface,
            "asymmetric_vpc": asymmetric_vpc,'''
    content, _ = find_and_replace(content, old_cfg, new_cfg, "Config: interface2 + asymmetric flag")

    # --- PATCH D: deploy_vpc step 1 — per-node interfaces ---
    old_s1 = '''    # 1. Set Port Description on both nodes
    print(f"\\n  [1/4] Setting port description on both nodes: {port_description}")
    interface_eth = f"eth{config['interface']}"
    
    success1, _ = set_port_description(session, apic_url, config['node1'], config['interface'], port_description)
    print(f"        Node {config['node1']} {interface_eth}: {'[SUCCESS]' if success1 else '[WARNING]'}")
    
    success2, _ = set_port_description(session, apic_url, config['node2'], config['interface'], port_description)
    print(f"        Node {config['node2']} {interface_eth}: {'[SUCCESS]' if success2 else '[WARNING]'}")'''

    new_s1 = '''    # 1. Set Port Description on both nodes (supports asymmetric VPC ports)
    iface1 = config['interface']
    iface2 = config.get('interface2', config['interface'])
    print(f"\\n  [1/4] Setting port description on both nodes: {port_description}")
    
    success1, _ = set_port_description(session, apic_url, config['node1'], iface1, port_description)
    print(f"        Node {config['node1']} eth{iface1}: {'[SUCCESS]' if success1 else '[WARNING]'}")
    
    success2, _ = set_port_description(session, apic_url, config['node2'], iface2, port_description)
    print(f"        Node {config['node2']} eth{iface2}: {'[SUCCESS]' if success2 else '[WARNING]'}")'''

    content, _ = find_and_replace(content, old_s1, new_s1, "deploy_vpc step 1: per-node interfaces")

    # --- PATCH E: deploy_vpc step 3 — asymmetric port selectors ---
    old_s3 = '''    # 3. Create Access Port Selector
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
        return results'''

    new_s3 = '''    # 3. Create Access Port Selector(s) — supports asymmetric VPC ports
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
            return results'''

    content, _ = find_and_replace(content, old_s3, new_s3, "deploy_vpc step 3: asymmetric selectors")

    # --- PATCH F: dry-run preview step 3 ---
    old_dr3 = '''        print(f"    3. Create Access Port Selector on: {config['interface_profile']}")
        print(f"       - Name: {config['policy_group']}")
        print(f"       - Interface IDs: {config['interface']}")
        print(f"       - Interface Policy Group: {config['policy_group']}")'''

    new_dr3 = '''        iface2 = config.get('interface2', config['interface'])
        if config.get('asymmetric_vpc'):
            print(f"    3. Create Port Selectors (asymmetric VPC) on: {config['interface_profile']}")
            print(f"       - Port 1: {config['interface']} (node {config['node1']})")
            print(f"       - Port 2: {iface2} (node {config['node2']})")
        else:
            print(f"    3. Create Access Port Selector on: {config['interface_profile']}")
            print(f"       - Name: {config['policy_group']}")
            print(f"       - Interface IDs: {config['interface']}")
        print(f"       - Interface Policy Group: {config['policy_group']}")'''

    content, _ = find_and_replace(content, old_dr3, new_dr3, "deploy_vpc dry-run: asymmetric preview")

    # --- PATCH G: Cleanup injection + deploy call ---
    old_deploy = '''            elif confirm in ['Y', 'YES']:
                # Deploy
                print("\\n  Deploying..." if not dry_run else "\\n  Dry-run...")
                results = deploy_vpc(session, apic_url, config, aep, dry_run)'''

    new_deploy = '''            elif confirm in ['Y', 'YES']:
                # Deploy
                print("\\n  Deploying..." if not dry_run else "\\n  Dry-run...")
                
                # Full cleanup if overriding in-use port(s)
                need_clean1 = not selected_port.get('valid', True) if selected_port else False
                need_clean2 = not selected_port2.get('valid', True) if selected_port2 else False
                
                if not dry_run and (need_clean1 or need_clean2):
                    print("\\n  [CLEANUP] Wiping existing port configuration...")
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
                    print(f"  [CLEANUP] Done\\n")
                
                results = deploy_vpc(session, apic_url, config, aep, dry_run)'''

    content, _ = find_and_replace(content, old_deploy, new_deploy, "VPC cleanup + deploy call")

    # --- PATCH H: Preview — show per-node interfaces ---
    old_pv = '''    print(f"  Interface IDs:          {config['interface']}")'''
    new_pv = '''    iface2 = config.get('interface2', config['interface'])
    if config.get('asymmetric_vpc'):
        print(f"  Interface (node {config['node1']}):  eth{config['interface']}")
        print(f"  Interface (node {config['node2']}):  eth{iface2}")
    else:
        print(f"  Interface IDs:          {config['interface']}")'''
    content, _ = find_and_replace(content, old_pv, new_pv, "VPC preview: per-node interfaces")

    # --- PATCH I: PG Mode toggle after Flow Control selection ---
    old_fc_end = '''    # Get credentials
    print("\\n" + "-" * 70)
    print(" AUTHENTICATION")'''

    new_fc_end = '''    # Policy Group Mode
    print("\\n" + "-" * 70)
    print(" POLICY GROUP MODE")
    print("-" * 70)
    print("\\n  [1] Create NEW policy group per deployment (default)")
    print("  [2] Reuse EXISTING policy group (query by link level)")
    
    while True:
        pg_mode_choice = prompt_input("\\nSelect (1/2) [default=1]: ").strip()
        if pg_mode_choice in ["", "1", "2"]:
            break
    reuse_pg_mode = (pg_mode_choice == '2')
    
    # Get credentials
    print("\\n" + "-" * 70)
    print(" AUTHENTICATION")'''

    content, _ = find_and_replace(content, old_fc_end, new_fc_end, "PG mode toggle prompt")

    # --- PATCH J: PG reuse query during config building ---
    # After config dict is built with 'policy_group', inject the reuse query
    old_check_vpc_pg = '''        # Check if VPC policy group already exists
        if check_vpc_policy_group_exists(session, apic_url, config['policy_group']):'''

    new_check_vpc_pg = '''        # Policy group: reuse existing or create new
        if reuse_pg_mode:
            print(f"\\n  [PG REUSE] Querying existing VPC policy groups...")
            existing_pgs = query_existing_vpc_policy_groups(session, apic_url)
            print(f"  [PG REUSE] Found {len(existing_pgs)} VPC policy group(s)")
            selected_pg = display_policy_group_selection(
                existing_pgs, pg_type="vpc",
                link_level=config['link_level'], aep=aep
            )
            if selected_pg:
                config['policy_group'] = selected_pg
                config['reuse_policy_group'] = True
            else:
                print(f"  [INFO] Will create new policy group: {config['policy_group']}")
                config['reuse_policy_group'] = False
        
        # Check if VPC policy group already exists
        if not config.get('reuse_policy_group') and check_vpc_policy_group_exists(session, apic_url, config['policy_group']):'''

    content, _ = find_and_replace(content, old_check_vpc_pg, new_check_vpc_pg, "PG reuse query for VPC")

    # --- PATCH K: deploy_vpc step 2 — skip if reusing existing PG ---
    old_step2_full = '''    # 2. Create VPC Policy Group
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
        return results'''

    new_step2_full = '''    # 2. Create or reuse VPC Policy Group
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
            return results'''

    content, _ = find_and_replace(content, old_step2_full, new_step2_full, "deploy_vpc step 2: reuse or create")

    return content


def patch_individual_port_display(content):
    """Replace individual port query and display logic with new all-ports version."""

    # --- PATCH 0: Update import block if already patched with old imports ---
    import_patterns = [
        # v1.0 — no cleanup, no PG reuse
        """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, display_port_selection,
    get_validated_available_ports
)""",
        # v1.1 — cleanup but no PG reuse
        """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, display_port_selection,
    get_validated_available_ports,
    cleanup_port_for_redeployment
)""",
    ]

    current_ind_import = """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_interface,
    prompt_input, sort_port_key,
    get_all_ports_with_status, display_port_selection,
    get_validated_available_ports,
    cleanup_port_for_redeployment,
    query_existing_access_policy_groups, display_policy_group_selection
)"""

    for old_imp in import_patterns:
        if old_imp in content:
            content, _ = find_and_replace(content, old_imp, current_ind_import, "Individual: update imports")
            break

    # --- Patch the validation header text ---
    old_header = '''        print(f"  Querying and validating ports (checking 4 criteria)...")
        print(f"    1. Usage = \'discovery\'")
        print(f"    2. No description")
        print(f"    3. No policy group assigned")
        print(f"    4. No EPG deployed")'''

    new_header = '''        print(f"  Querying all ports and checking status...")
        print(f"    Criteria: discovery usage, no description, no policy group, no EPG")
        print(f"    [AVAIL] = passes all checks  |  [IN-USE] = has existing config")'''

    content, _ = find_and_replace(content, old_header, new_header, "Individual validation header")

    # --- Patch the port query calls ---
    old_query = '''        ports = get_validated_available_ports(session, apic_url, node_id)
        print(f"\\n  Found {len(ports)} validated available ports")
        
        if not ports:
            print(f"  [SKIP] No validated available ports")
            skipped += 1
            continue
        
        # Select port
        selected_port = display_validated_ports(ports, node_id)'''

    new_query = '''        all_ports = get_all_ports_with_status(session, apic_url, node_id, POD_ID)
        avail_count = sum(1 for p in all_ports if p['valid'])
        print(f"\\n  Found {len(all_ports)} total ports ({avail_count} available)")
        
        if not all_ports:
            print(f"  [SKIP] No ports found on node")
            skipped += 1
            continue
        
        # Select port (shows all with color coding)
        selected_port = display_port_selection(all_ports, f"node {node_id}", POD_ID)'''

    content, _ = find_and_replace(content, old_query, new_query, "Individual port query/display")

    # --- Fix variable rename: ports -> all_ports in preview call ---
    if "display_deployment_preview(config, all_profiles, all_link_levels, all_aeps, ports)" in content:
        content, _ = find_and_replace(
            content,
            "display_deployment_preview(config, all_profiles, all_link_levels, all_aeps, ports)",
            "display_deployment_preview(config, all_profiles, all_link_levels, all_aeps, all_ports)",
            "Individual: fix ports -> all_ports variable rename"
        )

    # --- Inject cleanup before deploy_individual_port() call ---
    old_indiv_deploy_call = '''            elif confirm in ['Y', 'YES']:
                # Deploy
                print("\\n  Deploying..." if not dry_run else "\\n  Dry-run...")
                results = deploy_individual_port(session, apic_url, config, dry_run)'''

    new_indiv_deploy_call = '''            elif confirm in ['Y', 'YES']:
                # Deploy
                print("\\n  Deploying..." if not dry_run else "\\n  Dry-run...")
                
                # Full cleanup if overriding an in-use port
                if not dry_run and not selected_port.get('valid', True):
                    print("\\n  [CLEANUP] Wiping existing port configuration...")
                    cleanup_results = cleanup_port_for_redeployment(
                        session, apic_url, config['node_id'], config['interface'],
                        config['interface_profile'], POD_ID
                    )
                    print(f"  [CLEANUP] Done: {cleanup_results['bindings_deleted']} binding(s) removed, "
                          f"selector: {'removed' if cleanup_results['selector_deleted'] else 'n/a'}, "
                          f"description: {'cleared' if cleanup_results['description_cleared'] else 'n/a'}")
                    print()
                
                results = deploy_individual_port(session, apic_url, config, dry_run)'''

    content, _ = find_and_replace(content, old_indiv_deploy_call, new_indiv_deploy_call, "Individual cleanup injection")

    # --- PATCH I-ind: PG Mode toggle after Run Mode selection ---
    old_ind_auth = '''    # Get credentials
    print("\\n" + "-" * 70)
    print(" AUTHENTICATION")
    print("-" * 70)
    username = prompt_input("\\nUsername: ").strip()'''

    new_ind_auth = '''    # Policy Group Mode
    print("\\n" + "-" * 70)
    print(" POLICY GROUP MODE")
    print("-" * 70)
    print("\\n  [1] Create NEW policy group per deployment (default)")
    print("  [2] Reuse EXISTING policy group (query by link level)")
    
    while True:
        pg_mode_choice = prompt_input("\\nSelect (1/2) [default=1]: ").strip()
        if pg_mode_choice in ["", "1", "2"]:
            break
    reuse_pg_mode = (pg_mode_choice == '2')
    
    # Get credentials
    print("\\n" + "-" * 70)
    print(" AUTHENTICATION")
    print("-" * 70)
    username = prompt_input("\\nUsername: ").strip()'''

    content, _ = find_and_replace(content, old_ind_auth, new_ind_auth, "Individual: PG mode toggle")

    # --- PATCH J-ind: PG reuse query during config building ---
    old_check_ind_pg = '''        # Check if policy group already exists
        if check_policy_group_exists(session, apic_url, policy_group_name):
            print(f"\\n  [WARNING] Policy group '{policy_group_name}' already exists")
            use_existing = prompt_input("  Use existing policy group? (yes/no): ").strip().lower()
            if use_existing not in ['yes', 'y']:
                print(f"  [SKIP] Policy group already exists")
                skipped += 1
                continue'''

    new_check_ind_pg = '''        # Policy group: reuse existing or create new
        reuse_this_pg = False
        if reuse_pg_mode:
            print(f"\\n  [PG REUSE] Querying existing access policy groups...")
            existing_pgs = query_existing_access_policy_groups(session, apic_url)
            print(f"  [PG REUSE] Found {len(existing_pgs)} access policy group(s)")
            selected_pg = display_policy_group_selection(
                existing_pgs, pg_type="access",
                link_level=link_level, aep=aep
            )
            if selected_pg:
                policy_group_name = selected_pg
                reuse_this_pg = True
            else:
                print(f"  [INFO] Will create new policy group: {policy_group_name}")
        
        # Check if policy group already exists (only when creating new)
        if not reuse_this_pg and check_policy_group_exists(session, apic_url, policy_group_name):
            print(f"\\n  [WARNING] Policy group '{policy_group_name}' already exists")
            use_existing = prompt_input("  Use existing policy group? (yes/no): ").strip().lower()
            if use_existing not in ['yes', 'y']:
                print(f"  [SKIP] Policy group already exists")
                skipped += 1
                continue'''

    content, _ = find_and_replace(content, old_check_ind_pg, new_check_ind_pg, "Individual: PG reuse query")

    # Need to store reuse flag in config after it's built
    old_ind_config_epg = '''            "epg_bindings": epg_bindings
        }'''

    new_ind_config_epg = '''            "epg_bindings": epg_bindings,
            "reuse_policy_group": reuse_this_pg
        }'''

    content, _ = find_and_replace(content, old_ind_config_epg, new_ind_config_epg, "Individual: store reuse flag in config")

    # --- PATCH K-ind: deploy_individual_port step 2 — skip if reusing ---
    old_ind_step2 = '''    # Step 2: Create Leaf Access Port Policy Group
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
        return results'''

    new_ind_step2 = '''    # Step 2: Create or reuse Leaf Access Port Policy Group
    if config.get('reuse_policy_group'):
        print(f"  [2/4] Using EXISTING Policy Group: {config['policy_group_name']}")
        print(f"        [REUSE] Skipping creation")
        results["policy_group"] = True
    else:
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
            return results'''

    content, _ = find_and_replace(content, old_ind_step2, new_ind_step2, "Individual: step 2 reuse or create")

    return content


# =============================================================================
# EPG ADD PATCHES
# =============================================================================

def patch_epg_add(content):
    """Patch EPG Add script: multi-port CSV expansion + overwrite mode."""

    # --- PATCH 0: Update import block if already patched with old imports ---
    old_epg_import = """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_port,
    prompt_input
)"""

    new_epg_import = """from aci_port_utils import (
    detect_environment, extract_node_id, parse_vlans, parse_port,
    parse_ports, prompt_input,
    query_all_bindings_on_port, delete_all_bindings_on_port
)"""

    if old_epg_import in content:
        content, _ = find_and_replace(content, old_epg_import, new_epg_import, "EPG Add: update imports")

    # --- PATCH A: Multi-port CSV loader ---
    # Replace load_epg_add_csv to expand "1/67, 1/68, 1/69" in PORT column
    old_csv_loader = '''def load_epg_add_csv(filename):
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
        return None'''

    new_csv_loader = '''def load_epg_add_csv(filename):
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
        return None'''

    content, _ = find_and_replace(content, old_csv_loader, new_csv_loader, "EPG Add: multi-port CSV loader")

    # --- PATCH B: Info line after loading showing expansion ---
    old_loaded = '''    print(f"[INFO] Loaded {len(deployments)} deployment(s)")
    
    # Select run mode'''

    new_loaded = '''    print(f"[INFO] Loaded {len(deployments)} deployment(s)")
    
    # Show unique switch+port combos vs total rows (multi-port expansion)
    unique_ports = set((d['switch'], d['port']) for d in deployments)
    if len(unique_ports) != len(deployments):
        ports_per = len(deployments) / max(len(unique_ports), 1)
        print(f"       ({len(unique_ports)} unique switch+port combos, avg {ports_per:.0f} VLANs each)")
    
    # Select run mode'''

    content, _ = find_and_replace(content, old_loaded, new_loaded, "EPG Add: expansion info")

    # --- PATCH C: EPG Mode toggle after binding mode ---
    old_post_binding = '''    # Get credentials
    print("\\n" + "-" * 70)
    print(" AUTHENTICATION")
    print("-" * 70)
    sys.stdout.write("\\nUsername: ")
    sys.stdout.flush()
    username = input().strip()'''

    new_post_binding = '''    # EPG Mode: Add or Overwrite
    print("\\n" + "-" * 70)
    print(" EPG MODE")
    print("-" * 70)
    print("\\n  [1] Add - Add new EPG bindings (keep existing)")
    print("  [2] Overwrite - Delete ALL existing bindings first, then add new")
    print("                  (clean replacement of port EPGs)")
    
    while True:
        sys.stdout.write("\\nSelect mode (1/2) [default=1]: ")
        sys.stdout.flush()
        epg_mode_choice = input().strip()
        if epg_mode_choice in ["", "1", "2"]:
            break
    overwrite_mode = (epg_mode_choice == '2')
    
    if overwrite_mode:
        print("\\n  [WARNING] Overwrite mode: ALL existing EPG bindings on each port")
        print("            will be DELETED before adding the new ones.")
    
    # Get credentials
    print("\\n" + "-" * 70)
    print(" AUTHENTICATION")
    print("-" * 70)
    sys.stdout.write("\\nUsername: ")
    sys.stdout.flush()
    username = input().strip()'''

    content, _ = find_and_replace(content, old_post_binding, new_post_binding, "EPG Add: overwrite mode toggle")

    # --- PATCH D: Inject overwrite deletion before Phase 4 deployment ---
    old_phase4_deploy = '''    # Deploy
    print("\\n[INFO] Deploying bindings...")
    
    success_count = 0
    fail_count = 0
    
    for b in new_bindings:'''

    new_phase4_deploy = '''    # Overwrite mode: delete existing bindings first
    overwrite_deleted = 0
    if overwrite_mode:
        print("\\n[INFO] Overwrite mode — removing existing EPG bindings first...")
        
        # Build unique set of switch+port combinations
        ports_to_clean = set()
        for b in all_bindings:
            ports_to_clean.add((b['switch'], b['port'], b['node_id'], b['env']))
        
        for switch, port, node_id, env in sorted(ports_to_clean):
            if env not in sessions:
                continue
            session = sessions[env]
            apic_url = APIC_URLS[env]
            
            # Query all existing bindings on this port
            existing = query_all_bindings_on_port(session, apic_url, node_id, port, POD_ID)
            if existing:
                print(f"  {switch} port {port}: {len(existing)} existing binding(s)")
                del_ok, del_fail, del_details = delete_all_bindings_on_port(
                    session, apic_url, node_id, port, POD_ID
                )
                for d in del_details:
                    status = "[DELETED]" if d['success'] else "[FAIL]"
                    print(f"    {status} VLAN {d['vlan']} ({d['epg']})")
                overwrite_deleted += del_ok
            else:
                print(f"  {switch} port {port}: no existing bindings")
        
        print(f"\\n[INFO] Overwrite cleanup done: {overwrite_deleted} binding(s) removed")
    
    # Deploy new bindings
    print("\\n[INFO] Deploying bindings...")
    
    success_count = 0
    fail_count = 0
    
    # In overwrite mode, deploy ALL bindings (not just "new" ones since we just wiped the port)
    deploy_list = all_bindings if overwrite_mode else new_bindings
    
    for b in deploy_list:'''

    content, _ = find_and_replace(content, old_phase4_deploy, new_phase4_deploy, "EPG Add: overwrite deletion + deploy list")

    # --- PATCH E: Update Phase 3 preview to show overwrite info ---
    old_preview_summary = '''    print(f"\\n  Total bindings: {len(all_bindings)}")
    print(f"  New bindings:   {len(new_bindings)}")
    print(f"  Already exist:  {len(existing_bindings)} (will be skipped)")'''

    new_preview_summary = '''    print(f"\\n  Total bindings: {len(all_bindings)}")
    print(f"  New bindings:   {len(new_bindings)}")
    print(f"  Already exist:  {len(existing_bindings)}" + 
          (" (will be RE-DEPLOYED after wipe)" if overwrite_mode else " (will be skipped)"))
    if overwrite_mode:
        unique_ports = set((b['switch'], b['port']) for b in all_bindings)
        print(f"  [OVERWRITE] {len(unique_ports)} port(s) will have ALL existing bindings wiped first")'''

    content, _ = find_and_replace(content, old_preview_summary, new_preview_summary, "EPG Add: preview overwrite info")

    # --- PATCH F: Update final summary for overwrite ---
    old_summary = '''    print(f"\\n  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    print(f"  Skipped: {len(existing_bindings)} (already existed)")'''

    new_summary = '''    print(f"\\n  Success: {success_count}")
    print(f"  Failed:  {fail_count}")
    if overwrite_mode:
        print(f"  Wiped:   {overwrite_deleted} (existing bindings removed)")
    else:
        print(f"  Skipped: {len(existing_bindings)} (already existed)")'''

    content, _ = find_and_replace(content, old_summary, new_summary, "EPG Add: final summary with overwrite count")

    # --- PATCH G: Fix deploy confirmation to show correct count ---
    old_confirm = '''    print(f"\\nReady to deploy {len(new_bindings)} binding(s)")'''

    new_confirm = '''    deploy_count = len(all_bindings) if overwrite_mode else len(new_bindings)
    if overwrite_mode:
        print(f"\\nReady to OVERWRITE: wipe existing + deploy {deploy_count} binding(s)")
    else:
        print(f"\\nReady to deploy {len(new_bindings)} binding(s)")'''

    content, _ = find_and_replace(content, old_confirm, new_confirm, "EPG Add: confirm count for overwrite")

    return content


# =============================================================================
# WEB APP PATCH
# =============================================================================

def patch_deployment_app(content):
    """Patch aci_deployment_app.py with CSS and JavaScript changes."""

    # --- PATCH 1: CSS — Add port status styles after .bracket-num ---
    css_anchor = ".bracket-num{color:var(--accent-blue)!important;font-weight:700}"
    css_addition = """.bracket-num{color:var(--accent-blue)!important;font-weight:700}
.port-avail{color:var(--accent-green)!important;font-weight:700}
.port-inuse{color:var(--accent-red)!important;font-weight:700}
.terminal-line.port-available{background:rgba(63,185,80,.06);border-left:3px solid var(--accent-green);padding-left:8px}
.terminal-line.port-in-use{background:rgba(248,81,73,.06);border-left:3px solid var(--accent-red);padding-left:8px}"""

    content, _ = find_and_replace(content, css_anchor, css_addition, "CSS port status styles")

    # --- PATCH 2: JS addLine() — Add AVAIL/IN-USE detection before existing tags ---
    old_detection = """if(tu.includes('[FOUND]')||tu.includes('[SUCCESS]')||tu.includes('[OK]')||tu.includes('[CREATED]')||tu.includes('[DEPLOYED]'))lineType='success';"""
    new_detection = """if(tu.includes('[AVAIL]'))lineType='port-available';
    else if(tu.includes('[IN-USE]'))lineType='port-in-use';
    else if(tu.includes('[FOUND]')||tu.includes('[SUCCESS]')||tu.includes('[OK]')||tu.includes('[CREATED]')||tu.includes('[DEPLOYED]'))lineType='success';"""

    content, _ = find_and_replace(content, old_detection, new_detection, "JS addLine() tag detection")

    # --- PATCH 3: JS — Update the lineType includes check for HTML highlighting ---
    old_includes = "if(['success','error','warning','info','credential'].includes(lineType)){"
    new_includes = "if(['success','error','warning','info','credential','port-available','port-in-use'].includes(lineType)){"

    content, _ = find_and_replace(content, old_includes, new_includes, "JS lineType includes check")

    # --- PATCH 4: JS — Add AVAIL/IN-USE/FAIL tag highlighting in regex chain ---
    # Find the first .replace in the highlighting chain and prepend our new ones
    old_first_replace = """.replace(/\\[(FOUND|SUCCESS|OK|CREATED|DEPLOYED)\\]/gi,'<span style="color:var(--accent-green);font-weight:600">[$1]</span>')"""
    new_first_replace = """.replace(/\\[(AVAIL)\\]/gi,'<span class="port-avail">[$1]</span>')
      .replace(/\\[(IN-USE)\\]/gi,'<span class="port-inuse">[$1]</span>')
      .replace(/\\[(FOUND|SUCCESS|OK|CREATED|DEPLOYED)\\]/gi,'<span style="color:var(--accent-green);font-weight:600">[$1]</span>')"""

    content, _ = find_and_replace(content, old_first_replace, new_first_replace, "JS AVAIL/IN-USE highlighting")

    # --- PATCH 5: JS — Add FAIL/OVERRIDE/CANCELLED tags after existing chain ---
    old_auto_replace = """.replace(/\\[(AUTO)\\]/gi,'<span style="color:#ffd200;font-weight:600">[$1]</span>');"""
    new_auto_replace = """.replace(/\\[(AUTO)\\]/gi,'<span style="color:#ffd200;font-weight:600">[$1]</span>')
      .replace(/\\[(FAIL)\\]/gi,'<span style="color:var(--accent-red);font-weight:600">[$1]</span>')
      .replace(/\\[(OVERRIDE)\\]/gi,'<span style="color:var(--accent-orange);font-weight:600">[$1]</span>')
      .replace(/\\[(CANCELLED)\\]/gi,'<span style="color:var(--accent-orange);font-weight:600">[$1]</span>');"""

    content, _ = find_and_replace(content, old_auto_replace, new_auto_replace, "JS FAIL/OVERRIDE tags")

    # --- PATCH 6: Update CSV requirements for epgdelete (VLANS now optional) ---
    old_csv_req = '"epgdelete": {"required": ["SWITCH", "PORT", "VLANS"]'
    new_csv_req = '"epgdelete": {"required": ["SWITCH", "PORT"]'
    content, _ = find_and_replace(content, old_csv_req, new_csv_req, "EPG Delete CSV: VLANS now optional")

    return content


# =============================================================================
# MAIN PATCHER
# =============================================================================

def patch_script(filepath, import_block, funcs_to_remove, port_patcher=None, label=""):
    """Apply all patches to a deployment script."""
    if not os.path.exists(filepath):
        print(f"\n  [NOT FOUND] {filepath}")
        return False

    print(f"\n{'='*70}")
    print(f"  Patching: {os.path.basename(filepath)}  ({label})")
    print(f"{'='*70}")

    content = read_file(filepath)
    original = content

    # Check if already patched
    if "from aci_port_utils import" in content:
        print(f"  [SKIP] Already patched (import found)")
        # Still apply port display patches if needed
        if port_patcher:
            content = port_patcher(content)
            if content != original:
                if not DRY_RUN:
                    if BACKUP:
                        backup_file(filepath)
                    write_file(filepath, content)
                print(f"  [DONE] Port display patches applied")
            return True
        return True

    # Step 1: Inject import
    content = inject_import(content, import_block, os.path.basename(filepath))

    # Step 2: Remove duplicated functions
    for func_name in funcs_to_remove:
        content, _ = find_and_delete_function(content, func_name)

    # Step 3: Apply port display patches
    if port_patcher:
        content = port_patcher(content)

    # Step 4: Write
    if content != original:
        if not DRY_RUN:
            if BACKUP:
                backup_file(filepath)
            write_file(filepath, content)
        changes = sum(1 for a, b in zip(original.splitlines(), content.splitlines()) if a != b)
        print(f"\n  [DONE] ~{changes} line(s) changed")
    else:
        print(f"\n  [NO CHANGES] File unchanged")

    return True


def main():
    print("\n" + "=" * 70)
    print(" ACI INTEGRATION PATCHER")
    print("=" * 70)

    if DRY_RUN:
        print("\n  *** DRY RUN MODE — no files will be modified ***\n")
    else:
        print(f"\n  Backups: {'enabled' if BACKUP else 'disabled'}")
        print()

    # Check aci_port_utils.py exists
    utils_path = os.path.join(BASE_DIR, "aci_port_utils.py")
    if not os.path.exists(utils_path):
        print(f"[ERROR] aci_port_utils.py not found in {BASE_DIR}")
        print(f"        Place it in the same directory as this script and your deployment scripts.")
        sys.exit(1)
    print(f"  [OK] aci_port_utils.py found")

    # Patch each script
    results = {}

    # 1. VPC Deploy
    results['vpc'] = patch_script(
        os.path.join(BASE_DIR, "aci_bulk_vpc_deploy.py"),
        VPC_IMPORT,
        VPC_FUNCS_TO_REMOVE,
        port_patcher=patch_vpc_port_display,
        label="VPC Deploy"
    )

    # 2. Individual Deploy
    results['individual'] = patch_script(
        os.path.join(BASE_DIR, "aci_bulk_individual_deploy.py"),
        INDIVIDUAL_IMPORT,
        INDIVIDUAL_FUNCS_TO_REMOVE,
        port_patcher=patch_individual_port_display,
        label="Static Port Deploy"
    )

    # 3. EPG Add
    results['epgadd'] = patch_script(
        os.path.join(BASE_DIR, "aci_bulk_epg_add.py"),
        EPGADD_IMPORT,
        EPGADD_FUNCS_TO_REMOVE,
        port_patcher=patch_epg_add,
        label="EPG Add"
    )

    # 4. EPG Delete — replaced entirely, just verify it exists
    epg_delete_path = os.path.join(BASE_DIR, "aci_bulk_epg_delete.py")
    print(f"\n{'='*70}")
    print(f"  Checking: aci_bulk_epg_delete.py  (Full Replacement)")
    print(f"{'='*70}")
    if os.path.exists(epg_delete_path):
        content = read_file(epg_delete_path)
        if "query_all_bindings_on_port" in content:
            print(f"  [OK] Already updated to v2.0 (query-and-select)")
        else:
            print(f"  [ACTION NEEDED] Replace with the new aci_bulk_epg_delete.py")
            print(f"  The new version includes query-and-select mode.")
            if BACKUP:
                backup_file(epg_delete_path)
    else:
        print(f"  [NOT FOUND] Place the new aci_bulk_epg_delete.py in {BASE_DIR}")

    # 5. Web App
    app_path = os.path.join(BASE_DIR, "aci_deployment_app.py")
    if os.path.exists(app_path):
        print(f"\n{'='*70}")
        print(f"  Patching: aci_deployment_app.py  (Web UI)")
        print(f"{'='*70}")

        content = read_file(app_path)
        original = content

        if ".port-avail" in content:
            print(f"  [SKIP] Already patched (CSS found)")
        else:
            content = patch_deployment_app(content)

            if content != original:
                if not DRY_RUN:
                    if BACKUP:
                        backup_file(app_path)
                    write_file(app_path, content)
                print(f"\n  [DONE] Web UI patched")
            else:
                print(f"\n  [NO CHANGES]")
    else:
        print(f"\n  [NOT FOUND] aci_deployment_app.py")

    # Summary
    print(f"\n{'='*70}")
    print(f" PATCH SUMMARY")
    print(f"{'='*70}")
    for name, ok in results.items():
        status = "✓ patched" if ok else "✗ not found"
        print(f"  {name:<15} {status}")
    print(f"  {'epgdelete':<15} → replace with new v2.0 file")
    print(f"  {'webapp':<15} {'✓ patched' if os.path.exists(app_path) else '✗ not found'}")

    if DRY_RUN:
        print(f"\n  *** DRY RUN — no files were modified ***")
        print(f"  Run without --dry-run to apply changes.")
    else:
        print(f"\n  All patches applied. Backup files saved as .bak")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    main()
