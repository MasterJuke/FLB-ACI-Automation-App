#!/usr/bin/env python3
"""
ACI Bulk Deployment Web Application
====================================
Flask-based web UI for running ACI deployment scripts.

Features (v1.3.0):
- Sleek Docker/VS Code inspired interface
- Real-time terminal output with interactive input
- APIC Credential Manager (in-memory, auto-injects into scripts)
- CSV Validation before deployment
- Auto-save deployment logs (sanitized, no credentials)
- Rollback script generation per deployment
- Deployment log with time-saved calculator
- File picker for CSV selection
- Bracket-number coloring in terminal

Requirements:
- Python 3.6+
- Flask (pip install flask)

Usage:
    python aci_deployment_app.py
    Open http://localhost:5000 in your browser.

Author: Network Automation
Version: 1.3.0
"""

import os
import sys
import csv
import json
import re
import subprocess
import threading
import queue
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, send_file

app = Flask(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG_FILE = "aci_deploy_config.json"
LOG_FILE = "aci_deploy_log.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'csv_uploads')
SAVED_LOGS_FOLDER = os.path.join(BASE_DIR, 'saved_logs')
ROLLBACK_FOLDER = os.path.join(BASE_DIR, 'rollback_scripts')

DEFAULT_CONFIG = {
    "vpc_script": "aci_bulk_vpc_deploy.py",
    "individual_script": "aci_bulk_individual_deploy.py",
    "epgadd_script": "aci_bulk_epg_add.py",
    "epgdelete_script": "aci_bulk_epg_delete.py",
    "default_vpc_csv": "vpc_deployments.csv",
    "default_individual_csv": "individual_port_deployments.csv",
    "default_epgadd_csv": "epg_add.csv",
    "default_epgdelete_csv": "epg_delete.csv",
    "version": "1.3.0"
}

TIME_ESTIMATES = {
    "vpc": {"per_deployment": 18, "label": "VPC Port Channel + EPG Bindings"},
    "individual": {"per_deployment": 12, "label": "Individual Port Policy + EPG Bindings"},
    "epgadd": {"per_deployment": 6, "label": "EPG Static Path Binding Addition"},
    "epgdelete": {"per_deployment": 5, "label": "EPG Static Path Binding Removal"}
}

# CSV column requirements per script type
CSV_REQUIREMENTS = {
    "vpc": {"required": ["HOSTNAME", "SWITCH1", "SWITCH2", "SPEED", "VLANS", "WORKORDER"],
            "validators": {"SPEED": r"^(1G|10G|25G|40G|100G)$", "VLANS": r"^[\d,\-\s\"]+$"}},
    "individual": {"required": ["HOSTNAME", "SWITCH", "TYPE", "SPEED", "VLANS", "WORKORDER"],
                   "validators": {"TYPE": r"^(ACCESS|TRUNK)$", "SPEED": r"^(1G|10G|25G|40G|100G)$", "VLANS": r"^[\d,\-\s\"]+$"}},
    "epgadd": {"required": ["SWITCH", "PORT", "VLANS"],
               "validators": {"PORT": r"^(eth)?[\d]+/[\d]+$", "VLANS": r"^[\d,\-\s\"]+$"}},
    "epgdelete": {"required": ["SWITCH", "PORT", "VLANS"],
                  "validators": {"PORT": r"^(eth)?[\d]+/[\d]+$", "VLANS": r"^[\d,\-\s\"]+$"}}
}

# Global state
running_process = None
output_queue = queue.Queue()
input_queue = queue.Queue()

# In-memory credential storage (NEVER written to disk)
stored_credentials = {"username": None, "password": None, "set": False}

# Track current run for logging
current_run = {"type": None, "start_time": None, "csv_path": None, "output_lines": [], "status": None}

# =============================================================================
# CONFIG MANAGEMENT
# =============================================================================

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                return config
        except:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

# =============================================================================
# DEPLOYMENT LOG MANAGEMENT
# =============================================================================

def load_log():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"entries": [], "total_time_saved_minutes": 0, "total_deployments": 0}

def save_log(log_data):
    with open(LOG_FILE, 'w') as f:
        json.dump(log_data, f, indent=2)

def add_log_entry(deploy_type, csv_path, status, deployment_count, duration_seconds, output_lines):
    log = load_log()
    est = TIME_ESTIMATES.get(deploy_type, {})
    manual_minutes = est.get("per_deployment", 10) * deployment_count
    auto_minutes = round(duration_seconds / 60, 2)
    saved_minutes = max(0, round(manual_minutes - auto_minutes, 1))
    entry_id = len(log["entries"]) + 1
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    entry = {
        "id": entry_id,
        "timestamp": timestamp,
        "type": deploy_type,
        "csv_file": os.path.basename(csv_path) if csv_path else "inline",
        "status": status,
        "deployment_count": deployment_count,
        "duration_seconds": round(duration_seconds, 1),
        "manual_estimate_minutes": manual_minutes,
        "automated_minutes": auto_minutes,
        "time_saved_minutes": saved_minutes,
        "output_summary": output_lines[-8:] if output_lines else [],
        "saved_log_file": None,
        "rollback_file": None
    }

    # Auto-save sanitized log
    saved_log = auto_save_log(deploy_type, entry_id, timestamp, output_lines)
    if saved_log:
        entry["saved_log_file"] = saved_log

    # Generate rollback script
    rollback = generate_rollback_script(deploy_type, entry_id, timestamp, output_lines)
    if rollback:
        entry["rollback_file"] = rollback

    log["entries"].append(entry)
    log["total_time_saved_minutes"] = round(sum(e.get("time_saved_minutes", 0) for e in log["entries"]), 1)
    log["total_deployments"] = sum(e.get("deployment_count", 0) for e in log["entries"])
    save_log(log)
    return entry

# =============================================================================
# AUTO-SAVE LOGS (sanitized - no credentials)
# =============================================================================

SENSITIVE_PATTERNS = [
    re.compile(r'(?i)(password|pwd|passwd|secret|token)\s*[:=]\s*\S+'),
    re.compile(r'(?i)^Password:\s*.*$'),
    re.compile(r'"pwd"\s*:\s*"[^"]*"'),
    re.compile(r'Auto-filled password'),
]

def sanitize_line(line):
    """Remove sensitive data from a log line."""
    for pattern in SENSITIVE_PATTERNS:
        if pattern.search(line):
            # For credential auto-fill lines, keep the info but redact
            if 'Auto-filled password' in line:
                return '[CREDENTIALS] Auto-filled password: ••••••••'
            return None  # Skip this line entirely
    # Also catch lines that are just the raw password input echo
    if stored_credentials.get('password') and stored_credentials['password']:
        pwd = stored_credentials['password']
        # Check if line IS the password (echoed from stdin) or contains it
        stripped = line.strip()
        if stripped == pwd or (line.startswith('> ') and pwd in line):
            return '> [PASSWORD REDACTED]'
    return line

def auto_save_log(deploy_type, entry_id, timestamp, output_lines):
    """Save sanitized deployment log to saved_logs/ folder."""
    try:
        os.makedirs(SAVED_LOGS_FOLDER, exist_ok=True)
        ts = timestamp.replace(":", "").replace("-", "").replace(" ", "_")
        filename = f"{ts}_{deploy_type}_run{entry_id}.txt"
        filepath = os.path.join(SAVED_LOGS_FOLDER, filename)

        with open(filepath, 'w') as f:
            f.write(f"ACI Deployment Log\n")
            f.write(f"{'=' * 60}\n")
            f.write(f"Type:      {deploy_type.upper()}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Run ID:    {entry_id}\n")
            f.write(f"{'=' * 60}\n\n")

            for line in output_lines:
                sanitized = sanitize_line(line)
                if sanitized is not None:
                    f.write(sanitized + '\n')

            f.write(f"\n{'=' * 60}\n")
            f.write(f"End of log\n")

        return filename
    except Exception as e:
        print(f"[WARNING] Failed to save log: {e}")
        return None

# =============================================================================
# ROLLBACK SCRIPT GENERATION
# =============================================================================

def generate_rollback_script(deploy_type, entry_id, timestamp, output_lines):
    """Parse deployment output and generate a rollback Python script."""
    try:
        os.makedirs(ROLLBACK_FOLDER, exist_ok=True)
        ts = timestamp.replace(":", "").replace("-", "").replace(" ", "_")
        filename = f"rollback_{ts}_{deploy_type}_run{entry_id}.py"
        filepath = os.path.join(ROLLBACK_FOLDER, filename)

        # Parse output to find created objects
        rollback_actions = parse_deployment_output(deploy_type, output_lines)

        if not rollback_actions:
            return None

        # Generate the rollback script
        script = build_rollback_script(deploy_type, entry_id, timestamp, rollback_actions)

        with open(filepath, 'w') as f:
            f.write(script)

        return filename
    except Exception as e:
        print(f"[WARNING] Failed to generate rollback: {e}")
        return None


def parse_deployment_output(deploy_type, lines):
    """
    Parse terminal output to extract created ACI objects for rollback.
    Matches exact print statements from deployment scripts:
      VPC:        deploy_vpc() in aci_bulk_vpc_deploy.py
      Individual: deploy_individual_port() in aci_bulk_individual_deploy.py
      EPG Add:    main() in aci_bulk_epg_add.py
      EPG Delete: main() in aci_bulk_epg_delete.py
    """
    actions = []
    full_text = '\n'.join(lines)

    if deploy_type == 'vpc':
        # VPC policy group: "[2/4] Creating VPC Interface Policy Group: {name}"
        for m in re.finditer(r'Creating VPC Interface Policy Group:\s*(\S+)', full_text):
            actions.append({"action": "delete_vpc_ipg", "name": m.group(1)})

        # Port selector (VPC uses policy_group as selector name):
        # "[3/4] Creating Access Port Selector: {name}"
        for m in re.finditer(r'Creating Access Port Selector:\s*(\S+)', full_text):
            actions.append({"action": "delete_port_selector_vpc", "name": m.group(1)})

        # Interface profile: "Interface Profile: {name}" (printed right after selector)
        int_profile_matches = re.findall(r'Interface Profile:\s*(\S+)', full_text)
        if int_profile_matches:
            for a in actions:
                if a["action"] == "delete_port_selector_vpc" and "int_profile" not in a:
                    a["int_profile"] = int_profile_matches[0]

        # Node pair from preview: "vPC Leaf Switch Pair:   {node1}-{node2}"
        node_pair = re.search(r'vPC Leaf Switch Pair:\s+(\d+)-(\d+)', full_text)

        # VPC policy group name (used in path construction)
        vpc_pg_name = None
        for a in actions:
            if a["action"] == "delete_vpc_ipg":
                vpc_pg_name = a["name"]
                break

        # Successful bindings: "VLAN {vlan}: OK"
        for m in re.finditer(r'VLAN\s+(\d+):\s*OK', full_text):
            vlan = m.group(1)
            binding = {"action": "delete_binding", "vlan": vlan}
            if node_pair:
                binding["node1"] = node_pair.group(1)
                binding["node2"] = node_pair.group(2)
            if vpc_pg_name:
                binding["vpc_pg"] = vpc_pg_name
            actions.append(binding)

        # EPG info from preview lines: "VLAN   32 -> AppProf / EPG_Name [TenantName]"
        epg_map = {}
        for m in re.finditer(r'VLAN\s+(\d+)\s*->\s*(\S+)\s*/\s*(\S+?)(?:\s+\[(\S+?)\])?\s*$', full_text, re.MULTILINE):
            epg_map[m.group(1)] = {"app_profile": m.group(2), "epg": m.group(3), "tenant": m.group(4) or ""}

        for a in actions:
            if a["action"] == "delete_binding" and a.get("vlan") in epg_map:
                a.update(epg_map[a["vlan"]])

        # Port descriptions: "Node {node_id} eth{interface}: [SUCCESS]"
        for m in re.finditer(r'Node\s+(\d+)\s+eth([\d/]+):\s*\[SUCCESS\]', full_text):
            actions.append({"action": "clear_description", "node_id": m.group(1), "interface": m.group(2)})

    elif deploy_type == 'individual':
        # Policy group: "[2/4] Creating Leaf Access Port Policy Group: {name}"
        for m in re.finditer(r'Creating Leaf Access Port Policy Group:\s*(\S+)', full_text):
            actions.append({"action": "delete_access_ipg", "name": m.group(1)})

        # Port selector: "[3/4] Creating Port Selector: {name}"
        for m in re.finditer(r'Creating Port Selector:\s*(\S+)', full_text):
            actions.append({"action": "delete_port_selector_individual", "name": m.group(1)})

        # Interface profile: "Interface Profile: {name}"
        int_profile_matches = re.findall(r'Interface Profile:\s*(\S+)', full_text)
        if int_profile_matches:
            for a in actions:
                if a["action"] == "delete_port_selector_individual" and "int_profile" not in a:
                    a["int_profile"] = int_profile_matches[0]

        # Node ID from preview: "Node ID:               {node_id}"
        node_match = re.search(r'Node ID:\s+(\d+)', full_text)
        # Interface from preview: "Interface:             eth{interface}"
        int_match = re.search(r'Interface:\s+eth([\d/]+)', full_text)

        # Successful bindings: "VLAN {vlan}: OK"
        for m in re.finditer(r'VLAN\s+(\d+):\s*OK', full_text):
            binding = {"action": "delete_binding", "vlan": m.group(1)}
            if node_match:
                binding["node_id"] = node_match.group(1)
            if int_match:
                binding["interface"] = int_match.group(1)
            actions.append(binding)

        # EPG info from preview: "VLAN   32 -> AppProf / EPG_Name [TenantName]"
        epg_map = {}
        for m in re.finditer(r'VLAN\s+(\d+)\s*->\s*(\S+)\s*/\s*(\S+?)(?:\s+\[(\S+?)\])?\s*$', full_text, re.MULTILINE):
            epg_map[m.group(1)] = {"app_profile": m.group(2), "epg": m.group(3), "tenant": m.group(4) or ""}
        for a in actions:
            if a["action"] == "delete_binding" and a.get("vlan") in epg_map:
                a.update(epg_map[a["vlan"]])

        # Port description: "[1/4] Setting port description: ..." then "[SUCCESS]"
        if node_match and int_match:
            desc_success = re.search(r'\[1/4\].*description.*\n.*\[SUCCESS\]', full_text)
            if desc_success:
                actions.append({"action": "clear_description", "node_id": node_match.group(1), "interface": int_match.group(1)})

    elif deploy_type == 'epgadd':
        # EPG Add output: "[OK] {switch} port {port}: VLAN {vlan}"
        for m in re.finditer(r'\[OK\]\s+(\S+)\s+port\s+([\d/]+):\s*VLAN\s+(\d+)', full_text):
            actions.append({"action": "delete_binding", "switch": m.group(1), "port": m.group(2), "vlan": m.group(3)})

    elif deploy_type == 'epgdelete':
        # EPG Delete output: "[DELETED] {switch} port {port}: VLAN {vlan}"
        for m in re.finditer(r'\[DELETED\]\s+(\S+)\s+port\s+([\d/]+):\s*VLAN\s+(\d+)', full_text):
            actions.append({"action": "recreate_binding", "switch": m.group(1), "port": m.group(2), "vlan": m.group(3)})

    return actions


def build_rollback_script(deploy_type, entry_id, timestamp, actions):
    """Build a Python rollback script from parsed actions."""
    # Count meaningful actions
    meaningful = [a for a in actions if a["action"] not in ["clear_description"]]
    if not meaningful:
        meaningful = actions

    script = f'''#!/usr/bin/env python3
"""
ACI ROLLBACK SCRIPT
====================
Auto-generated rollback for: {deploy_type.upper()} deployment
Original Run ID: {entry_id}
Original Timestamp: {timestamp}
Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

WARNING: This script will REVERSE the changes made by the original deployment.
         Review carefully before executing!

Actions to perform:
'''
    for i, a in enumerate(meaningful, 1):
        if a["action"] == "delete_vpc_ipg":
            script += f'  {i}. Delete VPC Policy Group: {a.get("name", "?")}\n'
        elif a["action"] == "delete_access_ipg":
            script += f'  {i}. Delete Leaf Access Port Policy Group: {a.get("name", "?")}\n'
        elif a["action"].startswith("delete_port_selector"):
            script += f'  {i}. Delete Port Selector: {a.get("name", "?")} from {a.get("int_profile", "?")}\n'
        elif a["action"] == "delete_binding":
            script += f'  {i}. Delete static binding VLAN {a.get("vlan", "?")} from EPG {a.get("epg", "?")}\n'
        elif a["action"] == "recreate_binding":
            script += f'  {i}. Re-create static binding VLAN {a.get("vlan", "?")} on {a.get("switch", "?")} port {a.get("port", "?")}\n'
        elif a["action"] == "clear_description":
            script += f'  {i}. Clear port description on node {a.get("node_id", "?")} eth{a.get("interface", "?")}\n'

    script += f'''
"""

import os
import sys
import getpass
import requests
import urllib3
import re

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =============================================================================
# APIC CONFIGURATION - Update these if different from deployment scripts
# =============================================================================

APIC_URLS = {{
    "D1": "",  # <-- UPDATE or copy from your deployment script
    "D2": "",  # <-- UPDATE or copy from your deployment script
    "D3": ""   # <-- UPDATE or copy from your deployment script
}}

POD_ID = "1"

def detect_environment(switch_name):
    s = switch_name.upper()
    if "NSM" in s: return "D3"
    elif "SDC" in s: return "D2"
    return "D1"

def extract_node_id(switch_name):
    m = re.search(r"(\\d+)$", switch_name)
    return m.group(1) if m else None

def login_to_apic(session, apic_url, username, password):
    try:
        r = session.post(f"{{apic_url}}/api/aaaLogin.json",
            json={{"aaaUser": {{"attributes": {{"name": username, "pwd": password}}}}}},
            verify=False, timeout=30)
        return r.status_code == 200
    except:
        return False


def main():
    print("\\n" + "=" * 60)
    print(" ACI ROLLBACK SCRIPT")
    print(f" Original: {deploy_type.upper()} Run #{entry_id} ({timestamp})")
    print("=" * 60)

    # Authenticate
    username = input("\\nUsername: ").strip()
    password = getpass.getpass("Password: ")

    sessions = {{}}
    needed_envs = set()
'''

    # Add environment detection based on actions
    env_refs = set()
    for a in actions:
        if a.get("node1"):
            env_refs.add("node_pair")
        if a.get("node_id"):
            env_refs.add("single_node")
        if a.get("switch"):
            env_refs.add("switch")

    # Determine which environments to authenticate to
    script += '''
    # Determine needed environments
'''

    if deploy_type in ['vpc', 'individual']:
        script += f'''    # Based on original deployment
    needed_envs = set()
'''
        for a in actions:
            if a.get("node_id"):
                script += f'    needed_envs.add("D1")  # Adjust based on your node IDs\n'
                break
            if a.get("node1"):
                script += f'    needed_envs.add("D1")  # Adjust based on your node IDs\n'
                break

    script += '''
    if not needed_envs:
        needed_envs = {"D1"}  # Default - update as needed

    for env in needed_envs:
        if not APIC_URLS.get(env):
            print(f"  [SKIP] No APIC URL for {env}")
            continue
        print(f"\\n[INFO] Authenticating to {env}...")
        session = requests.Session()
        if login_to_apic(session, APIC_URLS[env], username, password):
            sessions[env] = session
            print(f"       [SUCCESS]")
        else:
            print(f"       [FAILED]")

    if not sessions:
        print("\\n[ERROR] No successful authentications.")
        sys.exit(1)

    # Use first available session
    env = list(sessions.keys())[0]
    session = sessions[env]
    apic_url = APIC_URLS[env]

    print("\\n" + "=" * 60)
    print(" ROLLBACK ACTIONS")
    print("=" * 60)
    print()
'''

    # Generate specific rollback code for each action
    action_num = 0

    # Delete bindings first (reverse order of creation)
    binding_actions = [a for a in actions if a["action"] in ["delete_binding"]]
    for a in binding_actions:
        action_num += 1
        tenant = a.get("tenant", "TENANT")
        ap = a.get("app_profile", "APP_PROFILE")
        epg = a.get("epg", "EPG")
        vlan = a.get("vlan", "0")

        if deploy_type == 'vpc':
            node1 = a.get("node1", "NODE1")
            node2 = a.get("node2", "NODE2")
            pg = a.get("vpc_pg", "")
            if not pg:
                for x in actions:
                    if x["action"] == "delete_vpc_ipg":
                        pg = x.get("name", "POLICY_GROUP")
                        break
            script += f'''
    # Action {action_num}: Delete static binding VLAN {vlan}
    print(f"  [{action_num}] Deleting static binding VLAN {vlan} from {epg}...")
    try:
        path = "topology/pod-{POD_ID}/protpaths-{node1}-{node2}/pathep-[{pg}]"
        dn = f"uni/tn-{tenant}/ap-{ap}/epg-{epg}/rspathAtt-[{{path}}]"
        r = session.delete(f"{{apic_url}}/api/mo/{{dn}}.json", verify=False, timeout=30)
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''
        elif deploy_type == 'individual':
            node_id = a.get("node_id", "NODE")
            interface = a.get("interface", "1/1")
            script += f'''
    # Action {action_num}: Delete static binding VLAN {vlan}
    print(f"  [{action_num}] Deleting static binding VLAN {vlan} from {epg}...")
    try:
        path = "topology/pod-{POD_ID}/paths-{node_id}/pathep-[eth{interface}]"
        dn = f"uni/tn-{tenant}/ap-{ap}/epg-{epg}/rspathAtt-[{{path}}]"
        r = session.delete(f"{{apic_url}}/api/mo/{{dn}}.json", verify=False, timeout=30)
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''

    # Delete port selectors
    selector_actions = [a for a in actions if "port_selector" in a["action"]]
    for a in selector_actions:
        action_num += 1
        name = a.get("name", "SELECTOR")
        int_profile = a.get("int_profile", "INT_PROFILE")
        script += f'''
    # Action {action_num}: Delete Port Selector
    print(f"  [{action_num}] Deleting port selector: {name}...")
    try:
        r = session.delete(
            f"{{apic_url}}/api/mo/uni/infra/accportprof-{int_profile}/hports-{name}-typ-range.json",
            verify=False, timeout=30)
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''

    # Delete policy groups
    ipg_actions = [a for a in actions if a["action"] in ["delete_vpc_ipg", "delete_access_ipg"]]
    for a in ipg_actions:
        action_num += 1
        name = a.get("name", "POLICY_GROUP")
        if a["action"] == "delete_vpc_ipg":
            path = f"uni/infra/funcprof/accbundle-{name}"
            label = "VPC Policy Group"
        else:
            path = f"uni/infra/funcprof/accportgrp-{name}"
            label = "Leaf Access Port Policy Group"
        script += f'''
    # Action {action_num}: Delete {label}
    print(f"  [{action_num}] Deleting {label}: {name}...")
    try:
        r = session.delete(f"{{apic_url}}/api/mo/{path}.json", verify=False, timeout=30)
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''

    # Clear port descriptions
    desc_actions = [a for a in actions if a["action"] == "clear_description"]
    for a in desc_actions:
        action_num += 1
        node_id = a.get("node_id", "NODE")
        interface = a.get("interface", "1/1")
        script += f'''
    # Action {action_num}: Clear port description
    print(f"  [{action_num}] Clearing description on node {node_id} eth{interface}...")
    try:
        dn = f"topology/pod-{POD_ID}/node-{node_id}/sys/phys-[eth{interface}]"
        r = session.post(f"{{apic_url}}/api/node/mo/{{dn}}.json",
            json={{"l1PhysIf": {{"attributes": {{"descr": ""}}}}}}, verify=False, timeout=30)
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''

    # EPG Add rollback = delete bindings
    epgadd_actions = [a for a in actions if a["action"] == "delete_binding" and deploy_type == "epgadd"]
    for a in epgadd_actions:
        action_num += 1
        switch = a.get("switch", "SWITCH")
        port = a.get("port", "1/1")
        vlan = a.get("vlan", "0")
        script += f'''
    # Action {action_num}: Delete EPG binding VLAN {vlan} from {switch} port {port}
    print(f"  [{action_num}] Deleting VLAN {vlan} from {switch} port {port}...")
    # NOTE: You need to fill in tenant/ap/epg for this VLAN
    # This requires querying the APIC for the EPG containing VLAN {vlan}
    print(f"      [MANUAL] Locate and delete fvRsPathAtt for VLAN {vlan} on node/pathep")
'''

    # EPG Delete rollback = recreate bindings
    recreate_actions = [a for a in actions if a["action"] == "recreate_binding"]
    for a in recreate_actions:
        action_num += 1
        switch = a.get("switch", "SWITCH")
        port = a.get("port", "1/1")
        vlan = a.get("vlan", "0")
        script += f'''
    # Action {action_num}: Re-create binding VLAN {vlan} on {switch} port {port}
    print(f"  [{action_num}] Re-creating VLAN {vlan} on {switch} port {port}...")
    # NOTE: Requires tenant/ap/epg info and original mode (trunk/access)
    # Query APIC for EPG containing VLAN {vlan}, then re-create fvRsPathAtt
    print(f"      [MANUAL] Use EPG Add script to re-bind VLAN {vlan} to {switch} port {port}")
'''

    script += f'''
    print()
    print("=" * 60)
    print(" ROLLBACK COMPLETE")
    print("=" * 60)
    print(f"\\n  {action_num} action(s) processed")
    print()


if __name__ == "__main__":
    print("\\n[WARNING] This will REVERSE changes from {deploy_type.upper()} Run #{entry_id}")
    confirm = input("Type 'YES' to confirm rollback: ").strip()
    if confirm != "YES":
        print("\\n[CANCELLED]")
        sys.exit(0)
    main()
'''
    return script


# =============================================================================
# CSV VALIDATION
# =============================================================================

def validate_csv_file(filepath, script_type):
    """Validate a CSV file against requirements for the given script type."""
    results = {"valid": True, "errors": [], "warnings": [], "row_count": 0, "columns_found": []}
    reqs = CSV_REQUIREMENTS.get(script_type)
    if not reqs:
        results["warnings"].append(f"No validation rules for type: {script_type}")
        return results

    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                results["valid"] = False
                results["errors"].append("CSV file is empty or has no headers")
                return results

            # Normalize headers
            headers = [h.strip().upper() for h in reader.fieldnames if h]
            results["columns_found"] = headers

            # Check required columns
            missing = [col for col in reqs["required"] if col not in headers]
            if missing:
                results["valid"] = False
                results["errors"].append(f"Missing columns: {', '.join(missing)}")
                return results

            # Validate rows
            rows = list(reader)
            results["row_count"] = len(rows)
            if not rows:
                results["valid"] = False
                results["errors"].append("CSV has headers but no data rows")
                return results

            # Validate each row
            validators = reqs.get("validators", {})
            for i, row in enumerate(rows, 1):
                normalized = {k.strip().upper(): (v.strip() if v else "") for k, v in row.items() if k}
                for col, pattern in validators.items():
                    val = normalized.get(col, "").strip().strip('"').strip("'")
                    if val and not re.match(pattern, val, re.IGNORECASE):
                        results["warnings"].append(f"Row {i}: {col} value '{val}' may be invalid")

                # Check for empty required fields
                for col in reqs["required"]:
                    if not normalized.get(col, "").strip():
                        results["warnings"].append(f"Row {i}: {col} is empty")

    except FileNotFoundError:
        results["valid"] = False
        results["errors"].append(f"File not found: {filepath}")
    except Exception as e:
        results["valid"] = False
        results["errors"].append(f"Parse error: {str(e)}")

    # Cap warnings
    if len(results["warnings"]) > 10:
        total = len(results["warnings"])
        results["warnings"] = results["warnings"][:10]
        results["warnings"].append(f"... and {total - 10} more warnings")

    return results


# =============================================================================
# PROCESS MANAGEMENT
# =============================================================================

def run_script_thread(script_path, csv_path):
    global running_process, current_run
    current_run["start_time"] = time.time()
    current_run["output_lines"] = []

    try:
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        env['ACI_WEB_UI'] = '1'

        running_process = subprocess.Popen(
            [sys.executable, '-u', script_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, env=env,
            cwd=os.path.dirname(os.path.abspath(script_path)) or '.'
        )

        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

        buffer = []
        buffer_lock = threading.Lock()
        last_char_time = [time.time()]
        read_complete = [False]

        # Auto-credential tracking
        cred_state = {"awaiting": None}  # None, "username", "password"

        def reader_thread():
            while True:
                try:
                    byte = running_process.stdout.read(1)
                    if not byte: read_complete[0] = True; break
                    with buffer_lock: buffer.append(byte); last_char_time[0] = time.time()
                except: read_complete[0] = True; break

        reader = threading.Thread(target=reader_thread, daemon=True)
        reader.start()

        while not read_complete[0] or buffer:
            time.sleep(0.05)
            with buffer_lock:
                if not buffer: continue
                try:
                    data = b''.join(buffer)
                    text = data.decode('utf-8', errors='replace')
                except: continue

                while '\n' in text:
                    line, text = text.split('\n', 1)
                    line = ansi_escape.sub('', line).strip()
                    if line:
                        output_queue.put(('output', line))
                        current_run["output_lines"].append(line)

                buffer.clear()
                if text: buffer.extend([bytes([b]) for b in text.encode('utf-8')])

                if text.strip():
                    time_since_last = time.time() - last_char_time[0]
                    tc = text.strip()
                    tl = tc.lower()
                    is_prompt = (tc.endswith(':') or tc.endswith('?') or
                                '(1/2)' in tl or '(yes/no)' in tl or '(y/n)' in tl)
                    if is_prompt and time_since_last > 0.1:
                        text_out = ansi_escape.sub('', text).strip()
                        if text_out:
                            output_queue.put(('output', text_out))
                            current_run["output_lines"].append(text_out)
                        buffer.clear()

                        # AUTO-CREDENTIAL INJECTION
                        if stored_credentials.get("set"):
                            if 'username' in tl and tl.endswith(':'):
                                cred_state["awaiting"] = "username"
                                time.sleep(0.1)
                                try:
                                    uname = stored_credentials["username"]
                                    running_process.stdin.write((uname + '\n').encode('utf-8'))
                                    running_process.stdin.flush()
                                    output_queue.put(('output', f'[CREDENTIALS] Auto-filled username: {uname}'))
                                    current_run["output_lines"].append(f'[CREDENTIALS] Auto-filled username: {uname}')
                                except:
                                    pass
                            elif 'password' in tl and tl.endswith(':'):
                                cred_state["awaiting"] = "password"
                                time.sleep(0.1)
                                try:
                                    running_process.stdin.write((stored_credentials["password"] + '\n').encode('utf-8'))
                                    running_process.stdin.flush()
                                    output_queue.put(('output', '[CREDENTIALS] Auto-filled password: ••••••••'))
                                    current_run["output_lines"].append('[CREDENTIALS] Auto-filled password')
                                except:
                                    pass

        reader.join(timeout=1.0)
        with buffer_lock:
            if buffer:
                try:
                    data = b''.join(buffer)
                    text = data.decode('utf-8', errors='replace')
                    text = ansi_escape.sub('', text).strip()
                    if text:
                        output_queue.put(('output', text))
                        current_run["output_lines"].append(text)
                except: pass

        running_process.wait()
        exit_code = running_process.returncode
        output_queue.put(('exit', exit_code))

        duration = time.time() - current_run["start_time"]
        status = "success" if exit_code == 0 else "failed"
        lines = current_run["output_lines"]
        deploy_count = sum(1 for l in lines if any(m in l.upper() for m in
                          ['[DEPLOYED]', '[CREATED]', '[SUCCESS]', 'BINDING CREATED', ': OK',
                           '[OK]', '[DELETED]']))
        deploy_count = max(1, deploy_count)
        if current_run["type"]:
            add_log_entry(current_run["type"], current_run["csv_path"], status, deploy_count, duration, lines)

    except Exception as e:
        output_queue.put(('error', str(e)))
        if current_run["type"] and current_run["start_time"]:
            duration = time.time() - current_run["start_time"]
            add_log_entry(current_run["type"], current_run["csv_path"], "failed", 0, duration, current_run["output_lines"])
    finally:
        running_process = None


def send_input_to_process(text):
    global running_process
    if running_process and running_process.stdin:
        try:
            running_process.stdin.write((text + '\n').encode('utf-8'))
            running_process.stdin.flush()
            return True
        except: pass
    return False

def stop_process():
    global running_process, current_run
    if running_process:
        try:
            running_process.terminate()
            time.sleep(0.5)
            if running_process.poll() is None: running_process.kill()
        except: pass
        if current_run["type"] and current_run["start_time"]:
            duration = time.time() - current_run["start_time"]
            add_log_entry(current_run["type"], current_run["csv_path"], "stopped", 0, duration, current_run["output_lines"])
        running_process = None


# =============================================================================
# HTML TEMPLATE
# =============================================================================

HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ACI Bulk Deployment Console</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
:root{--bg-darkest:#0d1117;--bg-dark:#161b22;--bg-sidebar:#0d1117;--bg-terminal:#1e1e2e;--bg-input:#252535;--border-color:#30363d;--text-primary:#e6edf3;--text-secondary:#8b949e;--text-muted:#6e7681;--accent-blue:#58a6ff;--accent-cyan:#39d4d4;--accent-green:#3fb950;--accent-orange:#f0883e;--accent-red:#f85149;--accent-purple:#a371f7;--accent-yellow:#d29922;--glow-cyan:rgba(57,212,212,0.15)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'IBM Plex Sans',-apple-system,sans-serif;background:var(--bg-darkest);color:var(--text-primary);height:100vh;overflow:hidden}
.app-container{display:flex;height:100vh}
.sidebar{width:280px;background:var(--bg-sidebar);border-right:1px solid var(--border-color);display:flex;flex-direction:column}
.sidebar-header{padding:20px;border-bottom:1px solid var(--border-color)}
.logo{display:flex;align-items:center;gap:12px}
.logo-icon{width:40px;height:40px;background:linear-gradient(135deg,var(--accent-cyan),var(--accent-blue));border-radius:10px;display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:14px;color:var(--bg-darkest);box-shadow:0 4px 20px var(--glow-cyan)}
.logo-text{font-size:18px;font-weight:600;background:linear-gradient(135deg,var(--accent-cyan),var(--accent-blue));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.logo-subtitle{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1.5px}
.nav-section{padding:16px 12px;flex:1;overflow-y:auto}
.nav-label{font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:1px;padding:0 12px;margin-bottom:8px;margin-top:16px}
.nav-label:first-child{margin-top:0}
.nav-item{display:flex;align-items:center;gap:12px;padding:12px 16px;border-radius:8px;cursor:pointer;transition:all .2s ease;margin-bottom:4px;border:1px solid transparent}
.nav-item:hover{background:rgba(88,166,255,.08);border-color:rgba(88,166,255,.2)}
.nav-item.active{background:linear-gradient(135deg,rgba(57,212,212,.12),rgba(88,166,255,.12));border-color:var(--accent-cyan);box-shadow:0 0 20px var(--glow-cyan)}
.nav-icon{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px}
.nav-item.vpc .nav-icon{background:linear-gradient(135deg,var(--accent-purple),var(--accent-blue))}
.nav-item.individual .nav-icon{background:linear-gradient(135deg,var(--accent-orange),var(--accent-yellow))}
.nav-item.settings .nav-icon{background:linear-gradient(135deg,var(--accent-cyan),var(--accent-green))}
.nav-item.readme .nav-icon{background:linear-gradient(135deg,#f093fb,#f5576c)}
.nav-item.epgadd .nav-icon{background:linear-gradient(135deg,#11998e,#38ef7d)}
.nav-item.epgdelete .nav-icon{background:linear-gradient(135deg,#eb3349,#f45c43)}
.nav-item.logs .nav-icon{background:linear-gradient(135deg,#667eea,#764ba2)}
.nav-item.credentials .nav-icon{background:linear-gradient(135deg,#f7971e,#ffd200)}
.nav-item-title{font-weight:600;font-size:14px;margin-bottom:2px}
.nav-item-desc{font-size:11px;color:var(--text-muted)}
.cred-badge{margin-left:auto;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600}
.cred-badge.set{background:rgba(63,185,80,.2);color:var(--accent-green)}
.cred-badge.unset{background:rgba(248,81,73,.15);color:var(--accent-red)}
.status-indicator{width:8px;height:8px;border-radius:50%;background:var(--accent-green);box-shadow:0 0 8px var(--accent-green);animation:pulse 2s infinite}
.status-indicator.running{background:var(--accent-orange);box-shadow:0 0 8px var(--accent-orange);animation:pulse .5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.sidebar-footer{padding:16px;border-top:1px solid var(--border-color)}
.footer-info{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text-muted)}
.footer-version{padding:2px 8px;background:var(--bg-input);border-radius:4px;font-family:'JetBrains Mono',monospace}
.main-content{flex:1;display:flex;flex-direction:column;background:var(--bg-dark);overflow:hidden}
.header-bar{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:var(--bg-darkest);border-bottom:1px solid var(--border-color)}
.header-title{display:flex;align-items:center;gap:12px}
.header-title h2{font-size:16px;font-weight:600}
.header-badge{padding:4px 10px;border-radius:12px;font-size:11px;font-weight:600;text-transform:uppercase}
.header-badge.vpc{background:rgba(163,113,247,.2);color:var(--accent-purple)}
.header-badge.individual{background:rgba(240,136,62,.2);color:var(--accent-orange)}
.header-badge.settings{background:rgba(57,212,212,.2);color:var(--accent-cyan)}
.header-badge.readme{background:rgba(245,87,108,.2);color:#f5576c}
.header-badge.epgadd{background:rgba(56,239,125,.2);color:#38ef7d}
.header-badge.epgdelete{background:rgba(244,92,67,.2);color:#f45c43}
.header-badge.logs{background:rgba(118,75,162,.2);color:#a78bfa}
.header-badge.credentials{background:rgba(247,151,30,.2);color:#ffd200}
.header-actions{display:flex;gap:8px}
.header-btn{padding:8px 16px;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;transition:all .2s ease;border:1px solid var(--border-color);background:transparent;color:var(--text-secondary);font-family:inherit}
.header-btn:hover{border-color:var(--accent-cyan);color:var(--accent-cyan)}
.header-btn.primary{background:linear-gradient(135deg,var(--accent-cyan),var(--accent-blue));border:none;color:var(--bg-darkest);font-weight:600}
.header-btn.primary:hover{box-shadow:0 4px 20px var(--glow-cyan)}
.header-btn.primary:disabled{opacity:.5;cursor:not-allowed}
.header-btn.danger{border-color:var(--accent-red);color:var(--accent-red)}
.header-btn.danger:hover{background:rgba(248,81,73,.1)}
.header-btn.danger:disabled{opacity:.3;cursor:not-allowed}
.config-panel{padding:20px;background:var(--bg-darkest);border-bottom:1px solid var(--border-color)}
.config-row{display:flex;gap:16px;align-items:flex-end}
.config-group{flex:1}
.config-label{display:block;font-size:12px;font-weight:600;color:var(--text-secondary);margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px}
.config-input{width:100%;padding:12px 16px;background:var(--bg-input);border:1px solid var(--border-color);border-radius:8px;color:var(--text-primary);font-family:'JetBrains Mono',monospace;font-size:13px}
.config-input:focus{outline:none;border-color:var(--accent-cyan);box-shadow:0 0 0 3px var(--glow-cyan)}
.file-picker-row{display:flex;gap:12px;align-items:center}
.file-picker-display{flex:1;padding:12px 16px;background:var(--bg-input);border:2px dashed var(--border-color);border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:13px;display:flex;align-items:center;gap:10px;min-height:46px;transition:border-color .2s}
.file-picker-display.has-file{border-style:solid;border-color:var(--accent-green)}
.fp-icon{font-size:18px}.fp-name{color:var(--text-primary);font-weight:500}.fp-placeholder{color:var(--text-muted)}
.file-picker-btn{padding:12px 20px;border:1px solid var(--accent-cyan);border-radius:8px;background:linear-gradient(135deg,rgba(57,212,212,.1),rgba(88,166,255,.1));color:var(--accent-cyan);font-family:'IBM Plex Sans',sans-serif;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s;white-space:nowrap}
.file-picker-btn:hover{background:linear-gradient(135deg,rgba(57,212,212,.2),rgba(88,166,255,.2));box-shadow:0 0 16px var(--glow-cyan)}
.file-input-hidden{display:none}
.csv-validation{margin-top:8px;padding:10px 14px;border-radius:6px;font-size:12px;font-family:'JetBrains Mono',monospace}
.csv-validation.valid{background:rgba(63,185,80,.1);border:1px solid rgba(63,185,80,.3);color:var(--accent-green)}
.csv-validation.invalid{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:var(--accent-red)}
.csv-validation.warnings{background:rgba(210,153,34,.1);border:1px solid rgba(210,153,34,.3);color:var(--accent-yellow)}
.csv-reference{padding:16px 20px;background:rgba(57,212,212,.05);border-bottom:1px solid var(--border-color)}
.csv-reference-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.csv-reference-title{font-size:12px;font-weight:600;color:var(--accent-cyan);text-transform:uppercase;letter-spacing:1px}
.csv-reference-toggle{font-size:11px;color:var(--text-muted);cursor:pointer;padding:4px 8px;border-radius:4px}
.csv-reference-toggle:hover{background:var(--bg-input);color:var(--text-primary)}
.csv-table{width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:12px}
.csv-table th{background:var(--bg-input);padding:10px 12px;text-align:left;font-weight:600;color:var(--accent-cyan);border:1px solid var(--border-color)}
.csv-table td{padding:8px 12px;border:1px solid var(--border-color);color:var(--text-secondary)}
.csv-example{margin-top:12px;padding:12px;background:var(--bg-terminal);border-radius:6px;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text-muted)}
.csv-example-label{color:var(--accent-green);margin-bottom:4px}
.terminal-container{flex:1;display:flex;flex-direction:column;margin:16px;border-radius:12px;overflow:hidden;border:1px solid var(--border-color);background:var(--bg-terminal);min-height:0}
.terminal-header{display:flex;align-items:center;padding:12px 16px;background:rgba(0,0,0,.3);border-bottom:1px solid var(--border-color)}
.terminal-dots{display:flex;gap:8px;margin-right:16px}
.terminal-dot{width:12px;height:12px;border-radius:50%}
.terminal-dot.red{background:#ff5f56}.terminal-dot.yellow{background:#ffbd2e}.terminal-dot.green{background:#27ca40}
.terminal-title{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text-muted)}
.terminal-status{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text-muted)}
.terminal-status-dot{width:6px;height:6px;border-radius:50%;background:var(--accent-green)}
.terminal-status.running .terminal-status-dot{background:var(--accent-orange);animation:pulse .5s infinite}
.terminal-output{flex:1;padding:16px;overflow-y:auto;overflow-x:hidden;font-family:'JetBrains Mono',monospace;font-size:13px;line-height:1.6;min-height:0;max-height:100%}
.terminal-output::-webkit-scrollbar{width:10px}
.terminal-output::-webkit-scrollbar-track{background:var(--bg-darkest);border-radius:5px}
.terminal-output::-webkit-scrollbar-thumb{background:var(--border-color);border-radius:5px;border:2px solid var(--bg-darkest)}
.terminal-output::-webkit-scrollbar-thumb:hover{background:var(--text-muted)}
.terminal-output{scrollbar-width:thin;scrollbar-color:var(--border-color) var(--bg-darkest)}
.terminal-line{white-space:pre-wrap;word-break:break-all;margin-bottom:1px}
.terminal-line.header{color:var(--accent-cyan);font-weight:600}
.terminal-line.success{color:var(--accent-green)}
.terminal-line.error{color:var(--accent-red)}
.terminal-line.warning{color:var(--accent-orange)}
.terminal-line.info{color:var(--accent-blue)}
.terminal-line.muted{color:var(--text-muted)}
.terminal-line.prompt{color:var(--accent-purple)}
.terminal-line.credential{color:var(--accent-yellow)}
.bracket-num{color:var(--accent-blue)!important;font-weight:700}
.terminal-input-area{display:flex;align-items:center;padding:12px 16px;background:rgba(0,0,0,.3);border-top:1px solid var(--border-color);gap:12px}
.terminal-prompt{color:var(--accent-cyan);font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600}
.terminal-input{flex:1;background:transparent;border:none;color:var(--text-primary);font-family:'JetBrains Mono',monospace;font-size:13px;outline:none}
.terminal-input:disabled{opacity:.5}
.terminal-submit{padding:8px 16px;background:linear-gradient(135deg,var(--accent-cyan),var(--accent-blue));border:none;border-radius:6px;color:var(--bg-darkest);font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;cursor:pointer}
.terminal-submit:disabled{opacity:.5;cursor:not-allowed}
.welcome-screen{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px;text-align:center}
.welcome-icon{width:80px;height:80px;background:linear-gradient(135deg,var(--accent-cyan),var(--accent-blue));border-radius:20px;display:flex;align-items:center;justify-content:center;font-size:36px;margin-bottom:24px;box-shadow:0 8px 40px var(--glow-cyan)}
.welcome-title{font-size:28px;font-weight:700;margin-bottom:12px}
.welcome-desc{color:var(--text-muted);font-size:15px;max-width:500px;line-height:1.6;margin-bottom:32px}
.welcome-cards{display:flex;gap:20px}
.welcome-card{padding:24px;background:var(--bg-darkest);border:1px solid var(--border-color);border-radius:12px;cursor:pointer;transition:all .3s ease;width:200px}
.welcome-card:hover{border-color:var(--accent-cyan);transform:translateY(-4px);box-shadow:0 8px 30px var(--glow-cyan)}
.welcome-card-icon{width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:24px;margin-bottom:16px}
.welcome-card.vpc .welcome-card-icon{background:linear-gradient(135deg,var(--accent-purple),var(--accent-blue))}
.welcome-card.individual .welcome-card-icon{background:linear-gradient(135deg,var(--accent-orange),var(--accent-yellow))}
.welcome-card-title{font-weight:600;font-size:15px;margin-bottom:8px}
.welcome-card-desc{font-size:12px;color:var(--text-muted);line-height:1.5}
.time-saved-banner{margin-top:32px;padding:20px 36px;background:linear-gradient(135deg,rgba(57,212,212,.06),rgba(88,166,255,.06));border:1px solid rgba(57,212,212,.25);border-radius:12px;display:flex;gap:32px;align-items:center}
.ts-stat{text-align:center}
.ts-value{font-size:28px;font-weight:700;font-family:'JetBrains Mono',monospace;background:linear-gradient(135deg,var(--accent-cyan),var(--accent-green));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.ts-value.blue{background:linear-gradient(135deg,var(--accent-blue),var(--accent-purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.ts-value.purple{-webkit-text-fill-color:var(--accent-purple)}
.ts-label{font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:1px;margin-top:4px}
.ts-divider{width:1px;height:40px;background:var(--border-color)}
/* Credential Panel */
.cred-panel{padding:32px;overflow-y:auto;flex:1}
.cred-section{background:var(--bg-darkest);border:1px solid var(--border-color);border-radius:12px;padding:24px;margin-bottom:20px;max-width:600px}
.cred-section-title{font-size:14px;font-weight:600;color:#ffd200;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.cred-status{padding:12px 16px;border-radius:8px;margin-bottom:20px;font-size:13px;display:flex;align-items:center;gap:10px}
.cred-status.set{background:rgba(63,185,80,.1);border:1px solid rgba(63,185,80,.3);color:var(--accent-green)}
.cred-status.unset{background:rgba(248,81,73,.08);border:1px solid rgba(248,81,73,.2);color:var(--accent-red)}
.cred-row{margin-bottom:16px}
.cred-label{display:block;font-size:12px;font-weight:500;color:var(--text-secondary);margin-bottom:8px}
.cred-input{width:100%;padding:12px 16px;background:var(--bg-input);border:1px solid var(--border-color);border-radius:8px;color:var(--text-primary);font-family:'JetBrains Mono',monospace;font-size:13px}
.cred-input:focus{outline:none;border-color:#ffd200;box-shadow:0 0 0 3px rgba(247,151,30,.15)}
.cred-hint{font-size:11px;color:var(--text-muted);margin-top:12px;padding:10px;background:var(--bg-input);border-radius:6px;line-height:1.6}
.cred-actions{display:flex;gap:12px;margin-top:20px}
.cred-btn{padding:10px 20px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:none;font-family:inherit}
.cred-btn.save{background:linear-gradient(135deg,#f7971e,#ffd200);color:#0d1117}
.cred-btn.save:hover{box-shadow:0 4px 16px rgba(247,151,30,.3)}
.cred-btn.clear{background:transparent;border:1px solid var(--accent-red);color:var(--accent-red)}
.cred-btn.clear:hover{background:rgba(248,81,73,.1)}
/* Settings, Readme, Logs panels - same as v1.2.0 */
.settings-panel{padding:24px;overflow-y:auto;flex:1}
.settings-section{background:var(--bg-darkest);border:1px solid var(--border-color);border-radius:12px;padding:20px;margin-bottom:20px}
.settings-section-title{font-size:14px;font-weight:600;color:var(--accent-cyan);margin-bottom:16px}
.settings-row{margin-bottom:16px}.settings-row:last-child{margin-bottom:0}
.settings-label{display:block;font-size:12px;font-weight:500;color:var(--text-secondary);margin-bottom:8px}
.settings-input{width:100%;padding:12px 16px;background:var(--bg-input);border:1px solid var(--border-color);border-radius:8px;color:var(--text-primary);font-family:'JetBrains Mono',monospace;font-size:13px}
.settings-input:focus{outline:none;border-color:var(--accent-cyan)}
.settings-hint{font-size:11px;color:var(--text-muted);margin-top:6px}
.readme-panel{padding:24px;overflow-y:auto;flex:1}
.readme-section{background:var(--bg-darkest);border:1px solid var(--border-color);border-radius:12px;padding:24px;margin-bottom:20px}
.readme-section-title{font-size:18px;font-weight:700;color:var(--text-primary);margin-bottom:16px;display:flex;align-items:center;gap:10px}
.readme-section-title span{font-size:24px}
.readme-content{color:var(--text-secondary);line-height:1.8;font-size:14px}
.readme-content h3{color:var(--accent-cyan);font-size:15px;margin:20px 0 12px;font-weight:600}
.readme-content h3:first-child{margin-top:0}
.readme-content p{margin-bottom:12px}
.readme-tabs{display:flex;gap:8px;padding:0 24px;margin-bottom:0}
.readme-tab{padding:10px 16px;border-radius:8px 8px 0 0;background:var(--bg-darkest);border:1px solid var(--border-color);border-bottom:none;color:var(--text-muted);cursor:pointer;font-size:13px;font-weight:500}
.readme-tab.active{background:var(--bg-dark);color:var(--text-primary);border-bottom:2px solid var(--accent-cyan)}
.readme-tab-content{display:none}.readme-tab-content.active{display:block}
.step{display:flex;gap:16px;margin-bottom:20px}.step-number{width:32px;height:32px;border-radius:50%;background:var(--accent-cyan);color:var(--bg-darkest);display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0}.step-title{font-weight:600;margin-bottom:4px}
/* Logs */
.logs-panel{padding:24px;overflow-y:auto;flex:1}
.log-stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}
.log-stat-card{background:var(--bg-darkest);border:1px solid var(--border-color);border-radius:10px;padding:16px;text-align:center}
.sv{font-size:24px;font-weight:700;font-family:'JetBrains Mono',monospace}
.sv.green{color:var(--accent-green)}.sv.blue{color:var(--accent-blue)}.sv.purple{color:var(--accent-purple)}.sv.orange{color:var(--accent-orange)}
.sl{font-size:12px;color:var(--text-secondary);margin-top:4px}.ss{font-size:10px;color:var(--text-muted)}
.log-entries-section{background:var(--bg-darkest);border:1px solid var(--border-color);border-radius:10px;padding:16px}
.log-entries-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.log-entries-title{font-size:14px;font-weight:600}
.log-clear-btn{padding:6px 12px;border-radius:6px;background:transparent;border:1px solid var(--accent-red);color:var(--accent-red);cursor:pointer;font-size:11px;font-family:inherit}
.log-entry{display:flex;align-items:center;gap:12px;padding:12px;border-radius:8px;margin-bottom:8px;background:var(--bg-dark);border:1px solid var(--border-color);transition:border-color .2s}
.log-entry:hover{border-color:var(--accent-cyan)}
.log-entry-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.log-entry-dot.success{background:var(--accent-green)}.log-entry-dot.failed{background:var(--accent-red)}.log-entry-dot.stopped{background:var(--accent-orange)}
.log-entry-info{flex:1;min-width:0}
.log-entry-title{font-weight:600;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.log-entry-meta{font-size:11px;color:var(--text-muted)}
.log-entry-type{padding:3px 8px;border-radius:6px;font-size:10px;font-weight:600;white-space:nowrap}
.log-entry-type.vpc{background:rgba(163,113,247,.2);color:var(--accent-purple)}
.log-entry-type.individual{background:rgba(240,136,62,.2);color:var(--accent-orange)}
.log-entry-type.epgadd{background:rgba(56,239,125,.2);color:#38ef7d}
.log-entry-type.epgdelete{background:rgba(244,92,67,.2);color:#f45c43}
.log-entry-saved{font-size:12px;font-weight:600;color:var(--accent-green);white-space:nowrap}
.log-entry-actions{display:flex;gap:6px;flex-shrink:0}
.log-action-btn{padding:4px 10px;border-radius:5px;font-size:10px;font-weight:600;cursor:pointer;border:none;font-family:inherit;transition:all .2s}
.log-action-btn.download{background:rgba(88,166,255,.15);color:var(--accent-blue)}
.log-action-btn.download:hover{background:rgba(88,166,255,.3)}
.log-action-btn.rollback{background:rgba(248,81,73,.12);color:var(--accent-red)}
.log-action-btn.rollback:hover{background:rgba(248,81,73,.25)}
.log-empty{text-align:center;padding:40px;color:var(--text-muted)}
.log-empty-icon{font-size:40px;margin-bottom:12px}
/* CSV toggles & editor */
.csv-toggle-group{display:flex;gap:8px;margin-bottom:12px}
.csv-toggle{padding:8px 16px;border-radius:6px;background:var(--bg-input);border:1px solid var(--border-color);color:var(--text-muted);cursor:pointer;font-size:12px;font-weight:500;font-family:inherit}
.csv-toggle.active{border-color:var(--accent-cyan);color:var(--accent-cyan);background:rgba(57,212,212,.08)}
.csv-editor-section{margin-top:12px}
.csv-editor-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.csv-editor-title{font-size:12px;font-weight:600;color:var(--text-secondary)}
.csv-editor-actions{display:flex;gap:8px}
.csv-editor-btn{padding:6px 12px;border-radius:5px;background:var(--bg-input);border:1px solid var(--border-color);color:var(--text-secondary);cursor:pointer;font-size:11px;font-family:inherit}
.csv-editor-btn.add{border-color:var(--accent-green);color:var(--accent-green)}
.csv-editor-table{width:100%;border-collapse:collapse;font-size:12px}
.csv-editor-table th{background:var(--bg-input);padding:8px 10px;text-align:left;color:var(--accent-cyan);border:1px solid var(--border-color);font-weight:600}
.csv-editor-table td{padding:4px;border:1px solid var(--border-color)}
.csv-editor-table input{width:100%;padding:6px 8px;background:var(--bg-terminal);border:1px solid transparent;border-radius:4px;color:var(--text-primary);font-family:'JetBrains Mono',monospace;font-size:11px}
.csv-editor-table input:focus{border-color:var(--accent-cyan);outline:none}
.row-actions{width:36px;text-align:center}
.delete-row{background:none;border:none;color:var(--accent-red);cursor:pointer;font-size:14px;opacity:.5}
.delete-row:hover{opacity:1}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="app-container">
<aside class="sidebar">
<div class="sidebar-header"><div class="logo"><div class="logo-icon">ACI</div><div><div class="logo-text">Bulk Deploy</div><div class="logo-subtitle">Automation Console</div></div></div></div>
<nav class="nav-section">
<div class="nav-label">Deployments</div>
<div class="nav-item vpc" onclick="selectView('vpc')"><div class="nav-icon">⚡</div><div><div class="nav-item-title">VPC Bulk</div><div class="nav-item-desc">Virtual Port Channels</div></div></div>
<div class="nav-item individual" onclick="selectView('individual')"><div class="nav-icon">🔌</div><div><div class="nav-item-title">Port Bulk</div><div class="nav-item-desc">Individual Ports</div></div></div>
<div class="nav-item epgadd" onclick="selectView('epgadd')"><div class="nav-icon">➕</div><div><div class="nav-item-title">EPG Add</div><div class="nav-item-desc">Add EPGs to ports</div></div></div>
<div class="nav-item epgdelete" onclick="selectView('epgdelete')"><div class="nav-icon">➖</div><div><div class="nav-item-title">EPG Delete</div><div class="nav-item-desc">Remove EPGs from ports</div></div></div>
<div class="nav-label">Management</div>
<div class="nav-item credentials" onclick="selectView('credentials')"><div class="nav-icon">🔑</div><div><div class="nav-item-title">Credentials</div><div class="nav-item-desc">APIC authentication</div></div><span class="cred-badge unset" id="credNavBadge">NOT SET</span></div>
<div class="nav-item logs" onclick="selectView('logs')"><div class="nav-icon">📊</div><div><div class="nav-item-title">Deploy Log</div><div class="nav-item-desc">History &amp; rollback</div></div></div>
<div class="nav-item settings" onclick="selectView('settings')"><div class="nav-icon">⚙️</div><div><div class="nav-item-title">Settings</div><div class="nav-item-desc">Script paths &amp; configuration</div></div></div>
<div class="nav-label">Documentation</div>
<div class="nav-item readme" onclick="selectView('readme')"><div class="nav-icon">📖</div><div><div class="nav-item-title">README</div><div class="nav-item-desc">Instructions &amp; help guide</div></div></div>
</nav>
<div class="sidebar-footer"><div class="footer-info"><span class="status-indicator" id="globalStatus"></span><span id="statusText">Ready</span><span class="footer-version" id="versionBadge">v{{ config.version }}</span></div></div>
</aside>
<main class="main-content" id="mainContent">

<!-- WELCOME -->
<div class="welcome-screen" id="welcomeScreen">
<div class="welcome-icon">🚀</div><h1 class="welcome-title">ACI Bulk Deployment</h1>
<p class="welcome-desc">Streamline your Cisco ACI fabric deployments with automated VPC and individual port configurations.</p>
<div class="welcome-cards">
<div class="welcome-card vpc" onclick="selectView('vpc')"><div class="welcome-card-icon">⚡</div><div class="welcome-card-title">VPC Bulk</div><div class="welcome-card-desc">Deploy Virtual Port Channels across switch pairs</div></div>
<div class="welcome-card individual" onclick="selectView('individual')"><div class="welcome-card-icon">🔌</div><div class="welcome-card-title">Port Bulk</div><div class="welcome-card-desc">Deploy individual access and trunk ports</div></div>
</div>
<div class="time-saved-banner"><div class="ts-stat"><div class="ts-value" id="wTimeSaved">0m</div><div class="ts-label">Total Time Saved</div></div><div class="ts-divider"></div><div class="ts-stat"><div class="ts-value blue" id="wDeploys">0</div><div class="ts-label">Total Deployments</div></div><div class="ts-divider"></div><div class="ts-stat"><div class="ts-value purple" id="wRuns">0</div><div class="ts-label">Script Runs</div></div></div>
</div>

<!-- DEPLOYMENT SCREENS -->
<div id="vpcScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden"></div>
<div id="individualScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden"></div>
<div id="epgaddScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden"></div>
<div id="epgdeleteScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden"></div>

<!-- CREDENTIALS -->
<div id="credentialsScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
<div class="header-bar"><div class="header-title"><h2>APIC Credentials</h2><span class="header-badge credentials">AUTH</span></div></div>
<div class="cred-panel">
<div class="cred-section">
<div class="cred-section-title">🔑 APIC Credential Manager</div>
<div class="cred-status unset" id="credStatusBox"><span id="credStatusIcon">⚠️</span> <span id="credStatusText">No credentials stored</span></div>
<div class="cred-row"><label class="cred-label">APIC Username</label><input type="text" class="cred-input" id="credUsername" placeholder="admin" autocomplete="off"></div>
<div class="cred-row"><label class="cred-label">APIC Password</label><input type="password" class="cred-input" id="credPassword" placeholder="••••••••" autocomplete="off"></div>
<div class="cred-actions"><button class="cred-btn save" onclick="saveCredentials()">Save Credentials</button><button class="cred-btn clear" onclick="clearCredentials()">Clear</button></div>
<div class="cred-hint">🛡️ Credentials are stored <strong>in memory only</strong> and are never written to disk. They clear automatically when the app restarts.<br><br>When set, credentials are auto-injected into scripts when they prompt for Username/Password — no manual typing needed.</div>
</div>
</div></div>

<!-- LOGS -->
<div id="logsScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
<div class="header-bar"><div class="header-title"><h2>Deployment Log</h2><span class="header-badge logs">LOG</span></div><div class="header-actions"><button class="header-btn" onclick="refreshLog()">Refresh</button></div></div>
<div class="logs-panel">
<div class="log-stats-grid">
<div class="log-stat-card"><div class="sv green" id="logTimeSaved">0m</div><div class="sl">Time Saved</div><div class="ss">vs manual APIC GUI</div></div>
<div class="log-stat-card"><div class="sv blue" id="logDeploys">0</div><div class="sl">Deployments</div><div class="ss">total objects created</div></div>
<div class="log-stat-card"><div class="sv purple" id="logRuns">0</div><div class="sl">Script Runs</div><div class="ss">total executions</div></div>
<div class="log-stat-card"><div class="sv orange" id="logSuccessRate">—</div><div class="sl">Success Rate</div><div class="ss">completed runs</div></div>
</div>
<div class="log-entries-section"><div class="log-entries-header"><span class="log-entries-title">📋 Run History</span><button class="log-clear-btn" onclick="clearLog()">Clear Log</button></div><div id="logEntriesContainer"><div class="log-empty"><div class="log-empty-icon">📭</div>No deployments yet. Run a script to see history here.</div></div></div>
</div></div>

<!-- SETTINGS -->
<div id="settingsScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
<div class="header-bar"><div class="header-title"><h2>Settings</h2><span class="header-badge settings">CONFIG</span></div><div class="header-actions"><button class="header-btn primary" onclick="saveSettings()">Save Settings</button></div></div>
<div class="settings-panel">
<div class="settings-section"><div class="settings-section-title">📁 Script Paths</div>
<div class="settings-row"><label class="settings-label">VPC Deployment Script</label><input type="text" class="settings-input" id="settingsVpcScript" value="{{ config.vpc_script }}"><div class="settings-hint">Path to the VPC bulk deployment Python script</div></div>
<div class="settings-row"><label class="settings-label">Individual Port Deployment Script</label><input type="text" class="settings-input" id="settingsIndividualScript" value="{{ config.individual_script }}"><div class="settings-hint">Path to the individual port bulk deployment Python script</div></div>
<div class="settings-row"><label class="settings-label">EPG Add Script</label><input type="text" class="settings-input" id="settingsEpgaddScript" value="{{ config.epgadd_script }}"></div>
<div class="settings-row"><label class="settings-label">EPG Delete Script</label><input type="text" class="settings-input" id="settingsEpgdeleteScript" value="{{ config.epgdelete_script }}"></div>
</div>
<div class="settings-section"><div class="settings-section-title">ℹ️ Application Info</div><div class="settings-row"><label class="settings-label">Version</label><input type="text" class="settings-input" id="settingsVersion" value="{{ config.version }}"></div></div>
</div></div>

<!-- README -->
<div id="readmeScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
<div class="header-bar"><div class="header-title"><h2>Documentation</h2><span class="header-badge readme">README</span></div></div>
<div class="readme-panel">
<div class="readme-tabs">
<div class="readme-tab active" onclick="switchReadmeTab('ui')">🖥️ Using the UI</div>
<div class="readme-tab" onclick="switchReadmeTab('vpc')">⚡ VPC</div>
<div class="readme-tab" onclick="switchReadmeTab('individual')">🔌 Port</div>
<div class="readme-tab" onclick="switchReadmeTab('troubleshoot')">🔧 Troubleshoot</div>
</div>
<div id="readmeTabUi" class="readme-tab-content active"><div class="readme-section"><div class="readme-section-title"><span>🚀</span> Getting Started</div><div class="readme-content">
<div class="step"><div class="step-number">1</div><div class="step-content"><div class="step-title">Set Credentials</div><div>Click <strong>Credentials</strong> in the sidebar and enter your APIC username/password. These auto-fill during deployments.</div></div></div>
<div class="step"><div class="step-number">2</div><div class="step-content"><div class="step-title">Select Deployment Type</div><div>Click a deployment type in the sidebar.</div></div></div>
<div class="step"><div class="step-number">3</div><div class="step-content"><div class="step-title">Select CSV File</div><div>Click <strong>Browse Files</strong> to open your file explorer. CSV is validated automatically.</div></div></div>
<div class="step"><div class="step-number">4</div><div class="step-content"><div class="step-title">Run &amp; Respond</div><div>Click <strong>Run Script</strong>. Credentials auto-inject. Type responses for prompts in the input bar.</div></div></div>
<div class="step"><div class="step-number">5</div><div class="step-content"><div class="step-title">Review Logs &amp; Rollback</div><div>Check <strong>Deploy Log</strong> for history. Download sanitized logs for work orders. Use <strong>Rollback</strong> to generate reversal scripts.</div></div></div>
</div></div></div>
<div id="readmeTabVpc" class="readme-tab-content"><div class="readme-section"><div class="readme-section-title"><span>⚡</span> VPC Bulk Deployment</div><div class="readme-content"><p>Deploy VPCs across switch pairs. CSV columns: Hostname, Switch1, Switch2, Speed, VLANS, WorkOrder.</p></div></div></div>
<div id="readmeTabIndividual" class="readme-tab-content"><div class="readme-section"><div class="readme-section-title"><span>🔌</span> Individual Port Deployment</div><div class="readme-content"><p>Deploy individual ports. ACCESS = single VLAN untagged, TRUNK = multiple VLANs tagged.</p></div></div></div>
<div id="readmeTabTroubleshoot" class="readme-tab-content"><div class="readme-section"><div class="readme-section-title"><span>🔧</span> Troubleshooting</div><div class="readme-content"><h3>Script Not Found</h3><p>Go to Settings and verify script paths.</p><h3>CSV Errors</h3><p>Check headers match exactly. Wrap VLAN ranges in quotes. Save as UTF-8.</p><h3>Credentials Not Auto-Filling</h3><p>Ensure credentials are set in the Credentials panel before running scripts.</p></div></div></div>
</div></div>

</main></div>

<script>
let currentView='welcome',isRunning=false,pollInterval=null,csvModes={vpc:'file',individual:'file',epgadd:'file',epgdelete:'file'};
let credSet=false;

// Build deployment screens dynamically
const screenDefs = [
  {id:'vpc',title:'VPC Bulk Deployment',badge:'VPC',badgeCls:'vpc',console:'vpc-deployment-console',
   csvCols:['Hostname','Switch1','Switch2','Speed','VLANS','WorkOrder'],
   csvPh:['MEDHVIOP173_SEA_PROD','EDCLEAFACC1501','EDCLEAFACC1502','25G','32,64-67','WO123456'],
   csvRef:'<tr><th>Hostname</th><th>Switch1</th><th>Switch2</th><th>Speed</th><th>VLANS</th><th>WorkOrder</th></tr><tr><td>Device name</td><td>First VPC switch</td><td>Second VPC switch</td><td>1G, 10G, 25G</td><td>VLAN IDs</td><td>Work order #</td></tr>',
   csvEx:'MEDHVIOP173_SEA_PROD,EDCLEAFACC1501,EDCLEAFACC1502,25G,&quot;32,64-67,92-95&quot;,WO123456',
   defCsv:'{{ config.default_vpc_csv }}'},
  {id:'individual',title:'Individual Port Deployment',badge:'PORT',badgeCls:'individual',console:'individual-port-console',
   csvCols:['Hostname','Switch','Type','Speed','VLANS','WorkOrder'],
   csvPh:['MEDHVIOP173_MGMT','EDCLEAFNSM2163','ACCESS','1G','2958','WO123456'],
   csvRef:'<tr><th>Hostname</th><th>Switch</th><th>Type</th><th>Speed</th><th>VLANS</th><th>WorkOrder</th></tr><tr><td>Device name</td><td>Target switch</td><td>ACCESS/TRUNK</td><td>1G, 10G, 25G</td><td>VLAN IDs</td><td>Work order #</td></tr>',
   csvEx:'MEDHVIOP173_MGMT,EDCLEAFNSM2163,ACCESS,1G,2958,WO123456',
   defCsv:'{{ config.default_individual_csv }}'},
  {id:'epgadd',title:'EPG Add - Add EPGs to Existing Ports',badge:'ADD',badgeCls:'epgadd',console:'epg-add-console',
   csvCols:['Switch','Port','VLANS'],csvPh:['EDCLEAFACC1501','1/68','32,64-67'],
   csvRef:'<tr><th>Switch</th><th>Port</th><th>VLANS</th></tr><tr><td>Switch name</td><td>Port (e.g., 1/68)</td><td>VLAN IDs</td></tr>',
   csvEx:'EDCLEAFACC1501,1/68,&quot;32,64-67&quot;', defCsv:'epg_add.csv'},
  {id:'epgdelete',title:'EPG Delete - Remove EPGs from Ports',badge:'DELETE',badgeCls:'epgdelete',console:'epg-delete-console',
   csvCols:['Switch','Port','VLANS'],csvPh:['EDCLEAFACC1501','1/68','32,64-67'],
   csvRef:'<tr><th>Switch</th><th>Port</th><th>VLANS</th></tr><tr><td>Switch name</td><td>Port (e.g., 1/68)</td><td>VLAN IDs to remove</td></tr>',
   csvEx:'EDCLEAFACC1501,1/68,&quot;32,64-67&quot;', defCsv:'epg_delete.csv'}
];

screenDefs.forEach(s => {
  const el = document.getElementById(s.id+'Screen');
  const thRow = s.csvCols.map(c=>'<th>'+c+'</th>').join('')+'<th class="row-actions"></th>';
  const tdRow = s.csvCols.map((c,i)=>'<td><input type="text" placeholder="'+s.csvPh[i]+'"></td>').join('')+'<td class="row-actions"><button class="delete-row" onclick="deleteCsvRow(this)">✕</button></td>';
  el.innerHTML = `
<div class="header-bar"><div class="header-title"><h2>${s.title}</h2><span class="header-badge ${s.badgeCls}">${s.badge}</span></div><div class="header-actions"><button class="header-btn" onclick="clearTerminal('${s.id}')">Clear</button><button class="header-btn danger" onclick="stopScript()" id="${s.id}StopBtn" disabled>Stop</button><button class="header-btn primary" onclick="runScript('${s.id}')" id="${s.id}RunBtn">Run Script</button></div></div>
<div class="config-panel">
<div class="csv-toggle-group"><button class="csv-toggle active" onclick="toggleCsvMode('${s.id}','file')">📁 Use CSV File</button><button class="csv-toggle" onclick="toggleCsvMode('${s.id}','inline')">✏️ Edit Inline</button></div>
<div id="${s.id}FileMode"><label class="config-label">CSV File</label><div class="file-picker-row"><div class="file-picker-display" id="${s.id}FpDisplay"><span class="fp-icon">📄</span><span class="fp-placeholder">No file selected — click Browse</span></div><button class="file-picker-btn" onclick="document.getElementById('${s.id}FileInput').click()">Browse Files</button><input type="file" class="file-input-hidden" id="${s.id}FileInput" accept=".csv,.txt" onchange="handleFileSelect('${s.id}',this)"><input type="hidden" id="${s.id}CsvPath" value="${s.defCsv}"></div><div id="${s.id}Validation"></div></div>
<div id="${s.id}InlineMode" style="display:none"><div class="csv-editor-section" style="padding:0"><div class="csv-editor-header"><span class="csv-editor-title">Inline CSV Editor</span><div class="csv-editor-actions"><button class="csv-editor-btn add" onclick="addCsvRow('${s.id}')">+ Add Row</button><button class="csv-editor-btn" onclick="exportCsv('${s.id}')">Export CSV</button></div></div><table class="csv-editor-table" id="${s.id}CsvTable"><thead><tr>${thRow}</tr></thead><tbody><tr>${tdRow}</tr></tbody></table></div></div>
</div>
<div class="csv-reference" id="${s.id}CsvRef"><div class="csv-reference-header"><span class="csv-reference-title">📋 CSV Format Reference</span><span class="csv-reference-toggle" onclick="toggleCsvRef('${s.id}')">Hide</span></div><table class="csv-table">${s.csvRef}</table><div class="csv-example"><div class="csv-example-label"># Example:</div>${s.csvEx}</div></div>
<div class="terminal-container"><div class="terminal-header"><div class="terminal-dots"><div class="terminal-dot red"></div><div class="terminal-dot yellow"></div><div class="terminal-dot green"></div></div><span class="terminal-title">${s.console}</span><div class="terminal-status" id="${s.id}TerminalStatus"><div class="terminal-status-dot"></div><span>Ready</span></div></div><div class="terminal-output" id="${s.id}Output"><div class="terminal-line muted">// ${s.title}</div><div class="terminal-line muted">// Select a CSV file and click "Run Script" to begin</div></div><div class="terminal-input-area"><span class="terminal-prompt">❯</span><input type="text" class="terminal-input" id="${s.id}Input" placeholder="Type response here..." onkeypress="handleInputKeypress(event,'${s.id}')" disabled><button class="terminal-submit" id="${s.id}SubmitBtn" onclick="submitInput('${s.id}')" disabled>Send</button></div></div>`;
});

function selectView(view){
  currentView=view;
  document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));
  const nav=document.querySelector('.nav-item.'+view); if(nav) nav.classList.add('active');
  ['welcomeScreen','vpcScreen','individualScreen','settingsScreen','readmeScreen','epgaddScreen','epgdeleteScreen','logsScreen','credentialsScreen'].forEach(id=>{document.getElementById(id).classList.add('hidden')});
  const sc=document.getElementById(view+'Screen');
  if(sc){sc.classList.remove('hidden');sc.style.display='flex'}else if(view==='welcome'){document.getElementById('welcomeScreen').classList.remove('hidden')}
  if(view==='welcome'||view==='logs') refreshLog();
}

// CREDENTIALS
function saveCredentials(){
  const u=document.getElementById('credUsername').value.trim();
  const p=document.getElementById('credPassword').value;
  if(!u||!p){alert('Both username and password are required');return}
  fetch('/api/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})})
  .then(r=>r.json()).then(d=>{if(d.status==='saved'){updateCredStatus(true);alert('Credentials saved (in memory only)')}});
}
function clearCredentials(){
  fetch('/api/credentials',{method:'DELETE'}).then(r=>r.json()).then(()=>{
    updateCredStatus(false);document.getElementById('credUsername').value='';document.getElementById('credPassword').value='';
  });
}
function updateCredStatus(isSet){
  credSet=isSet;
  const box=document.getElementById('credStatusBox'),icon=document.getElementById('credStatusIcon'),txt=document.getElementById('credStatusText'),badge=document.getElementById('credNavBadge');
  if(isSet){box.className='cred-status set';icon.textContent='✅';txt.textContent='Credentials stored (auto-fill active)';badge.className='cred-badge set';badge.textContent='SET'}
  else{box.className='cred-status unset';icon.textContent='⚠️';txt.textContent='No credentials stored';badge.className='cred-badge unset';badge.textContent='NOT SET'}
}
function checkCredentials(){fetch('/api/credentials').then(r=>r.json()).then(d=>updateCredStatus(d.set))}

// FILE PICKER with CSV VALIDATION
function handleFileSelect(type,input){
  if(!input.files||!input.files.length) return;
  const fd=new FormData(); fd.append('file',input.files[0]);
  fetch('/api/upload',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{
    if(d.status==='ok'){
      document.getElementById(type+'CsvPath').value=d.path;
      const dp=document.getElementById(type+'FpDisplay');
      dp.innerHTML='<span class="fp-icon">✅</span><span class="fp-name">'+d.filename+'</span>';
      dp.classList.add('has-file');
      // Validate CSV
      fetch('/api/validate-csv',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:d.path,type:type})})
      .then(r=>r.json()).then(v=>{
        const vd=document.getElementById(type+'Validation');
        if(v.valid && !v.warnings.length){vd.innerHTML='<div class="csv-validation valid">✅ Valid: '+v.row_count+' row(s), columns: '+v.columns_found.join(', ')+'</div>'}
        else if(v.valid && v.warnings.length){vd.innerHTML='<div class="csv-validation warnings">⚠️ Valid with warnings ('+v.row_count+' rows): '+v.warnings.slice(0,3).join('; ')+'</div>'}
        else{vd.innerHTML='<div class="csv-validation invalid">❌ Invalid: '+v.errors.join('; ')+'</div>'}
      });
    } else alert('Upload failed: '+(d.message||'Unknown'));
  }).catch(e=>alert('Upload error: '+e));
}

// CSV EDITOR
function toggleCsvMode(type,mode){csvModes[type]=mode;const fm=document.getElementById(type+'FileMode'),im=document.getElementById(type+'InlineMode');document.querySelectorAll('#'+type+'Screen .csv-toggle').forEach(t=>t.classList.remove('active'));event.target.classList.add('active');if(mode==='file'){fm.style.display='block';im.style.display='none'}else{fm.style.display='none';im.style.display='block'}}
function addCsvRow(type){const tb=document.getElementById(type+'CsvTable').getElementsByTagName('tbody')[0],row=tb.insertRow();const def=screenDefs.find(s=>s.id===type);if(!def)return;def.csvCols.forEach((c,i)=>{const cell=row.insertCell();const inp=document.createElement('input');inp.type='text';inp.placeholder=def.csvPh[i]||'';cell.appendChild(inp)});const ac=row.insertCell();ac.className='row-actions';ac.innerHTML='<button class="delete-row" onclick="deleteCsvRow(this)">✕</button>'}
function deleteCsvRow(btn){const r=btn.closest('tr');if(r.parentNode.rows.length>1)r.remove()}
function exportCsv(type){const table=document.getElementById(type+'CsvTable'),rows=table.getElementsByTagName('tbody')[0].rows,headers=Array.from(table.getElementsByTagName('th')).map(th=>th.textContent).filter(h=>h);let csv=headers.join(',')+'\n';for(let row of rows){const vals=Array.from(row.getElementsByTagName('input')).map(inp=>{let v=inp.value.trim();if(v.includes(',')||v.includes('-'))v='"'+v+'"';return v});if(vals.some(v=>v))csv+=vals.join(',')+'\n'}const blob=new Blob([csv],{type:'text/csv'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=type+'_export.csv';a.click()}

// README
function switchReadmeTab(tab){document.querySelectorAll('.readme-tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.readme-tab-content').forEach(c=>c.classList.remove('active'));event.target.classList.add('active');const map={ui:'readmeTabUi',vpc:'readmeTabVpc',individual:'readmeTabIndividual',troubleshoot:'readmeTabTroubleshoot'};const el=document.getElementById(map[tab]);if(el)el.classList.add('active')}
function toggleCsvRef(type){const ref=document.getElementById(type+'CsvRef'),t=ref.querySelector('.csv-table'),e=ref.querySelector('.csv-example'),tog=ref.querySelector('.csv-reference-toggle');if(t.style.display==='none'){t.style.display='';e.style.display='';tog.textContent='Hide'}else{t.style.display='none';e.style.display='none';tog.textContent='Show'}}

// TERMINAL
function highlightBracketNums(html){return html.replace(/\[(\d+|[A-Za-z])\]/g,'<span class="bracket-num">[$1]</span>')}

function addLine(type,text,lineType='normal'){
  const output=document.getElementById(type+'Output'),line=document.createElement('div');
  if(lineType==='normal'){const tu=text.toUpperCase();
    if(tu.includes('[FOUND]')||tu.includes('[SUCCESS]')||tu.includes('[OK]')||tu.includes('[CREATED]')||tu.includes('[DEPLOYED]'))lineType='success';
    else if(tu.includes('[ERROR]')||tu.includes('[FAILED]')||tu.includes('[FAILURE]'))lineType='error';
    else if(tu.includes('[WARNING]')||tu.includes('[WARN]')||tu.includes('[SKIP]')||tu.includes('[SKIPPED]'))lineType='warning';
    else if(tu.includes('[INFO]'))lineType='info';
    else if(tu.includes('[CREDENTIALS]'))lineType='credential';
    else if(text.startsWith('===')||text.startsWith('---')||text.startsWith('***'))lineType='header';
  }
  line.className='terminal-line '+lineType;
  const esc=text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  if(['success','error','warning','info','credential'].includes(lineType)){
    let h=esc.replace(/\[(FOUND|SUCCESS|OK|CREATED|DEPLOYED)\]/gi,'<span style="color:var(--accent-green);font-weight:600">[$1]</span>')
      .replace(/\[(ERROR|FAILED|FAILURE)\]/gi,'<span style="color:var(--accent-red);font-weight:600">[$1]</span>')
      .replace(/\[(WARNING|WARN|SKIP|SKIPPED)\]/gi,'<span style="color:var(--accent-orange);font-weight:600">[$1]</span>')
      .replace(/\[(INFO)\]/gi,'<span style="color:var(--accent-blue);font-weight:600">[$1]</span>')
      .replace(/\[(CREDENTIALS)\]/gi,'<span style="color:#ffd200;font-weight:600">[$1]</span>');
    line.innerHTML=highlightBracketNums(h);
  } else { line.innerHTML=highlightBracketNums(esc); }
  output.appendChild(line); output.scrollTop=output.scrollHeight;
}

function clearTerminal(type){document.getElementById(type+'Output').innerHTML='<div class="terminal-line muted">// Terminal cleared</div>'}
function setStatus(type,text,running){const s=document.getElementById(type+'TerminalStatus');s.querySelector('span').textContent=text;s.classList.toggle('running',running);document.getElementById('globalStatus').classList.toggle('running',running);document.getElementById('statusText').textContent=running?'Running':'Ready'}

function runScript(type){
  const csvPath=document.getElementById(type+'CsvPath').value;
  if(!csvPath){addLine(type,'[ERROR] Please select a CSV file first','error');return}
  isRunning=true;setStatus(type,'Running',true);
  document.getElementById(type+'RunBtn').disabled=true;document.getElementById(type+'StopBtn').disabled=false;
  document.getElementById(type+'Input').disabled=false;document.getElementById(type+'SubmitBtn').disabled=false;
  clearTerminal(type);addLine(type,'[INFO] Starting script...','info');addLine(type,'[INFO] CSV: '+csvPath,'info');
  if(credSet) addLine(type,'[CREDENTIALS] Auto-fill active — credentials will be injected','credential');
  fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:type,csv_path:csvPath})})
  .then(r=>r.json()).then(d=>{if(d.status==='started')startPolling(type);else{addLine(type,'[ERROR] '+d.message,'error');scriptEnded(type)}})
  .catch(e=>{addLine(type,'[ERROR] '+e,'error');scriptEnded(type)});
}

function startPolling(type){pollInterval=setInterval(()=>{fetch('/api/output').then(r=>r.json()).then(d=>{d.lines.forEach(item=>{if(item.type==='output'){let lt='normal';if(item.text.includes('===')||item.text.includes('---'))lt='header';else if(item.text.includes('[SUCCESS]')||item.text.includes(' OK'))lt='success';else if(item.text.includes('[ERROR]')||item.text.includes('[FAILED]'))lt='error';else if(item.text.includes('[WARNING]'))lt='warning';else if(item.text.includes('[INFO]'))lt='info';else if(item.text.includes('[CREDENTIALS]'))lt='credential';else if(item.text.includes('Select')||item.text.endsWith(':')||item.text.endsWith('?'))lt='prompt';addLine(type,item.text,lt)}else if(item.type==='exit'){addLine(type,'[EXIT] Code: '+item.code,item.code===0?'success':'error');scriptEnded(type)}else if(item.type==='error'){addLine(type,'[ERROR] '+item.text,'error');scriptEnded(type)}})})},100)}

function scriptEnded(type){isRunning=false;if(pollInterval){clearInterval(pollInterval);pollInterval=null}setStatus(type,'Ready',false);document.getElementById(type+'RunBtn').disabled=false;document.getElementById(type+'StopBtn').disabled=true;document.getElementById(type+'Input').disabled=true;document.getElementById(type+'SubmitBtn').disabled=true}
function stopScript(){fetch('/api/stop',{method:'POST'}).then(()=>{addLine(currentView,'[STOPPED] Terminated by user','warning');scriptEnded(currentView)})}
function submitInput(type){const input=document.getElementById(type+'Input');if(!input.value&&input.value!=='')return;addLine(type,'> '+input.value,'info');fetch('/api/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:input.value})});input.value=''}
function handleInputKeypress(e,type){if(e.key==='Enter')submitInput(type)}

function saveSettings(){const s={vpc_script:document.getElementById('settingsVpcScript').value,individual_script:document.getElementById('settingsIndividualScript').value,epgadd_script:document.getElementById('settingsEpgaddScript').value,epgdelete_script:document.getElementById('settingsEpgdeleteScript').value,version:document.getElementById('settingsVersion').value};fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(s)}).then(r=>r.json()).then(d=>{if(d.status==='saved'){alert('Settings saved!');document.getElementById('versionBadge').textContent='v'+s.version}})}

// LOG with download + rollback buttons
function fmtMin(m){if(m<60)return Math.round(m)+'m';return Math.floor(m/60)+'h '+Math.round(m%60)+'m'}
function refreshLog(){fetch('/api/logs').then(r=>r.json()).then(log=>{
  const e=log.entries||[],ts=log.total_time_saved_minutes||0,td=log.total_deployments||0,runs=e.length,ok=e.filter(x=>x.status==='success').length,rate=runs>0?Math.round(ok/runs*100)+'%':'—';
  const u=id=>document.getElementById(id);
  u('wTimeSaved').textContent=fmtMin(ts);u('wDeploys').textContent=td;u('wRuns').textContent=runs;
  if(u('logTimeSaved')){u('logTimeSaved').textContent=fmtMin(ts);u('logDeploys').textContent=td;u('logRuns').textContent=runs;u('logSuccessRate').textContent=rate}
  const c=u('logEntriesContainer');if(!e.length){c.innerHTML='<div class="log-empty"><div class="log-empty-icon">📭</div>No deployments yet.</div>';return}
  const labels={vpc:'VPC',individual:'PORT',epgadd:'EPG ADD',epgdelete:'EPG DEL'};let h='';
  for(let i=e.length-1;i>=0;i--){const x=e[i];
    let actionBtns='<div class="log-entry-actions">';
    if(x.saved_log_file) actionBtns+='<button class="log-action-btn download" onclick="downloadLog(\''+x.saved_log_file+'\')">📥 Log</button>';
    if(x.rollback_file) actionBtns+='<button class="log-action-btn rollback" onclick="downloadRollback(\''+x.rollback_file+'\')">↩ Rollback</button>';
    actionBtns+='</div>';
    h+='<div class="log-entry"><div class="log-entry-dot '+x.status+'"></div><div class="log-entry-info"><div class="log-entry-title">'+(x.csv_file||'inline')+'</div><div class="log-entry-meta">'+x.timestamp+' · '+x.deployment_count+' items · '+x.duration_seconds+'s</div></div><span class="log-entry-type '+x.type+'">'+(labels[x.type]||x.type)+'</span><div class="log-entry-saved">-'+fmtMin(x.time_saved_minutes)+'</div>'+actionBtns+'</div>'}
  c.innerHTML=h}).catch(()=>{})}
function clearLog(){if(!confirm('Clear all deployment log entries?'))return;fetch('/api/logs/clear',{method:'POST'}).then(()=>refreshLog())}
function downloadLog(filename){window.open('/api/saved-logs/'+encodeURIComponent(filename),'_blank')}
function downloadRollback(filename){window.open('/api/rollback/'+encodeURIComponent(filename),'_blank')}

// Init
checkCredentials();
selectView('welcome');
</script>
</body></html>
'''

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=load_config())

@app.route('/api/run', methods=['POST'])
def api_run():
    global running_process, output_queue, current_run
    if running_process is not None:
        return jsonify({'status': 'error', 'message': 'Script already running'})
    data = request.json
    config = load_config()
    script_type = data.get('type')
    script_map = {'vpc':'vpc_script','individual':'individual_script','epgadd':'epgadd_script','epgdelete':'epgdelete_script'}
    script_key = script_map.get(script_type)
    if not script_key or script_key not in config:
        return jsonify({'status': 'error', 'message': f'Unknown script type: {script_type}'})
    script_path = config[script_key]
    if not os.path.exists(script_path):
        return jsonify({'status': 'error', 'message': f'Script not found: {script_path}'})
    while not output_queue.empty():
        try: output_queue.get_nowait()
        except: break
    current_run["type"] = script_type
    current_run["csv_path"] = data.get('csv_path', '')
    current_run["start_time"] = None
    current_run["output_lines"] = []
    thread = threading.Thread(target=run_script_thread, args=(script_path, data.get('csv_path')))
    thread.daemon = True
    thread.start()
    return jsonify({'status': 'started'})

@app.route('/api/output')
def api_output():
    lines = []
    while not output_queue.empty():
        try:
            item = output_queue.get_nowait()
            if item[0] == 'output': lines.append({'type': 'output', 'text': item[1]})
            elif item[0] == 'exit': lines.append({'type': 'exit', 'code': item[1]})
            elif item[0] == 'error': lines.append({'type': 'error', 'text': item[1]})
        except: break
    return jsonify({'lines': lines})

@app.route('/api/input', methods=['POST'])
def api_input():
    return jsonify({'status': 'sent' if send_input_to_process(request.json.get('text', '')) else 'failed'})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    stop_process()
    return jsonify({'status': 'stopped'})

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'GET':
        return jsonify(load_config())
    try:
        save_config(request.json)
        return jsonify({'status': 'saved'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({'status': 'error', 'message': 'No file provided'})
    f = request.files['file']
    if not f.filename:
        return jsonify({'status': 'error', 'message': 'No file selected'})
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    filename = f.filename.replace('..', '').replace('/', '_').replace('\\', '_')
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    f.save(filepath)
    return jsonify({'status': 'ok', 'path': filepath, 'filename': filename})

@app.route('/api/credentials', methods=['GET', 'POST', 'DELETE'])
def api_credentials():
    global stored_credentials
    if request.method == 'GET':
        return jsonify({'set': stored_credentials.get('set', False), 'username': stored_credentials.get('username', '')})
    elif request.method == 'POST':
        data = request.json
        stored_credentials['username'] = data.get('username', '')
        stored_credentials['password'] = data.get('password', '')
        stored_credentials['set'] = bool(stored_credentials['username'] and stored_credentials['password'])
        return jsonify({'status': 'saved', 'set': stored_credentials['set']})
    elif request.method == 'DELETE':
        stored_credentials = {"username": None, "password": None, "set": False}
        return jsonify({'status': 'cleared'})

@app.route('/api/validate-csv', methods=['POST'])
def api_validate_csv():
    data = request.json
    filepath = data.get('path', '')
    script_type = data.get('type', '')
    results = validate_csv_file(filepath, script_type)
    return jsonify(results)

@app.route('/api/logs')
def api_logs():
    return jsonify(load_log())

@app.route('/api/logs/clear', methods=['POST'])
def api_logs_clear():
    save_log({"entries": [], "total_time_saved_minutes": 0, "total_deployments": 0})
    return jsonify({'status': 'cleared'})

@app.route('/api/saved-logs/<filename>')
def api_saved_log_download(filename):
    safe_name = filename.replace('..', '').replace('/', '_').replace('\\', '_')
    filepath = os.path.join(SAVED_LOGS_FOLDER, safe_name)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True, download_name=safe_name)
    return jsonify({'status': 'error', 'message': 'Log file not found'}), 404

@app.route('/api/rollback/<filename>')
def api_rollback_download(filename):
    safe_name = filename.replace('..', '').replace('/', '_').replace('\\', '_')
    filepath = os.path.join(ROLLBACK_FOLDER, safe_name)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True, download_name=safe_name)
    return jsonify({'status': 'error', 'message': 'Rollback script not found'}), 404

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    for folder in [UPLOAD_FOLDER, SAVED_LOGS_FOLDER, ROLLBACK_FOLDER]:
        os.makedirs(folder, exist_ok=True)
    print("\n" + "=" * 60)
    print(" ACI BULK DEPLOYMENT WEB APPLICATION v1.3.0")
    print("=" * 60)
    print("\n Starting server...")
    print(" Open http://localhost:5000 in your browser")
    print("\n Press Ctrl+C to stop")
    print("=" * 60 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
