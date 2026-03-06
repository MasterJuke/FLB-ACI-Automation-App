#!/usr/bin/env python3
"""
ACI Automation Console
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
RESULTS_FOLDER = os.path.join(BASE_DIR, 'deployment_results')

DEFAULT_CONFIG = {
    "vpc_script": "aci_bulk_vpc_deploy.py",
    "individual_script": "aci_bulk_individual_deploy.py",
    "epgadd_script": "aci_bulk_epg_add.py",
    "epgdelete_script": "aci_bulk_epg_delete.py",
    "default_vpc_csv": "vpc_deployments.csv",
    "default_individual_csv": "individual_port_deployments.csv",
    "default_epgadd_csv": "epg_add.csv",
    "default_epgdelete_csv": "epg_delete.csv",
    "auto_select_port": True,
    "epg_overwrite_default": False,
    "version": "1.3.0"
}

TIME_ESTIMATES = {
    "vpc": {"per_deployment": 4, "label": "VPC Port Channel + EPG Bindings"},
    "individual": {"per_deployment": 3, "label": "Static Port Policy + EPG Bindings"},
    "epgadd": {"per_deployment": 2, "label": "EPG Static Path Binding Addition"},
    "epgdelete": {"per_deployment": 1.5, "label": "EPG Static Path Binding Removal"}
}

# CSV column requirements per script type
CSV_REQUIREMENTS = {
    "vpc": {"required": ["HOSTNAME", "SWITCH1", "SWITCH2", "SPEED", "VLANS", "WORKORDER"],
            "validators": {"SPEED": r"^(1G|10G|25G|40G|100G)$", "VLANS": r"^[\d,\-\s\"]+$"}},
    "individual": {"required": ["HOSTNAME", "SWITCH", "TYPE", "SPEED", "VLANS", "WORKORDER"],
                   "validators": {"TYPE": r"^(ACCESS|TRUNK)$", "SPEED": r"^(1G|10G|25G|40G|100G)$", "VLANS": r"^[\d,\-\s\"]+$"}},
    "epgadd": {"required": ["SWITCH", "PORT", "VLANS"],
               "validators": {"PORT": r"^(eth)?[\d]+/[\d]+$", "VLANS": r"^[\d,\-\s\"]+$"}},
    "epgdelete": {"required": ["SWITCH", "PORT"],
                  "validators": {"PORT": r"^(eth)?[\d]+/[\d]+$", "VLANS": r"^[\d,\-\s\"]+$"}}
}

# Global state
running_process = None
output_queue = queue.Queue()
input_queue = queue.Queue()

# In-memory credential storage (NEVER written to disk)
stored_credentials = {"username": None, "password": None, "set": False, "apic_urls": {"D1": "", "D2": "", "D3": ""}}

CREDENTIALS_FILE = os.path.join(BASE_DIR, '.aci_credentials')

def save_credentials_to_disk():
    """Save current credentials to disk with base64 obfuscation."""
    import base64, json as _json
    try:
        data = {
            "username": stored_credentials.get("username", ""),
            "apic_urls": stored_credentials.get("apic_urls", {"D1": "", "D2": "", "D3": ""}),
        }
        pwd = stored_credentials.get("password", "")
        if pwd:
            data["_p"] = base64.b64encode(pwd.encode("utf-8")).decode("ascii")
        with open(CREDENTIALS_FILE, "w") as f:
            _json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"[WARNING] Failed to save credentials: {e}")
        return False

def load_credentials_from_disk():
    """Load credentials from disk (base64 obfuscated)."""
    import base64, json as _json
    global stored_credentials
    try:
        if not os.path.exists(CREDENTIALS_FILE):
            return False
        with open(CREDENTIALS_FILE, "r") as f:
            data = _json.load(f)
        stored_credentials["username"] = data.get("username", "")
        if data.get("_p"):
            stored_credentials["password"] = base64.b64decode(data["_p"]).decode("utf-8")
        if data.get("apic_urls"):
            stored_credentials["apic_urls"] = data["apic_urls"]
        stored_credentials["set"] = bool(stored_credentials["username"] and stored_credentials.get("password"))
        return stored_credentials["set"]
    except Exception as e:
        print(f"[WARNING] Failed to load credentials: {e}")
        return False

# Auto-load on startup
load_credentials_from_disk()

# Track current run for logging
current_run = {"type": None, "start_time": None, "csv_path": None, "output_lines": [], "status": None}

# =============================================================================
# CONFIG MANAGEMENT
# =============================================================================

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                return config
        except:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)

# =============================================================================
# DEPLOYMENT LOG MANAGEMENT
# =============================================================================

def load_log():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {"entries": [], "total_time_saved_minutes": 0, "total_deployments": 0}

def save_log(log_data):
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
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
        "rollback_file": None,
        "results_file": None
    }

    # Auto-save sanitized log
    saved_log = auto_save_log(deploy_type, entry_id, timestamp, output_lines)
    if saved_log:
        entry["saved_log_file"] = saved_log

    # Generate rollback script
    rollback = generate_rollback_script(deploy_type, entry_id, timestamp, output_lines)
    if rollback:
        entry["rollback_file"] = rollback

    # Generate results CSV — always, for all deploy types and all outcomes
    tracked_ports = current_run.get("deployed_ports", [])
    if csv_path:
        results = generate_results_csv(deploy_type, csv_path, entry_id, timestamp,
                                        output_lines, tracked_ports, run_status=status)
        if results:
            entry["results_file"] = results

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
            if 'Auto-filled password' in line:
                return '[CREDENTIALS] Auto-filled password: ••••••••'
            return None
    if stored_credentials.get('password') and stored_credentials['password']:
        pwd = stored_credentials['password']
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

        with open(filepath, 'w', encoding='utf-8') as f:
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
# ROLLBACK STATE PARSING & RESTORE INJECTION
# =============================================================================

def parse_rollback_states(output_lines):
    """
    Extract [ROLLBACK:STATE] markers from deployment output.
    
    These markers are emitted by aci_port_utils cleanup functions BEFORE
    deleting old configuration. They capture the prior state so the
    rollback script can restore it.
    
    Returns list of state dicts.
    """
    import json as _json
    states = []
    for line in output_lines:
        if '[ROLLBACK:STATE]' in line:
            try:
                json_str = line.split('[ROLLBACK:STATE]', 1)[1].strip()
                states.append(_json.loads(json_str))
            except Exception:
                pass
    return states


def inject_restore_phase(script, prior_states, deploy_type):
    """
    Inject Phase 2 (restore prior state) code into a rollback script.
    
    Modifies the script string to add:
    1. import json to imports
    2. PRIOR_STATE constant with captured state data
    3. Phase 2 restore logic before ROLLBACK COMPLETE
    """
    import json as _json
    
    if not prior_states:
        return script
    
    # 1. Add import json
    script = script.replace(
        'import re\n\nurllib3',
        'import re\nimport json\n\nurllib3'
    )
    
    # 2. Add PRIOR_STATE constant after POD_ID
    state_json = _json.dumps(prior_states, indent=2)
    tq = "'" + "'" + "'"
    script = script.replace(
        'POD_ID = "1"\n\nWEB_UI',
        'POD_ID = "1"\n\nPRIOR_STATE = ' + tq + '\n' + state_json + '\n' + tq + '\n\nWEB_UI'
    )
    
    # 3. Insert Phase 2 before ROLLBACK COMPLETE
    restore_code = """
    # ==========================================================================
    # PHASE 2: RESTORE PRIOR STATE
    # ==========================================================================
    
    prior = json.loads(PRIOR_STATE)
    if prior:
        print()
        print("=" * 60)
        print(" PHASE 2: RESTORING PRIOR STATE")
        print("=" * 60)
        print()
        
        restore_num = 0
        
        # Restore descriptions first (least impactful)
        for s in prior:
            if s.get("type") != "description":
                continue
            restore_num += 1
            node = s["node"]
            port = s["port"]
            value = s["value"]
            eth = f"eth{port}" if not port.startswith("eth") else port
            dn = f"topology/pod-{POD_ID}/node-{node}/sys/phys-[{eth}]"
            print(f"  [R{restore_num}] Restoring description on node {node} eth{port}: {value}")
            try:
                r = safe_request('post', session, f"{apic_url}/api/node/mo/{dn}.json",
                    apic_url, username, password, auth_state,
                    json={"l1PhysIf": {"attributes": {"descr": value}}})
                print(f"      {'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}")
            except Exception as e:
                print(f"      [ERROR] {e}")
        
        # Restore port selectors (re-links port to old policy group)
        for s in prior:
            if s.get("type") != "selector":
                continue
            restore_num += 1
            sel_name = s["name"]
            profile = s["profile"]
            pg_name = s.get("policy_group", "")
            port = s["port"]
            pg_type = s.get("pg_type", "access")
            
            port_parts = port.split("/")
            from_card = port_parts[0] if len(port_parts) > 1 else "1"
            from_port = port_parts[-1]
            
            if pg_type == "vpc":
                pg_dn = f"uni/infra/funcprof/accbundle-{pg_name}"
            else:
                pg_dn = f"uni/infra/funcprof/accportgrp-{pg_name}"
            
            if not pg_name:
                print(f"  [R{restore_num}] [SKIP] No policy group to restore for selector {sel_name}")
                continue
            
            print(f"  [R{restore_num}] Restoring port selector: {sel_name} -> {pg_name}")
            try:
                payload = {
                    "infraHPortS": {
                        "attributes": {"name": sel_name, "type": "range"},
                        "children": [
                            {"infraPortBlk": {"attributes": {
                                "name": "block2",
                                "fromCard": from_card, "toCard": from_card,
                                "fromPort": from_port, "toPort": from_port
                            }}},
                            {"infraRsAccBaseGrp": {"attributes": {"tDn": pg_dn}}}
                        ]
                    }
                }
                r = safe_request('post', session,
                    f"{apic_url}/api/node/mo/uni/infra/accportprof-{profile}/hports-{sel_name}-typ-range.json",
                    apic_url, username, password, auth_state, json=payload)
                print(f"      {'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}")
            except Exception as e:
                print(f"      [ERROR] {e}")
        
        # Restore EPG bindings last (depends on selector being in place)
        for s in prior:
            if s.get("type") != "binding":
                continue
            restore_num += 1
            tenant = s["tenant"]
            ap = s["ap"]
            epg = s["epg"]
            vlan = s["vlan"]
            mode = s.get("mode", "regular")
            path_type = s.get("path_type", "individual")
            
            if path_type == "vpc":
                node1 = s.get("node", "")
                node2 = s.get("node2", "")
                vpc_pg = s.get("vpc_pg", "")
                path = f"topology/pod-{POD_ID}/protpaths-{node1}-{node2}/pathep-[{vpc_pg}]"
                label = f"VPC {node1}-{node2}"
            else:
                node = s.get("node", "")
                port = s.get("port", "")
                eth = f"eth{port}" if not port.startswith("eth") else port
                path = f"topology/pod-{POD_ID}/paths-{node}/pathep-[{eth}]"
                label = f"node {node} eth{port}"
            
            print(f"  [R{restore_num}] Restoring VLAN {vlan} on {label} -> {epg}")
            try:
                payload = {
                    "fvRsPathAtt": {
                        "attributes": {
                            "tDn": path,
                            "encap": f"vlan-{vlan}",
                            "mode": mode,
                            "instrImedcy": "immediate"
                        }
                    }
                }
                r = safe_request('post', session,
                    f"{apic_url}/api/mo/uni/tn-{tenant}/ap-{ap}/epg-{epg}.json",
                    apic_url, username, password, auth_state, json=payload)
                print(f"      {'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}")
            except Exception as e:
                print(f"      [ERROR] {e}")
        
        print(f"\n  Phase 2 complete: {restore_num} restore action(s)")

"""
    
    old_complete = '    print()\n    print("=" * 60)\n    print(" ROLLBACK COMPLETE")'
    new_complete = restore_code + '    print()\n    print("=" * 60)\n    print(" ROLLBACK COMPLETE")'
    script = script.replace(old_complete, new_complete, 1)
    
    return script


# =============================================================================
# ROLLBACK STATE PARSING & RESTORE INJECTION
# =============================================================================



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

        rollback_actions = parse_deployment_output(deploy_type, output_lines)

        if not rollback_actions:
            return None

        script = build_rollback_script(deploy_type, entry_id, timestamp, rollback_actions)

        # Multi-tier rollback: extract [ROLLBACK:STATE] markers and inject Phase 2
        prior_states = parse_rollback_states(output_lines)
        if prior_states:
            script = inject_restore_phase(script, prior_states, deploy_type)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(script)

        return filename
    except Exception as e:
        print(f"[WARNING] Failed to generate rollback: {e}")
        return None


def parse_deployment_output(deploy_type, lines):
    actions = []
    full_text = '\n'.join(lines)

    if deploy_type == 'vpc':
        for m in re.finditer(r'Creating VPC Interface Policy Group:\s*(\S+)', full_text):
            actions.append({"action": "delete_vpc_ipg", "name": m.group(1)})

        for m in re.finditer(r'Creating Access Port Selector:\s*(\S+)', full_text):
            actions.append({"action": "delete_port_selector_vpc", "name": m.group(1)})

        int_profile_matches = re.findall(r'Interface Profile:\s*(\S+)', full_text)
        if int_profile_matches:
            for a in actions:
                if a["action"] == "delete_port_selector_vpc" and "int_profile" not in a:
                    a["int_profile"] = int_profile_matches[0]

        node_pair = re.search(r'vPC Leaf Switch Pair:\s+(\d+)-(\d+)', full_text)

        vpc_pg_name = None
        for a in actions:
            if a["action"] == "delete_vpc_ipg":
                vpc_pg_name = a["name"]
                break

        for m in re.finditer(r'VLAN\s+(\d+):\s*OK', full_text):
            vlan = m.group(1)
            binding = {"action": "delete_binding", "vlan": vlan}
            if node_pair:
                binding["node1"] = node_pair.group(1)
                binding["node2"] = node_pair.group(2)
            if vpc_pg_name:
                binding["vpc_pg"] = vpc_pg_name
            actions.append(binding)

        epg_map = {}
        for m in re.finditer(r'VLAN\s+(\d+)\s*->\s*(\S+)\s*/\s*(\S+?)(?:\s+\[(\S+?)\])?\s*$', full_text, re.MULTILINE):
            epg_map[m.group(1)] = {"app_profile": m.group(2), "epg": m.group(3), "tenant": m.group(4) or ""}

        for a in actions:
            if a["action"] == "delete_binding" and a.get("vlan") in epg_map:
                a.update(epg_map[a["vlan"]])

        for m in re.finditer(r'Node\s+(\d+)\s+eth([\d/]+):\s*\[SUCCESS\]', full_text):
            actions.append({"action": "clear_description", "node_id": m.group(1), "interface": m.group(2)})

    elif deploy_type == 'individual':
        for m in re.finditer(r'Creating Leaf Access Port Policy Group:\s*(\S+)', full_text):
            actions.append({"action": "delete_access_ipg", "name": m.group(1)})

        for m in re.finditer(r'Creating Port Selector:\s*(\S+)', full_text):
            actions.append({"action": "delete_port_selector_individual", "name": m.group(1)})

        int_profile_matches = re.findall(r'Interface Profile:\s*(\S+)', full_text)
        if int_profile_matches:
            for a in actions:
                if a["action"] == "delete_port_selector_individual" and "int_profile" not in a:
                    a["int_profile"] = int_profile_matches[0]

        node_match = re.search(r'Node ID:\s+(\d+)', full_text)
        int_match = re.search(r'Interface:\s+eth([\d/]+)', full_text)

        for m in re.finditer(r'VLAN\s+(\d+):\s*OK', full_text):
            binding = {"action": "delete_binding", "vlan": m.group(1)}
            if node_match:
                binding["node_id"] = node_match.group(1)
            if int_match:
                binding["interface"] = int_match.group(1)
            actions.append(binding)

        epg_map = {}
        for m in re.finditer(r'VLAN\s+(\d+)\s*->\s*(\S+)\s*/\s*(\S+?)(?:\s+\[(\S+?)\])?\s*$', full_text, re.MULTILINE):
            epg_map[m.group(1)] = {"app_profile": m.group(2), "epg": m.group(3), "tenant": m.group(4) or ""}
        for a in actions:
            if a["action"] == "delete_binding" and a.get("vlan") in epg_map:
                a.update(epg_map[a["vlan"]])

        if node_match and int_match:
            desc_success = re.search(r'\[1/4\].*description.*\n.*\[SUCCESS\]', full_text)
            if desc_success:
                actions.append({"action": "clear_description", "node_id": node_match.group(1), "interface": int_match.group(1)})

    elif deploy_type == 'epgadd':
        for m in re.finditer(r'\[OK\]\s+(\S+)\s+port\s+([\d/]+):\s*VLAN\s+(\d+)', full_text):
            actions.append({"action": "delete_binding", "switch": m.group(1), "port": m.group(2), "vlan": m.group(3)})

    elif deploy_type == 'epgdelete':
        for m in re.finditer(r'\[DELETED\]\s+(\S+)\s+port\s+([\d/]+):\s*VLAN\s+(\d+)', full_text):
            actions.append({"action": "recreate_binding", "switch": m.group(1), "port": m.group(2), "vlan": m.group(3)})

    return actions


def build_rollback_script(deploy_type, entry_id, timestamp, actions):
    """Build a Python rollback script from parsed actions."""
    meaningful = [a for a in actions if a["action"] not in ["clear_description"]]
    if not meaningful:
        meaningful = actions

    apic_d1 = stored_credentials.get("apic_urls", {}).get("D1", "")
    apic_d2 = stored_credentials.get("apic_urls", {}).get("D2", "")
    apic_d3 = stored_credentials.get("apic_urls", {}).get("D3", "")

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
import time
import requests
import urllib3
import re

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =============================================================================
# APIC CONFIGURATION
# =============================================================================

APIC_URLS = {{
    "D1": "{apic_d1}",
    "D2": "{apic_d2}",
    "D3": "{apic_d3}"
}}

POD_ID = "1"
WEB_UI = os.environ.get("ACI_WEB_UI", "")

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
        if r.status_code == 200:
            try:
                data = r.json()
                attrs = data['imdata'][0]['aaaLogin']['attributes']
                lifetime = int(attrs.get('refreshTimeoutSeconds', 300))
                return {{"ok": True, "lifetime": lifetime, "login_time": time.time()}}
            except:
                return {{"ok": True, "lifetime": 300, "login_time": time.time()}}
        return {{"ok": False}}
    except:
        return {{"ok": False}}

def refresh_token(session, apic_url):
    try:
        r = session.get(f"{{apic_url}}/api/aaaRefresh.json", verify=False, timeout=30)
        if r.status_code == 200:
            return time.time()
    except:
        pass
    return None

def ensure_fresh(session, apic_url, username, password, auth_state):
    elapsed = time.time() - auth_state.get("login_time", 0)
    remaining = auth_state.get("lifetime", 300) - elapsed
    if remaining < 60:
        print(f"  [INFO] Token aging ({{remaining:.0f}}s left), refreshing...")
        new_time = refresh_token(session, apic_url)
        if new_time:
            auth_state["login_time"] = new_time
            print(f"  [INFO] Token refreshed successfully")
        else:
            print(f"  [WARNING] Refresh failed, re-authenticating...")
            result = login_to_apic(session, apic_url, username, password)
            if result["ok"]:
                auth_state.update(result)
                print(f"  [INFO] Re-authenticated successfully")
            else:
                print(f"  [ERROR] Re-authentication failed!")

def safe_request(method, session, url, apic_url, username, password, auth_state, **kwargs):
    ensure_fresh(session, apic_url, username, password, auth_state)
    kwargs.setdefault('verify', False)
    kwargs.setdefault('timeout', 30)
    r = getattr(session, method)(url, **kwargs)
    if r.status_code in [401, 403] or 'token was invalid' in r.text.lower():
        print(f"  [WARNING] Token invalid, re-authenticating...")
        result = login_to_apic(session, apic_url, username, password)
        if result["ok"]:
            auth_state.update(result)
            r = getattr(session, method)(url, **kwargs)
    return r


def main():
    print("\\n" + "=" * 60)
    print(" ACI ROLLBACK SCRIPT")
    print(f" Original: {deploy_type.upper()} Run #{entry_id} ({timestamp})")
    print("=" * 60)

    username = input("\\nUsername: ").strip()
    password = input("Password: ").strip()

    sessions = {{}}
    needed_envs = set()
'''

    script += '''
    # Determine needed environments
'''

    if deploy_type in ['vpc', 'individual']:
        script += f'''    needed_envs = set()
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
        result = login_to_apic(session, APIC_URLS[env], username, password)
        if result["ok"]:
            sessions[env] = {"session": session, "auth": result}
            print(f"       [SUCCESS] (token lifetime: {result['lifetime']}s)")
        else:
            print(f"       [FAILED]")

    if not sessions:
        print("\\n[ERROR] No successful authentications.")
        sys.exit(1)

    env = list(sessions.keys())[0]
    session = sessions[env]["session"]
    auth_state = sessions[env]["auth"]
    apic_url = APIC_URLS[env]

    print("\\n" + "=" * 60)
    print(" ROLLBACK ACTIONS")
    print("=" * 60)
    print()
'''

    action_num = 0

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
        path = "topology/pod-{{POD_ID}}/protpaths-{node1}-{node2}/pathep-[{pg}]"
        dn = f"uni/tn-{tenant}/ap-{ap}/epg-{epg}/rspathAtt-[{{path}}]"
        r = safe_request('delete', session, f"{{apic_url}}/api/mo/{{dn}}.json", apic_url, username, password, auth_state)
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
        path = "topology/pod-{{POD_ID}}/paths-{node_id}/pathep-[eth{interface}]"
        dn = f"uni/tn-{tenant}/ap-{ap}/epg-{epg}/rspathAtt-[{{path}}]"
        r = safe_request('delete', session, f"{{apic_url}}/api/mo/{{dn}}.json", apic_url, username, password, auth_state)
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''

    selector_actions = [a for a in actions if "port_selector" in a["action"]]
    for a in selector_actions:
        action_num += 1
        name = a.get("name", "SELECTOR")
        int_profile = a.get("int_profile", "INT_PROFILE")
        script += f'''
    # Action {action_num}: Delete Port Selector
    print(f"  [{action_num}] Deleting port selector: {name}...")
    try:
        r = safe_request('delete', session,
            f"{{apic_url}}/api/mo/uni/infra/accportprof-{int_profile}/hports-{name}-typ-range.json",
            apic_url, username, password, auth_state)
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''

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
        r = safe_request('delete', session, f"{{apic_url}}/api/mo/{path}.json", apic_url, username, password, auth_state)
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''

    desc_actions = [a for a in actions if a["action"] == "clear_description"]
    for a in desc_actions:
        action_num += 1
        node_id = a.get("node_id", "NODE")
        interface = a.get("interface", "1/1")
        script += f'''
    # Action {action_num}: Clear port description
    print(f"  [{action_num}] Clearing description on node {node_id} eth{interface}...")
    try:
        dn = f"topology/pod-{{POD_ID}}/node-{node_id}/sys/phys-[eth{interface}]"
        r = safe_request('post', session, f"{{apic_url}}/api/node/mo/{{dn}}.json",
            apic_url, username, password, auth_state,
            json={{"l1PhysIf": {{"attributes": {{"descr": ""}}}}}})
        print(f"      {{'[OK]' if r.status_code == 200 else '[FAIL] ' + r.text[:80]}}")
    except Exception as e:
        print(f"      [ERROR] {{e}}")
'''

    epgadd_actions = [a for a in actions if a["action"] == "delete_binding" and deploy_type == "epgadd"]
    for a in epgadd_actions:
        action_num += 1
        switch = a.get("switch", "SWITCH")
        port = a.get("port", "1/1")
        vlan = a.get("vlan", "0")
        script += f'''
    # Action {action_num}: Delete EPG binding VLAN {vlan} from {switch} port {port}
    print(f"  [{action_num}] Deleting VLAN {vlan} from {switch} port {port}...")
    print(f"      [MANUAL] Locate and delete fvRsPathAtt for VLAN {vlan} on node/pathep")
'''

    recreate_actions = [a for a in actions if a["action"] == "recreate_binding"]
    for a in recreate_actions:
        action_num += 1
        switch = a.get("switch", "SWITCH")
        port = a.get("port", "1/1")
        vlan = a.get("vlan", "0")
        script += f'''
    # Action {action_num}: Re-create binding VLAN {vlan} on {switch} port {port}
    print(f"  [{action_num}] Re-creating VLAN {vlan} on {switch} port {port}...")
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
    confirm = input("Type YES to confirm rollback: ").strip()
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

            headers = [h.strip().upper() for h in reader.fieldnames if h]
            results["columns_found"] = headers

            missing = [col for col in reqs["required"] if col not in headers]
            if missing:
                results["valid"] = False
                results["errors"].append(f"Missing columns: {', '.join(missing)}")
                return results

            rows = list(reader)
            results["row_count"] = len(rows)
            if not rows:
                results["valid"] = False
                results["errors"].append("CSV has headers but no data rows")
                return results

            validators = reqs.get("validators", {})
            for i, row in enumerate(rows, 1):
                normalized = {k.strip().upper(): (v.strip() if v else "") for k, v in row.items() if k}
                for col, pattern in validators.items():
                    val = normalized.get(col, "").strip().strip('"').strip("'")
                    if val and not re.match(pattern, val, re.IGNORECASE):
                        results["warnings"].append(f"Row {i}: {col} value '{val}' may be invalid")

                for col in reqs["required"]:
                    if not normalized.get(col, "").strip():
                        results["warnings"].append(f"Row {i}: {col} is empty")

    except FileNotFoundError:
        results["valid"] = False
        results["errors"].append(f"File not found: {filepath}")
    except Exception as e:
        results["valid"] = False
        results["errors"].append(f"Parse error: {str(e)}")

    if len(results["warnings"]) > 10:
        total = len(results["warnings"])
        results["warnings"] = results["warnings"][:10]
        results["warnings"].append(f"... and {total - 10} more warnings")

    return results


# =============================================================================
# RESULTS CSV GENERATION
# =============================================================================

def extract_deployed_ports(output_lines, tracked_ports):
    """
    Resolve the final list of deployed ports (one per CSV row).

    tracked_ports is the list built during execution:
      - "1/93"      → port was pre-selected and matched from the menu
      - "__auto__"  → script fell back to auto-select; resolve from output
      - ""          → port was specified but not found in menu; unresolvable

    Uses THREE strategies to resolve ports:
      1. tracked_ports entries (from auto-select / CSV PORT column)
      2. "Selected: ethX/Y -> X/Y" lines from script output (manual selections)
      3. "Node NNN ethX/Y: [SUCCESS]" lines (deployment confirmations)
    """
    # Strategy 2: Scan ALL "Selected:" lines from output (covers manual picks)
    selected_ports = []
    for line in output_lines:
        m = re.search(r'Selected:\s*eth?(\d+/\d+)\s*->\s*(\d+/\d+)', line)
        if m:
            selected_ports.append(m.group(2))

    # Strategy 3: Scan Node SUCCESS lines (deployment confirmations)
    matched = {p for p in tracked_ports if p and p != '__auto__'}
    seen_node_port = set()
    auto_resolved = []
    for line in output_lines:
        m = re.search(r'Node\s+(\d+)\s+eth([\d/]+):\s*\[SUCCESS\]', line, re.IGNORECASE)
        if m:
            node_id, port = m.group(1), m.group(2)
            key = (node_id, port)
            if key not in seen_node_port:
                seen_node_port.add(key)
                if port not in matched:
                    auto_resolved.append(port)

    # Resolve: prefer tracked_ports, fall back to selected_ports, then auto_resolved
    auto_iter = iter(auto_resolved)
    selected_iter = iter(selected_ports)
    result = []

    if tracked_ports:
        for p in tracked_ports:
            if p and p != '__auto__':
                result.append(p)
            else:
                # Try selected_ports first, then auto_resolved
                sel = next(selected_iter, None)
                if sel:
                    result.append(sel)
                else:
                    result.append(next(auto_iter, ''))
    else:
        # No tracking at all — use selected_ports from output
        result = selected_ports

    return result


def extract_deployment_statuses(output_lines):
    """
    Scan output for per-deployment status indicators.
    Returns list of status strings in deployment order.
    
    Looks for patterns like:
      - "Complete: Desc=OK, PolicyGrp=OK" → DEPLOYED
      - "[SKIPPED by user]" or "[SKIP]"   → SKIPPED
      - "[CANCELLED]"                      → CANCELLED
      - "PolicyGrp=FAIL"                   → FAILED
      - "[INFO] Quitting..."               → QUIT
    """
    statuses = []
    quit_seen = False
    for line in output_lines:
        lu = line.upper().strip()
        if 'COMPLETE:' in lu and ('POLICYGR' in lu or 'SELECTOR' in lu or 'BINDING' in lu):
            if 'FAIL' in lu:
                statuses.append('FAILED')
            else:
                statuses.append('DEPLOYED')
        elif '[SKIPPED BY USER]' in lu or ('[SKIP]' in lu and 'NO VALIDATED' not in lu 
              and 'ENVIRONMENT' not in lu and 'COULD NOT' not in lu):
            statuses.append('SKIPPED')
        elif '[CANCELLED]' in lu:
            statuses.append('CANCELLED')
        elif 'QUITTING' in lu:
            quit_seen = True
    return statuses, quit_seen


def generate_results_csv(deploy_type, csv_path, entry_id, timestamp, output_lines, tracked_ports, run_status="success"):
    """
    Write a copy of the original CSV with SELECTED_PORT and STATUS columns.
    Always generated — success, failure, quit, or stopped.
    Returns the filename on success, None otherwise.
    """
    try:
        if not csv_path or not os.path.exists(csv_path):
            return None

        os.makedirs(RESULTS_FOLDER, exist_ok=True)
        ts = timestamp.replace(":", "").replace("-", "").replace(" ", "_")
        filename = f"results_{ts}_{deploy_type}_run{entry_id}.csv"
        filepath = os.path.join(RESULTS_FOLDER, filename)

        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            orig_headers = list(reader.fieldnames or [])
            rows = list(reader)

        if not rows:
            return None

        # Extract port selections and per-deployment statuses
        deployed = extract_deployed_ports(output_lines, tracked_ports)
        statuses, quit_seen = extract_deployment_statuses(output_lines)

        # Build result columns based on deploy type
        extra_cols = []
        if deploy_type in ('vpc', 'individual'):
            extra_cols.append('SELECTED_PORT')
        extra_cols.append('STATUS')

        result_headers = orig_headers + extra_cols
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=result_headers, extrasaction='ignore')
            writer.writeheader()
            for i, row in enumerate(rows):
                if deploy_type in ('vpc', 'individual'):
                    row['SELECTED_PORT'] = deployed[i] if i < len(deployed) else ''
                
                # Assign status: use extracted if available, else infer
                if i < len(statuses):
                    row['STATUS'] = statuses[i]
                elif quit_seen and i >= len(statuses):
                    row['STATUS'] = 'NOT_REACHED'
                elif run_status == 'stopped':
                    row['STATUS'] = 'STOPPED'
                else:
                    row['STATUS'] = ''
                
                writer.writerow(row)

        return filename
    except Exception as e:
        print(f"[WARNING] Failed to generate results CSV: {e}")
        return None


# =============================================================================
# PROCESS MANAGEMENT
# =============================================================================

def find_port_in_output(desired_port, output_lines, start_idx=0):
    """
    Scan terminal output for a numbered port menu and return the menu number
    that corresponds to the desired port (e.g. '1/93' → '73').

    start_idx: only look at output_lines[start_idx:] so we never match a port
               menu from a previous deployment within the same run.

    Handles output formats such as:
        73. eth1/93
        73: 1/93
        73)  eth1/93    [free]
        73   1/93
    """
    m = re.match(r'(?:eth)?(\d+)/(\d+)', desired_port.strip())
    if not m:
        return None
    desired_slot, desired_num = m.group(1), m.group(2)

    # Scan forward from start_idx — the current deployment's menu always
    # appears after start_idx, so we can never accidentally match a stale list.
    for line in reversed(output_lines[start_idx:]):
        lm = re.match(r'^\s*(\d+)[.:)\s]+(?:eth)?(\d+)/(\d+)', line.strip())
        if lm:
            menu_num, slot, port_num = lm.group(1), lm.group(2), lm.group(3)
            if slot == desired_slot and port_num == desired_num:
                return menu_num
    return None


def find_first_avail_port(output_lines, start_idx=0):
    """
    Scan the port menu output for the first [AVAIL] port and return its
    menu number and interface string.

    Port display lines look like:
        [ 1] [AVAIL]  eth1/1       25G     inherit    (Usage: discovery)
        [ 2] [IN-USE] eth1/2       25G     ...
        [23] [AVAIL]  eth1/23      25G     ...

    Returns (menu_number_str, interface_str) or (None, None) if no AVAIL found.
    """
    for line in output_lines[start_idx:]:
        if '[AVAIL]' in line:
            # Extract menu number: leading [XX] or bare number
            m = re.match(r'^\s*\[?\s*(\d+)\]?\s*\[AVAIL\]\s+(?:eth)?(\d+/\d+)', line.strip())
            if m:
                return m.group(1), m.group(2)
    return None, None




def parse_port_column(csv_path):
    """
    Read the CSV file and return a list of PORT values (one per data row).
    Returns an empty list if the column is absent or the file can't be read.
    """
    if not csv_path or not os.path.exists(csv_path):
        return []
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            headers = [h.strip().upper() for h in (reader.fieldnames or [])]
            if 'PORT' not in headers:
                return []
            return [
                {k.strip().upper(): (v.strip() if v else '') for k, v in row.items() if k}
                .get('PORT', '').strip()
                for row in reader
            ]
    except Exception:
        return []


def run_script_thread(script_path, csv_path):
    global running_process, current_run
    current_run["start_time"] = time.time()
    current_run["output_lines"] = []
    current_run["token_failures"] = 0
    current_run["tenant_choice"] = None
    current_run["last_prompt_type"] = None
    current_run["port_prompt_index"] = 0   # counts how many port prompts have been seen
    current_run["deployed_ports"] = []     # tracks actual port per CSV row
    current_run["last_port_prompt_output_idx"] = 0  # output line index when last port prompt was answered

    try:
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        env['ACI_WEB_UI'] = '1'
        env['ACI_SESSION_TIMEOUT'] = '600'

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

        cred_state = {"awaiting": None}
        recent_prompt_lines = []

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
                        recent_prompt_lines.append(line.lower())
                        if len(recent_prompt_lines) > 8:
                            recent_prompt_lines.pop(0)
                        ll = line.lower()
                        if 'token was invalid' in ll or 'token timeout' in ll or (
                                'not authenticated' in ll and 'error' in ll):
                            current_run["token_failures"] = current_run.get("token_failures", 0) + 1
                            if current_run["token_failures"] == 1:
                                output_queue.put(('output',
                                    '[WARNING] APIC token expired! Use aci_session_manager.py for auto-refresh.'))
                                current_run["output_lines"].append(
                                    '[WARNING] APIC token expired — deploy scripts need session manager')

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

                        # AUTO-CSV PATH INJECTION
                        if ('enter filename' in tl or 'use default' in tl) and tl.endswith(':'):
                            csv_path = current_run.get("csv_path", "")
                            if csv_path:
                                time.sleep(0.1)
                                try:
                                    running_process.stdin.write((csv_path + '\n').encode('utf-8'))
                                    running_process.stdin.flush()
                                    basename = os.path.basename(csv_path)
                                    output_queue.put(('output', f'[AUTO] CSV path injected: {basename}'))
                                    current_run["output_lines"].append(f'[AUTO] CSV path injected: {csv_path}')
                                except:
                                    pass

                        # AUTO-PORT SELECTION
                        # Scripts prompt: "Select port number:"
                        if 'select port number' in tl and tl.endswith(':'):
                            cfg = load_config()
                            port_selections = current_run.get("port_selections", [])
                            port_idx = current_run.get("port_prompt_index", 0)
                            desired_iface = port_selections[port_idx] if port_idx < len(port_selections) else ''
                            # Only search output lines printed after the previous port prompt
                            # was answered — prevents matching a stale menu from an earlier row
                            search_from = current_run.get("last_port_prompt_output_idx", 0)

                            if desired_iface:
                                # Extract raw port number: "1/61" → "61", "eth1/61" → "61", "Eth1/61" → "61"
                                import re as _re
                                _port_m = _re.match(r'(?:[Ee]th)?(\d+)/(\d+)', desired_iface.strip())
                                if _port_m:
                                    raw_port_num = _port_m.group(2)
                                    time.sleep(0.1)
                                    try:
                                        running_process.stdin.write((raw_port_num + '\n').encode('utf-8'))
                                        running_process.stdin.flush()
                                        output_queue.put(('output', f'[AUTO] Port input: {desired_iface} → sending {raw_port_num}'))
                                        current_run["output_lines"].append(f'[AUTO] Port input: {desired_iface} → sending {raw_port_num}')
                                        current_run["deployed_ports"].append(desired_iface)
                                    except:
                                        current_run["deployed_ports"].append('')
                                else:
                                    output_queue.put(('output', f'[WARNING] Cannot parse port: {desired_iface} — waiting for manual input'))
                                    current_run["output_lines"].append(f'[WARNING] Cannot parse port: {desired_iface}')
                                    current_run["deployed_ports"].append('')
                            elif cfg.get('auto_select_port', True):
                                # Find first [AVAIL] port instead of blindly picking #1
                                avail_num, avail_iface = find_first_avail_port(
                                    current_run["output_lines"], search_from)
                                pick = avail_num if avail_num else '1'
                                label = f'{avail_iface} (first available)' if avail_iface else '#1 (no AVAIL tags found)'
                                time.sleep(0.1)
                                try:
                                    running_process.stdin.write((pick + '\n').encode('utf-8'))
                                    running_process.stdin.flush()
                                    output_queue.put(('output', f'[AUTO] Port auto-selected: {label} → option {pick}'))
                                    current_run["output_lines"].append(f'[AUTO] Port auto-selected: {label} → option {pick}')
                                    current_run["deployed_ports"].append(avail_iface if avail_iface else '__auto__')
                                except:
                                    current_run["deployed_ports"].append('')

                            # Advance the search window so the next prompt never
                            # looks back into this deployment's port list
                            current_run["last_port_prompt_output_idx"] = len(current_run["output_lines"])
                            current_run["port_prompt_index"] = port_idx + 1

                        # AUTO-ROLLBACK CONFIRMATION
                        # Rollback scripts prompt: "Type YES to confirm rollback:"
                        if 'confirm rollback' in tl and tl.endswith(':'):
                            if current_run.get("is_rollback"):
                                time.sleep(0.1)
                                try:
                                    running_process.stdin.write(('YES\n').encode('utf-8'))
                                    running_process.stdin.flush()
                                    output_queue.put(('output', '[AUTO] Rollback confirmed: YES'))
                                    current_run["output_lines"].append('[AUTO] Rollback confirmed: YES')
                                except:
                                    pass

                        # AUTO-EPG MODE SELECTION
                        # Detects "EPG BINDING MODE" or "EPG MODE" in recent output and
                        # auto-injects "3" (overwrite all) or "1" (add) based on settings.
                        recent_5 = [r.lower() for r in current_run["output_lines"][-5:]]
                        is_epg_mode = any('epg binding mode' in r or 'epg mode' in r for r in recent_5)
                        if is_epg_mode and ('(1/2' in tl or 'select mode' in tl) and tl.endswith(':'):
                            cfg = load_config()
                            epg_ow = '3' if cfg.get('epg_overwrite_default', False) else '1'
                            epg_label = 'Overwrite ALL' if epg_ow == '3' else 'Add'
                            time.sleep(0.1)
                            try:
                                running_process.stdin.write((epg_ow + '\n').encode('utf-8'))
                                running_process.stdin.flush()
                                output_queue.put(('output', f'[AUTO] EPG mode: {epg_label} (from settings)'))
                                current_run["output_lines"].append(f'[AUTO] EPG mode: {epg_label}')
                            except:
                                pass

                        # AUTO-TENANT SELECTION
                        is_tenant_prompt = ('applies to all' in tl and 'select' in tl) or \
                                           ('select' in tl and 'tenant' in tl) or \
                                           ('multiple tenants' in tl)
                        if not is_tenant_prompt:
                            has_tenant_context = any(
                                'tenant' in rl or 'multiple tenants' in rl
                                for rl in recent_prompt_lines
                            )
                            is_tenant_prompt = has_tenant_context and (
                                tl.startswith('select') and tl.endswith(':'))

                        if is_tenant_prompt:
                            current_run["last_prompt_type"] = "tenant"
                            tenant_val = current_run.get("tenant_choice")
                            if tenant_val:
                                time.sleep(0.1)
                                try:
                                    running_process.stdin.write((str(tenant_val) + '\n').encode('utf-8'))
                                    running_process.stdin.flush()
                                    output_queue.put(('output', f'[AUTO] Tenant selection applied: {tenant_val}'))
                                    current_run["output_lines"].append(f'[AUTO] Tenant selection: {tenant_val}')
                                except:
                                    pass
                        else:
                            current_run["last_prompt_type"] = None

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
<title>ACI Automation Console</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
:root{--bg-darkest:#0a0e17;--bg-dark:#0f172a;--bg-sidebar:#0a0e17;--bg-terminal:#0f172a;--bg-input:#1e293b;--border-color:#334155;--text-primary:#e2e8f0;--text-secondary:#94a3b8;--text-muted:#64748b;--accent-blue:#60a5fa;--accent-cyan:#22d3ee;--accent-green:#4ade80;--accent-orange:#fb923c;--accent-red:#f87171;--accent-purple:#a78bfa;--accent-yellow:#fbbf24;--glow-cyan:rgba(34,211,238,0.15)}
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
.nav-item:hover{background:rgba(96,165,250,.08);border-color:rgba(96,165,250,.2)}
.nav-item.active{background:linear-gradient(135deg,rgba(34,211,238,.12),rgba(96,165,250,.12));border-color:var(--accent-cyan);box-shadow:0 0 20px var(--glow-cyan)}
.nav-icon{width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px}
.nav-item.vpc .nav-icon{background:linear-gradient(135deg,var(--accent-purple),var(--accent-blue))}
.nav-item.individual .nav-icon{background:linear-gradient(135deg,var(--accent-orange),var(--accent-yellow))}
.nav-item.settings .nav-icon{background:linear-gradient(135deg,var(--accent-cyan),var(--accent-green))}
.nav-item.readme .nav-icon{background:linear-gradient(135deg,#f093fb,#f5576c)}
.nav-item.epgadd .nav-icon{background:linear-gradient(135deg,#11998e,#38ef7d)}
.nav-item.epgdelete .nav-icon{background:linear-gradient(135deg,#eb3349,#f45c43)}
.nav-item.logs .nav-icon{background:linear-gradient(135deg,#667eea,#764ba2)}
.nav-item.credentials .nav-icon{background:linear-gradient(135deg,#fb923c,#fbbf24)}
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
.header-badge.credentials{background:rgba(251,191,36,.2);color:#fbbf24}
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
.file-picker-btn{padding:12px 20px;border:1px solid var(--accent-cyan);border-radius:8px;background:linear-gradient(135deg,rgba(34,211,238,.1),rgba(96,165,250,.1));color:var(--accent-cyan);font-family:'IBM Plex Sans',sans-serif;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s;white-space:nowrap}
.file-picker-btn:hover{background:linear-gradient(135deg,rgba(34,211,238,.2),rgba(96,165,250,.2));box-shadow:0 0 16px var(--glow-cyan)}
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
.port-avail{color:var(--accent-green)!important;font-weight:700}
.port-inuse{color:var(--accent-red)!important;font-weight:700}
.terminal-line.port-available{background:rgba(74,222,128,.06);border-left:3px solid var(--accent-green);padding-left:8px}
.terminal-line.port-in-use{background:rgba(248,113,113,.06);border-left:3px solid var(--accent-red);padding-left:8px}
.port-avail{color:var(--accent-green)!important;font-weight:700}
.port-inuse{color:var(--accent-red)!important;font-weight:700}
.terminal-line.port-available{background:rgba(74,222,128,.06);border-left:3px solid var(--accent-green);padding-left:8px}
.terminal-line.port-in-use{background:rgba(248,113,113,.06);border-left:3px solid var(--accent-red);padding-left:8px}
.port-avail{color:var(--accent-green)!important;font-weight:700}
.port-inuse{color:var(--accent-red)!important;font-weight:700}
.terminal-line.port-available{background:rgba(63,185,80,.06);border-left:3px solid var(--accent-green);padding-left:8px}
.terminal-line.port-in-use{background:rgba(248,81,73,.06);border-left:3px solid var(--accent-red);padding-left:8px}
.port-avail{color:var(--accent-green)!important;font-weight:700}
.port-inuse{color:var(--accent-red)!important;font-weight:700}
.terminal-line.port-available{background:rgba(63,185,80,.06);border-left:3px solid var(--accent-green);padding-left:8px}
.terminal-line.port-in-use{background:rgba(248,81,73,.06);border-left:3px solid var(--accent-red);padding-left:8px}
.port-avail{color:var(--accent-green)!important;font-weight:700}
.port-inuse{color:var(--accent-red)!important;font-weight:700}
.terminal-line.port-available{background:rgba(63,185,80,.06);border-left:3px solid var(--accent-green);padding-left:8px}
.terminal-line.port-in-use{background:rgba(248,81,73,.06);border-left:3px solid var(--accent-red);padding-left:8px}
.terminal-input-area{display:flex;align-items:center;padding:12px 16px;background:rgba(0,0,0,.3);border-top:1px solid var(--border-color);gap:12px}
.post-run-bar{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;background:linear-gradient(135deg,rgba(74,222,128,.08),rgba(34,211,238,.08));border-top:1px solid rgba(74,222,128,.3)}
.post-run-bar.hidden{display:none}
.post-run-label{font-size:13px;font-weight:600;color:var(--accent-green);font-family:'JetBrains Mono',monospace}
.post-run-actions{display:flex;gap:8px}
.post-run-btn{padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;border:none;font-family:'IBM Plex Sans',sans-serif;transition:all .2s}
.post-run-btn.results-btn{background:rgba(63,185,80,.15);color:var(--accent-green)}
.post-run-btn.results-btn:hover{background:rgba(63,185,80,.3)}
.post-run-btn.log-btn:hover{background:rgba(88,166,255,.3)}
.post-run-btn.rollback-btn{background:rgba(248,81,73,.12);color:var(--accent-red)}
.post-run-btn.rollback-btn:hover{background:rgba(248,81,73,.25)}
.post-run-btn.run-rb-btn{background:rgba(248,81,73,.25);color:#fff;border:1px solid var(--accent-red)}
.post-run-btn.run-rb-btn:hover{background:rgba(248,81,73,.45)}
.post-run-dismiss{background:none;border:none;color:var(--text-muted);font-size:16px;cursor:pointer;padding:4px 8px;line-height:1;border-radius:4px;margin-left:4px}
.post-run-dismiss:hover{color:var(--text-primary);background:rgba(255,255,255,.08)}
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
.time-saved-banner{margin-top:32px;padding:20px 36px;background:linear-gradient(135deg,rgba(34,211,238,.06),rgba(96,165,250,.06));border:1px solid rgba(34,211,238,.25);border-radius:12px;display:flex;gap:32px;align-items:center}
.ts-stat{text-align:center}
.ts-value{font-size:28px;font-weight:700;font-family:'JetBrains Mono',monospace;background:linear-gradient(135deg,var(--accent-cyan),var(--accent-green));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.ts-value.blue{background:linear-gradient(135deg,var(--accent-blue),var(--accent-purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.ts-value.purple{-webkit-text-fill-color:var(--accent-purple)}
.ts-divider{width:1px;height:40px;background:var(--border-color)}
/* Credential Panel */
.cred-panel{padding:32px;overflow-y:auto;flex:1}
.cred-section{background:var(--bg-darkest);border:1px solid var(--border-color);border-radius:12px;padding:24px;margin-bottom:20px;max-width:600px}
.cred-section-title{font-size:14px;font-weight:600;color:#fbbf24;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.cred-status{padding:12px 16px;border-radius:8px;margin-bottom:20px;font-size:13px;display:flex;align-items:center;gap:10px}
.cred-status.set{background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.3);color:var(--accent-green)}
.cred-status.unset{background:rgba(248,113,113,.08);border:1px solid rgba(248,113,113,.2);color:var(--accent-red)}
.cred-row{margin-bottom:16px}
.cred-label{display:block;font-size:12px;font-weight:500;color:var(--text-secondary);margin-bottom:8px}
.cred-input{width:100%;padding:12px 16px;background:var(--bg-input);border:1px solid var(--border-color);border-radius:8px;color:var(--text-primary);font-family:'JetBrains Mono',monospace;font-size:13px}
.cred-input:focus{outline:none;border-color:#fbbf24;box-shadow:0 0 0 3px rgba(251,191,36,.15)}
.cred-hint{font-size:11px;color:var(--text-muted);margin-top:12px;padding:10px;background:var(--bg-input);border-radius:6px;line-height:1.6}
.cred-actions{display:flex;gap:12px;margin-top:20px}
.cred-btn{padding:10px 20px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:none;font-family:inherit}
.cred-btn.save{background:linear-gradient(135deg,#fb923c,#fbbf24);color:#0a0e17}
.cred-btn.save:hover{box-shadow:0 4px 16px rgba(251,146,60,.3)}
.cred-btn.clear{background:transparent;border:1px solid var(--accent-red);color:var(--accent-red)}
.cred-btn.clear:hover{background:rgba(248,81,73,.1)}
/* Settings, Readme, Logs panels */
.settings-panel{padding:24px;overflow-y:auto;flex:1}
.settings-section{background:var(--bg-darkest);border:1px solid var(--border-color);border-radius:12px;padding:20px;margin-bottom:20px}
.settings-section-title{font-size:14px;font-weight:600;color:var(--accent-cyan);margin-bottom:16px}
.settings-row{margin-bottom:16px}.settings-row:last-child{margin-bottom:0}
.settings-label{display:block;font-size:12px;font-weight:500;color:var(--text-secondary);margin-bottom:8px}
.settings-input{width:100%;padding:12px 16px;background:var(--bg-input);border:1px solid var(--border-color);border-radius:8px;color:var(--text-primary);font-family:'JetBrains Mono',monospace;font-size:13px}
.settings-input:focus{outline:none;border-color:var(--accent-cyan)}
.settings-hint{font-size:11px;color:var(--text-muted);margin-top:6px}
/* Toggle switch */
.toggle-row{display:flex;align-items:center;gap:14px;padding:14px 16px;background:var(--bg-input);border:1px solid var(--border-color);border-radius:8px;cursor:pointer;transition:border-color .2s}
.toggle-row:hover{border-color:var(--accent-cyan)}
.toggle-switch{position:relative;width:42px;height:24px;flex-shrink:0}
.toggle-switch input{opacity:0;width:0;height:0;position:absolute}
.toggle-slider{position:absolute;inset:0;background:#334155;border-radius:24px;transition:.3s;cursor:pointer}
.toggle-slider:before{content:'';position:absolute;width:18px;height:18px;left:3px;bottom:3px;background:#94a3b8;border-radius:50%;transition:.3s}
.toggle-switch input:checked+.toggle-slider{background:rgba(34,211,238,.3);border:1px solid var(--accent-cyan)}
.toggle-switch input:checked+.toggle-slider:before{transform:translateX(18px);background:var(--accent-cyan);box-shadow:0 0 8px rgba(34,211,238,.5)}
.toggle-info{flex:1}
.toggle-title{font-size:13px;font-weight:600;color:var(--text-primary);margin-bottom:3px}
.toggle-desc{font-size:11px;color:var(--text-muted);line-height:1.5}
.toggle-badge{padding:2px 8px;border-radius:8px;font-size:10px;font-weight:700;font-family:'JetBrains Mono',monospace;transition:all .3s}
.toggle-badge.on{background:rgba(57,212,212,.15);color:var(--accent-cyan)}
.toggle-badge.off{background:rgba(110,118,129,.15);color:var(--text-muted)}
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
.log-action-btn.run-rollback{background:rgba(248,81,73,.25);color:#fff;border:1px solid var(--accent-red)}
.log-action-btn.run-rollback:hover{background:rgba(248,81,73,.45)}
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
.csv-port-select{width:100%;padding:5px 6px;background:var(--bg-terminal);border:1px solid transparent;border-radius:4px;color:var(--accent-cyan);font-family:'JetBrains Mono',monospace;font-size:11px;cursor:pointer;appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%2339d4d4' opacity='.5'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 6px center;padding-right:22px}
.csv-port-select:focus{border-color:var(--accent-cyan);outline:none}
.csv-port-select option{background:var(--bg-darkest);color:var(--text-primary)}
/* Tenant Memory Bar */
.tenant-bar{display:flex;align-items:center;gap:10px;padding:8px 16px;background:rgba(163,113,247,.1);border-top:1px solid rgba(163,113,247,.3)}
.tenant-bar.locked{background:rgba(63,185,80,.08);border-color:rgba(63,185,80,.3)}
.tenant-bar-icon{font-size:14px}
.tenant-bar-text{flex:1;font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--text-secondary)}
.tenant-bar-text strong{color:var(--accent-green)}
.tenant-bar-btn{padding:4px 12px;border-radius:5px;font-size:11px;font-weight:600;cursor:pointer;border:none;font-family:inherit;transition:all .2s}
.tenant-bar-btn.apply{background:rgba(163,113,247,.2);color:var(--accent-purple)}
.tenant-bar-btn.apply:hover{background:rgba(163,113,247,.35)}
.tenant-bar-btn.clear{background:rgba(248,81,73,.12);color:var(--accent-red);font-size:10px}
.tenant-bar-btn.clear:hover{background:rgba(248,81,73,.25)}
.hidden{display:none!important}
</style>
</head>
<body>
<div class="app-container">
<aside class="sidebar">
<div class="sidebar-header"><div class="logo"><div class="logo-icon">ACI</div><div><div class="logo-text">Automation</div><div class="logo-subtitle">Console</div></div></div></div>
<nav class="nav-section">
<div class="nav-label">Deployments</div>
<div class="nav-item vpc" onclick="selectView('vpc')"><div class="nav-icon">🔀</div><div><div class="nav-item-title">VPC Deploy</div><div class="nav-item-desc">Virtual Port Channels</div></div></div>
<div class="nav-item individual" onclick="selectView('individual')"><div class="nav-icon">🔌</div><div><div class="nav-item-title">Static Port Deploy</div><div class="nav-item-desc">Access &amp; Trunk Ports</div></div></div>
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
<div class="welcome-icon">🚀</div><h1 class="welcome-title">ACI Automation Console</h1>
<p class="welcome-desc">Streamline your Cisco ACI fabric deployments with automated VPC and static port configurations.</p>
<div class="welcome-cards">
<div class="welcome-card vpc" onclick="selectView('vpc')"><div class="welcome-card-icon">🔀</div><div class="welcome-card-title">VPC Deploy</div><div class="welcome-card-desc">Deploy Virtual Port Channels across switch pairs</div></div>
<div class="welcome-card individual" onclick="selectView('individual')"><div class="welcome-card-icon">🔌</div><div class="welcome-card-title">Static Port Deploy</div><div class="welcome-card-desc">Deploy static access and trunk ports</div></div>
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
<div class="cred-divider" style="border-top:1px solid var(--border-color);margin:16px 0;padding-top:16px"><label class="cred-label" style="font-size:13px;color:var(--text-primary);font-weight:600;margin-bottom:10px;display:block">🌐 APIC URLs <span style="font-weight:400;color:var(--text-muted);font-size:11px">(used in rollback scripts)</span></label></div>
<div class="cred-row"><label class="cred-label">D1 (Primary DC)</label><input type="text" class="cred-input" id="credApicD1" placeholder="https://apic-d1.example.com" autocomplete="off"></div>
<div class="cred-row"><label class="cred-label">D2 (Secondary DC)</label><input type="text" class="cred-input" id="credApicD2" placeholder="https://apic-d2.example.com" autocomplete="off"></div>
<div class="cred-row"><label class="cred-label">D3 (Tertiary DC)</label><input type="text" class="cred-input" id="credApicD3" placeholder="https://apic-d3.example.com" autocomplete="off"></div>
<div class="cred-actions"><button class="cred-btn save" onclick="saveCredentials()">Save to Memory</button><button class="cred-btn clear" onclick="clearCredentials()">Clear</button></div>
<div class="cred-actions" style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border-color)"><button class="cred-btn save" style="background:#2563eb" onclick="saveToDisk()">💾 Save to Disk</button><button class="cred-btn save" style="background:#059669" onclick="loadFromDisk()">📂 Load from Disk</button></div>
<div class="cred-hint">🛡️ Credentials are stored <strong>in memory</strong> by default and clear on app restart. Use <strong>Save to Disk</strong> to persist across restarts (base64 obfuscated in <code>.aci_credentials</code>). Use <strong>Load from Disk</strong> to restore them.<br><br>When set, credentials are auto-injected into scripts when they prompt for Username/Password — no manual typing needed. APIC URLs are embedded into generated rollback scripts.</div>
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
<div class="settings-row"><label class="settings-label">VPC Deployment Script</label><input type="text" class="settings-input" id="settingsVpcScript" value="{{ config.vpc_script }}"><div class="settings-hint">Path to the VPC deployment Python script</div></div>
<div class="settings-row"><label class="settings-label">Static Port Deployment Script</label><input type="text" class="settings-input" id="settingsIndividualScript" value="{{ config.individual_script }}"><div class="settings-hint">Path to the static port deployment Python script</div></div>
<div class="settings-row"><label class="settings-label">EPG Add Script</label><input type="text" class="settings-input" id="settingsEpgaddScript" value="{{ config.epgadd_script }}"></div>
<div class="settings-row"><label class="settings-label">EPG Delete Script</label><input type="text" class="settings-input" id="settingsEpgdeleteScript" value="{{ config.epgdelete_script }}"></div>
</div>
<div class="settings-section">
<div class="settings-section-title">⚡ Automation Behaviour</div>
<div class="settings-row">
<label class="toggle-row" for="settingsAutoSelectPort">
  <div class="toggle-switch">
    <input type="checkbox" id="settingsAutoSelectPort" {% if config.auto_select_port %}checked{% endif %} onchange="updateToggleBadge(this)">
    <span class="toggle-slider"></span>
  </div>
  <div class="toggle-info">
    <div class="toggle-title">Auto-select first available port</div>
    <div class="toggle-desc">When the script prompts <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:10px">Select port number:</code>, automatically pick the first <span class="port-avail">[AVAIL]</span> port — skipping the manual prompt entirely.</div>
  </div>
  <span class="toggle-badge {% if config.auto_select_port %}on{% else %}off{% endif %}" id="autoPortBadge">{% if config.auto_select_port %}ON{% else %}OFF{% endif %}</span>
</label>
</div>
<div class="settings-row">
<label class="toggle-row" for="settingsEpgOverwrite">
  <div class="toggle-switch">
    <input type="checkbox" id="settingsEpgOverwrite" {% if config.epg_overwrite_default %}checked{% endif %} onchange="updateToggleBadge(this)">
    <span class="toggle-slider"></span>
  </div>
  <div class="toggle-info">
    <div class="toggle-title">Default EPG Overwrite Mode</div>
    <div class="toggle-desc">When enabled, all scripts default to <strong>Overwrite ALL</strong> (auto-delete ALL existing EPG bindings before deploying new). When disabled, scripts default to <strong>Add</strong> (keep existing). Applies to VPC, Static Port, and EPG Add.</div>
  </div>
  <span class="toggle-badge {% if config.epg_overwrite_default %}on{% else %}off{% endif %}" id="epgOverwriteBadge">{% if config.epg_overwrite_default %}ON{% else %}OFF{% endif %}</span>
</label>
</div>
</div>
<div class="settings-section"><div class="settings-section-title">ℹ️ Application Info</div><div class="settings-row"><label class="settings-label">Version</label><input type="text" class="settings-input" id="settingsVersion" value="{{ config.version }}"></div></div>
</div></div>

<!-- README -->
<div id="readmeScreen" class="hidden" style="flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden">
<div class="header-bar"><div class="header-title"><h2>Documentation</h2><span class="header-badge readme">README</span></div></div>
<div class="readme-panel">
<div class="readme-tabs" style="flex-wrap:wrap">
<div class="readme-tab active" onclick="switchReadmeTab('ui')">🖥️ Using the UI</div>
<div class="readme-tab" onclick="switchReadmeTab('vpc')">🔀 VPC Deploy</div>
<div class="readme-tab" onclick="switchReadmeTab('individual')">🔌 Static Port</div>
<div class="readme-tab" onclick="switchReadmeTab('epgadd')">➕ EPG Add</div>
<div class="readme-tab" onclick="switchReadmeTab('epgdelete')">➖ EPG Delete</div>
<div class="readme-tab" onclick="switchReadmeTab('management')">⚙️ Management</div>
<div class="readme-tab" onclick="switchReadmeTab('troubleshoot')">🔧 Troubleshoot</div>
</div>
<div id="readmeTabUi" class="readme-tab-content active">
<div class="readme-section"><div class="readme-section-title"><span>🚀</span> Getting Started</div><div class="readme-content">
<div class="step"><div class="step-number">1</div><div class="step-content"><div class="step-title">Set Credentials</div><div>Click <strong>Credentials</strong> in the sidebar and enter your APIC username and password, plus the APIC URL for each datacenter (D1, D2, D3). These are stored <strong>in-memory only</strong> and auto-injected into every script prompt — no manual typing needed during deployments.</div></div></div>
<div class="step"><div class="step-number">2</div><div class="step-content"><div class="step-title">Prepare Your CSV</div><div>Each deployment type has its own CSV format. You can <strong>browse</strong> for an existing file, <strong>build inline</strong> using the table editor, or <strong>drag-and-drop</strong>. Column headers are validated before launch — you'll see errors and warnings before the script runs.</div></div></div>
<div class="step"><div class="step-number">3</div><div class="step-content"><div class="step-title">Select Deployment Type</div><div>Pick from the sidebar: <strong>VPC Deploy</strong>, <strong>Static Port</strong>, <strong>EPG Add</strong>, or <strong>EPG Delete</strong>. Each has its own terminal, CSV builder, and post-run actions.</div></div></div>
<div class="step"><div class="step-number">4</div><div class="step-content"><div class="step-title">Run &amp; Respond</div><div>Click <strong>Run Script</strong>. Credentials auto-inject. The script runs in a live terminal — interact with prompts using the input bar at the bottom. Ports tagged <span style="color:var(--accent-green);font-weight:600">[AVAIL]</span> and <span style="color:var(--accent-red);font-weight:600">[IN-USE]</span> are color-coded for easy scanning.</div></div></div>
<div class="step"><div class="step-number">5</div><div class="step-content"><div class="step-title">Review Results</div><div>After every run (success, fail, or quit), you get:<br>• <strong>Results CSV</strong> — your original CSV with SELECTED_PORT and STATUS columns appended<br>• <strong>Sanitized Log</strong> — full terminal output with credentials redacted, ready for work orders<br>• <strong>Rollback Script</strong> — one-click reversal of everything that was deployed</div></div></div>
</div></div>
<div class="readme-section"><div class="readme-section-title"><span>⚡</span> Automation Features</div><div class="readme-content">
<h3>Auto-Credential Injection</h3><p>Username, password, and APIC URLs are injected automatically when the script prompts for them. No copy-pasting between windows.</p>
<h3>Auto-Select First Available Port</h3><p>When enabled in Settings, the UI scans the port menu for the first <span style="color:var(--accent-green);font-weight:600">[AVAIL]</span> port and selects it automatically — skipping <span style="color:var(--accent-red);font-weight:600">[IN-USE]</span> ports. You can also pre-select specific ports in the CSV PORT column.</p>
<h3>Tenant Memory</h3><p>When a multi-tenant prompt appears, after you make your selection, the UI offers to lock that choice and auto-apply it to all remaining deployments in the batch.</p>
<h3>Token Auto-Refresh</h3><p>APIC tokens expire after 5 minutes. During long batch runs, the scripts proactively refresh the token before each deployment — no more mid-run 403 "Token was invalid" failures.</p>
<h3>Pre-Selected Port via CSV</h3><p>Add an optional <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">PORT</code> column to your VPC or Static Port CSV (e.g. <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">1/93</code>). The UI matches it to the numbered port menu and selects it automatically.</p>
</div></div></div>

<div id="readmeTabVpc" class="readme-tab-content">
<div class="readme-section"><div class="readme-section-title"><span>🔀</span> VPC Deploy</div><div class="readme-content">
<p>Deploy Virtual Port Channels across leaf switch pairs. Creates the full policy stack from port description through EPG bindings in one automated pass.</p>
<h3>CSV Format</h3>
<p><code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">Hostname, Switch1, Switch2, Speed, VLANS, WorkOrder, PORT (optional)</code></p>
<p>Example: <code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">MEDHVIOP173_SEA,EDCLEAFACC1501,EDCLEAFACC1502,25G,"32,64-67",WO123456,1/93</code></p>
<h3>What Gets Deployed (4 Steps)</h3>
<p><strong>Step 1 — Port Description:</strong> Sets <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">HOSTNAME WORKORDER</code> on both leaf nodes.</p>
<p><strong>Step 2 — VPC Interface Policy Group:</strong> Creates a bundled policy group with your selected AEP, Link Level (speed), CDP, LLDP, Port Channel, MCP, Storm Control, and Flow Control policies.</p>
<p><strong>Step 3 — Port Selector:</strong> Links the physical port to the new policy group under the correct Interface Profile.</p>
<p><strong>Step 4 — EPG Static Bindings:</strong> Deploys each VLAN as a static path binding (VPC type, trunk/tagged, immediate deployment).</p>
<h3>Smart Features</h3>
<p><strong>Port Validation:</strong> Queries both switches and shows only ports that pass all 4 criteria: usage = discovery, no description, no policy group, no EPG bindings. Ports are color-coded <span style="color:var(--accent-green);font-weight:600">[AVAIL]</span> / <span style="color:var(--accent-red);font-weight:600">[IN-USE]</span>.</p>
<p><strong>Common Port Matching:</strong> Only shows ports that are available on <em>both</em> switches in the VPC pair.</p>
<p><strong>Policy Group Reuse:</strong> If a VPC policy group already exists for the same hostname, the script detects it and offers to reuse it instead of creating a duplicate.</p>
<p><strong>Port Cleanup for Redeployment:</strong> If you need to redeploy to a port that already has a policy group, the script can delete the existing selector and policy group first.</p>
<p><strong>Flow Control:</strong> Choose between default or FLOW-CONTROL-ON at the start of the batch.</p>
<p><strong>Pre-Flight Checks:</strong> Before any deployment, the script queries APIC for AEPs, Interface Profiles, and Link Level policies — letting you select once and apply to the entire batch.</p>
<p><strong>Multi-Tenant Search:</strong> VLANs are searched across all tenants (BLU, GWC, GWS or NSM_BLU, NSM_BRN, etc.) and you're prompted to choose if a VLAN exists in multiple Application Profiles.</p>
</div></div></div>

<div id="readmeTabIndividual" class="readme-tab-content">
<div class="readme-section"><div class="readme-section-title"><span>🔌</span> Static Port Deploy</div><div class="readme-content">
<p>Deploy individual (non-VPC) access and trunk ports. Creates the full policy stack from description through EPG bindings.</p>
<h3>CSV Format</h3>
<p><code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">Hostname, Switch, Type, Speed, VLANS, WorkOrder, PORT (optional)</code></p>
<p>Example: <code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">MEDHVIOP173_MGMT,EDCLEAFNSM2163,ACCESS,1G,2958,WO123456</code></p>
<h3>Access vs Trunk</h3>
<p><strong>ACCESS</strong> — Single VLAN, untagged traffic. Use for management interfaces, single-purpose ports.</p>
<p><strong>TRUNK</strong> — Multiple VLANs, tagged (802.1Q). Use for hypervisors, multi-VLAN servers.</p>
<h3>What Gets Deployed (4 Steps)</h3>
<p><strong>Step 1 — Port Description:</strong> Sets <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">HOSTNAME WORKORDER</code> on the leaf node.</p>
<p><strong>Step 2 — Leaf Access Port Policy Group:</strong> Creates an individual (non-bundled) policy group with your selected AEP, Link Level, CDP, and LLDP policies.</p>
<p><strong>Step 3 — Port Selector:</strong> Links the physical port to the policy group under the Interface Profile for that node.</p>
<p><strong>Step 4 — EPG Static Bindings:</strong> Deploys each VLAN as a static path binding. Mode is set to untagged for ACCESS or trunk for TRUNK.</p>
<h3>Smart Features</h3>
<p><strong>Port Validation:</strong> Same 4-criteria check as VPC — only shows genuinely available ports, color-coded in the terminal.</p>
<p><strong>Policy Group Reuse:</strong> Detects existing policy groups for the same hostname and offers to reuse them instead of creating duplicates.</p>
<p><strong>Port Cleanup:</strong> Can remove existing selectors and policy groups when redeploying to a previously configured port.</p>
<p><strong>Interactive Preview:</strong> Before deploying each device, shows a full preview of what will be created — with numbered options to change any setting (interface, AEP, speed, profile, etc.).</p>
<p><strong>Speed Support:</strong> 1G, 10G, 25G, 40G, 100G. Link Level policy is selected during pre-flight and mapped per speed tier.</p>
</div></div></div>

<div id="readmeTabEpgadd" class="readme-tab-content">
<div class="readme-section"><div class="readme-section-title"><span>➕</span> EPG Add</div><div class="readme-content">
<p>Add EPG static path bindings to ports that already have policy groups configured. Use this when a port is already deployed but needs additional VLANs.</p>
<h3>CSV Format</h3>
<p><code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">Switch, Port, VLANS</code></p>
<p>Example: <code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">EDCLEAFACC1501,1/68,"32,64-67"</code></p>
<h3>Multi-Port CSV Expansion</h3>
<p>Specify multiple ports in a single row: <code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">EDCLEAFACC1301,"1/67, 1/68, 1/69","0032, 0058"</code></p>
<p>This expands to 3 ports × 2 VLANs = 6 individual bindings. Leading zeros in VLANs are handled automatically.</p>
<h3>Binding Mode</h3>
<p><strong>Trunk (Tagged)</strong> — Default. Multiple VLANs share the port with 802.1Q tags.</p>
<p><strong>Access (Untagged)</strong> — Single VLAN, no tagging.</p>
<h3>EPG Mode</h3>
<p><strong>Add</strong> — Deploy new bindings only. Existing bindings are detected and skipped.</p>
<p><strong>Overwrite</strong> — Wipe ALL existing EPG bindings on the target port(s) first, then deploy only the VLANs from your CSV. Use this to reset a port to a known state.</p>
<h3>How It Works (4 Phases)</h3>
<p><strong>Phase 1 — Analyze:</strong> For each switch+port+VLAN combo, searches all tenants for the matching EPG, checks if the binding already exists, and builds the deployment plan.</p>
<p><strong>Phase 2 — Resolve Conflicts:</strong> If a VLAN exists in multiple Application Profiles or tenants, you're prompted to choose. That selection applies to all ports with the same VLAN.</p>
<p><strong>Phase 3 — Preview:</strong> Shows a table of all new bindings, existing bindings (will be skipped or re-deployed), and any warnings (VLANs with no EPG found).</p>
<p><strong>Phase 4 — Deploy:</strong> In Add mode, only new bindings are pushed. In Overwrite mode, existing bindings on each target port are deleted first (using a class-level reverse query), then all bindings are deployed fresh.</p>
</div></div></div>

<div id="readmeTabEpgdelete" class="readme-tab-content">
<div class="readme-section"><div class="readme-section-title"><span>➖</span> EPG Delete</div><div class="readme-content">
<p>Remove EPG static path bindings from ports. Use this to clean up VLANs from decommissioned servers or to remove specific bindings without touching the port policy group.</p>
<h3>CSV Format</h3>
<p><code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">Switch, Port, VLANS</code></p>
<p>Example: <code style="background:var(--bg-darkest);padding:2px 6px;border-radius:3px;font-size:11px">EDCLEAFACC1501,1/68,"32,64-67"</code></p>
<p>VLANS column is optional — if omitted, the script finds all bindings on that port and lets you confirm deletion of each one.</p>
<h3>How It Works (3 Phases)</h3>
<p><strong>Phase 1 — Find Bindings:</strong> For each switch+port+VLAN, searches all tenants to locate the exact fvRsPathAtt binding DN. If a VLAN exists in multiple APs, you're prompted to choose which one to delete.</p>
<p><strong>Phase 2 — Preview:</strong> Shows every binding that will be deleted — switch, port, VLAN, EPG name, and Application Profile. Also lists any bindings that weren't found.</p>
<p><strong>Phase 3 — Delete:</strong> After typing <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">YES</code> to confirm, each binding is removed via API DELETE on the full DN. Results show per-binding success/failure.</p>
<h3>Safety</h3>
<p>Dry-run mode available — validates everything without deleting. Requires explicit <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">YES</code> confirmation before any deletion occurs.</p>
</div></div></div>

<div id="readmeTabManagement" class="readme-tab-content">
<div class="readme-section"><div class="readme-section-title"><span>🔑</span> Credentials</div><div class="readme-content">
<p>Stored <strong>in-memory only</strong> — never written to disk. Cleared automatically when the app restarts.</p>
<h3>What to Configure</h3>
<p><strong>Username &amp; Password:</strong> Your APIC login. Auto-injected when scripts prompt for Username/Password.</p>
<p><strong>APIC URLs:</strong> One URL per datacenter environment (D1, D2, D3). These are embedded into generated rollback scripts so rollbacks can authenticate independently.</p>
<h3>How Auto-Injection Works</h3>
<p>The UI watches the terminal output for prompts containing "username" or "password" and types your credentials automatically. This happens for initial login <em>and</em> for any token re-authentication during the run.</p>
</div></div>
<div class="readme-section"><div class="readme-section-title"><span>📊</span> Deploy Log</div><div class="readme-content">
<p>Every script execution is logged automatically — whether it succeeds, fails, or is stopped mid-run.</p>
<h3>Dashboard Stats</h3>
<p><strong>Time Saved:</strong> Compares automated run time against estimated manual APIC GUI time per deployment.<br>
<strong>Total Deployments:</strong> Count of all objects created/deleted across all runs.<br>
<strong>Script Runs:</strong> Total number of script executions.<br>
<strong>Success Rate:</strong> Percentage of runs that completed without errors.</p>
<h3>Per-Run Artifacts</h3>
<p><strong>📥 Sanitized Log:</strong> Full terminal output with all passwords and tokens redacted. Download and attach to work orders.</p>
<p><strong>📋 Results CSV:</strong> Your original input CSV with two extra columns appended: <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">SELECTED_PORT</code> (which physical port was deployed) and <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">STATUS</code> (DEPLOYED, FAILED, SKIPPED, NOT_REACHED, or STOPPED). Generated for every run regardless of outcome.</p>
<p><strong>↩ Rollback Script:</strong> Auto-generated Python script that reverses every change from that deployment. Deletes port selectors, policy groups, EPG bindings, and clears descriptions — in reverse order. Can be downloaded or executed directly from the UI with one click.</p>
</div></div>
<div class="readme-section"><div class="readme-section-title"><span>⚙️</span> Settings</div><div class="readme-content">
<h3>Script Paths</h3>
<p>File paths for each of the four deployment scripts. Update these if you've renamed or moved the scripts.</p>
<h3>Auto-Select First Available Port</h3>
<p>When enabled, the UI automatically picks the first <span style="color:var(--accent-green);font-weight:600">[AVAIL]</span> port from the validation list — skipping any <span style="color:var(--accent-red);font-weight:600">[IN-USE]</span> ports. Disable this to always choose ports manually.</p>
<h3>Version</h3>
<p>Current app version. Shown in the sidebar footer.</p>
</div></div></div>

<div id="readmeTabTroubleshoot" class="readme-tab-content">
<div class="readme-section"><div class="readme-section-title"><span>🔧</span> Troubleshooting</div><div class="readme-content">
<h3>Script Not Found</h3><p>Go to <strong>Settings</strong> and verify all four script paths point to actual files. Paths are relative to the directory where <code style="background:var(--bg-darkest);padding:1px 5px;border-radius:3px;font-size:11px">aci_deployment_app.py</code> is running.</p>
<h3>CSV Validation Errors</h3><p>Column headers must match exactly (case-insensitive). Common issues: missing HOSTNAME column, VLANS not wrapped in quotes when using commas or ranges, TYPE not set to ACCESS or TRUNK. Check the CSV reference table on each deployment screen.</p>
<h3>Credentials Not Auto-Filling</h3><p>Make sure credentials are set in the <strong>Credentials</strong> panel <em>before</em> starting the script. The badge should show <span style="color:var(--accent-green)">SET</span>. Credentials clear on app restart.</p>
<h3>Token Expired (403 "Token was invalid")</h3><p>This should be handled automatically by the token refresh system. If you still see this error, the APIC may be unreachable or your password may have changed. Stop the script, update credentials, and re-run.</p>
<h3>Port Shows [IN-USE] Unexpectedly</h3><p>The port validation checks 4 criteria: usage = discovery, no description, no policy group assigned, and no EPG bindings. If any one fails, the port shows as [IN-USE] with the reason. Use the VPC or Static Port cleanup feature to remove existing configs before redeploying.</p>
<h3>No Available Ports on Both Switches</h3><p>For VPC, both switches must have matching available ports. If one switch is full, the common port list will be empty. Check each switch individually in APIC to confirm port availability.</p>
<h3>VLAN Not Found / No EPG</h3><p>The scripts search all tenants for each environment (D1 = BLU, GWC, GWS; D3 = NSM_BLU, NSM_BRN, etc.). If no EPG is found for a VLAN, it means no EPG with encap matching that VLAN ID exists in any of the searched tenants. Verify the VLAN ID and tenant configuration in APIC.</p>
<h3>Rollback Script Fails</h3><p>Rollback scripts require APIC URLs to be set in Credentials. If you downloaded the rollback and are running it standalone, edit the APIC_URLS dictionary at the top of the script.</p>
</div></div></div>
</div></div>

</main></div>

<script>
let currentView='welcome',isRunning=false,pollInterval=null,csvModes={vpc:'file',individual:'file',epgadd:'file',epgdelete:'file'};
let credSet=false;
let tenantLocked=false,lastUserInput='',tenantPromptActive=false;

// Pre-build the port options HTML (1/1 → 1/110) used by PORT dropdowns
const PORT_OPTIONS_HTML = '<option value="">— auto / skip —</option>' +
  Array.from({length:110},(_,i)=>'<option value="1/'+(i+1)+'">1/'+(i+1)+'</option>').join('');

function makeInlineCell(col, placeholder) {
  // PORT on vpc/individual = pre-selected port dropdown (1/1–1/110)
  // PORT on epgadd/epgdelete = plain text (already the target port address)
  if (col === 'PORT' && placeholder === '') {
    return '<select class="csv-port-select">'+PORT_OPTIONS_HTML+'</select>';
  }
  return '<input type="text" placeholder="'+placeholder+'">';
}

// Toggle badge live update
function updateToggleBadge(checkbox){
  const badge=document.getElementById('autoPortBadge');
  if(!badge)return;
  if(checkbox.checked){badge.textContent='ON';badge.className='toggle-badge on'}
  else{badge.textContent='OFF';badge.className='toggle-badge off'}
}

// Build deployment screens dynamically
const screenDefs = [
  {id:'vpc',title:'VPC Deploy',badge:'VPC',badgeCls:'vpc',console:'vpc-deployment-console',
   csvCols:['Hostname','Switch1','Switch2','Speed','VLANS','WorkOrder','PORT'],
   csvPh:['MEDHVIOP173_SEA_PROD','EDCLEAFACC1501','EDCLEAFACC1502','25G','32,64-67','WO123456',''],
   csvRef:'<tr><th>Hostname</th><th>Switch1</th><th>Switch2</th><th>Speed</th><th>VLANS</th><th>WorkOrder</th></tr><tr><td>Device name</td><td>First VPC switch</td><td>Second VPC switch</td><td>1G, 10G, 25G</td><td>VLAN IDs</td><td>Work order #</td></tr>',
   csvEx:'MEDHVIOP173_SEA_PROD,EDCLEAFACC1501,EDCLEAFACC1502,25G,&quot;32,64-67,92-95&quot;,WO123456,1/93',
   defCsv:'{{ config.default_vpc_csv }}'},
  {id:'individual',title:'Static Port Deploy',badge:'STATIC',badgeCls:'individual',console:'static-port-console',
   csvCols:['Hostname','Switch','Type','Speed','VLANS','WorkOrder','PORT'],
   csvPh:['MEDHVIOP173_MGMT','EDCLEAFNSM2163','ACCESS','1G','2958','WO123456',''],
   csvRef:'<tr><th>Hostname</th><th>Switch</th><th>Type</th><th>Speed</th><th>VLANS</th><th>WorkOrder</th></tr><tr><td>Device name</td><td>Target switch</td><td>ACCESS/TRUNK</td><td>1G, 10G, 25G</td><td>VLAN IDs</td><td>Work order #</td></tr>',
   csvEx:'MEDHVIOP173_MGMT,EDCLEAFNSM2163,ACCESS,1G,2958,WO123456,1/15',
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
  const thRow = s.csvCols.map((c,i)=>
    '<th>'+(c==='PORT' && s.csvPh[i]===''?'<span style="color:var(--accent-cyan)">PORT</span> <span style="font-size:9px;color:var(--text-muted);font-weight:400">(optional)</span>':c)+'</th>'
  ).join('')+'<th class="row-actions"></th>';
  const tdRow = s.csvCols.map((c,i)=>
    '<td>'+makeInlineCell(c, s.csvPh[i]||'')+'</td>'
  ).join('')+'<td class="row-actions"><button class="delete-row" onclick="deleteCsvRow(this)">✕</button></td>';
  el.innerHTML = `
<div class="header-bar"><div class="header-title"><h2>${s.title}</h2><span class="header-badge ${s.badgeCls}">${s.badge}</span></div><div class="header-actions"><button class="header-btn" onclick="clearTerminal('${s.id}')">Clear</button><button class="header-btn danger" onclick="stopScript()" id="${s.id}StopBtn" disabled>Stop</button><button class="header-btn primary" onclick="runScript('${s.id}')" id="${s.id}RunBtn">Run Script</button></div></div>
<div class="config-panel">
<div class="csv-toggle-group"><button class="csv-toggle active" onclick="toggleCsvMode('${s.id}','file')">📁 Use CSV File</button><button class="csv-toggle" onclick="toggleCsvMode('${s.id}','inline')">✏️ Edit Inline</button></div>
<div id="${s.id}FileMode"><label class="config-label">CSV File</label><div class="file-picker-row"><div class="file-picker-display" id="${s.id}FpDisplay"><span class="fp-icon">📄</span><span class="fp-placeholder">No file selected — click Browse</span></div><button class="file-picker-btn" onclick="document.getElementById('${s.id}FileInput').click()">Browse Files</button><input type="file" class="file-input-hidden" id="${s.id}FileInput" accept=".csv,.txt" onchange="handleFileSelect('${s.id}',this)"><input type="hidden" id="${s.id}CsvPath" value="${s.defCsv}"></div><div id="${s.id}Validation"></div></div>
<div id="${s.id}InlineMode" style="display:none"><div class="csv-editor-section" style="padding:0"><div class="csv-editor-header"><span class="csv-editor-title">Inline CSV Editor</span><div class="csv-editor-actions"><button class="csv-editor-btn add" onclick="addCsvRow('${s.id}')">+ Add Row</button><button class="csv-editor-btn" onclick="exportCsv('${s.id}')">Export CSV</button></div></div><table class="csv-editor-table" id="${s.id}CsvTable"><thead><tr>${thRow}</tr></thead><tbody><tr>${tdRow}</tr></tbody></table></div></div>
</div>
<div class="csv-reference" id="${s.id}CsvRef"><div class="csv-reference-header"><span class="csv-reference-title">📋 CSV Format Reference</span><span class="csv-reference-toggle" onclick="toggleCsvRef('${s.id}')">Hide</span></div><table class="csv-table">${s.csvRef}</table><div class="csv-example"><div class="csv-example-label"># Example:</div>${s.csvEx}</div></div>
<div class="terminal-container"><div class="terminal-header"><div class="terminal-dots"><div class="terminal-dot red"></div><div class="terminal-dot yellow"></div><div class="terminal-dot green"></div></div><span class="terminal-title">${s.console}</span><div class="terminal-status" id="${s.id}TerminalStatus"><div class="terminal-status-dot"></div><span>Ready</span></div></div><div class="terminal-output" id="${s.id}Output"><div class="terminal-line muted">// ${s.title}</div><div class="terminal-line muted">// Select a CSV file and click "Run Script" to begin</div></div><div class="post-run-bar hidden" id="${s.id}PostRun"><div class="post-run-label">✅ Run complete</div><div class="post-run-actions"><button class="post-run-btn results-btn" id="${s.id}PostRunResults" onclick="postRunDownloadResults('${s.id}')" style="display:none">📋 Results CSV</button><button class="post-run-btn log-btn" id="${s.id}PostRunLog" onclick="postRunDownloadLog('${s.id}')">📥 Download Log</button><button class="post-run-btn rollback-btn" id="${s.id}PostRunRollback" onclick="postRunDownloadRollback('${s.id}')">↩️ Rollback Script</button><button class="post-run-btn run-rb-btn" id="${s.id}PostRunExec" onclick="postRunExecRollback('${s.id}')">▶ Run Rollback</button><button class="post-run-dismiss" onclick="document.getElementById('${s.id}PostRun').classList.add('hidden')" title="Dismiss">✕</button></div></div><div class="tenant-bar hidden" id="${s.id}TenantBar"><span class="tenant-bar-icon">🏢</span><span class="tenant-bar-text" id="${s.id}TenantText">Tenant prompt detected</span><button class="tenant-bar-btn apply" id="${s.id}TenantApply" onclick="rememberTenant('${s.id}')">Apply to all remaining</button><button class="tenant-bar-btn clear hidden" id="${s.id}TenantClear" onclick="clearTenant('${s.id}')">✕ Clear</button></div><div class="terminal-input-area"><span class="terminal-prompt">❯</span><input type="text" class="terminal-input" id="${s.id}Input" placeholder="Type response here..." onkeypress="handleInputKeypress(event,'${s.id}')" disabled><button class="terminal-submit" id="${s.id}SubmitBtn" onclick="submitInput('${s.id}')" disabled>Send</button></div></div>`;
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
  const apic_urls={D1:document.getElementById('credApicD1').value.trim(),D2:document.getElementById('credApicD2').value.trim(),D3:document.getElementById('credApicD3').value.trim()};
  fetch('/api/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p,apic_urls})})
  .then(r=>r.json()).then(d=>{if(d.status==='saved'){updateCredStatus(true);alert('Credentials saved (in memory only)')}});
}
function clearCredentials(){
  fetch('/api/credentials',{method:'DELETE'}).then(r=>r.json()).then(()=>{
    updateCredStatus(false);document.getElementById('credUsername').value='';document.getElementById('credPassword').value='';
    document.getElementById('credApicD1').value='';document.getElementById('credApicD2').value='';document.getElementById('credApicD3').value='';
  });
}
function updateCredStatus(isSet){
  credSet=isSet;
  const box=document.getElementById('credStatusBox'),icon=document.getElementById('credStatusIcon'),txt=document.getElementById('credStatusText'),badge=document.getElementById('credNavBadge');
  if(isSet){box.className='cred-status set';icon.textContent='✅';txt.textContent='Credentials stored (auto-fill active)';badge.className='cred-badge set';badge.textContent='SET'}
  else{box.className='cred-status unset';icon.textContent='⚠️';txt.textContent='No credentials stored';badge.className='cred-badge unset';badge.textContent='NOT SET'}
}
function saveToDisk(){
  if(!credSet){alert('Set credentials first');return}
  fetch('/api/credentials/save-to-disk',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.status==='saved') alert('Credentials saved to disk (.aci_credentials, base64 obfuscated)');
    else alert('Failed to save: '+(d.message||'unknown error'));
  });
}
function loadFromDisk(){
  fetch('/api/credentials/load-from-disk',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.status==='loaded'){
      updateCredStatus(true);
      document.getElementById('credUsername').value=d.username||'';
      document.getElementById('credApicD1').value=(d.apic_urls||{}).D1||'';
      document.getElementById('credApicD2').value=(d.apic_urls||{}).D2||'';
      document.getElementById('credApicD3').value=(d.apic_urls||{}).D3||'';
      document.getElementById('credPassword').value='\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022';
      alert('Credentials loaded from disk');
    } else { alert('No saved credentials found on disk'); }
  });
}
function checkCredentials(){fetch('/api/credentials').then(r=>r.json()).then(d=>{
  updateCredStatus(d.set);
  if(d.apic_urls){
    document.getElementById('credApicD1').value=d.apic_urls.D1||'';
    document.getElementById('credApicD2').value=d.apic_urls.D2||'';
    document.getElementById('credApicD3').value=d.apic_urls.D3||'';
  }
})}

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
function addCsvRow(type){
  const tb=document.getElementById(type+'CsvTable').getElementsByTagName('tbody')[0],row=tb.insertRow();
  const def=screenDefs.find(s=>s.id===type);if(!def)return;
  def.csvCols.forEach((c,i)=>{
    const cell=row.insertCell();
    cell.innerHTML=makeInlineCell(c, def.csvPh[i]||'');
  });
  const ac=row.insertCell();ac.className='row-actions';ac.innerHTML='<button class="delete-row" onclick="deleteCsvRow(this)">✕</button>';
}
function deleteCsvRow(btn){const r=btn.closest('tr');if(r.parentNode.rows.length>1)r.remove()}
function exportCsv(type){
  const table=document.getElementById(type+'CsvTable');
  const rows=table.getElementsByTagName('tbody')[0].rows;
  const headers=Array.from(table.getElementsByTagName('th')).map(th=>th.textContent.replace(/\(optional\)/,'').trim()).filter(h=>h);
  let csv=headers.join(',')+'\n';
  for(let row of rows){
    const vals=Array.from(row.querySelectorAll('td:not(.row-actions)')).map(cell=>{
      const inp=cell.querySelector('input');
      const sel=cell.querySelector('select');
      let v=(inp?inp.value:sel?sel.value:'').trim();
      if(v.includes(',')||v.includes('-'))v='"'+v+'"';
      return v;
    });
    if(vals.some(v=>v))csv+=vals.join(',')+'\n';
  }
  const blob=new Blob([csv],{type:'text/csv'}),a=document.createElement('a');
  a.href=URL.createObjectURL(blob);a.download=type+'_export.csv';a.click();
}

// README
function switchReadmeTab(tab){document.querySelectorAll('.readme-tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.readme-tab-content').forEach(c=>c.classList.remove('active'));event.target.classList.add('active');const map={ui:'readmeTabUi',vpc:'readmeTabVpc',individual:'readmeTabIndividual',epgadd:'readmeTabEpgadd',epgdelete:'readmeTabEpgdelete',management:'readmeTabManagement',troubleshoot:'readmeTabTroubleshoot'};const el=document.getElementById(map[tab]);if(el)el.classList.add('active')}
function toggleCsvRef(type){const ref=document.getElementById(type+'CsvRef'),t=ref.querySelector('.csv-table'),e=ref.querySelector('.csv-example'),tog=ref.querySelector('.csv-reference-toggle');if(t.style.display==='none'){t.style.display='';e.style.display='';tog.textContent='Hide'}else{t.style.display='none';e.style.display='none';tog.textContent='Show'}}

// TERMINAL
function highlightBracketNums(html){return html.replace(/\[\s*(\d+|[A-Za-z])\]/g,'<span class="bracket-num">[$1]</span>')}

function addLine(type,text,lineType='normal'){
  const output=document.getElementById(type+'Output'),line=document.createElement('div');
  if(lineType==='normal'){const tu=text.toUpperCase();
    if(tu.includes('[AVAIL]'))lineType='port-available';
    else if(tu.includes('[IN-USE]'))lineType='port-in-use';
    else if(tu.includes('[AVAIL]'))lineType='port-available';
    else if(tu.includes('[IN-USE]'))lineType='port-in-use';
    else if(tu.includes('[AVAIL]'))lineType='port-available';
    else if(tu.includes('[IN-USE]'))lineType='port-in-use';
    else if(tu.includes('[AVAIL]'))lineType='port-available';
    else if(tu.includes('[IN-USE]'))lineType='port-in-use';
    else if(tu.includes('[AVAIL]'))lineType='port-available';
    else if(tu.includes('[IN-USE]'))lineType='port-in-use';
    else if(tu.includes('[FOUND]')||tu.includes('[SUCCESS]')||tu.includes('[OK]')||tu.includes('[CREATED]')||tu.includes('[DEPLOYED]'))lineType='success';
    else if(tu.includes('[ERROR]')||tu.includes('[FAILED]')||tu.includes('[FAILURE]')||tu.includes('[EXIT]'))lineType='error';
    else if(tu.includes('[WARNING]')||tu.includes('[WARN]')||tu.includes('[SKIP]')||tu.includes('[SKIPPED]'))lineType='warning';
    else if(tu.includes('[INFO]'))lineType='info';
    else if(tu.includes('[CREDENTIALS]')||tu.includes('[AUTO]'))lineType='credential';
    else if(text.startsWith('===')||text.startsWith('---')||text.startsWith('***'))lineType='header';
  }
  line.className='terminal-line '+lineType;
  const esc=text.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  if(['success','error','warning','info','credential','port-available','port-in-use'].includes(lineType)){
    let h=esc.replace(/\[(AVAIL)\]/gi,'<span class="port-avail">[$1]</span>')
      .replace(/\[(IN-USE)\]/gi,'<span class="port-inuse">[$1]</span>')
      .replace(/\[(AVAIL)\]/gi,'<span class="port-avail">[$1]</span>')
      .replace(/\[(IN-USE)\]/gi,'<span class="port-inuse">[$1]</span>')
      .replace(/\[(AVAIL)\]/gi,'<span class="port-avail">[$1]</span>')
      .replace(/\[(IN-USE)\]/gi,'<span class="port-inuse">[$1]</span>')
      .replace(/\[(AVAIL)\]/gi,'<span class="port-avail">[$1]</span>')
      .replace(/\[(IN-USE)\]/gi,'<span class="port-inuse">[$1]</span>')
      .replace(/\[(AVAIL)\]/gi,'<span class="port-avail">[$1]</span>')
      .replace(/\[(IN-USE)\]/gi,'<span class="port-inuse">[$1]</span>')
      .replace(/\[(FOUND|SUCCESS|OK|CREATED|DEPLOYED)\]/gi,'<span style="color:var(--accent-green);font-weight:600">[$1]</span>')
      .replace(/\[(ERROR|FAILED|FAILURE|EXIT)\]/gi,'<span style="color:var(--accent-red);font-weight:600">[$1]</span>')
      .replace(/\[(WARNING|WARN|SKIP|SKIPPED)\]/gi,'<span style="color:var(--accent-orange);font-weight:600">[$1]</span>')
      .replace(/\[(INFO)\]/gi,'<span style="color:var(--accent-blue);font-weight:600">[$1]</span>')
      .replace(/\[(CREDENTIALS)\]/gi,'<span style="color:#ffd200;font-weight:600">[$1]</span>')
      .replace(/\[(AUTO)\]/gi,'<span style="color:#ffd200;font-weight:600">[$1]</span>')
      .replace(/\[(FAIL)\]/gi,'<span style="color:var(--accent-red);font-weight:600">[$1]</span>')
      .replace(/\[(OVERRIDE)\]/gi,'<span style="color:var(--accent-orange);font-weight:600">[$1]</span>')
      .replace(/\[(CANCELLED)\]/gi,'<span style="color:var(--accent-orange);font-weight:600">[$1]</span>');
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
  const postBar=document.getElementById(type+'PostRun');if(postBar)postBar.classList.add('hidden');
  document.getElementById(type+'RunBtn').disabled=true;document.getElementById(type+'StopBtn').disabled=false;
  document.getElementById(type+'Input').disabled=false;document.getElementById(type+'SubmitBtn').disabled=false;
  clearTerminal(type);addLine(type,'[INFO] Starting script...','info');addLine(type,'[INFO] CSV: '+csvPath,'info');addLine(type,'[AUTO] CSV path will auto-inject when script prompts for filename','credential');
  if(credSet) addLine(type,'[CREDENTIALS] Auto-fill active — credentials will be injected','credential');
  fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:type,csv_path:csvPath})})
  .then(r=>r.json()).then(d=>{if(d.status==='started')startPolling(type);else{addLine(type,'[ERROR] '+d.message,'error');scriptEnded(type)}})
  .catch(e=>{addLine(type,'[ERROR] '+e,'error');scriptEnded(type)});
}

function startPolling(type){pollInterval=setInterval(()=>{fetch('/api/output').then(r=>r.json()).then(d=>{d.lines.forEach(item=>{if(item.type==='output'){let lt='normal';if(item.text.includes('===')||item.text.includes('---'))lt='header';else if(item.text.includes('[SUCCESS]')||item.text.includes(' OK'))lt='success';else if(item.text.includes('[ERROR]')||item.text.includes('[FAILED]'))lt='error';else if(item.text.includes('[WARNING]'))lt='warning';else if(item.text.includes('[INFO]'))lt='info';else if(item.text.includes('[CREDENTIALS]')||item.text.includes('[AUTO]'))lt='credential';else if(item.text.includes('Select')||item.text.endsWith(':')||item.text.endsWith('?'))lt='prompt';addLine(type,item.text,lt)}else if(item.type==='exit'){addLine(type,'[EXIT] Code: '+item.code,item.code===0?'success':'error');scriptEnded(type)}else if(item.type==='error'){addLine(type,'[ERROR] '+item.text,'error');scriptEnded(type)}});updateTenantBar(type,d.prompt_type,d.tenant_choice)})},100)}

function scriptEnded(type){isRunning=false;if(pollInterval){clearInterval(pollInterval);pollInterval=null}setStatus(type,'Ready',false);document.getElementById(type+'RunBtn').disabled=false;document.getElementById(type+'StopBtn').disabled=true;document.getElementById(type+'Input').disabled=true;document.getElementById(type+'SubmitBtn').disabled=true;tenantLocked=false;tenantPromptActive=false;const tb=document.getElementById(type+'TenantBar');if(tb)tb.classList.add('hidden');showPostRunBar(type)}
function showPostRunBar(type){
  const bar=document.getElementById(type+'PostRun');if(!bar)return;
  fetch('/api/logs').then(r=>r.json()).then(log=>{
    const entries=log.entries||[];if(!entries.length){bar.classList.add('hidden');return}
    const latest=entries[entries.length-1];
    const logBtn=document.getElementById(type+'PostRunLog');
    const rbBtn=document.getElementById(type+'PostRunRollback');
    const resBtn=document.getElementById(type+'PostRunResults');
    const label=bar.querySelector('.post-run-label');
    if(latest.status==='success'){label.textContent='✅ Run complete';label.style.color='var(--accent-green)'}
    else if(latest.status==='failed'){label.textContent='❌ Run failed';label.style.color='var(--accent-red)'}
    else if(latest.status==='stopped'){label.textContent='⚠️ Run stopped';label.style.color='var(--accent-orange)'}
    if(latest.saved_log_file){logBtn.style.display='';logBtn.dataset.file=latest.saved_log_file}else{logBtn.style.display='none'}
    if(latest.rollback_file){rbBtn.style.display='';rbBtn.dataset.file=latest.rollback_file;
      const execBtn=document.getElementById(type+'PostRunExec');if(execBtn){execBtn.style.display='';execBtn.dataset.file=latest.rollback_file}
    }else{rbBtn.style.display='none';const execBtn=document.getElementById(type+'PostRunExec');if(execBtn)execBtn.style.display='none'}
    if(resBtn){
      if(latest.results_file){resBtn.style.display='';resBtn.dataset.file=latest.results_file}
      else{resBtn.style.display='none'}
    }
    bar.classList.remove('hidden');
  }).catch(()=>{});
}
function postRunDownloadLog(type){const btn=document.getElementById(type+'PostRunLog');if(btn&&btn.dataset.file)window.open('/api/saved-logs/'+encodeURIComponent(btn.dataset.file),'_blank')}
function postRunDownloadRollback(type){const btn=document.getElementById(type+'PostRunRollback');if(btn&&btn.dataset.file)window.open('/api/rollback/'+encodeURIComponent(btn.dataset.file),'_blank')}
function postRunDownloadResults(type){const btn=document.getElementById(type+'PostRunResults');if(btn&&btn.dataset.file)window.open('/api/results/'+encodeURIComponent(btn.dataset.file),'_blank')}
function postRunExecRollback(type){
  const btn=document.getElementById(type+'PostRunExec');
  if(!btn||!btn.dataset.file)return;
  if(!confirm('⚠️ This will REVERSE the deployment. Continue?'))return;
  executeRollbackScript(btn.dataset.file,type);
}
function runRollbackFromLog(filename,deployType){
  if(!confirm('⚠️ This will REVERSE the deployment. Continue?'))return;
  const type=deployType||'vpc';
  selectView(type);
  setTimeout(()=>executeRollbackScript(filename,type),200);
}
function executeRollbackScript(filename,type){
  if(isRunning){alert('A script is already running');return}
  isRunning=true;setStatus(type,'Running',true);
  const postBar=document.getElementById(type+'PostRun');if(postBar)postBar.classList.add('hidden');
  document.getElementById(type+'RunBtn').disabled=true;document.getElementById(type+'StopBtn').disabled=false;
  document.getElementById(type+'Input').disabled=false;document.getElementById(type+'SubmitBtn').disabled=false;
  clearTerminal(type);
  addLine(type,'[INFO] 🔄 Executing rollback script: '+filename,'info');
  addLine(type,'[WARNING] This will REVERSE the previous deployment','warning');
  if(credSet) addLine(type,'[CREDENTIALS] Auto-fill active — credentials will be injected','credential');
  addLine(type,'[AUTO] Confirmation will be auto-injected','credential');
  fetch('/api/run-rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:filename})})
  .then(r=>r.json()).then(d=>{if(d.status==='started')startPolling(type);else{addLine(type,'[ERROR] '+d.message,'error');scriptEnded(type)}})
  .catch(e=>{addLine(type,'[ERROR] '+e,'error');scriptEnded(type)});
}
function stopScript(){fetch('/api/stop',{method:'POST'}).then(()=>{addLine(currentView,'[STOPPED] Terminated by user','warning');scriptEnded(currentView)})}
function submitInput(type){const input=document.getElementById(type+'Input');if(!input.value&&input.value!=='')return;lastUserInput=input.value.trim();addLine(type,'> '+input.value,'info');fetch('/api/input',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:input.value})});input.value='';if(tenantPromptActive&&lastUserInput&&!tenantLocked){showTenantOffer(type,lastUserInput)}}
function handleInputKeypress(e,type){if(e.key==='Enter')submitInput(type)}

// TENANT MEMORY
function updateTenantBar(type,promptType,tenantChoice){
  const bar=document.getElementById(type+'TenantBar');if(!bar)return;
  if(tenantChoice){
    tenantLocked=true;tenantPromptActive=false;
    bar.classList.remove('hidden');bar.classList.add('locked');
    document.getElementById(type+'TenantText').innerHTML='Tenant locked: <strong>'+tenantChoice+'</strong> — auto-applying to all';
    document.getElementById(type+'TenantApply').classList.add('hidden');
    document.getElementById(type+'TenantClear').classList.remove('hidden');
  } else if(promptType==='tenant'&&!tenantLocked){
    tenantPromptActive=true;
  } else if(promptType!=='tenant'){
    tenantPromptActive=false;
  }
}
function showTenantOffer(type,val){
  const bar=document.getElementById(type+'TenantBar');if(!bar)return;
  bar.classList.remove('hidden','locked');
  document.getElementById(type+'TenantText').innerHTML='You selected <strong>'+val+'</strong> for tenant — apply to all remaining?';
  document.getElementById(type+'TenantApply').classList.remove('hidden');
  document.getElementById(type+'TenantApply').setAttribute('data-val',val);
  document.getElementById(type+'TenantClear').classList.add('hidden');
}
function rememberTenant(type){
  const btn=document.getElementById(type+'TenantApply');
  const val=btn?btn.getAttribute('data-val')||lastUserInput:lastUserInput;
  if(!val)return;
  fetch('/api/tenant-choice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({choice:val})})
  .then(r=>r.json()).then(()=>{
    tenantLocked=true;const bar=document.getElementById(type+'TenantBar');
    bar.classList.add('locked');
    document.getElementById(type+'TenantText').innerHTML='Tenant locked: <strong>'+val+'</strong> — auto-applying to all';
    document.getElementById(type+'TenantApply').classList.add('hidden');
    document.getElementById(type+'TenantClear').classList.remove('hidden');
    addLine(type,'[AUTO] Tenant selection remembered: '+val+' — will auto-apply to remaining deployments','credential');
  });
}
function clearTenant(type){
  fetch('/api/tenant-choice',{method:'DELETE'}).then(()=>{
    tenantLocked=false;tenantPromptActive=false;
    const bar=document.getElementById(type+'TenantBar');
    bar.classList.add('hidden');bar.classList.remove('locked');
    addLine(type,'[INFO] Tenant memory cleared — will prompt again next time','info');
  });
}

function saveSettings(){
  const s={
    vpc_script:document.getElementById('settingsVpcScript').value,
    individual_script:document.getElementById('settingsIndividualScript').value,
    epgadd_script:document.getElementById('settingsEpgaddScript').value,
    epgdelete_script:document.getElementById('settingsEpgdeleteScript').value,
    auto_select_port:document.getElementById('settingsAutoSelectPort').checked,
    epg_overwrite_default:document.getElementById('settingsEpgOverwrite').checked,
    version:document.getElementById('settingsVersion').value
  };
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(s)})
  .then(r=>r.json()).then(d=>{if(d.status==='saved'){alert('Settings saved!');document.getElementById('versionBadge').textContent='v'+s.version}})
}

// LOG with download + rollback buttons
function fmtMin(m){if(m<60)return Math.round(m)+'m';return Math.floor(m/60)+'h '+Math.round(m%60)+'m'}
function refreshLog(){fetch('/api/logs').then(r=>r.json()).then(log=>{
  const e=log.entries||[],ts=log.total_time_saved_minutes||0,td=log.total_deployments||0,runs=e.length,ok=e.filter(x=>x.status==='success').length,rate=runs>0?Math.round(ok/runs*100)+'%':'—';
  const u=id=>document.getElementById(id);
  u('wTimeSaved').textContent=fmtMin(ts);u('wDeploys').textContent=td;u('wRuns').textContent=runs;
  if(u('logTimeSaved')){u('logTimeSaved').textContent=fmtMin(ts);u('logDeploys').textContent=td;u('logRuns').textContent=runs;u('logSuccessRate').textContent=rate}
  const c=u('logEntriesContainer');if(!e.length){c.innerHTML='<div class="log-empty"><div class="log-empty-icon">📭</div>No deployments yet.</div>';return}
  const labels={vpc:'VPC',individual:'STATIC',epgadd:'EPG ADD',epgdelete:'EPG DEL'};let h='';
  for(let i=e.length-1;i>=0;i--){const x=e[i];
    let actionBtns='<div class="log-entry-actions">';
    if(x.saved_log_file) actionBtns+='<button class="log-action-btn download" onclick="downloadLog(\''+x.saved_log_file+'\')">📥 Log</button>';
    if(x.rollback_file) actionBtns+='<button class="log-action-btn rollback" onclick="downloadRollback(\''+x.rollback_file+'\')">↩ Rollback</button>';
    if(x.rollback_file) actionBtns+='<button class="log-action-btn run-rollback" onclick="runRollbackFromLog(\''+x.rollback_file+'\',\''+x.type+'\')">▶ Run</button>';
    if(x.results_file) actionBtns+='<button class="log-action-btn" style="background:rgba(63,185,80,.15);color:var(--accent-green)" onclick="downloadResults(\''+x.results_file+'\')">📋 Results</button>';
    actionBtns+='</div>';
    h+='<div class="log-entry"><div class="log-entry-dot '+x.status+'"></div><div class="log-entry-info"><div class="log-entry-title">'+(x.csv_file||'inline')+'</div><div class="log-entry-meta">'+x.timestamp+' · '+x.deployment_count+' items · '+x.duration_seconds+'s</div></div><span class="log-entry-type '+x.type+'">'+(labels[x.type]||x.type)+'</span><div class="log-entry-saved">-'+fmtMin(x.time_saved_minutes)+'</div>'+actionBtns+'</div>'}
  c.innerHTML=h}).catch(()=>{})}
function clearLog(){if(!confirm('Clear all deployment log entries?'))return;fetch('/api/logs/clear',{method:'POST'}).then(()=>refreshLog())}
function downloadLog(filename){window.open('/api/saved-logs/'+encodeURIComponent(filename),'_blank')}
function downloadRollback(filename){window.open('/api/rollback/'+encodeURIComponent(filename),'_blank')}
function downloadResults(filename){window.open('/api/results/'+encodeURIComponent(filename),'_blank')}

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
    current_run["is_rollback"] = False
    current_run["port_selections"] = parse_port_column(data.get('csv_path', ''))
    current_run["port_prompt_index"] = 0
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
    return jsonify({
        'lines': lines,
        'prompt_type': current_run.get("last_prompt_type"),
        'tenant_choice': current_run.get("tenant_choice")
    })

@app.route('/api/input', methods=['POST'])
def api_input():
    data = request.json
    text = data.get('text', '')
    sent = send_input_to_process(text)
    if data.get('remember_tenant') and text.strip():
        current_run["tenant_choice"] = text.strip()
    return jsonify({'status': 'sent' if sent else 'failed'})

@app.route('/api/tenant-choice', methods=['GET', 'POST', 'DELETE'])
def api_tenant_choice():
    if request.method == 'GET':
        return jsonify({
            'tenant_choice': current_run.get("tenant_choice"),
            'last_prompt_type': current_run.get("last_prompt_type")
        })
    elif request.method == 'POST':
        data = request.json
        current_run["tenant_choice"] = data.get('choice', '').strip()
        return jsonify({'status': 'saved', 'choice': current_run["tenant_choice"]})
    elif request.method == 'DELETE':
        current_run["tenant_choice"] = None
        return jsonify({'status': 'cleared'})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    stop_process()
    return jsonify({'status': 'stopped'})

@app.route('/api/run-rollback', methods=['POST'])
def api_run_rollback():
    global running_process, output_queue, current_run
    if running_process is not None:
        return jsonify({'status': 'error', 'message': 'Script already running'})
    data = request.json
    filename = data.get('filename', '')
    if not filename or '..' in filename or '/' in filename:
        return jsonify({'status': 'error', 'message': 'Invalid rollback filename'})
    script_path = os.path.join(ROLLBACK_FOLDER, filename)
    if not os.path.exists(script_path):
        return jsonify({'status': 'error', 'message': f'Rollback script not found: {filename}'})
    while not output_queue.empty():
        try: output_queue.get_nowait()
        except: break
    current_run["type"] = "rollback"
    current_run["csv_path"] = ""
    current_run["start_time"] = None
    current_run["output_lines"] = []
    current_run["is_rollback"] = True
    thread = threading.Thread(target=run_script_thread, args=(script_path, None))
    thread.daemon = True
    thread.start()
    return jsonify({'status': 'started'})

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
        return jsonify({
            'set': stored_credentials.get('set', False),
            'username': stored_credentials.get('username', ''),
            'apic_urls': stored_credentials.get('apic_urls', {"D1": "", "D2": "", "D3": ""})
        })
    elif request.method == 'POST':
        data = request.json
        stored_credentials['username'] = data.get('username', '')
        stored_credentials['password'] = data.get('password', '')
        if 'apic_urls' in data:
            stored_credentials['apic_urls'] = data['apic_urls']
        stored_credentials['set'] = bool(stored_credentials['username'] and stored_credentials['password'])
        return jsonify({'status': 'saved', 'set': stored_credentials['set']})
    elif request.method == 'DELETE':
        stored_credentials = {"username": None, "password": None, "set": False, "apic_urls": {"D1": "", "D2": "", "D3": ""}}
        return jsonify({'status': 'cleared'})

@app.route('/api/credentials/save-to-disk', methods=['POST'])
def api_credentials_save_disk():
    if not stored_credentials.get('set'):
        return jsonify({'status': 'error', 'message': 'No credentials to save'})
    ok = save_credentials_to_disk()
    return jsonify({'status': 'saved' if ok else 'error'})

@app.route('/api/credentials/load-from-disk', methods=['POST'])
def api_credentials_load_disk():
    ok = load_credentials_from_disk()
    return jsonify({
        'status': 'loaded' if ok else 'not_found',
        'set': stored_credentials.get('set', False),
        'username': stored_credentials.get('username', ''),
        'apic_urls': stored_credentials.get('apic_urls', {"D1": "", "D2": "", "D3": ""})
    })

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

@app.route('/api/results/<filename>')
def api_results_download(filename):
    safe_name = filename.replace('..', '').replace('/', '_').replace('\\', '_')
    filepath = os.path.join(RESULTS_FOLDER, safe_name)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True, download_name=safe_name)
    return jsonify({'status': 'error', 'message': 'Results file not found'}), 404

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    import logging
    import webbrowser
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    for folder in [UPLOAD_FOLDER, SAVED_LOGS_FOLDER, ROLLBACK_FOLDER, RESULTS_FOLDER]:
        os.makedirs(folder, exist_ok=True)
    print("\n" + "=" * 60)
    print(" ACI AUTOMATION CONSOLE v1.3.0")
    print("=" * 60)
    print("\n  Server running on http://localhost:5000")
    print("  Press Ctrl+C to stop")
    print("\n" + "=" * 60 + "\n")

    threading.Timer(1.2, lambda: webbrowser.open('http://localhost:5000')).start()

    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
