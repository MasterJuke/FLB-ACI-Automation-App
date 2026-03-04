#!/usr/bin/env python3
"""
ACI Port Utilities - Shared Module
====================================
Consolidates common helper functions and port query/display logic
used across all ACI deployment scripts.

Features:
- Shared helpers: detect_environment, extract_node_id, parse_vlans, etc.
- Multi-port CSV expansion: parse_ports("1/67, 1/68, 1/69") -> 3 entries
- Full port inventory query (ALL ports, not just available)
- Color-coded port display: green [AVAIL] / red [IN-USE]
- ANSI colors for CLI + bracket tags for web UI parsing
- In-use port warning with existing config details + override confirm
- Full port cleanup for redeployment (wipe bindings, selector, description)
- VPC common port matching across switch pairs
- Asymmetric VPC port selection (different port per switch)
- Existing policy group query & reuse (query by link level + AEP)
- Port binding query & overwrite (query/delete all fvRsPathAtt on a port)

CCIE Automation Exam Relevance:
- ACI REST API queries (l1PhysIf, infraPortBlk, fvRsPathAtt)
- Subtree queries with rsp-subtree=children for policy group introspection
- Class-level reverse lookups (fvRsPathAtt filtered by tDn)
- Modular code design / reusable libraries
- Parallel API calls with ThreadPoolExecutor
- Infrastructure state validation before deployment

Usage:
    from aci_port_utils import (
        detect_environment, extract_node_id, parse_vlans, parse_port,
        parse_ports, prompt_input,
        get_all_ports_with_status, display_port_selection,
        find_common_ports_with_status, display_vpc_port_selection,
        display_vpc_independent_port_selection,
        cleanup_port_for_redeployment, cleanup_vpc_port_for_redeployment,
        query_existing_access_policy_groups, query_existing_vpc_policy_groups,
        display_policy_group_selection,
        query_all_bindings_on_port, delete_all_bindings_on_port
    )

Author: Network Automation
Version: 1.4.0 — Merged dual-strategy query (always runs both, deduplicates)
"""

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed


# =============================================================================
# ANSI COLOR CODES (for CLI display)
# =============================================================================

class Colors:
    """ANSI escape codes for terminal coloring."""
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    @staticmethod
    def enabled():
        """Check if color output should be used (disabled in web UI)."""
        return os.environ.get('ACI_WEB_UI', '') != '1'


def colored(text, color_code):
    """Apply ANSI color if running in CLI mode."""
    if Colors.enabled():
        return f"{color_code}{text}{Colors.RESET}"
    return text


# =============================================================================
# SHARED HELPER FUNCTIONS
# =============================================================================

def prompt_input(prompt_text):
    """Print prompt and get input - ensures prompt is visible in web UI."""
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    return input()


def detect_environment(switch_name):
    """
    Detect data center from switch name.
    D3 = NSM, D2 = SDC, D1 = everything else (ACC, etc.)
    """
    switch_upper = switch_name.upper()
    if "NSM" in switch_upper:
        return "D3"
    elif "SDC" in switch_upper:
        return "D2"
    else:
        return "D1"


def extract_node_id(switch_name):
    """Extract node ID from switch name (trailing digits)."""
    match = re.search(r'(\d+)$', switch_name)
    return match.group(1) if match else None


def parse_vlans(vlan_string):
    """Parse VLAN string into sorted list of unique integers.

    Handles: "32,64-67,92" -> [32, 64, 65, 66, 67, 92]
    """
    vlans = []
    vlan_string = str(vlan_string).replace(" ", "").strip('"').strip("'")
    for part in vlan_string.split(","):
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                vlans.extend(range(int(start), int(end) + 1))
            except (ValueError, TypeError):
                pass
        else:
            try:
                vlans.append(int(part))
            except (ValueError, TypeError):
                pass
    return sorted(set(vlans))


def parse_port(port_string):
    """Parse port string to standard format.

    '1/68' or 'eth1/68' or 'ethernet1/68' -> '1/68'
    """
    port_string = str(port_string).strip().lower()
    port_string = port_string.replace("ethernet", "").replace("eth", "")
    if "/" in port_string:
        return port_string
    return f"1/{port_string}"


def parse_ports(port_string):
    """
    Parse a comma-separated port string into a list of individual ports.

    Handles multi-port CSV entries like "1/67, 1/68, 1/69" or "eth1/67,eth1/68".
    Each port is normalized through parse_port().

    CCIE Automation Note:
    This is a CSV pre-processing pattern. In ACI, each fvRsPathAtt is a
    separate relationship object on a unique path DN — there's no concept
    of a multi-port binding. So "1/67, 1/68" in a CSV must expand to
    separate API calls, each targeting a different pathep DN.

    Returns:
        List of normalized port strings, e.g. ['1/67', '1/68', '1/69']
    """
    if not port_string or not str(port_string).strip():
        return []

    raw = str(port_string).strip()
    # Split on commas, normalize each piece
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return [parse_port(p) for p in parts]


def parse_interface(port_string):
    """Parse full port DN to interface format.

    'eth1/68' -> '1/68'
    """
    port_string = str(port_string).strip().lower()
    port_string = port_string.replace("ethernet", "").replace("eth", "")
    if "/" in port_string:
        return port_string
    return None


def sort_port_key(port):
    """Generate sort key for port ordering (module/number)."""
    port_str = port.get('port', '') if isinstance(port, dict) else str(port)
    mod_match = re.search(r'eth(\d+)/', port_str)
    num_match = re.search(r'/(\d+)$', port_str)
    return (
        int(mod_match.group(1)) if mod_match else 0,
        int(num_match.group(1)) if num_match else 0
    )


# =============================================================================
# PORT STATUS QUERY FUNCTIONS
# =============================================================================

def _validate_single_port(session, apic_url, node_id, port, pod_id="1"):
    """
    Validate a single port for policy group and EPG bindings.
    Enriches the port dict with validation results.

    Checks:
      3. Policy group assigned (infraPortBlk)
      4. EPG bindings deployed (fvRsPathAtt)

    Note: Checks 1 (usage) and 2 (description) are done during initial query.
    """
    port_num = port['interface'].split('/')[-1]
    issues = list(port.get('issues', []))
    details = dict(port.get('config_details', {}))

    # --- Check 3: Policy group (port selector) ---
    try:
        url = (f"{apic_url}/api/class/infraPortBlk.json?"
               f"query-target-filter=and("
               f"eq(infraPortBlk.fromPort,\"{port_num}\"),"
               f"eq(infraPortBlk.toPort,\"{port_num}\"))")
        response = session.get(url, verify=False, timeout=15)
        if response.status_code == 200:
            data = response.json().get("imdata", [])
            for item in data:
                dn = item.get("infraPortBlk", {}).get("attributes", {}).get("dn", "")
                if node_id in dn:
                    issues.append("Policy group assigned")
                    # Extract policy group name from DN for detail display
                    pg_match = re.search(r'hports-([^-]+)-', dn)
                    if pg_match:
                        details['port_selector'] = pg_match.group(1)
                    break
    except Exception:
        pass

    # --- Check 4: EPG bindings ---
    try:
        eth_iface = f"eth{port['interface']}"
        path_dn = f"topology/pod-{pod_id}/paths-{node_id}/pathep-[{eth_iface}]"
        url = (f"{apic_url}/api/class/fvRsPathAtt.json?"
               f"query-target-filter=eq(fvRsPathAtt.tDn,\"{path_dn}\")")
        response = session.get(url, verify=False, timeout=15)
        if response.status_code == 200:
            data = response.json().get("imdata", [])
            if data:
                issues.append("EPG deployed")
                # Collect bound EPG names for detail display
                epg_names = []
                for item in data:
                    dn = item.get("fvRsPathAtt", {}).get("attributes", {}).get("dn", "")
                    epg_match = re.search(r'/epg-([^/]+)/', dn)
                    if epg_match:
                        epg_names.append(epg_match.group(1))
                if epg_names:
                    details['epg_bindings'] = epg_names
    except Exception:
        pass

    port['issues'] = issues
    port['config_details'] = details
    port['valid'] = len(issues) == 0
    return port


def get_all_ports_with_status(session, apic_url, node_id, pod_id="1"):
    """
    Query ALL physical ports on a node and validate each against 4 criteria.

    Returns a list of port dicts, each containing:
      - port: full port name (e.g., 'eth1/68')
      - interface: short form (e.g., '1/68')
      - speed: port speed
      - admin_state: up/down
      - usage: discovery/epg/blacklist etc.
      - description: port description (if any)
      - valid: True if passes all 4 checks
      - issues: list of failed check descriptions
      - config_details: dict with existing config info (for warning display)

    Unlike get_validated_available_ports(), this returns ALL ports —
    available ones are marked valid=True, in-use ones are valid=False
    with their issues listed.
    """
    url = f"{apic_url}/api/class/topology/pod-{pod_id}/node-{node_id}/l1PhysIf.json"

    try:
        response = session.get(url, verify=False, timeout=60)
        if response.status_code != 200:
            print(f"    [ERROR] Failed to query ports: HTTP {response.status_code}")
            return []

        ports = []
        for item in response.json().get("imdata", []):
            attrs = item.get("l1PhysIf", {}).get("attributes", {})

            usage = attrs.get("usage", "").lower()
            description = attrs.get("descr", "").strip()
            admin_state = attrs.get("adminSt", "")
            oper_speed = attrs.get("speed", "inherit")

            # Extract port from DN
            dn = attrs.get("dn", "")
            port_match = re.search(r'phys-\[(.+?)\]', dn)
            if not port_match:
                continue

            port_name = port_match.group(1)
            interface = parse_interface(port_name)
            if not interface:
                continue

            # Skip non-ethernet ports (e.g., mgmt0)
            if not re.match(r'eth\d+/', port_name):
                continue

            # Build initial issues from checks 1 & 2
            issues = []
            config_details = {}

            if admin_state != "up":
                issues.append(f"Admin state: {admin_state}")
                config_details['admin_state'] = admin_state

            if usage != "discovery":
                issues.append(f"Usage: {usage}")
                config_details['usage'] = usage

            if description:
                issues.append(f"Description: {description}")
                config_details['description'] = description

            ports.append({
                "port": port_name,
                "interface": interface,
                "speed": oper_speed,
                "admin_state": admin_state,
                "usage": usage,
                "description": description,
                "valid": len(issues) == 0,  # Preliminary — updated after checks 3 & 4
                "issues": issues,
                "config_details": config_details
            })

        # Sort ports by module/number
        ports.sort(key=sort_port_key)

        # Validate checks 3 & 4 in parallel (policy group + EPG bindings)
        # Only validate ports that passed checks 1 & 2 OR have usage != "blacklist"
        # (blacklisted ports are fabric ports — skip parallel validation)
        ports_to_validate = [p for p in ports if p['usage'] != 'blacklist']
        ports_skip_validate = [p for p in ports if p['usage'] == 'blacklist']

        if ports_to_validate:
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_idx = {}
                for idx, port in enumerate(ports_to_validate):
                    future = executor.submit(
                        _validate_single_port, session, apic_url,
                        node_id, port.copy(), pod_id
                    )
                    future_to_idx[future] = idx

                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        ports_to_validate[idx] = future.result()
                    except Exception:
                        pass

        # Recombine and sort
        all_ports = ports_to_validate + ports_skip_validate
        all_ports.sort(key=sort_port_key)

        return all_ports

    except Exception as e:
        print(f"    [ERROR] Failed to query ports: {e}")
        return []


# =============================================================================
# PORT DISPLAY FUNCTIONS
# =============================================================================

def _format_port_line(index, port, show_issues_inline=True):
    """
    Format a single port line with color coding.

    Green [AVAIL] for valid ports, Red [IN-USE] for invalid ports.
    ANSI colors for CLI, bracket tags for web UI parsing.
    """
    iface = f"eth{port['interface']}"
    speed = port.get('speed', 'inherit')

    if port['valid']:
        # Green available port
        tag = "[AVAIL]"
        if Colors.enabled():
            line = (f"  [{index:>3}] "
                    f"{Colors.GREEN}{tag}{Colors.RESET} "
                    f"{iface:<15} {speed:<10}")
        else:
            line = f"  [{index:>3}] {tag} {iface:<15} {speed:<10}"
    else:
        # Red in-use port
        tag = "[IN-USE]"
        issue_summary = "; ".join(port['issues'][:2])  # Show first 2 issues inline
        if len(port['issues']) > 2:
            issue_summary += f" (+{len(port['issues']) - 2} more)"

        if Colors.enabled():
            line = (f"  [{index:>3}] "
                    f"{Colors.RED}{tag}{Colors.RESET} "
                    f"{Colors.DIM}{iface:<15} {speed:<10}{Colors.RESET}")
            if show_issues_inline:
                line += f" {Colors.DIM}({issue_summary}){Colors.RESET}"
        else:
            line = f"  [{index:>3}] {tag} {iface:<15} {speed:<10}"
            if show_issues_inline:
                line += f" ({issue_summary})"

    return line


def _display_in_use_warning(port, node_label):
    """
    Display detailed warning when user selects an in-use port.
    Shows existing configuration details and asks for confirmation.
    """
    print(f"\n  {'='*60}")
    print(f"  [WARNING] Port eth{port['interface']} on {node_label} is IN-USE")
    print(f"  {'='*60}")

    # Show all issues
    print(f"\n  Failed Validation Checks:")
    for i, issue in enumerate(port['issues'], 1):
        if Colors.enabled():
            print(f"    {Colors.RED}✗{Colors.RESET} {issue}")
        else:
            print(f"    [FAIL] {issue}")

    # Show existing config details
    details = port.get('config_details', {})
    if details:
        print(f"\n  Existing Configuration:")
        if 'description' in details:
            print(f"    Description:    {details['description']}")
        if 'usage' in details:
            print(f"    Usage State:    {details['usage']}")
        if 'admin_state' in details:
            print(f"    Admin State:    {details['admin_state']}")
        if 'port_selector' in details:
            print(f"    Port Selector:  {details['port_selector']}")
        if 'epg_bindings' in details:
            epgs = details['epg_bindings']
            if len(epgs) <= 5:
                print(f"    EPG Bindings:   {', '.join(epgs)}")
            else:
                print(f"    EPG Bindings:   {', '.join(epgs[:5])} (+{len(epgs)-5} more)")

    print(f"\n  {'='*60}")
    print(f"  [OVERRIDE] Selecting this port will WIPE all existing config:")
    print(f"    • Delete ALL EPG static bindings on this port")
    print(f"    • Delete the existing port selector")
    print(f"    • Clear the port description")
    print(f"  Then deploy your new configuration from scratch.")
    print(f"  {'='*60}")

    confirm = prompt_input("\n  Wipe existing config and redeploy? (yes/no): ").strip().lower()
    return confirm in ['yes', 'y']


def display_port_selection(ports, node_label, pod_id="1"):
    """
    Display ALL ports with color-coded status and handle selection.

    Shows green [AVAIL] ports and red [IN-USE] ports.
    If user selects an in-use port, shows warning with config details.

    Args:
        ports: List of port dicts from get_all_ports_with_status()
        node_label: Display label (e.g., "node 2163" or "nodes 1501 & 1502")

    Returns:
        Selected port dict, "SKIP", or "QUIT"
    """
    if not ports:
        print(f"\n  [WARNING] No ports found on {node_label}")
        return None

    # Filter out blacklisted/fabric ports for display
    displayable = [p for p in ports if p.get('usage') != 'blacklist']

    avail_count = sum(1 for p in displayable if p['valid'])
    inuse_count = sum(1 for p in displayable if not p['valid'])

    print(f"\n  All ports on {node_label}:")
    print(f"  {colored(f'{avail_count} available', Colors.GREEN)}  |  "
          f"{colored(f'{inuse_count} in-use', Colors.RED)}")
    print("  " + "-" * 70)
    print(f"  {'#':>5}  {'Status':<10} {'Port':<15} {'Speed':<10} Details")
    print("  " + "-" * 70)

    for i, port in enumerate(displayable, 1):
        print(_format_port_line(i, port))

    print("  " + "-" * 70)
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
            if 0 <= idx < len(displayable):
                selected = displayable[idx]

                # If in-use, show warning and ask for confirmation
                if not selected['valid']:
                    if _display_in_use_warning(selected, node_label):
                        print(f"\n  [WARNING] Proceeding with in-use port eth{selected['interface']}")
                        return selected
                    else:
                        print(f"  [CANCELLED] Select a different port")
                        continue
                else:
                    return selected
        except ValueError:
            pass
        print("  [ERROR] Invalid selection")


# =============================================================================
# VPC-SPECIFIC PORT FUNCTIONS
# =============================================================================

def find_common_ports_with_status(ports1, ports2):
    """
    Find ports present on BOTH switches for VPC deployment.

    Unlike find_common_validated_ports(), this includes ALL common ports
    (both available and in-use) so they can be displayed with status.

    A port is "valid" for VPC only if it's valid on BOTH switches.
    If in-use on either switch, the combined issues are merged.
    """
    ports1_dict = {p['interface']: p for p in ports1}
    ports2_dict = {p['interface']: p for p in ports2}

    common_interfaces = set(ports1_dict.keys()) & set(ports2_dict.keys())

    common_ports = []
    for interface in common_interfaces:
        p1 = ports1_dict[interface]
        p2 = ports2_dict[interface]

        # Merge issues from both switches
        combined_issues = []
        combined_details = {}

        for issue in p1.get('issues', []):
            combined_issues.append(f"SW1: {issue}")
        for issue in p2.get('issues', []):
            combined_issues.append(f"SW2: {issue}")

        # Merge config details
        if p1.get('config_details'):
            for k, v in p1['config_details'].items():
                combined_details[f"sw1_{k}"] = v
        if p2.get('config_details'):
            for k, v in p2['config_details'].items():
                combined_details[f"sw2_{k}"] = v

        combined_port = p1.copy()
        combined_port['issues'] = combined_issues
        combined_port['config_details'] = combined_details
        combined_port['valid'] = (p1['valid'] and p2['valid'])
        combined_port['sw1_valid'] = p1['valid']
        combined_port['sw2_valid'] = p2['valid']

        common_ports.append(combined_port)

    common_ports.sort(key=sort_port_key)
    return common_ports


def _display_vpc_in_use_warning(port, node1, node2):
    """
    Display detailed warning for VPC port selection, showing issues per switch.
    """
    print(f"\n  {'='*60}")
    print(f"  [WARNING] Port eth{port['interface']} has issues on one or both switches")
    print(f"  {'='*60}")

    # Show issues grouped by switch
    sw1_issues = [i for i in port['issues'] if i.startswith("SW1:")]
    sw2_issues = [i for i in port['issues'] if i.startswith("SW2:")]

    if sw1_issues:
        print(f"\n  Node {node1} Issues:")
        for issue in sw1_issues:
            detail = issue.replace("SW1: ", "")
            if Colors.enabled():
                print(f"    {Colors.RED}✗{Colors.RESET} {detail}")
            else:
                print(f"    [FAIL] {detail}")

    if sw2_issues:
        print(f"\n  Node {node2} Issues:")
        for issue in sw2_issues:
            detail = issue.replace("SW2: ", "")
            if Colors.enabled():
                print(f"    {Colors.RED}✗{Colors.RESET} {detail}")
            else:
                print(f"    [FAIL] {detail}")

    # Show config details
    details = port.get('config_details', {})
    sw1_details = {k.replace('sw1_', ''): v for k, v in details.items() if k.startswith('sw1_')}
    sw2_details = {k.replace('sw2_', ''): v for k, v in details.items() if k.startswith('sw2_')}

    if sw1_details:
        print(f"\n  Node {node1} Existing Config:")
        for k, v in sw1_details.items():
            if k == 'epg_bindings' and isinstance(v, list):
                label = ', '.join(v[:5])
                if len(v) > 5:
                    label += f" (+{len(v)-5} more)"
                print(f"    {k.replace('_', ' ').title()}: {label}")
            else:
                print(f"    {k.replace('_', ' ').title()}: {v}")

    if sw2_details:
        print(f"\n  Node {node2} Existing Config:")
        for k, v in sw2_details.items():
            if k == 'epg_bindings' and isinstance(v, list):
                label = ', '.join(v[:5])
                if len(v) > 5:
                    label += f" (+{len(v)-5} more)"
                print(f"    {k.replace('_', ' ').title()}: {label}")
            else:
                print(f"    {k.replace('_', ' ').title()}: {v}")

    print(f"\n  {'='*60}")
    print(f"  [OVERRIDE] Selecting this port will WIPE all existing config")
    print(f"  on BOTH switches:")
    print(f"    • Delete ALL EPG static bindings (individual + VPC paths)")
    print(f"    • Delete the existing port selector")
    print(f"    • Clear port descriptions on both nodes")
    print(f"  Then deploy your new VPC configuration from scratch.")
    print(f"  {'='*60}")

    confirm = prompt_input("\n  Wipe existing config and redeploy? (yes/no): ").strip().lower()
    return confirm in ['yes', 'y']


def display_vpc_port_selection(ports, node1, node2):
    """
    Display ALL common ports across VPC switch pair with color-coded status.

    Args:
        ports: List from find_common_ports_with_status()
        node1: First switch node ID
        node2: Second switch node ID

    Returns:
        Selected port dict, "SKIP", or "QUIT"
    """
    if not ports:
        print(f"\n  [WARNING] No common ports found on nodes {node1} & {node2}")
        return None

    # Filter out blacklisted ports
    displayable = [p for p in ports if p.get('usage') != 'blacklist']

    avail_count = sum(1 for p in displayable if p['valid'])
    inuse_count = sum(1 for p in displayable if not p['valid'])

    print(f"\n  Common ports on nodes {node1} & {node2}:")
    print(f"  {colored(f'{avail_count} available', Colors.GREEN)}  |  "
          f"{colored(f'{inuse_count} in-use (on one or both)', Colors.RED)}")
    print("  " + "-" * 70)
    print(f"  {'#':>5}  {'Status':<10} {'Port':<15} {'Speed':<10} Details")
    print("  " + "-" * 70)

    for i, port in enumerate(displayable, 1):
        print(_format_port_line(i, port))

    print("  " + "-" * 70)
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
            if 0 <= idx < len(displayable):
                selected = displayable[idx]

                if not selected['valid']:
                    if _display_vpc_in_use_warning(selected, node1, node2):
                        print(f"\n  [WARNING] Proceeding with in-use port eth{selected['interface']}")
                        return selected
                    else:
                        print(f"  [CANCELLED] Select a different port")
                        continue
                else:
                    return selected
        except ValueError:
            pass
        print("  [ERROR] Invalid selection")


def display_vpc_independent_port_selection(ports1, ports2, node1, node2):
    """
    Display ports for each VPC switch independently and let user select
    different ports on each node.

    This supports asymmetric VPC cabling where each switch uses a different
    physical interface (e.g., node 1301 → 1/45, node 1302 → 1/46).

    ACI handles this fine — each switch gets its own port selector on the
    shared VPC interface profile, both pointing to the same VPC bundle PG.

    CCIE Automation Note:
    ACI's VPC model is a control-plane construct (fabricProtPol + fabricExplicitGEp).
    The physical port numbers on each switch are independent — they're mapped via
    separate infraHPortS objects to the same infraAccBndlGrp (VPC PG). This is
    different from traditional EtherChannel where both sides must agree on member ports.

    Returns:
        (port1_dict, port2_dict) — selected port for each switch
        (None, None) if user skips
    """
    def _select_port_for_node(ports, node_id, switch_label):
        """Show ports and let user pick one for a specific switch."""
        displayable = [p for p in ports if p.get('usage') != 'blacklist']
        avail = sum(1 for p in displayable if p['valid'])
        inuse = sum(1 for p in displayable if not p['valid'])

        print(f"\n  All ports on {switch_label} (node {node_id}):")
        print(f"  {colored(f'{avail} available', Colors.GREEN)}  |  "
              f"{colored(f'{inuse} in-use', Colors.RED)}")
        print("  " + "-" * 70)
        print(f"  {'#':>5}  {'Status':<10} {'Port':<15} {'Speed':<10} Details")
        print("  " + "-" * 70)

        for i, port in enumerate(displayable, 1):
            print(_format_port_line(i, port))

        print("  " + "-" * 70)
        print("  [S] Skip this deployment")

        while True:
            choice = prompt_input(f"\n  Select port for node {node_id}: ").strip().upper()
            if choice == 'S':
                return None
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(displayable):
                    selected = displayable[idx]
                    if not selected['valid']:
                        # In-use warning (individual switch context)
                        if _display_in_use_warning(selected, f"node {node_id}"):
                            print(f"\n  [OVERRIDE] Proceeding with in-use port eth{selected['interface']} on node {node_id}")
                            return selected
                        else:
                            print(f"  [CANCELLED] Select a different port")
                            continue
                    return selected
            except ValueError:
                pass
            print("  [ERROR] Invalid selection")

    print(f"\n  === INDEPENDENT PORT SELECTION (VPC Asymmetric) ===")
    print(f"  Select a port on each switch separately.")
    print(f"  The two ports can be different (e.g., 1/45 on one, 1/46 on the other).")

    # Switch 1
    port1 = _select_port_for_node(ports1, node1, f"Switch 1")
    if port1 is None:
        return None, None

    print(f"\n  ✓ Node {node1}: eth{port1['interface']} selected")

    # Switch 2
    port2 = _select_port_for_node(ports2, node2, f"Switch 2")
    if port2 is None:
        return None, None

    print(f"\n  ✓ Node {node2}: eth{port2['interface']} selected")

    # Summary
    if port1['interface'] == port2['interface']:
        print(f"\n  [INFO] Same port on both switches: eth{port1['interface']}")
    else:
        print(f"\n  [INFO] Asymmetric VPC ports:")
        print(f"    Node {node1}: eth{port1['interface']}")
        print(f"    Node {node2}: eth{port2['interface']}")

    return port1, port2



# When a user selects a red [IN-USE] port, these functions wipe all existing
# configuration so the fresh deployment proceeds with zero conflicts.
#
# CCIE Automation Note:
# This is the reverse of the deployment stack. Deployment creates objects
# bottom-up (PG → selector → bindings), cleanup deletes top-down
# (bindings → selector → PG). Understanding this ordering is critical
# for the ACI programmability exam — deleting a selector before its
# bindings would leave orphaned fvRsPathAtt objects in the fabric.
#
# The multi-tier rollback extends this: before deleting the old state,
# we capture it as structured [ROLLBACK:STATE] markers in stdout. The
# web UI parses these markers to build a rollback script that can both
# DELETE new objects AND RESTORE the previous configuration. This is
# analogous to ACI's config snapshot/rollback (configSnapshot +
# configImportP) but at the individual port level.

import json as _json

def emit_rollback_state(state_dict):
    """
    Print a structured state marker that the rollback generator can parse.
    
    Format: [ROLLBACK:STATE] {"type":"binding","vlan":"32",...}
    
    The web UI collects these markers from terminal output and uses them
    to generate restore actions in the rollback script.
    """
    print(f"[ROLLBACK:STATE] {_json.dumps(state_dict, separators=(',', ':'))}")


def capture_port_description(session, apic_url, node_id, port, pod_id="1"):
    """
    Query the current port description from l1PhysIf.
    Returns the description string, or empty string if none/error.
    """
    eth = f"eth{port}" if not port.startswith("eth") else port
    url = (f"{apic_url}/api/mo/topology/pod-{pod_id}/node-{node_id}"
           f"/sys/phys-[{eth}].json")
    try:
        r = session.get(url, verify=False, timeout=15)
        if r.status_code == 200:
            data = r.json().get("imdata", [])
            if data:
                return data[0].get("l1PhysIf", {}).get("attributes", {}).get("descr", "")
    except Exception:
        pass
    return ""


def capture_selector_policy_group(session, apic_url, interface_profile, selector_name):
    """
    Query the policy group DN that a port selector points to.
    Returns (pg_name, pg_type) where pg_type is 'vpc' or 'access', or (None, None).
    """
    url = (f"{apic_url}/api/mo/uni/infra/accportprof-{interface_profile}"
           f"/hports-{selector_name}-typ-range.json"
           f"?query-target=children&target-subtree-class=infraRsAccBaseGrp")
    try:
        r = session.get(url, verify=False, timeout=15)
        if r.status_code == 200:
            for child in r.json().get("imdata", []):
                tdn = child.get("infraRsAccBaseGrp", {}).get("attributes", {}).get("tDn", "")
                pg_match = re.search(r'accbundle-(.+)$', tdn)
                if pg_match:
                    return pg_match.group(1), "vpc"
                pg_match = re.search(r'accportgrp-(.+)$', tdn)
                if pg_match:
                    return pg_match.group(1), "access"
    except Exception:
        pass
    return None, None


def capture_and_emit_port_state(session, apic_url, node_id, interface,
                                 interface_profile, pod_id="1", node2=None):
    """
    Capture the full state of a port BEFORE cleanup/overwrite and emit
    [ROLLBACK:STATE] markers for each component.
    
    Captures: description, EPG bindings, selector name, policy group name.
    For VPC (node2 provided): captures both node descriptions and VPC bindings.
    
    Returns the captured state dict for optional local use.
    """
    state = {"bindings": [], "description": "", "description2": "",
             "selector": None, "policy_group": None, "pg_type": None}
    
    eth_iface = f"eth{interface}" if not interface.startswith("eth") else interface
    port_num = interface.split('/')[-1]
    
    # --- Capture description(s) ---
    desc = capture_port_description(session, apic_url, node_id, interface, pod_id)
    state["description"] = desc
    if desc:
        emit_rollback_state({
            "type": "description", "node": str(node_id),
            "port": interface, "value": desc
        })
    
    if node2:
        desc2 = capture_port_description(session, apic_url, node2, interface, pod_id)
        state["description2"] = desc2
        if desc2:
            emit_rollback_state({
                "type": "description", "node": str(node2),
                "port": interface, "value": desc2
            })
    
    # --- Capture EPG bindings (individual path) ---
    bindings = query_all_bindings_on_port(session, apic_url, node_id, interface, pod_id)
    for b in bindings:
        state["bindings"].append(b)
        emit_rollback_state({
            "type": "binding", "node": str(node_id), "port": interface,
            "vlan": str(b["vlan"]), "tenant": b["tenant"],
            "ap": b["app_profile"], "epg": b["epg"],
            "mode": b.get("mode", "regular"),
            "path_type": "individual"
        })
    
    # --- Capture VPC bindings (protpaths) if VPC ---
    if node2:
        n1, n2 = (str(node_id), str(node2)) if int(node_id) < int(node2) else (str(node2), str(node_id))
        # Find old VPC PG first to query protpath bindings
        try:
            url = (f"{apic_url}/api/class/infraPortBlk.json"
                   f"?query-target-filter=and("
                   f"eq(infraPortBlk.fromPort,\"{port_num}\"),"
                   f"eq(infraPortBlk.toPort,\"{port_num}\"))")
            resp = session.get(url, verify=False, timeout=30)
            if resp.status_code == 200:
                for item in resp.json().get("imdata", []):
                    dn = item.get("infraPortBlk", {}).get("attributes", {}).get("dn", "")
                    if interface_profile in dn:
                        sel_match = re.search(r'hports-(.+?)-typ-range', dn)
                        if sel_match:
                            selector_name = sel_match.group(1)
                            state["selector"] = selector_name
                            pg_name, pg_type = capture_selector_policy_group(
                                session, apic_url, interface_profile, selector_name)
                            if pg_name:
                                state["policy_group"] = pg_name
                                state["pg_type"] = pg_type
                                emit_rollback_state({
                                    "type": "selector", "name": selector_name,
                                    "profile": interface_profile, "port": interface,
                                    "policy_group": pg_name, "pg_type": pg_type or "vpc"
                                })
                                # Query VPC protpath bindings
                                vpc_path = f"topology/pod-{pod_id}/protpaths-{n1}-{n2}/pathep-[{pg_name}]"
                                vpc_url = (f"{apic_url}/api/class/fvRsPathAtt.json"
                                           f"?query-target-filter=eq(fvRsPathAtt.tDn,\"{vpc_path}\")")
                                vpc_resp = session.get(vpc_url, verify=False, timeout=30)
                                if vpc_resp.status_code == 200:
                                    for vitem in vpc_resp.json().get("imdata", []):
                                        attrs = vitem.get("fvRsPathAtt", {}).get("attributes", {})
                                        vdn = attrs.get("dn", "")
                                        encap = attrs.get("encap", "")
                                        mode = attrs.get("mode", "regular")
                                        tn_m = re.search(r'/tn-([^/]+)/', vdn)
                                        ap_m = re.search(r'/ap-([^/]+)/', vdn)
                                        epg_m = re.search(r'/epg-([^/]+)/', vdn)
                                        vlan_m = re.search(r'vlan-(\d+)', encap)
                                        if tn_m and ap_m and epg_m and vlan_m:
                                            emit_rollback_state({
                                                "type": "binding",
                                                "node": n1, "node2": n2,
                                                "port": interface,
                                                "vlan": vlan_m.group(1),
                                                "tenant": tn_m.group(1),
                                                "ap": ap_m.group(1),
                                                "epg": epg_m.group(1),
                                                "mode": mode,
                                                "path_type": "vpc",
                                                "vpc_pg": pg_name
                                            })
        except Exception:
            pass
    else:
        # Individual port — find selector and PG
        try:
            url = (f"{apic_url}/api/class/infraPortBlk.json"
                   f"?query-target-filter=and("
                   f"eq(infraPortBlk.fromPort,\"{port_num}\"),"
                   f"eq(infraPortBlk.toPort,\"{port_num}\"))")
            resp = session.get(url, verify=False, timeout=30)
            if resp.status_code == 200:
                for item in resp.json().get("imdata", []):
                    dn = item.get("infraPortBlk", {}).get("attributes", {}).get("dn", "")
                    if interface_profile in dn:
                        sel_match = re.search(r'hports-(.+?)-typ-range', dn)
                        if sel_match:
                            selector_name = sel_match.group(1)
                            state["selector"] = selector_name
                            pg_name, pg_type = capture_selector_policy_group(
                                session, apic_url, interface_profile, selector_name)
                            if pg_name:
                                state["policy_group"] = pg_name
                                state["pg_type"] = pg_type
                            emit_rollback_state({
                                "type": "selector", "name": selector_name,
                                "profile": interface_profile, "port": interface,
                                "policy_group": pg_name or "", "pg_type": pg_type or "access"
                            })
        except Exception:
            pass
    
    binding_count = len(state["bindings"])
    if state.get("selector") or binding_count > 0 or state.get("description"):
        print(f"    [STATE] Captured before-state: {binding_count} binding(s), "
              f"selector={state.get('selector', 'none')}, "
              f"desc={'yes' if state.get('description') else 'none'}")
    
    return state

def cleanup_port_for_redeployment(session, apic_url, node_id, interface,
                                   interface_profile, pod_id="1"):
    """
    Full cleanup of an individual (non-VPC) port before redeployment.

    Deletes in order:
      1. ALL fvRsPathAtt (EPG static bindings) on this port's path DN
      2. Port selector (infraHPortS) on the interface profile for this port
      3. Clears the port description (l1PhysIf.descr)

    The policy group (infraAccPortGrp) is left in place — ACI's POST is
    idempotent for policy groups so step 2 of deployment will simply
    update the existing PG if the name matches, or create a new one.

    Returns dict: {bindings_deleted, selector_deleted, description_cleared, old_selector}
    """
    results = {
        "bindings_deleted": 0,
        "selector_deleted": False,
        "description_cleared": False,
        "old_selector": None
    }

    # --- Capture before-state for multi-tier rollback ---
    print(f"    [CAPTURE] Saving port state before cleanup...")
    capture_and_emit_port_state(session, apic_url, node_id, interface,
                                 interface_profile, pod_id)

    eth_iface = f"eth{interface}" if not interface.startswith("eth") else interface
    port_num = interface.split('/')[-1]

    # --- Step 1: Delete ALL EPG bindings on this port ---
    path_dn = f"topology/pod-{pod_id}/paths-{node_id}/pathep-[{eth_iface}]"
    try:
        url = (f"{apic_url}/api/class/fvRsPathAtt.json"
               f"?query-target-filter=eq(fvRsPathAtt.tDn,\"{path_dn}\")")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code == 200:
            items = resp.json().get("imdata", [])
            for item in items:
                dn = item.get("fvRsPathAtt", {}).get("attributes", {}).get("dn", "")
                if dn:
                    del_resp = session.delete(
                        f"{apic_url}/api/mo/{dn}.json", verify=False, timeout=30
                    )
                    if del_resp.status_code == 200:
                        results["bindings_deleted"] += 1
                        # Extract EPG name for logging
                        epg_match = re.search(r'/epg-([^/]+)/', dn)
                        vlan_match = re.search(r'vlan-(\d+)', item.get("fvRsPathAtt", {}).get("attributes", {}).get("encap", ""))
                        epg_name = epg_match.group(1) if epg_match else "?"
                        vlan_id = vlan_match.group(1) if vlan_match else "?"
                        print(f"    [REMOVED] EPG binding: {epg_name} VLAN {vlan_id}")
    except Exception as e:
        print(f"    [WARNING] Error cleaning bindings: {e}")

    # --- Step 2: Delete port selector on the interface profile ---
    try:
        url = (f"{apic_url}/api/class/infraPortBlk.json"
               f"?query-target-filter=and("
               f"eq(infraPortBlk.fromPort,\"{port_num}\"),"
               f"eq(infraPortBlk.toPort,\"{port_num}\"))")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code == 200:
            for item in resp.json().get("imdata", []):
                dn = item.get("infraPortBlk", {}).get("attributes", {}).get("dn", "")
                # Match only the port block on OUR interface profile
                if interface_profile in dn:
                    sel_match = re.search(r'hports-(.+?)-typ-range', dn)
                    if sel_match:
                        selector_name = sel_match.group(1)
                        results["old_selector"] = selector_name
                        del_url = (f"{apic_url}/api/mo/uni/infra/accportprof-"
                                   f"{interface_profile}/hports-{selector_name}-typ-range.json")
                        del_resp = session.delete(del_url, verify=False, timeout=30)
                        if del_resp.status_code == 200:
                            results["selector_deleted"] = True
                            print(f"    [REMOVED] Port selector: {selector_name}")
                        else:
                            print(f"    [WARNING] Failed to delete selector: {selector_name}")
    except Exception as e:
        print(f"    [WARNING] Error cleaning port selector: {e}")

    # --- Step 3: Clear port description ---
    try:
        phys_dn = f"topology/pod-{pod_id}/node-{node_id}/sys/phys-[{eth_iface}]"
        payload = {"l1PhysIf": {"attributes": {"descr": ""}}}
        resp = session.post(
            f"{apic_url}/api/node/mo/{phys_dn}.json",
            json=payload, verify=False, timeout=30
        )
        if resp.status_code == 200:
            results["description_cleared"] = True
            print(f"    [REMOVED] Port description cleared")
    except Exception as e:
        print(f"    [WARNING] Error clearing description: {e}")

    return results


def cleanup_vpc_port_for_redeployment(session, apic_url, node1, node2, interface,
                                       interface_profile, pod_id="1"):
    """
    Full cleanup of a VPC port pair before redeployment.

    Handles both individual-path and protpaths (VPC) bindings.

    Deletes in order:
      1a. ALL fvRsPathAtt on individual paths for BOTH nodes
      1b. ALL fvRsPathAtt on protpaths (VPC) — discovers old VPC PG name
          from the existing port selector's infraRsAccBaseGrp
      2.  Port selector on the VPC interface profile
      3.  Clears port description on both nodes

    Returns dict with cleanup counts.
    """
    results = {
        "bindings_deleted": 0,
        "selector_deleted": False,
        "descriptions_cleared": 0,
        "old_selector": None,
        "old_vpc_pg": None
    }

    # --- Capture before-state for multi-tier rollback ---
    print(f"    [CAPTURE] Saving VPC port state before cleanup...")
    capture_and_emit_port_state(session, apic_url, node1, interface,
                                 interface_profile, pod_id, node2=node2)

    eth_iface = f"eth{interface}" if not interface.startswith("eth") else interface
    port_num = interface.split('/')[-1]

    # --- Step 1a: Delete individual path bindings on BOTH nodes ---
    for node in [node1, node2]:
        path_dn = f"topology/pod-{pod_id}/paths-{node}/pathep-[{eth_iface}]"
        try:
            url = (f"{apic_url}/api/class/fvRsPathAtt.json"
                   f"?query-target-filter=eq(fvRsPathAtt.tDn,\"{path_dn}\")")
            resp = session.get(url, verify=False, timeout=30)
            if resp.status_code == 200:
                for item in resp.json().get("imdata", []):
                    dn = item.get("fvRsPathAtt", {}).get("attributes", {}).get("dn", "")
                    if dn:
                        del_resp = session.delete(
                            f"{apic_url}/api/mo/{dn}.json", verify=False, timeout=30
                        )
                        if del_resp.status_code == 200:
                            results["bindings_deleted"] += 1
                            epg_match = re.search(r'/epg-([^/]+)/', dn)
                            print(f"    [REMOVED] Node {node} individual binding: "
                                  f"{epg_match.group(1) if epg_match else '?'}")
        except Exception as e:
            print(f"    [WARNING] Error cleaning node {node} bindings: {e}")

    # --- Step 1b: Find old VPC PG from port selector, then delete VPC bindings ---
    old_vpc_pg = None
    try:
        url = (f"{apic_url}/api/class/infraPortBlk.json"
               f"?query-target-filter=and("
               f"eq(infraPortBlk.fromPort,\"{port_num}\"),"
               f"eq(infraPortBlk.toPort,\"{port_num}\"))")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code == 200:
            for item in resp.json().get("imdata", []):
                dn = item.get("infraPortBlk", {}).get("attributes", {}).get("dn", "")
                if interface_profile in dn:
                    sel_match = re.search(r'hports-(.+?)-typ-range', dn)
                    if sel_match:
                        selector_name = sel_match.group(1)
                        results["old_selector"] = selector_name
                        # Query the port selector to find its policy group (accbundle)
                        sel_url = (f"{apic_url}/api/mo/uni/infra/accportprof-"
                                   f"{interface_profile}/hports-{selector_name}-typ-range.json"
                                   f"?query-target=children&target-subtree-class=infraRsAccBaseGrp")
                        sel_resp = session.get(sel_url, verify=False, timeout=30)
                        if sel_resp.status_code == 200:
                            for child in sel_resp.json().get("imdata", []):
                                tdn = child.get("infraRsAccBaseGrp", {}).get("attributes", {}).get("tDn", "")
                                pg_match = re.search(r'accbundle-(.+)$', tdn)
                                if pg_match:
                                    old_vpc_pg = pg_match.group(1)
                                    results["old_vpc_pg"] = old_vpc_pg
    except Exception as e:
        print(f"    [WARNING] Error discovering old VPC PG: {e}")

    # Delete VPC protpaths bindings using discovered PG name
    if old_vpc_pg:
        # Ensure consistent node ordering for protpaths
        n1, n2 = (node1, node2) if int(node1) < int(node2) else (node2, node1)
        vpc_path = f"topology/pod-{pod_id}/protpaths-{n1}-{n2}/pathep-[{old_vpc_pg}]"
        try:
            url = (f"{apic_url}/api/class/fvRsPathAtt.json"
                   f"?query-target-filter=eq(fvRsPathAtt.tDn,\"{vpc_path}\")")
            resp = session.get(url, verify=False, timeout=30)
            if resp.status_code == 200:
                for item in resp.json().get("imdata", []):
                    dn = item.get("fvRsPathAtt", {}).get("attributes", {}).get("dn", "")
                    if dn:
                        del_resp = session.delete(
                            f"{apic_url}/api/mo/{dn}.json", verify=False, timeout=30
                        )
                        if del_resp.status_code == 200:
                            results["bindings_deleted"] += 1
                            epg_match = re.search(r'/epg-([^/]+)/', dn)
                            vlan_match = re.search(r'vlan-(\d+)',
                                item.get("fvRsPathAtt", {}).get("attributes", {}).get("encap", ""))
                            print(f"    [REMOVED] VPC binding: "
                                  f"{epg_match.group(1) if epg_match else '?'} "
                                  f"VLAN {vlan_match.group(1) if vlan_match else '?'}")
            print(f"    [INFO] Old VPC Policy Group: {old_vpc_pg}")
        except Exception as e:
            print(f"    [WARNING] Error cleaning VPC bindings: {e}")
    else:
        print(f"    [INFO] No existing VPC policy group found on this port")

    # --- Step 2: Delete port selector on VPC interface profile ---
    if results["old_selector"]:
        try:
            del_url = (f"{apic_url}/api/mo/uni/infra/accportprof-"
                       f"{interface_profile}/hports-{results['old_selector']}-typ-range.json")
            del_resp = session.delete(del_url, verify=False, timeout=30)
            if del_resp.status_code == 200:
                results["selector_deleted"] = True
                print(f"    [REMOVED] Port selector: {results['old_selector']}")
            else:
                print(f"    [WARNING] Failed to delete selector: {results['old_selector']}")
        except Exception as e:
            print(f"    [WARNING] Error deleting selector: {e}")

    # --- Step 3: Clear descriptions on both nodes ---
    for node in [node1, node2]:
        try:
            phys_dn = f"topology/pod-{pod_id}/node-{node}/sys/phys-[{eth_iface}]"
            payload = {"l1PhysIf": {"attributes": {"descr": ""}}}
            resp = session.post(
                f"{apic_url}/api/node/mo/{phys_dn}.json",
                json=payload, verify=False, timeout=30
            )
            if resp.status_code == 200:
                results["descriptions_cleared"] += 1
        except Exception:
            pass

    if results["descriptions_cleared"] > 0:
        print(f"    [REMOVED] Port descriptions cleared on {results['descriptions_cleared']} node(s)")

    return results


# =============================================================================
# EXISTING POLICY GROUP QUERY & SELECTION
# =============================================================================
# Instead of always creating a new policy group, these functions let the user
# discover and reuse an existing PG that already has the right link level,
# AEP, and other policies configured.
#
# CCIE Automation Note:
# This queries the ACI MIT using rsp-subtree=children to retrieve a policy
# group AND all its child relationship objects (infraRsHIfPol, infraRsAttEntP,
# infraRsCdpIfPol, etc.) in a single API call. This is the "subtree" query
# pattern — essential for the APIC REST API section of the exam. Without
# rsp-subtree, you'd need N+1 queries (one for the PG list, then one per PG
# to get its children). The subtree approach is O(1) API calls regardless of
# how many PGs exist.

def query_existing_access_policy_groups(session, apic_url):
    """
    Query ALL Leaf Access Port Policy Groups (infraAccPortGrp) with their
    child policy relationships in a single API call.

    Returns list of dicts:
        [{"name": "PG_NAME", "aep": "AEP_NAME", "link_level": "25G-POLICY",
          "cdp": "cdp-disabled", "lldp": "lldp-enabled", "dn": "..."}, ...]
    """
    try:
        url = (f"{apic_url}/api/class/infraAccPortGrp.json"
               f"?rsp-subtree=children")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code != 200:
            return []

        results = []
        for item in resp.json().get("imdata", []):
            attrs = item.get("infraAccPortGrp", {}).get("attributes", {})
            children = item.get("infraAccPortGrp", {}).get("children", [])
            pg = {
                "name": attrs.get("name", ""),
                "dn": attrs.get("dn", ""),
                "descr": attrs.get("descr", ""),
                "aep": None, "link_level": None, "cdp": None,
                "lldp": None, "mcp": None, "storm_control": None
            }
            for child in children:
                if "infraRsAttEntP" in child:
                    tdn = child["infraRsAttEntP"]["attributes"].get("tDn", "")
                    m = re.search(r'attentp-(.+)$', tdn)
                    if m:
                        pg["aep"] = m.group(1)
                elif "infraRsHIfPol" in child:
                    pg["link_level"] = child["infraRsHIfPol"]["attributes"].get("tnFabricHIfPolName", "")
                elif "infraRsCdpIfPol" in child:
                    pg["cdp"] = child["infraRsCdpIfPol"]["attributes"].get("tnCdpIfPolName", "")
                elif "infraRsLldpIfPol" in child:
                    pg["lldp"] = child["infraRsLldpIfPol"]["attributes"].get("tnLldpIfPolName", "")
                elif "infraRsMcpIfPol" in child:
                    pg["mcp"] = child["infraRsMcpIfPol"]["attributes"].get("tnMcpIfPolName", "")
                elif "infraRsStormctrlIfPol" in child:
                    pg["storm_control"] = child["infraRsStormctrlIfPol"]["attributes"].get("tnStormctrlIfPolName", "")
            results.append(pg)

        results.sort(key=lambda x: x["name"])
        return results
    except Exception as e:
        print(f"    [WARNING] Error querying access policy groups: {e}")
        return []


def query_existing_vpc_policy_groups(session, apic_url):
    """
    Query ALL VPC Interface Policy Groups (infraAccBndlGrp with lagT=node)
    with their child policy relationships in a single API call.

    Returns list of dicts:
        [{"name": "VPC_PG", "aep": "AEP", "link_level": "25G-POLICY",
          "cdp": "...", "lldp": "...", "lacp": "...", "mcp": "...",
          "storm_control": "...", "flow_control": "...", "dn": "..."}, ...]
    """
    try:
        url = (f"{apic_url}/api/class/infraAccBndlGrp.json"
               f"?query-target-filter=eq(infraAccBndlGrp.lagT,\"node\")"
               f"&rsp-subtree=children")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code != 200:
            return []

        results = []
        for item in resp.json().get("imdata", []):
            attrs = item.get("infraAccBndlGrp", {}).get("attributes", {})
            children = item.get("infraAccBndlGrp", {}).get("children", [])
            pg = {
                "name": attrs.get("name", ""),
                "dn": attrs.get("dn", ""),
                "descr": attrs.get("descr", ""),
                "aep": None, "link_level": None, "cdp": None,
                "lldp": None, "lacp": None, "mcp": None,
                "storm_control": None, "flow_control": None
            }
            for child in children:
                if "infraRsAttEntP" in child:
                    tdn = child["infraRsAttEntP"]["attributes"].get("tDn", "")
                    m = re.search(r'attentp-(.+)$', tdn)
                    if m:
                        pg["aep"] = m.group(1)
                elif "infraRsHIfPol" in child:
                    pg["link_level"] = child["infraRsHIfPol"]["attributes"].get("tnFabricHIfPolName", "")
                elif "infraRsCdpIfPol" in child:
                    pg["cdp"] = child["infraRsCdpIfPol"]["attributes"].get("tnCdpIfPolName", "")
                elif "infraRsLldpIfPol" in child:
                    pg["lldp"] = child["infraRsLldpIfPol"]["attributes"].get("tnLldpIfPolName", "")
                elif "infraRsLacpPol" in child:
                    pg["lacp"] = child["infraRsLacpPol"]["attributes"].get("tnLacpLagPolName", "")
                elif "infraRsMcpIfPol" in child:
                    pg["mcp"] = child["infraRsMcpIfPol"]["attributes"].get("tnMcpIfPolName", "")
                elif "infraRsStormctrlIfPol" in child:
                    pg["storm_control"] = child["infraRsStormctrlIfPol"]["attributes"].get("tnStormctrlIfPolName", "")
                elif "infraRsQosIngressDppIfPol" in child:
                    pg["flow_control"] = child["infraRsQosIngressDppIfPol"]["attributes"].get("tnQosDppPolName", "")
            results.append(pg)

        results.sort(key=lambda x: x["name"])
        return results
    except Exception as e:
        print(f"    [WARNING] Error querying VPC policy groups: {e}")
        return []


def filter_policy_groups_by_criteria(policy_groups, link_level=None, aep=None):
    """
    Filter policy groups by link level and/or AEP.

    Matching logic:
      - Exact match on link_level name if provided
      - Exact match on AEP name if provided
      - If neither provided, returns all

    Returns (exact_matches, partial_matches):
      - exact_matches: PGs matching ALL provided criteria
      - partial_matches: PGs matching link_level only (if AEP also specified)
    """
    if not link_level and not aep:
        return policy_groups, []

    exact = []
    partial = []

    for pg in policy_groups:
        ll_match = (not link_level) or (pg.get("link_level", "") == link_level)
        aep_match = (not aep) or (pg.get("aep", "") == aep)

        if ll_match and aep_match:
            exact.append(pg)
        elif ll_match and aep and not aep_match:
            partial.append(pg)

    return exact, partial


def display_policy_group_selection(policy_groups, pg_type="access", link_level=None, aep=None):
    """
    Display matching policy groups and let user select one or create new.

    Args:
        policy_groups: List of PG dicts from query functions
        pg_type: "access" or "vpc" (affects display columns)
        link_level: Current link level for highlighting matches
        aep: Current AEP for highlighting matches

    Returns:
        Selected PG name (str), or None if user wants to create new
    """
    exact, partial = filter_policy_groups_by_criteria(policy_groups, link_level, aep)

    if not exact and not partial:
        print(f"\n  [INFO] No existing policy groups match link level '{link_level}'")
        return None

    print(f"\n  {'='*70}")
    print(f"  EXISTING POLICY GROUPS (matching link level: {link_level})")
    print(f"  {'='*70}")

    all_display = []

    if exact:
        print(f"\n  {colored(f'Exact matches ({len(exact)}):', Colors.GREEN)} Link Level + AEP match")
        print("  " + "-" * 70)
        if pg_type == "vpc":
            print(f"  {'#':>4}  {'Name':<35} {'AEP':<20} {'LACP':<15}")
        else:
            print(f"  {'#':>4}  {'Name':<35} {'AEP':<20} {'CDP':<15}")
        print("  " + "-" * 70)

        for i, pg in enumerate(exact, 1):
            all_display.append(pg)
            if pg_type == "vpc":
                print(f"  [{i:>2}] {pg['name']:<35} {(pg['aep'] or '-'):<20} {(pg.get('lacp') or '-'):<15}")
            else:
                print(f"  [{i:>2}] {pg['name']:<35} {(pg['aep'] or '-'):<20} {(pg.get('cdp') or '-'):<15}")

    if partial:
        offset = len(exact)
        print(f"\n  {colored(f'Link level match only ({len(partial)}):', Colors.YELLOW)} different AEP")
        print("  " + "-" * 70)
        if pg_type == "vpc":
            print(f"  {'#':>4}  {'Name':<35} {'AEP':<20} {'LACP':<15}")
        else:
            print(f"  {'#':>4}  {'Name':<35} {'AEP':<20} {'CDP':<15}")
        print("  " + "-" * 70)

        for i, pg in enumerate(partial, offset + 1):
            all_display.append(pg)
            if pg_type == "vpc":
                print(f"  [{i:>2}] {pg['name']:<35} {(pg['aep'] or '-'):<20} {(pg.get('lacp') or '-'):<15}")
            else:
                print(f"  [{i:>2}] {pg['name']:<35} {(pg['aep'] or '-'):<20} {(pg.get('cdp') or '-'):<15}")

    print("  " + "-" * 70)
    print(f"  [N] Create NEW policy group instead")
    print(f"  [D] Show details of a policy group")

    while True:
        choice = prompt_input("\n  Select policy group (or N for new): ").strip().upper()

        if choice == 'N':
            return None

        if choice == 'D':
            detail_num = prompt_input("    Show details for #: ").strip()
            try:
                idx = int(detail_num) - 1
                if 0 <= idx < len(all_display):
                    pg = all_display[idx]
                    print(f"\n    === {pg['name']} ===")
                    print(f"    Link Level:    {pg.get('link_level') or '(default)'}")
                    print(f"    AEP:           {pg.get('aep') or '(none)'}")
                    print(f"    CDP:           {pg.get('cdp') or '(default)'}")
                    print(f"    LLDP:          {pg.get('lldp') or '(default)'}")
                    if pg_type == "vpc":
                        print(f"    LACP:          {pg.get('lacp') or '(default)'}")
                        print(f"    Flow Control:  {pg.get('flow_control') or '(default)'}")
                    print(f"    MCP:           {pg.get('mcp') or '(default)'}")
                    print(f"    Storm Control: {pg.get('storm_control') or '(default)'}")
                    if pg.get('descr'):
                        print(f"    Description:   {pg['descr']}")
            except ValueError:
                pass
            continue

        try:
            idx = int(choice) - 1
            if 0 <= idx < len(all_display):
                selected = all_display[idx]
                print(f"\n  [SELECTED] Using existing policy group: {selected['name']}")
                return selected['name']
        except ValueError:
            pass
        print("  [ERROR] Invalid selection")


# =============================================================================
# APIC TOKEN REFRESH
# =============================================================================
# APIC tokens expire after refreshTimeoutSeconds (default 300s / 5 minutes).
# In batch deployments with interactive port selection, the token can easily
# expire between deployments. These helpers keep the session alive.
#
# CCIE Automation Note:
# The APIC uses cookie-based auth (APIC-cookie). On login (aaaLogin), you get
# a token with a refreshTimeoutSeconds. Before it expires, call aaaRefresh to
# get a new token. If it already expired, you must re-authenticate via aaaLogin.
# The exam tests this lifecycle — particularly in scripts that run >5 minutes.
# Strategy: proactive refresh (check age before each operation) + reactive
# retry (catch 403 and re-auth). This is the same pattern used in production
# SDKs like cobra/acitoolkit.

import time as _time

def refresh_apic_token(session, apic_url):
    """
    Refresh APIC token via /api/aaaRefresh.json.
    
    Returns new token lifetime (seconds) on success, None on failure.
    """
    try:
        resp = session.get(f"{apic_url}/api/aaaRefresh.json", verify=False, timeout=30)
        if resp.status_code == 200:
            try:
                attrs = resp.json()['imdata'][0]['aaaLogin']['attributes']
                return int(attrs.get('refreshTimeoutSeconds', 300))
            except (KeyError, IndexError, ValueError):
                return 300
    except Exception:
        pass
    return None


def ensure_token_fresh(session, apic_url, token_state):
    """
    Check token age and refresh proactively if needed.
    
    token_state is a mutable dict: {"login_time": float, "lifetime": int}
    Call this before each deployment iteration to prevent 403 errors.
    
    Returns True if token is fresh, False if refresh failed (caller should re-auth).
    """
    if not token_state:
        return True  # No state tracked, skip
    
    elapsed = _time.time() - token_state.get('login_time', _time.time())
    remaining = token_state.get('lifetime', 300) - elapsed
    
    # Refresh when <60 seconds remain
    if remaining < 60:
        new_lifetime = refresh_apic_token(session, apic_url)
        if new_lifetime:
            token_state['login_time'] = _time.time()
            token_state['lifetime'] = new_lifetime
            print(f"  [TOKEN] Refreshed (was {remaining:.0f}s remaining, new lifetime: {new_lifetime}s)")
            return True
        else:
            print(f"  [TOKEN] Refresh failed — token may have expired")
            return False
    
    return True


def reauth_apic(session, apic_url, username, password, token_state=None):
    """
    Full re-authentication to APIC (when refresh fails / token already expired).
    
    Updates token_state in-place if provided.
    Returns True on success, False on failure.
    """
    payload = {"aaaUser": {"attributes": {"name": username, "pwd": password}}}
    try:
        resp = session.post(f"{apic_url}/api/aaaLogin.json", json=payload, verify=False, timeout=30)
        if resp.status_code == 200:
            if token_state is not None:
                token_state['login_time'] = _time.time()
                try:
                    attrs = resp.json()['imdata'][0]['aaaLogin']['attributes']
                    token_state['lifetime'] = int(attrs.get('refreshTimeoutSeconds', 300))
                except (KeyError, IndexError, ValueError):
                    token_state['lifetime'] = 300
            print(f"  [TOKEN] Re-authenticated successfully")
            return True
    except Exception:
        pass
    print(f"  [TOKEN] Re-authentication FAILED")
    return False


# =============================================================================
# EPG BINDING QUERY & OVERWRITE FUNCTIONS
# =============================================================================
# Used by EPG Add (overwrite mode) to wipe all existing bindings on a port
# before deploying new ones. Also reusable by any script that needs to
# discover what's currently bound to a physical port.
#
# CCIE Automation Note:
# fvRsPathAtt is queried at the CLASS level with a tDn filter — this is
# a "reverse lookup" pattern. Instead of querying each EPG individually to
# check if it has a binding to our port (O(N) calls where N = number of EPGs),
# we query the fvRsPathAtt class globally filtered by the port's path DN.
# This returns ALL bindings on that port in a SINGLE API call regardless of
# which tenant/AP/EPG they belong to. The exam tests this pattern heavily.

def query_all_bindings_on_port(session, apic_url, node_id, port, pod_id="1",
                               tenants=None, path_type="individual",
                               node2=None, pg_name=None, verbose=True):
    """
    Query ALL fvRsPathAtt bindings on a port using merged dual-strategy.

    ALWAYS runs both strategies and merges results with deduplication.
    This catches partial results from Strategy 1 on D1 where bracket
    encoding in the eq() filter causes the class-level query to miss
    some bindings.

    Args:
        session: requests.Session with APIC auth
        apic_url: APIC base URL
        node_id: Leaf node ID (e.g., '1301')
        port: Port string (e.g., '1/68')
        pod_id: Pod ID (default '1')
        tenants: List of tenant names for Strategy 2. If None, auto-discovers.
        path_type: "individual" for single ports, "vpc" for protpaths
        node2: Second node ID (required when path_type="vpc")
        pg_name: Policy group name (required when path_type="vpc")
        verbose: Print query progress (default True)

    Returns:
        List of dicts: [{"dn": "...", "tDn": "...", "encap": "vlan-32",
                         "mode": "regular", "tenant": "...",
                         "app_profile": "...", "epg": "...", "vlan": 32}, ...]
    """
    # Build the path DN based on port type
    if path_type == "vpc" and node2 and pg_name:
        path_dn = f"topology/pod-{pod_id}/protpaths-{node_id}-{node2}/pathep-[{pg_name}]"
    else:
        eth_port = f"eth{port}" if not port.startswith("eth") else port
        path_dn = f"topology/pod-{pod_id}/paths-{node_id}/pathep-[{eth_port}]"

    bindings = []
    seen_dns = set()

    # ------------------------------------------------------------------
    # Strategy 1: Class-level query with eq() filter (fast, single call)
    # ------------------------------------------------------------------
    s1_count = 0
    try:
        url = (f"{apic_url}/api/class/fvRsPathAtt.json"
               f"?query-target-filter=eq(fvRsPathAtt.tDn,\"{path_dn}\")")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code == 200:
            for item in resp.json().get("imdata", []):
                attrs = item.get("fvRsPathAtt", {}).get("attributes", {})
                dn = attrs.get("dn", "")
                if dn and dn not in seen_dns:
                    seen_dns.add(dn)
                    encap = attrs.get("encap", "")
                    tn_match = re.search(r'/tn-([^/]+)/', dn)
                    ap_match = re.search(r'/ap-([^/]+)/', dn)
                    epg_match = re.search(r'/epg-([^/]+)/', dn)
                    vlan_match = re.search(r'vlan-(\d+)', encap)
                    bindings.append({
                        "dn": dn,
                        "tDn": attrs.get("tDn", ""),
                        "encap": encap,
                        "mode": attrs.get("mode", ""),
                        "tenant": tn_match.group(1) if tn_match else "",
                        "app_profile": ap_match.group(1) if ap_match else "",
                        "epg": epg_match.group(1) if epg_match else "",
                        "vlan": int(vlan_match.group(1)) if vlan_match else 0
                    })
                    s1_count += 1
    except Exception as e:
        if verbose:
            print(f"    [WARNING] Strategy 1 error: {e}")

    if verbose:
        print(f"  [QUERY] Strategy 1 (class-level): {s1_count} binding(s)")

    # ------------------------------------------------------------------
    # Strategy 2: Per-tenant EPG subtree walk (ALWAYS runs, merges new)
    # Uses Python substring match — immune to URL encoding issues on D1.
    # ------------------------------------------------------------------
    # Auto-discover tenants if not provided
    if tenants is None:
        tenants = []
        try:
            t_resp = session.get(
                f"{apic_url}/api/class/fvTenant.json?query-target-filter=not(wcard(fvTenant.dn,\"common\"))",
                verify=False, timeout=15
            )
            if t_resp.status_code == 200:
                for t_item in t_resp.json().get("imdata", []):
                    t_name = t_item.get("fvTenant", {}).get("attributes", {}).get("name", "")
                    if t_name and t_name not in ("common", "infra", "mgmt"):
                        tenants.append(t_name)
        except:
            pass
        if verbose and tenants:
            print(f"  [QUERY] Auto-discovered {len(tenants)} tenant(s) for Strategy 2")

    s2_new = 0
    if verbose:
        print(f"  [QUERY] Strategy 2 (per-tenant EPG walk): scanning {len(tenants)} tenant(s)...")

    for tenant in tenants:
        try:
            # Get all app profiles in this tenant
            ap_url = f"{apic_url}/api/mo/uni/tn-{tenant}.json?query-target=children&target-subtree-class=fvAp"
            ap_resp = session.get(ap_url, verify=False, timeout=15)
            if ap_resp.status_code != 200:
                continue

            for ap_item in ap_resp.json().get("imdata", []):
                ap_name = ap_item.get("fvAp", {}).get("attributes", {}).get("name", "")
                if not ap_name:
                    continue

                # Get all fvRsPathAtt under this app profile
                epg_url = (f"{apic_url}/api/mo/uni/tn-{tenant}/ap-{ap_name}.json"
                           f"?query-target=subtree&target-subtree-class=fvRsPathAtt")
                epg_resp = session.get(epg_url, verify=False, timeout=20)
                if epg_resp.status_code != 200:
                    continue

                for item in epg_resp.json().get("imdata", []):
                    attrs = item.get("fvRsPathAtt", {}).get("attributes", {})
                    tdn = attrs.get("tDn", "")
                    dn = attrs.get("dn", "")

                    # Python substring match — catches what eq() misses
                    if path_dn in tdn and dn not in seen_dns:
                        seen_dns.add(dn)
                        encap = attrs.get("encap", "")
                        tn_match = re.search(r'/tn-([^/]+)/', dn)
                        ap_match = re.search(r'/ap-([^/]+)/', dn)
                        epg_match = re.search(r'/epg-([^/]+)/', dn)
                        vlan_match = re.search(r'vlan-(\d+)', encap)
                        bindings.append({
                            "dn": dn,
                            "tDn": tdn,
                            "encap": encap,
                            "mode": attrs.get("mode", ""),
                            "tenant": tn_match.group(1) if tn_match else "",
                            "app_profile": ap_match.group(1) if ap_match else "",
                            "epg": epg_match.group(1) if epg_match else "",
                            "vlan": int(vlan_match.group(1)) if vlan_match else 0
                        })
                        s2_new += 1
        except Exception as e:
            if verbose:
                print(f"    [WARNING] Strategy 2 error for {tenant}: {e}")

    if verbose:
        if s2_new > 0:
            print(f"  [QUERY] Strategy 2 found {s2_new} additional binding(s) missed by Strategy 1")
        else:
            print(f"  [QUERY] Strategy 2: confirmed (0 additional)")
        print(f"  [TOTAL] {len(bindings)} unique binding(s)")

    bindings.sort(key=lambda x: x.get("vlan", 0))
    return bindings


def delete_all_bindings_on_port(session, apic_url, node_id, port, pod_id="1",
                                tenants=None):
    """
    Delete ALL fvRsPathAtt bindings on a specific port.

    Queries all bindings first (merged dual-strategy), emits
    [ROLLBACK:STATE] for each one, then deletes each one.
    Returns (deleted_count, failed_count, binding_details).

    binding_details is a list of {"vlan", "epg", "success"} for logging.
    """
    bindings = query_all_bindings_on_port(session, apic_url, node_id, port,
                                          pod_id, tenants=tenants)

    if not bindings:
        return 0, 0, []

    # Emit state markers BEFORE deleting — rollback generator uses these
    # to restore old bindings if needed
    for b in bindings:
        emit_rollback_state({
            "type": "binding", "node": str(node_id), "port": port,
            "vlan": str(b["vlan"]), "tenant": b["tenant"],
            "ap": b["app_profile"], "epg": b["epg"],
            "mode": b.get("mode", "regular"),
            "path_type": "individual"
        })

    deleted = 0
    failed = 0
    details = []

    for b in bindings:
        try:
            resp = session.delete(
                f"{apic_url}/api/mo/{b['dn']}.json",
                verify=False, timeout=30
            )
            ok = resp.status_code == 200
        except Exception:
            ok = False

        details.append({
            "vlan": b["vlan"],
            "epg": b["epg"],
            "tenant": b["tenant"],
            "success": ok
        })
        if ok:
            deleted += 1
        else:
            failed += 1

    return deleted, failed, details


# =============================================================================
# BACKWARD-COMPATIBLE WRAPPERS
# =============================================================================
# These allow the existing scripts to switch to this module with minimal changes.

def get_validated_available_ports(session, apic_url, node_id, pod_id="1"):
    """
    Backward-compatible wrapper — returns ONLY valid (available) ports.

    Use get_all_ports_with_status() for the new full-inventory behavior.
    """
    all_ports = get_all_ports_with_status(session, apic_url, node_id, pod_id)
    return [p for p in all_ports if p['valid']]


def find_common_validated_ports(ports1, ports2):
    """
    Backward-compatible wrapper — returns ONLY valid common ports.

    Use find_common_ports_with_status() for the new full-inventory behavior.
    """
    all_common = find_common_ports_with_status(ports1, ports2)
    return [p for p in all_common if p['valid']]


# =============================================================================
# STANDALONE TEST
# =============================================================================

if __name__ == "__main__":
    print("ACI Port Utilities v1.0.0")
    print("=" * 50)
    print()
    print("Shared module — import into deployment scripts:")
    print()
    print("  from aci_port_utils import (")
    print("      detect_environment, extract_node_id, parse_vlans,")
    print("      get_all_ports_with_status, display_port_selection,")
    print("      find_common_ports_with_status, display_vpc_port_selection")
    print("  )")
    print()
    print("Helper Functions:")
    print(f"  detect_environment('EDCLEAFACC1501')  -> '{detect_environment('EDCLEAFACC1501')}'")
    print(f"  detect_environment('EDCLEAFNSM2163')  -> '{detect_environment('EDCLEAFNSM2163')}'")
    print(f"  extract_node_id('EDCLEAFACC1501')     -> '{extract_node_id('EDCLEAFACC1501')}'")
    print(f"  parse_vlans('32,64-67,92')            -> {parse_vlans('32,64-67,92')}")
    print(f"  parse_port('eth1/68')                 -> '{parse_port('eth1/68')}'")
    print(f"  parse_port('1/68')                    -> '{parse_port('1/68')}'")
    print()
    print("Color Demo:")
    # Simulate a few ports for display demo
    demo_ports = [
        {"port": "eth1/1", "interface": "1/1", "speed": "25G", "valid": True,
         "issues": [], "config_details": {}, "usage": "discovery", "admin_state": "up"},
        {"port": "eth1/2", "interface": "1/2", "speed": "25G", "valid": False,
         "issues": ["Description: SERVER01 WO123", "Policy group assigned"],
         "config_details": {"description": "SERVER01 WO123", "port_selector": "SERVER01_e2"},
         "usage": "epg", "admin_state": "up"},
        {"port": "eth1/3", "interface": "1/3", "speed": "10G", "valid": True,
         "issues": [], "config_details": {}, "usage": "discovery", "admin_state": "up"},
        {"port": "eth1/4", "interface": "1/4", "speed": "25G", "valid": False,
         "issues": ["Usage: epg", "EPG deployed"],
         "config_details": {"usage": "epg", "epg_bindings": ["V0032_EPG", "V0064_EPG"]},
         "usage": "epg", "admin_state": "up"},
    ]
    for i, p in enumerate(demo_ports, 1):
        print(_format_port_line(i, p))
