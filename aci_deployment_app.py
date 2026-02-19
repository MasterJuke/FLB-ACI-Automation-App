#!/usr/bin/env python3
"""
ACI Bulk Deployment Web Application
====================================
Flask-based web UI for running ACI deployment scripts.

Features:
- Sleek Docker/VS Code inspired interface
- Real-time terminal output
- Interactive input handling
- Settings management for script paths
- CSV format reference tables

Requirements:
- Python 3.6+
- Flask (pip install flask)

Usage:
    python aci_deployment_app.py

Then open http://localhost:5000 in your browser.

Author: Network Automation
Version: 1.0.0
"""

import os
import sys
import json
import subprocess
import threading
import queue
import time
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, Response

app = Flask(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG_FILE = "aci_deploy_config.json"

DEFAULT_CONFIG = {
    "vpc_script": "aci_bulk_vpc_deploy.py",
    "individual_script": "aci_bulk_individual_deploy.py",
    "epgadd_script": "aci_bulk_epg_add.py",
    "epgdelete_script": "aci_bulk_epg_delete.py",
    "default_vpc_csv": "vpc_deployments.csv",
    "default_individual_csv": "individual_port_deployments.csv",
    "default_epgadd_csv": "epg_add.csv",
    "default_epgdelete_csv": "epg_delete.csv",
    "version": "1.1.0"
}

# Global state for running processes
running_process = None
output_queue = queue.Queue()
input_queue = queue.Queue()

# =============================================================================
# CONFIG MANAGEMENT
# =============================================================================

def load_config():
    """Load configuration from file or create default."""
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
    """Save configuration to file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

# =============================================================================
# PROCESS MANAGEMENT
# =============================================================================

def run_script_thread(script_path, csv_path):
    """Run a script in a separate thread and capture output."""
    global running_process
    
    try:
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        
        # Use pseudo-terminal on Unix for proper prompt handling
        import platform
        use_pty = platform.system() != 'Windows'
        
        if use_pty:
            try:
                import pty
                import os as _os
                global pty_master_fd
                
                master_fd, slave_fd = pty.openpty()
                pty_master_fd = master_fd  # Store globally for send_input
                
                running_process = subprocess.Popen(
                    [sys.executable, '-u', script_path],
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    env=env,
                    cwd=os.path.dirname(os.path.abspath(script_path)) or '.'
                )
                
                _os.close(slave_fd)
                
                # Read from master
                current_line = ""
                while True:
                    try:
                        data = _os.read(master_fd, 1024)
                        if not data:
                            break
                        text = data.decode('utf-8', errors='replace')
                        
                        for char in text:
                            if char == '\n':
                                output_queue.put(('output', current_line))
                                current_line = ""
                            elif char == '\r':
                                pass
                            else:
                                current_line += char
                        
                        # Flush any partial line that looks like a prompt
                        if current_line and (
                            current_line.rstrip().endswith((':', '?', ')', ']', ' ')) or
                            any(kw in current_line.lower() for kw in 
                                ['select', 'enter', 'username', 'password', 'confirm', 
                                 'choice', 'yes/no', '1/2', 'press'])
                        ):
                            output_queue.put(('output', current_line))
                            current_line = ""
                            
                    except OSError:
                        break
                
                if current_line:
                    output_queue.put(('output', current_line))
                
                pty_master_fd = None  # Clear global ref
                _os.close(master_fd)
                running_process.wait()
                output_queue.put(('exit', running_process.returncode))
                
            except ImportError:
                use_pty = False
        
        if not use_pty:
            # Windows fallback - use threads to read output
            running_process = subprocess.Popen(
                [sys.executable, '-u', script_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                env=env,
                cwd=os.path.dirname(os.path.abspath(script_path)) or '.'
            )
            
            current_line = ""
            last_output_time = time.time()
            
            while True:
                byte = running_process.stdout.read(1)
                if not byte:
                    if current_line:
                        output_queue.put(('output', current_line))
                    break
                
                char = byte.decode('utf-8', errors='replace')
                
                if char == '\n':
                    output_queue.put(('output', current_line))
                    current_line = ""
                    last_output_time = time.time()
                elif char == '\r':
                    pass
                else:
                    current_line += char
                    # Check if this looks like a prompt waiting for input
                    if current_line and (
                        current_line.rstrip().endswith((':', '?', ')', ']')) or
                        any(kw in current_line.lower() for kw in 
                            ['select', 'enter', 'username', 'password', 'confirm', 
                             'choice', 'yes/no', '1/2', 'press'])
                    ):
                        # Wait a tiny bit to see if more data comes
                        time.sleep(0.05)
                        # Flush the prompt
                        output_queue.put(('output', current_line))
                        current_line = ""
            
            running_process.wait()
            output_queue.put(('exit', running_process.returncode))
        
    except Exception as e:
        output_queue.put(('error', str(e)))
    finally:
        running_process = None


# Global variable to store master_fd for pty
pty_master_fd = None

def send_input_to_process(text):
    """Send input to the running process."""
    global running_process, pty_master_fd
    
    # Try pty first
    if pty_master_fd is not None:
        try:
            import os as _os
            _os.write(pty_master_fd, (text + '\n').encode('utf-8'))
            return True
        except:
            pass
    
    # Fall back to stdin
    if running_process and running_process.stdin:
        try:
            data = (text + '\n').encode('utf-8')
            running_process.stdin.write(data)
            running_process.stdin.flush()
            return True
        except:
            pass
    return False

def stop_process():
    """Stop the running process."""
    global running_process
    if running_process:
        try:
            running_process.terminate()
            time.sleep(0.5)
            if running_process.poll() is None:
                running_process.kill()
        except:
            pass
        running_process = None

# =============================================================================
# HTML TEMPLATE
# =============================================================================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ACI Bulk Deployment Console</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
        
        :root {
            --bg-darkest: #0d1117;
            --bg-dark: #161b22;
            --bg-sidebar: #0d1117;
            --bg-terminal: #1e1e2e;
            --bg-input: #252535;
            --border-color: #30363d;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-muted: #6e7681;
            --accent-blue: #58a6ff;
            --accent-cyan: #39d4d4;
            --accent-green: #3fb950;
            --accent-orange: #f0883e;
            --accent-red: #f85149;
            --accent-purple: #a371f7;
            --accent-yellow: #d29922;
            --glow-cyan: rgba(57, 212, 212, 0.15);
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'IBM Plex Sans', -apple-system, sans-serif;
            background: var(--bg-darkest);
            color: var(--text-primary);
            height: 100vh;
            overflow: hidden;
        }

        .app-container { display: flex; height: 100vh; }

        /* Sidebar */
        .sidebar {
            width: 280px;
            background: var(--bg-sidebar);
            border-right: 1px solid var(--border-color);
            display: flex;
            flex-direction: column;
        }

        .sidebar-header {
            padding: 20px;
            border-bottom: 1px solid var(--border-color);
        }

        .logo {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .logo-icon {
            width: 40px;
            height: 40px;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-blue));
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: 'JetBrains Mono', monospace;
            font-weight: 700;
            font-size: 14px;
            color: var(--bg-darkest);
            box-shadow: 0 4px 20px var(--glow-cyan);
        }

        .logo-text {
            font-size: 18px;
            font-weight: 600;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-blue));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .logo-subtitle {
            font-size: 11px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 1.5px;
        }

        .nav-section { padding: 16px 12px; flex: 1; }

        .nav-label {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 1px;
            padding: 0 12px;
            margin-bottom: 8px;
            margin-top: 16px;
        }
        .nav-label:first-child { margin-top: 0; }

        .nav-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px 16px;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
            margin-bottom: 4px;
            border: 1px solid transparent;
        }

        .nav-item:hover {
            background: rgba(88, 166, 255, 0.08);
            border-color: rgba(88, 166, 255, 0.2);
        }

        .nav-item.active {
            background: linear-gradient(135deg, rgba(57, 212, 212, 0.12), rgba(88, 166, 255, 0.12));
            border-color: var(--accent-cyan);
            box-shadow: 0 0 20px var(--glow-cyan);
        }

        .nav-icon {
            width: 36px;
            height: 36px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
        }

        .nav-item.vpc .nav-icon { background: linear-gradient(135deg, var(--accent-purple), var(--accent-blue)); }
        .nav-item.individual .nav-icon { background: linear-gradient(135deg, var(--accent-orange), var(--accent-yellow)); }
        .nav-item.settings .nav-icon { background: linear-gradient(135deg, var(--accent-cyan), var(--accent-green)); }
        .nav-item.readme .nav-icon { background: linear-gradient(135deg, #f093fb, #f5576c); }
        .nav-item.epgadd .nav-icon { background: linear-gradient(135deg, #11998e, #38ef7d); }
        .nav-item.epgdelete .nav-icon { background: linear-gradient(135deg, #eb3349, #f45c43); }

        .nav-item-title { font-weight: 600; font-size: 14px; margin-bottom: 2px; }
        .nav-item-desc { font-size: 11px; color: var(--text-muted); }

        .status-indicator {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--accent-green);
            box-shadow: 0 0 8px var(--accent-green);
            animation: pulse 2s infinite;
        }
        .status-indicator.running {
            background: var(--accent-orange);
            box-shadow: 0 0 8px var(--accent-orange);
            animation: pulse 0.5s infinite;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .sidebar-footer {
            padding: 16px;
            border-top: 1px solid var(--border-color);
        }

        .footer-info {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            color: var(--text-muted);
        }

        .footer-version {
            padding: 2px 8px;
            background: var(--bg-input);
            border-radius: 4px;
            font-family: 'JetBrains Mono', monospace;
        }

        /* Main Content */
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            background: var(--bg-dark);
            overflow: hidden;
        }

        .header-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 20px;
            background: var(--bg-darkest);
            border-bottom: 1px solid var(--border-color);
        }

        .header-title { display: flex; align-items: center; gap: 12px; }
        .header-title h2 { font-size: 16px; font-weight: 600; }

        .header-badge {
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .header-badge.vpc { background: rgba(163, 113, 247, 0.2); color: var(--accent-purple); }
        .header-badge.individual { background: rgba(240, 136, 62, 0.2); color: var(--accent-orange); }
        .header-badge.settings { background: rgba(57, 212, 212, 0.2); color: var(--accent-cyan); }
        .header-badge.readme { background: rgba(245, 87, 108, 0.2); color: #f5576c; }
        .header-badge.epgadd { background: rgba(56, 239, 125, 0.2); color: #38ef7d; }
        .header-badge.epgdelete { background: rgba(244, 92, 67, 0.2); color: #f45c43; }

        .header-actions { display: flex; gap: 8px; }

        .header-btn {
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
            border: 1px solid var(--border-color);
            background: transparent;
            color: var(--text-secondary);
            font-family: inherit;
        }
        .header-btn:hover { border-color: var(--accent-cyan); color: var(--accent-cyan); }
        .header-btn.primary {
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-blue));
            border: none;
            color: var(--bg-darkest);
            font-weight: 600;
        }
        .header-btn.primary:hover { box-shadow: 0 4px 20px var(--glow-cyan); }
        .header-btn.primary:disabled { opacity: 0.5; cursor: not-allowed; }
        .header-btn.danger { border-color: var(--accent-red); color: var(--accent-red); }
        .header-btn.danger:hover { background: rgba(248, 81, 73, 0.1); }
        .header-btn.danger:disabled { opacity: 0.3; cursor: not-allowed; }

        .config-panel {
            padding: 20px;
            background: var(--bg-darkest);
            border-bottom: 1px solid var(--border-color);
        }

        .config-row { display: flex; gap: 16px; align-items: flex-end; }
        .config-group { flex: 1; }

        .config-label {
            display: block;
            font-size: 12px;
            font-weight: 600;
            color: var(--text-secondary);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .config-input {
            width: 100%;
            padding: 12px 16px;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
        }
        .config-input:focus {
            outline: none;
            border-color: var(--accent-cyan);
            box-shadow: 0 0 0 3px var(--glow-cyan);
        }

        /* CSV Reference */
        .csv-reference {
            padding: 16px 20px;
            background: rgba(57, 212, 212, 0.05);
            border-bottom: 1px solid var(--border-color);
        }

        .csv-reference-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }

        .csv-reference-title {
            font-size: 12px;
            font-weight: 600;
            color: var(--accent-cyan);
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .csv-reference-toggle {
            font-size: 11px;
            color: var(--text-muted);
            cursor: pointer;
            padding: 4px 8px;
            border-radius: 4px;
        }
        .csv-reference-toggle:hover { background: var(--bg-input); color: var(--text-primary); }

        .csv-table {
            width: 100%;
            border-collapse: collapse;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
        }
        .csv-table th {
            background: var(--bg-input);
            padding: 10px 12px;
            text-align: left;
            font-weight: 600;
            color: var(--accent-cyan);
            border: 1px solid var(--border-color);
        }
        .csv-table td {
            padding: 8px 12px;
            border: 1px solid var(--border-color);
            color: var(--text-secondary);
        }

        .csv-example {
            margin-top: 12px;
            padding: 12px;
            background: var(--bg-terminal);
            border-radius: 6px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 11px;
            color: var(--text-muted);
        }
        .csv-example-label { color: var(--accent-green); margin-bottom: 4px; }

        /* Terminal */
        .terminal-container {
            flex: 1;
            display: flex;
            flex-direction: column;
            margin: 16px;
            border-radius: 12px;
            overflow: hidden;
            border: 1px solid var(--border-color);
            background: var(--bg-terminal);
            min-height: 0;  /* Critical for nested flex scrolling */
        }

        .terminal-header {
            display: flex;
            align-items: center;
            padding: 12px 16px;
            background: rgba(0, 0, 0, 0.3);
            border-bottom: 1px solid var(--border-color);
        }

        .terminal-dots { display: flex; gap: 8px; margin-right: 16px; }
        .terminal-dot { width: 12px; height: 12px; border-radius: 50%; }
        .terminal-dot.red { background: #ff5f56; }
        .terminal-dot.yellow { background: #ffbd2e; }
        .terminal-dot.green { background: #27ca40; }

        .terminal-title {
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            color: var(--text-muted);
        }

        .terminal-status {
            margin-left: auto;
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 11px;
            color: var(--text-muted);
        }
        .terminal-status-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: var(--accent-green);
        }
        .terminal-status.running .terminal-status-dot {
            background: var(--accent-orange);
            animation: pulse 0.5s infinite;
        }

        .terminal-output {
            flex: 1;
            padding: 16px;
            overflow-y: auto;
            overflow-x: hidden;
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            line-height: 1.6;
            min-height: 0;  /* Important for flex children to scroll */
            max-height: 100%;
        }
        .terminal-output::-webkit-scrollbar { width: 10px; }
        .terminal-output::-webkit-scrollbar-track { background: var(--bg-darkest); border-radius: 5px; }
        .terminal-output::-webkit-scrollbar-thumb { 
            background: var(--border-color); 
            border-radius: 5px;
            border: 2px solid var(--bg-darkest);
        }
        .terminal-output::-webkit-scrollbar-thumb:hover { 
            background: var(--text-muted); 
        }
        /* Firefox scrollbar */
        .terminal-output {
            scrollbar-width: thin;
            scrollbar-color: var(--border-color) var(--bg-darkest);
        }

        .terminal-line { white-space: pre-wrap; word-break: break-all; margin-bottom: 1px; }
        .terminal-line.header { color: var(--accent-cyan); font-weight: 600; }
        .terminal-line.success { color: var(--accent-green); }
        .terminal-line.error { color: var(--accent-red); }
        .terminal-line.warning { color: var(--accent-orange); }
        .terminal-line.info { color: var(--accent-blue); }
        .terminal-line.muted { color: var(--text-muted); }
        .terminal-line.prompt { color: var(--accent-purple); }

        .terminal-input-area {
            display: flex;
            align-items: center;
            padding: 12px 16px;
            background: rgba(0, 0, 0, 0.3);
            border-top: 1px solid var(--border-color);
            gap: 12px;
        }

        .terminal-prompt {
            color: var(--accent-cyan);
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            font-weight: 600;
        }

        .terminal-input {
            flex: 1;
            background: transparent;
            border: none;
            color: var(--text-primary);
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            outline: none;
        }
        .terminal-input:disabled { opacity: 0.5; }

        .terminal-submit {
            padding: 8px 16px;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-blue));
            border: none;
            border-radius: 6px;
            color: var(--bg-darkest);
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }
        .terminal-submit:disabled { opacity: 0.5; cursor: not-allowed; }

        /* Welcome Screen */
        .welcome-screen {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 40px;
            text-align: center;
        }

        .welcome-icon {
            width: 80px;
            height: 80px;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-blue));
            border-radius: 20px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 36px;
            margin-bottom: 24px;
            box-shadow: 0 8px 40px var(--glow-cyan);
        }

        .welcome-title { font-size: 28px; font-weight: 700; margin-bottom: 12px; }
        .welcome-desc { color: var(--text-muted); font-size: 15px; max-width: 500px; line-height: 1.6; margin-bottom: 32px; }

        .welcome-cards { display: flex; gap: 20px; }

        .welcome-card {
            padding: 24px;
            background: var(--bg-darkest);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.3s ease;
            width: 200px;
        }
        .welcome-card:hover {
            border-color: var(--accent-cyan);
            transform: translateY(-4px);
            box-shadow: 0 8px 30px var(--glow-cyan);
        }

        .welcome-card-icon {
            width: 48px;
            height: 48px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
            margin-bottom: 16px;
        }
        .welcome-card.vpc .welcome-card-icon { background: linear-gradient(135deg, var(--accent-purple), var(--accent-blue)); }
        .welcome-card.individual .welcome-card-icon { background: linear-gradient(135deg, var(--accent-orange), var(--accent-yellow)); }

        .welcome-card-title { font-weight: 600; font-size: 15px; margin-bottom: 8px; }
        .welcome-card-desc { font-size: 12px; color: var(--text-muted); line-height: 1.5; }

        /* Settings Panel */
        .settings-panel { padding: 24px; overflow-y: auto; flex: 1; }

        .settings-section {
            background: var(--bg-darkest);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
        }

        .settings-section-title {
            font-size: 14px;
            font-weight: 600;
            color: var(--accent-cyan);
            margin-bottom: 16px;
        }

        .settings-row { margin-bottom: 16px; }
        .settings-row:last-child { margin-bottom: 0; }

        .settings-label {
            display: block;
            font-size: 12px;
            font-weight: 500;
            color: var(--text-secondary);
            margin-bottom: 8px;
        }

        .settings-input {
            width: 100%;
            padding: 12px 16px;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
        }
        .settings-input:focus { outline: none; border-color: var(--accent-cyan); }

        .settings-hint { font-size: 11px; color: var(--text-muted); margin-top: 6px; }

        /* README Panel */
        .readme-panel { padding: 24px; overflow-y: auto; flex: 1; }

        .readme-section {
            background: var(--bg-darkest);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
        }

        .readme-section-title {
            font-size: 18px;
            font-weight: 700;
            color: var(--text-primary);
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .readme-section-title span {
            font-size: 24px;
        }

        .readme-content {
            color: var(--text-secondary);
            line-height: 1.8;
            font-size: 14px;
        }

        .readme-content h3 {
            color: var(--accent-cyan);
            font-size: 15px;
            margin: 20px 0 12px 0;
            font-weight: 600;
        }

        .readme-content h3:first-child {
            margin-top: 0;
        }

        .readme-content p {
            margin-bottom: 12px;
        }

        .readme-content ul {
            margin: 12px 0;
            padding-left: 24px;
        }

        .readme-content li {
            margin-bottom: 8px;
        }

        .readme-content code {
            background: var(--bg-input);
            padding: 2px 8px;
            border-radius: 4px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
            color: var(--accent-cyan);
        }

        .readme-content .step {
            display: flex;
            gap: 16px;
            margin-bottom: 16px;
            padding: 16px;
            background: var(--bg-terminal);
            border-radius: 8px;
            border-left: 3px solid var(--accent-cyan);
        }

        .readme-content .step-number {
            width: 32px;
            height: 32px;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-blue));
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            color: var(--bg-darkest);
            flex-shrink: 0;
        }

        .readme-content .step-content {
            flex: 1;
        }

        .readme-content .step-title {
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 4px;
        }

        .readme-tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 12px;
        }

        .readme-tab {
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 500;
            font-size: 14px;
            transition: all 0.2s;
            background: transparent;
            color: var(--text-muted);
            border: 1px solid transparent;
        }

        .readme-tab:hover {
            color: var(--text-primary);
            background: var(--bg-input);
        }

        .readme-tab.active {
            background: linear-gradient(135deg, rgba(57, 212, 212, 0.15), rgba(88, 166, 255, 0.15));
            color: var(--accent-cyan);
            border-color: var(--accent-cyan);
        }

        .readme-tab-content {
            display: none;
        }

        .readme-tab-content.active {
            display: block;
        }

        /* Inline CSV Editor */
        .csv-editor-section {
            padding: 16px 20px;
            background: var(--bg-darkest);
            border-bottom: 1px solid var(--border-color);
        }

        .csv-editor-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }

        .csv-editor-title {
            font-size: 12px;
            font-weight: 600;
            color: var(--accent-cyan);
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .csv-editor-actions {
            display: flex;
            gap: 8px;
        }

        .csv-editor-btn {
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 500;
            cursor: pointer;
            border: 1px solid var(--border-color);
            background: transparent;
            color: var(--text-secondary);
            font-family: inherit;
            transition: all 0.2s;
        }

        .csv-editor-btn:hover {
            border-color: var(--accent-cyan);
            color: var(--accent-cyan);
        }

        .csv-editor-btn.add {
            border-color: var(--accent-green);
            color: var(--accent-green);
        }

        .csv-editor-btn.add:hover {
            background: rgba(63, 185, 80, 0.1);
        }

        .csv-editor-table {
            width: 100%;
            border-collapse: collapse;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
        }

        .csv-editor-table th {
            background: var(--bg-input);
            padding: 10px 8px;
            text-align: left;
            font-weight: 600;
            color: var(--accent-cyan);
            border: 1px solid var(--border-color);
        }

        .csv-editor-table td {
            padding: 4px;
            border: 1px solid var(--border-color);
        }

        .csv-editor-table input {
            width: 100%;
            padding: 8px;
            background: var(--bg-terminal);
            border: 1px solid transparent;
            border-radius: 4px;
            color: var(--text-primary);
            font-family: 'JetBrains Mono', monospace;
            font-size: 12px;
        }

        .csv-editor-table input:focus {
            outline: none;
            border-color: var(--accent-cyan);
        }

        .csv-editor-table .row-actions {
            width: 40px;
            text-align: center;
        }

        .csv-editor-table .delete-row {
            background: transparent;
            border: none;
            color: var(--accent-red);
            cursor: pointer;
            font-size: 14px;
            padding: 4px 8px;
            border-radius: 4px;
        }

        .csv-editor-table .delete-row:hover {
            background: rgba(248, 81, 73, 0.1);
        }

        .csv-toggle-group {
            display: flex;
            gap: 8px;
            margin-bottom: 12px;
        }

        .csv-toggle {
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            border: 1px solid var(--border-color);
            background: transparent;
            color: var(--text-muted);
            transition: all 0.2s;
        }

        .csv-toggle.active {
            border-color: var(--accent-cyan);
            color: var(--accent-cyan);
            background: rgba(57, 212, 212, 0.1);
        }

        .hidden { display: none !important; }
    </style>
</head>
<body>
    <div class="app-container">
        <!-- Sidebar -->
        <aside class="sidebar">
            <div class="sidebar-header">
                <div class="logo">
                    <div class="logo-icon">ACI</div>
                    <div>
                        <div class="logo-text">ACI Deploy</div>
                        <div class="logo-subtitle">Bulk Deployment Console</div>
                    </div>
                </div>
            </div>

            <nav class="nav-section">
                <div class="nav-label">Bulk Deployments</div>
                
                <div class="nav-item vpc" onclick="selectView('vpc')">
                    <div class="nav-icon">⚡</div>
                    <div class="nav-item-text">
                        <div class="nav-item-title">VPC Bulk</div>
                        <div class="nav-item-desc">Virtual Port Channel deployments</div>
                    </div>
                </div>

                <div class="nav-item individual" onclick="selectView('individual')">
                    <div class="nav-icon">🔌</div>
                    <div class="nav-item-text">
                        <div class="nav-item-title">Port Bulk</div>
                        <div class="nav-item-desc">Individual port deployments</div>
                    </div>
                </div>

                <div class="nav-label">EPG Management</div>

                <div class="nav-item epgadd" onclick="selectView('epgadd')">
                    <div class="nav-icon">➕</div>
                    <div class="nav-item-text">
                        <div class="nav-item-title">EPG Add</div>
                        <div class="nav-item-desc">Add EPGs to existing ports</div>
                    </div>
                </div>

                <div class="nav-item epgdelete" onclick="selectView('epgdelete')">
                    <div class="nav-icon">➖</div>
                    <div class="nav-item-text">
                        <div class="nav-item-title">EPG Delete</div>
                        <div class="nav-item-desc">Remove EPGs from ports</div>
                    </div>
                </div>

                <div class="nav-label">Configuration</div>

                <div class="nav-item settings" onclick="selectView('settings')">
                    <div class="nav-icon">⚙️</div>
                    <div class="nav-item-text">
                        <div class="nav-item-title">Settings</div>
                        <div class="nav-item-desc">Script paths & configuration</div>
                    </div>
                </div>

                <div class="nav-label">Documentation</div>

                <div class="nav-item readme" onclick="selectView('readme')">
                    <div class="nav-icon">📖</div>
                    <div class="nav-item-text">
                        <div class="nav-item-title">README</div>
                        <div class="nav-item-desc">Instructions & help guide</div>
                    </div>
                </div>
            </nav>

            <div class="sidebar-footer">
                <div class="footer-info">
                    <span class="status-indicator" id="globalStatus"></span>
                    <span id="statusText">Ready</span>
                    <span class="footer-version" id="versionBadge">v{{ config.version }}</span>
                </div>
            </div>
        </aside>

        <!-- Main Content -->
        <main class="main-content" id="mainContent">
            <!-- Welcome Screen -->
            <div class="welcome-screen" id="welcomeScreen">
                <div class="welcome-icon">🚀</div>
                <h1 class="welcome-title">ACI Bulk Deployment</h1>
                <p class="welcome-desc">Streamline your Cisco ACI fabric deployments with automated VPC and individual port configurations.</p>
                <div class="welcome-cards">
                    <div class="welcome-card vpc" onclick="selectView('vpc')">
                        <div class="welcome-card-icon">⚡</div>
                        <div class="welcome-card-title">VPC Bulk</div>
                        <div class="welcome-card-desc">Deploy Virtual Port Channels across switch pairs</div>
                    </div>
                    <div class="welcome-card individual" onclick="selectView('individual')">
                        <div class="welcome-card-icon">🔌</div>
                        <div class="welcome-card-title">Port Bulk</div>
                        <div class="welcome-card-desc">Deploy individual access and trunk ports</div>
                    </div>
                </div>
            </div>

            <!-- VPC Screen -->
            <div id="vpcScreen" class="hidden" style="flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden;">
                <div class="header-bar">
                    <div class="header-title">
                        <h2>VPC Bulk Deployment</h2>
                        <span class="header-badge vpc">VPC</span>
                    </div>
                    <div class="header-actions">
                        <button class="header-btn" onclick="clearTerminal('vpc')">Clear</button>
                        <button class="header-btn danger" onclick="stopScript()" id="vpcStopBtn" disabled>Stop</button>
                        <button class="header-btn primary" onclick="runScript('vpc')" id="vpcRunBtn">Run Script</button>
                    </div>
                </div>

                <div class="config-panel">
                    <div class="csv-toggle-group">
                        <button class="csv-toggle active" onclick="toggleCsvMode('vpc', 'file')">📁 Use CSV File</button>
                        <button class="csv-toggle" onclick="toggleCsvMode('vpc', 'inline')">✏️ Edit Inline</button>
                    </div>
                    <div id="vpcFileMode" class="config-row">
                        <div class="config-group">
                            <label class="config-label">CSV File Path</label>
                            <input type="text" class="config-input" id="vpcCsvPath" value="{{ config.default_vpc_csv }}">
                        </div>
                    </div>
                    <div id="vpcInlineMode" style="display: none;">
                        <div class="csv-editor-section" style="padding: 0;">
                            <div class="csv-editor-header">
                                <span class="csv-editor-title">Inline CSV Editor</span>
                                <div class="csv-editor-actions">
                                    <button class="csv-editor-btn add" onclick="addCsvRow('vpc')">+ Add Row</button>
                                    <button class="csv-editor-btn" onclick="exportCsv('vpc')">Export CSV</button>
                                </div>
                            </div>
                            <table class="csv-editor-table" id="vpcCsvTable">
                                <thead>
                                    <tr>
                                        <th>Hostname</th>
                                        <th>Switch1</th>
                                        <th>Switch2</th>
                                        <th>Speed</th>
                                        <th>VLANS</th>
                                        <th>WorkOrder</th>
                                        <th class="row-actions"></th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr>
                                        <td><input type="text" placeholder="MEDHVIOP173_SEA_PROD"></td>
                                        <td><input type="text" placeholder="EDCLEAFACC1501"></td>
                                        <td><input type="text" placeholder="EDCLEAFACC1502"></td>
                                        <td><input type="text" placeholder="25G"></td>
                                        <td><input type="text" placeholder="32,64-67"></td>
                                        <td><input type="text" placeholder="WO123456"></td>
                                        <td class="row-actions"><button class="delete-row" onclick="deleteCsvRow(this)">✕</button></td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div class="csv-reference" id="vpcCsvRef">
                    <div class="csv-reference-header">
                        <span class="csv-reference-title">📋 CSV Format Reference</span>
                        <span class="csv-reference-toggle" onclick="toggleCsvRef('vpc')">Hide</span>
                    </div>
                    <table class="csv-table">
                        <tr><th>Hostname</th><th>Switch1</th><th>Switch2</th><th>Speed</th><th>VLANS</th><th>WorkOrder</th></tr>
                        <tr><td>Device name</td><td>First VPC switch</td><td>Second VPC switch</td><td>1G, 10G, 25G</td><td>VLAN IDs</td><td>Work order #</td></tr>
                    </table>
                    <div class="csv-example">
                        <div class="csv-example-label"># Example:</div>
                        MEDHVIOP173_SEA_PROD,EDCLEAFACC1501,EDCLEAFACC1502,25G,"32,64-67,92-95",WO123456
                    </div>
                </div>

                <div class="terminal-container">
                    <div class="terminal-header">
                        <div class="terminal-dots"><div class="terminal-dot red"></div><div class="terminal-dot yellow"></div><div class="terminal-dot green"></div></div>
                        <span class="terminal-title">vpc-deployment-console</span>
                        <div class="terminal-status" id="vpcTerminalStatus"><div class="terminal-status-dot"></div><span>Ready</span></div>
                    </div>
                    <div class="terminal-output" id="vpcOutput">
                        <div class="terminal-line muted">// VPC Bulk Deployment Console</div>
                        <div class="terminal-line muted">// Configure CSV path and click "Run Script" to begin</div>
                    </div>
                    <div class="terminal-input-area">
                        <span class="terminal-prompt">❯</span>
                        <input type="text" class="terminal-input" id="vpcInput" placeholder="Type response here..." onkeypress="handleInputKeypress(event, 'vpc')" disabled>
                        <button class="terminal-submit" id="vpcSubmitBtn" onclick="submitInput('vpc')" disabled>Send</button>
                    </div>
                </div>
            </div>

            <!-- Individual Screen -->
            <div id="individualScreen" class="hidden" style="flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden;">
                <div class="header-bar">
                    <div class="header-title">
                        <h2>Individual Port Deployment</h2>
                        <span class="header-badge individual">PORT</span>
                    </div>
                    <div class="header-actions">
                        <button class="header-btn" onclick="clearTerminal('individual')">Clear</button>
                        <button class="header-btn danger" onclick="stopScript()" id="individualStopBtn" disabled>Stop</button>
                        <button class="header-btn primary" onclick="runScript('individual')" id="individualRunBtn">Run Script</button>
                    </div>
                </div>

                <div class="config-panel">
                    <div class="csv-toggle-group">
                        <button class="csv-toggle active" onclick="toggleCsvMode('individual', 'file')">📁 Use CSV File</button>
                        <button class="csv-toggle" onclick="toggleCsvMode('individual', 'inline')">✏️ Edit Inline</button>
                    </div>
                    <div id="individualFileMode" class="config-row">
                        <div class="config-group">
                            <label class="config-label">CSV File Path</label>
                            <input type="text" class="config-input" id="individualCsvPath" value="{{ config.default_individual_csv }}">
                        </div>
                    </div>
                    <div id="individualInlineMode" style="display: none;">
                        <div class="csv-editor-section" style="padding: 0;">
                            <div class="csv-editor-header">
                                <span class="csv-editor-title">Inline CSV Editor</span>
                                <div class="csv-editor-actions">
                                    <button class="csv-editor-btn add" onclick="addCsvRow('individual')">+ Add Row</button>
                                    <button class="csv-editor-btn" onclick="exportCsv('individual')">Export CSV</button>
                                </div>
                            </div>
                            <table class="csv-editor-table" id="individualCsvTable">
                                <thead>
                                    <tr>
                                        <th>Hostname</th>
                                        <th>Switch</th>
                                        <th>Type</th>
                                        <th>Speed</th>
                                        <th>VLANS</th>
                                        <th>WorkOrder</th>
                                        <th class="row-actions"></th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr>
                                        <td><input type="text" placeholder="MEDHVIOP173_MGMT"></td>
                                        <td><input type="text" placeholder="EDCLEAFNSM2163"></td>
                                        <td><input type="text" placeholder="ACCESS"></td>
                                        <td><input type="text" placeholder="1G"></td>
                                        <td><input type="text" placeholder="2958"></td>
                                        <td><input type="text" placeholder="WO123456"></td>
                                        <td class="row-actions"><button class="delete-row" onclick="deleteCsvRow(this)">✕</button></td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div class="csv-reference" id="individualCsvRef">
                    <div class="csv-reference-header">
                        <span class="csv-reference-title">📋 CSV Format Reference</span>
                        <span class="csv-reference-toggle" onclick="toggleCsvRef('individual')">Hide</span>
                    </div>
                    <table class="csv-table">
                        <tr><th>Hostname</th><th>Switch</th><th>Type</th><th>Speed</th><th>VLANS</th><th>WorkOrder</th></tr>
                        <tr><td>Device name</td><td>Target switch</td><td>ACCESS/TRUNK</td><td>1G, 10G, 25G</td><td>VLAN IDs</td><td>Work order #</td></tr>
                    </table>
                    <div class="csv-example">
                        <div class="csv-example-label"># ACCESS (single VLAN, untagged):</div>
                        MEDHVIOP173_MGMT,EDCLEAFNSM2163,ACCESS,1G,2958,WO123456
                        <div class="csv-example-label" style="margin-top:8px"># TRUNK (multiple VLANs, tagged):</div>
                        MEDHVIOP173_Clients,EDCLEAFNSM2163,TRUNK,25G,"2704-2719",WO123456
                    </div>
                </div>

                <div class="terminal-container">
                    <div class="terminal-header">
                        <div class="terminal-dots"><div class="terminal-dot red"></div><div class="terminal-dot yellow"></div><div class="terminal-dot green"></div></div>
                        <span class="terminal-title">individual-port-console</span>
                        <div class="terminal-status" id="individualTerminalStatus"><div class="terminal-status-dot"></div><span>Ready</span></div>
                    </div>
                    <div class="terminal-output" id="individualOutput">
                        <div class="terminal-line muted">// Individual Port Deployment Console</div>
                        <div class="terminal-line muted">// Configure CSV path and click "Run Script" to begin</div>
                    </div>
                    <div class="terminal-input-area">
                        <span class="terminal-prompt">❯</span>
                        <input type="text" class="terminal-input" id="individualInput" placeholder="Type response here..." onkeypress="handleInputKeypress(event, 'individual')" disabled>
                        <button class="terminal-submit" id="individualSubmitBtn" onclick="submitInput('individual')" disabled>Send</button>
                    </div>
                </div>
            </div>

            <!-- Settings Screen -->
            <div id="settingsScreen" class="hidden" style="flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden;">
                <div class="header-bar">
                    <div class="header-title">
                        <h2>Settings</h2>
                        <span class="header-badge settings">CONFIG</span>
                    </div>
                    <div class="header-actions">
                        <button class="header-btn primary" onclick="saveSettings()">Save Settings</button>
                    </div>
                </div>

                <div class="settings-panel">
                    <div class="settings-section">
                        <div class="settings-section-title">📁 Script Paths</div>
                        <div class="settings-row">
                            <label class="settings-label">VPC Deployment Script</label>
                            <input type="text" class="settings-input" id="settingsVpcScript" value="{{ config.vpc_script }}">
                            <div class="settings-hint">Path to the VPC bulk deployment Python script</div>
                        </div>
                        <div class="settings-row">
                            <label class="settings-label">Individual Port Deployment Script</label>
                            <input type="text" class="settings-input" id="settingsIndividualScript" value="{{ config.individual_script }}">
                            <div class="settings-hint">Path to the individual port bulk deployment Python script</div>
                        </div>
                        <div class="settings-row">
                            <label class="settings-label">EPG Add Script</label>
                            <input type="text" class="settings-input" id="settingsEpgaddScript" value="{{ config.epgadd_script }}">
                            <div class="settings-hint">Path to the EPG add Python script</div>
                        </div>
                        <div class="settings-row">
                            <label class="settings-label">EPG Delete Script</label>
                            <input type="text" class="settings-input" id="settingsEpgdeleteScript" value="{{ config.epgdelete_script }}">
                            <div class="settings-hint">Path to the EPG delete Python script</div>
                        </div>
                    </div>

                    <div class="settings-section">
                        <div class="settings-section-title">ℹ️ Application Info</div>
                        <div class="settings-row">
                            <label class="settings-label">Version</label>
                            <input type="text" class="settings-input" id="settingsVersion" value="{{ config.version }}">
                        </div>
                    </div>

                    <div class="settings-section">
                        <div class="settings-section-title">📋 CSV Format Quick Reference</div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 12px;">
                            <div>
                                <div style="font-weight: 600; color: var(--accent-purple); margin-bottom: 8px;">VPC Bulk CSV:</div>
                                <table class="csv-table" style="font-size: 11px;">
                                    <tr><th>Column</th><th>Description</th></tr>
                                    <tr><td>Hostname</td><td>Device hostname</td></tr>
                                    <tr><td>Switch1</td><td>First VPC switch</td></tr>
                                    <tr><td>Switch2</td><td>Second VPC switch</td></tr>
                                    <tr><td>Speed</td><td>Link speed</td></tr>
                                    <tr><td>VLANS</td><td>VLAN IDs</td></tr>
                                    <tr><td>WorkOrder</td><td>Work order #</td></tr>
                                </table>
                            </div>
                            <div>
                                <div style="font-weight: 600; color: var(--accent-orange); margin-bottom: 8px;">Individual Port CSV:</div>
                                <table class="csv-table" style="font-size: 11px;">
                                    <tr><th>Column</th><th>Description</th></tr>
                                    <tr><td>Hostname</td><td>Device hostname</td></tr>
                                    <tr><td>Switch</td><td>Target switch</td></tr>
                                    <tr><td>Type</td><td>ACCESS or TRUNK</td></tr>
                                    <tr><td>Speed</td><td>Link speed</td></tr>
                                    <tr><td>VLANS</td><td>VLAN IDs</td></tr>
                                    <tr><td>WorkOrder</td><td>Work order #</td></tr>
                                </table>
                            </div>
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 16px;">
                            <div>
                                <div style="font-weight: 600; color: #38ef7d; margin-bottom: 8px;">EPG Add CSV:</div>
                                <table class="csv-table" style="font-size: 11px;">
                                    <tr><th>Column</th><th>Description</th></tr>
                                    <tr><td>Switch</td><td>Target switch</td></tr>
                                    <tr><td>Port</td><td>Port number (e.g., 1/68)</td></tr>
                                    <tr><td>VLANS</td><td>VLAN IDs to add</td></tr>
                                </table>
                            </div>
                            <div>
                                <div style="font-weight: 600; color: #f45c43; margin-bottom: 8px;">EPG Delete CSV:</div>
                                <table class="csv-table" style="font-size: 11px;">
                                    <tr><th>Column</th><th>Description</th></tr>
                                    <tr><td>Switch</td><td>Target switch</td></tr>
                                    <tr><td>Port</td><td>Port number (e.g., 1/68)</td></tr>
                                    <tr><td>VLANS</td><td>VLAN IDs to remove</td></tr>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- README Screen -->
            <div id="readmeScreen" class="hidden" style="flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden;">
                <div class="header-bar">
                    <div class="header-title">
                        <h2>Documentation</h2>
                        <span class="header-badge readme">README</span>
                    </div>
                </div>

                <div class="readme-panel">
                    <div class="readme-tabs">
                        <div class="readme-tab active" onclick="switchReadmeTab('ui')">🖥️ Using the UI</div>
                        <div class="readme-tab" onclick="switchReadmeTab('vpc')">⚡ VPC</div>
                        <div class="readme-tab" onclick="switchReadmeTab('individual')">🔌 Port</div>
                        <div class="readme-tab" onclick="switchReadmeTab('epgadd')">➕ EPG Add</div>
                        <div class="readme-tab" onclick="switchReadmeTab('epgdelete')">➖ EPG Delete</div>
                        <div class="readme-tab" onclick="switchReadmeTab('troubleshoot')">🔧 Troubleshoot</div>
                    </div>

                    <!-- UI Tab -->
                    <div id="readmeTabUi" class="readme-tab-content active">
                        <div class="readme-section">
                            <div class="readme-section-title"><span>🚀</span> Getting Started</div>
                            <div class="readme-content">
                                <div class="step">
                                    <div class="step-number">1</div>
                                    <div class="step-content">
                                        <div class="step-title">Select Deployment Type</div>
                                        <div>Click <strong>VPC Bulk</strong> or <strong>Port Bulk</strong> in the left sidebar to choose your deployment type.</div>
                                    </div>
                                </div>
                                <div class="step">
                                    <div class="step-number">2</div>
                                    <div class="step-content">
                                        <div class="step-title">Configure CSV Path</div>
                                        <div>Enter the path to your CSV file in the input field. Check the CSV Reference table to ensure your format is correct.</div>
                                    </div>
                                </div>
                                <div class="step">
                                    <div class="step-number">3</div>
                                    <div class="step-content">
                                        <div class="step-title">Run the Script</div>
                                        <div>Click the <strong>Run Script</strong> button. The terminal will display the script output in real-time.</div>
                                    </div>
                                </div>
                                <div class="step">
                                    <div class="step-number">4</div>
                                    <div class="step-content">
                                        <div class="step-title">Respond to Prompts</div>
                                        <div>When the script asks for input, type your response in the input bar at the bottom and press <strong>Enter</strong> or click <strong>Send</strong>.</div>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <div class="readme-section">
                            <div class="readme-section-title"><span>🎨</span> Terminal Color Guide</div>
                            <div class="readme-content">
                                <ul>
                                    <li><span style="color: var(--accent-cyan)">■</span> <strong>Cyan</strong> - Headers and section dividers</li>
                                    <li><span style="color: var(--accent-green)">■</span> <strong>Green</strong> - Success messages</li>
                                    <li><span style="color: var(--accent-red)">■</span> <strong>Red</strong> - Errors and failures</li>
                                    <li><span style="color: var(--accent-orange)">■</span> <strong>Orange</strong> - Warnings</li>
                                    <li><span style="color: var(--accent-blue)">■</span> <strong>Blue</strong> - Info messages</li>
                                    <li><span style="color: var(--accent-purple)">■</span> <strong>Purple</strong> - Input prompts (waiting for your response)</li>
                                </ul>
                            </div>
                        </div>

                        <div class="readme-section">
                            <div class="readme-section-title"><span>⚙️</span> Settings</div>
                            <div class="readme-content">
                                <p>Click <strong>Settings</strong> in the sidebar to configure:</p>
                                <ul>
                                    <li><strong>Script Paths</strong> - Location of the Python deployment scripts</li>
                                    <li><strong>Default CSV Files</strong> - Pre-filled CSV paths for quick access</li>
                                    <li><strong>Version</strong> - Application version number</li>
                                </ul>
                                <p>Settings are saved to <code>aci_deploy_config.json</code> in the application directory.</p>
                            </div>
                        </div>
                    </div>

                    <!-- VPC Tab -->
                    <div id="readmeTabVpc" class="readme-tab-content">
                        <div class="readme-section">
                            <div class="readme-section-title"><span>⚡</span> VPC Bulk Deployment</div>
                            <div class="readme-content">
                                <p>Deploy Virtual Port Channels (VPCs) across switch pairs in your ACI fabric.</p>
                                
                                <h3>CSV Format</h3>
                                <table class="csv-table">
                                    <tr><th>Column</th><th>Description</th><th>Example</th></tr>
                                    <tr><td>Hostname</td><td>Device hostname for naming</td><td><code>MEDHVIOP173_SEA_PROD</code></td></tr>
                                    <tr><td>Switch1</td><td>First switch in VPC pair</td><td><code>EDCLEAFACC1501</code></td></tr>
                                    <tr><td>Switch2</td><td>Second switch in VPC pair</td><td><code>EDCLEAFACC1502</code></td></tr>
                                    <tr><td>Speed</td><td>Link speed</td><td><code>1G</code>, <code>10G</code>, <code>25G</code>, <code>100G</code></td></tr>
                                    <tr><td>VLANS</td><td>VLAN IDs (comma-separated, ranges OK)</td><td><code>32,64-67,92-95</code></td></tr>
                                    <tr><td>WorkOrder</td><td>Work order number</td><td><code>WO123456</code></td></tr>
                                </table>

                                <h3>Example CSV</h3>
                                <div class="csv-example">
Hostname,Switch1,Switch2,Speed,VLANS,WorkOrder
MEDHVIOP173_SEA_PROD,EDCLEAFACC1501,EDCLEAFACC1502,25G,"32,64-67,92-95",WO123456
MEDHVIOP174_SEA_PROD,EDCLEAFACC1501,EDCLEAFACC1502,25G,"32,64-67",WO123457
                                </div>

                                <h3>What Gets Created</h3>
                                <ul>
                                    <li><strong>Port Description</strong> - Set on both switches</li>
                                    <li><strong>VPC Interface Policy Group</strong> - Named <code>{Hostname}_e{Port}.vpc</code></li>
                                    <li><strong>Access Port Selector</strong> - Under the Interface Profile</li>
                                    <li><strong>Static EPG Bindings</strong> - One per VLAN (trunk mode)</li>
                                </ul>
                            </div>
                        </div>
                    </div>

                    <!-- Individual Port Tab -->
                    <div id="readmeTabIndividual" class="readme-tab-content">
                        <div class="readme-section">
                            <div class="readme-section-title"><span>🔌</span> Individual Port Deployment</div>
                            <div class="readme-content">
                                <p>Deploy individual access and trunk ports on single switches.</p>
                                
                                <h3>CSV Format</h3>
                                <table class="csv-table">
                                    <tr><th>Column</th><th>Description</th><th>Example</th></tr>
                                    <tr><td>Hostname</td><td>Device hostname for naming</td><td><code>MEDHVIOP173_MGMT</code></td></tr>
                                    <tr><td>Switch</td><td>Target switch</td><td><code>EDCLEAFNSM2163</code></td></tr>
                                    <tr><td>Type</td><td>Port type</td><td><code>ACCESS</code> or <code>TRUNK</code></td></tr>
                                    <tr><td>Speed</td><td>Link speed</td><td><code>1G</code>, <code>10G</code>, <code>25G</code></td></tr>
                                    <tr><td>VLANS</td><td>VLAN IDs</td><td><code>2958</code> or <code>2704-2719</code></td></tr>
                                    <tr><td>WorkOrder</td><td>Work order number</td><td><code>WO123456</code></td></tr>
                                </table>

                                <h3>ACCESS vs TRUNK</h3>
                                <ul>
                                    <li><strong>ACCESS</strong> - Single VLAN, untagged traffic</li>
                                    <li><strong>TRUNK</strong> - Multiple VLANs, tagged traffic</li>
                                </ul>

                                <h3>Example CSV</h3>
                                <div class="csv-example">
Hostname,Switch,Type,Speed,VLANS,WorkOrder
MEDHVIOP173_MGMT,EDCLEAFNSM2163,ACCESS,1G,2958,WO123456
MEDHVIOP173_Clients,EDCLEAFNSM2163,TRUNK,25G,"2704-2719",WO123456
                                </div>

                                <h3>What Gets Created</h3>
                                <ul>
                                    <li><strong>Port Description</strong> - <code>{Hostname} {WorkOrder}</code></li>
                                    <li><strong>Leaf Access Port Policy Group</strong> - Named <code>{Hostname}_e{Port}</code></li>
                                    <li><strong>Port Selector</strong> - Under the Interface Profile</li>
                                    <li><strong>Static EPG Bindings</strong> - Access=untagged, Trunk=tagged</li>
                                </ul>
                            </div>
                        </div>
                    </div>

                    <!-- EPG Add Tab -->
                    <div id="readmeTabEpgadd" class="readme-tab-content">
                        <div class="readme-section">
                            <div class="readme-section-title"><span>➕</span> EPG Add - Add EPGs to Existing Ports</div>
                            <div class="readme-content">
                                <p>Add EPG static bindings to ports that already have policy groups configured.</p>
                                
                                <h3>CSV Format</h3>
                                <table class="csv-table">
                                    <tr><th>Column</th><th>Description</th><th>Example</th></tr>
                                    <tr><td>Switch</td><td>Target switch name</td><td><code>EDCLEAFACC1501</code></td></tr>
                                    <tr><td>Port</td><td>Port number</td><td><code>1/68</code></td></tr>
                                    <tr><td>VLANS</td><td>VLAN IDs to add</td><td><code>32,64-67</code></td></tr>
                                </table>

                                <h3>Example CSV</h3>
                                <div class="csv-example">
Switch,Port,VLANS
EDCLEAFACC1501,1/68,"32,64-67"
EDCLEAFNSM2163,1/5,2958
                                </div>

                                <h3>Features</h3>
                                <ul>
                                    <li><strong>Dry-Run Mode</strong> - Validate without deploying</li>
                                    <li><strong>Multi-AP Detection</strong> - Alerts when VLAN exists in multiple Application Profiles</li>
                                    <li><strong>Batch Preview</strong> - Shows ALL bindings before deployment</li>
                                    <li><strong>Skip Existing</strong> - Automatically skips already-bound EPGs</li>
                                    <li><strong>Binding Mode</strong> - Choose Trunk (tagged) or Access (untagged)</li>
                                </ul>

                                <h3>Multi-Application Profile Handling</h3>
                                <p>If a VLAN exists in multiple Application Profiles, you'll be prompted:</p>
                                <div class="csv-example">
[ALERT] VLAN 32 exists in multiple Application Profiles:
--------------------------------------------------
  [1] APP_PROFILE_1 -> V0032_EPG
  [2] APP_PROFILE_2 -> V0032_BACKUP
--------------------------------------------------
Select Application Profile for VLAN 32: _
                                </div>

                                <h3>What Gets Created</h3>
                                <ul>
                                    <li><strong>Static Path Binding</strong> on the EPG</li>
                                    <li><strong>VLAN Encapsulation</strong> - vlan-{ID}</li>
                                    <li><strong>Deployment Immediacy</strong> - Immediate</li>
                                </ul>
                            </div>
                        </div>
                    </div>

                    <!-- EPG Delete Tab -->
                    <div id="readmeTabEpgdelete" class="readme-tab-content">
                        <div class="readme-section">
                            <div class="readme-section-title"><span>➖</span> EPG Delete - Remove EPGs from Ports</div>
                            <div class="readme-content">
                                <p>Remove EPG static bindings from existing ports.</p>
                                
                                <h3>CSV Format</h3>
                                <table class="csv-table">
                                    <tr><th>Column</th><th>Description</th><th>Example</th></tr>
                                    <tr><td>Switch</td><td>Target switch name</td><td><code>EDCLEAFACC1501</code></td></tr>
                                    <tr><td>Port</td><td>Port number</td><td><code>1/68</code></td></tr>
                                    <tr><td>VLANS</td><td>VLAN IDs to remove</td><td><code>32,64-67</code></td></tr>
                                </table>

                                <h3>Example CSV</h3>
                                <div class="csv-example">
Switch,Port,VLANS
EDCLEAFACC1501,1/68,"32,64-67"
EDCLEAFNSM2163,1/5,2958
                                </div>

                                <h3>Features</h3>
                                <ul>
                                    <li><strong>Dry-Run Mode</strong> - Validate without deleting</li>
                                    <li><strong>Multi-AP Handling</strong> - Check specific AP or ALL profiles</li>
                                    <li><strong>Batch Preview</strong> - Shows ALL bindings to delete</li>
                                    <li><strong>Safety Confirmation</strong> - Must type 'YES' to confirm deletion</li>
                                </ul>

                                <h3>Multi-Application Profile Handling</h3>
                                <p>When a VLAN exists in multiple Application Profiles:</p>
                                <div class="csv-example">
[ALERT] VLAN 32 exists in multiple Application Profiles:
----------------------------------------
  [1] APP_PROFILE_1 -> V0032_EPG
  [2] APP_PROFILE_2 -> V0032_BACKUP
  [A] Check ALL Application Profiles
----------------------------------------
Select for VLAN 32: _
                                </div>

                                <h3>Confirmation Required</h3>
                                <p>Deletion requires explicit confirmation:</p>
                                <div class="csv-example">
[WARNING] About to delete 5 EPG binding(s)
         This action cannot be undone!

Confirm deletion (type 'YES' to confirm): _
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Troubleshooting Tab -->
                    <div id="readmeTabTroubleshoot" class="readme-tab-content">
                        <div class="readme-section">
                            <div class="readme-section-title"><span>🔧</span> Troubleshooting</div>
                            <div class="readme-content">
                                <h3>Script Not Found</h3>
                                <p>If you see "Script not found" error:</p>
                                <ul>
                                    <li>Go to <strong>Settings</strong> and verify the script paths</li>
                                    <li>Use absolute paths if needed: <code>/home/user/scripts/aci_bulk_vpc_deploy.py</code></li>
                                    <li>Ensure the Python files are in the same directory as this app</li>
                                </ul>

                                <h3>No Output Appearing</h3>
                                <ul>
                                    <li>Ensure the scripts use <code>print()</code> statements</li>
                                    <li>Check that Python is in your system PATH</li>
                                    <li>Try running the script directly in terminal to test</li>
                                </ul>

                                <h3>Input Not Being Sent</h3>
                                <ul>
                                    <li>Make sure the input bar is enabled (not grayed out)</li>
                                    <li>The script must be running and waiting for input</li>
                                    <li>Press <strong>Enter</strong> or click <strong>Send</strong> to submit</li>
                                </ul>

                                <h3>CSV Format Errors</h3>
                                <ul>
                                    <li>Check column headers match exactly (case-sensitive)</li>
                                    <li>Wrap VLAN ranges in quotes: <code>"32,64-67"</code></li>
                                    <li>No spaces after commas in VLAN lists</li>
                                    <li>Save as UTF-8 encoding</li>
                                </ul>

                                <h3>Authentication Failures</h3>
                                <ul>
                                    <li>Verify your APIC credentials</li>
                                    <li>Check APIC URLs are configured in the script</li>
                                    <li>Ensure network connectivity to APIC</li>
                                </ul>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- EPG Add Screen -->
            <div id="epgaddScreen" class="hidden" style="flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden;">
                <div class="header-bar">
                    <div class="header-title">
                        <h2>EPG Add - Add EPGs to Existing Ports</h2>
                        <span class="header-badge epgadd">ADD</span>
                    </div>
                    <div class="header-actions">
                        <button class="header-btn" onclick="clearTerminal('epgadd')">Clear</button>
                        <button class="header-btn danger" onclick="stopScript()" id="epgaddStopBtn" disabled>Stop</button>
                        <button class="header-btn primary" onclick="runScript('epgadd')" id="epgaddRunBtn">Run Script</button>
                    </div>
                </div>

                <div class="config-panel">
                    <div class="csv-toggle-group">
                        <button class="csv-toggle active" onclick="toggleCsvMode('epgadd', 'file')">📁 Use CSV File</button>
                        <button class="csv-toggle" onclick="toggleCsvMode('epgadd', 'inline')">✏️ Edit Inline</button>
                    </div>
                    <div id="epgaddFileMode" class="config-row">
                        <div class="config-group">
                            <label class="config-label">CSV File Path</label>
                            <input type="text" class="config-input" id="epgaddCsvPath" value="epg_add.csv">
                        </div>
                    </div>
                    <div id="epgaddInlineMode" style="display: none;">
                        <div class="csv-editor-section" style="padding: 0;">
                            <div class="csv-editor-header">
                                <span class="csv-editor-title">Inline CSV Editor</span>
                                <div class="csv-editor-actions">
                                    <button class="csv-editor-btn add" onclick="addCsvRow('epgadd')">+ Add Row</button>
                                    <button class="csv-editor-btn" onclick="exportCsv('epgadd')">Export CSV</button>
                                </div>
                            </div>
                            <table class="csv-editor-table" id="epgaddCsvTable">
                                <thead>
                                    <tr>
                                        <th>Switch</th>
                                        <th>Port</th>
                                        <th>VLANS</th>
                                        <th class="row-actions"></th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr>
                                        <td><input type="text" placeholder="EDCLEAFACC1501"></td>
                                        <td><input type="text" placeholder="1/68"></td>
                                        <td><input type="text" placeholder="32,64-67"></td>
                                        <td class="row-actions"><button class="delete-row" onclick="deleteCsvRow(this)">✕</button></td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div class="csv-reference" id="epgaddCsvRef">
                    <div class="csv-reference-header">
                        <span class="csv-reference-title">📋 CSV Format Reference</span>
                        <span class="csv-reference-toggle" onclick="toggleCsvRef('epgadd')">Hide</span>
                    </div>
                    <table class="csv-table">
                        <tr><th>Switch</th><th>Port</th><th>VLANS</th></tr>
                        <tr><td>Switch name</td><td>Port (e.g., 1/68)</td><td>VLAN IDs</td></tr>
                    </table>
                    <div class="csv-example">
                        <div class="csv-example-label"># Example:</div>
                        EDCLEAFACC1501,1/68,"32,64-67"
                    </div>
                </div>

                <div class="terminal-container">
                    <div class="terminal-header">
                        <div class="terminal-dots"><div class="terminal-dot red"></div><div class="terminal-dot yellow"></div><div class="terminal-dot green"></div></div>
                        <span class="terminal-title">epg-add-console</span>
                        <div class="terminal-status" id="epgaddTerminalStatus"><div class="terminal-status-dot"></div><span>Ready</span></div>
                    </div>
                    <div class="terminal-output" id="epgaddOutput">
                        <div class="terminal-line muted">// EPG Add Console</div>
                        <div class="terminal-line muted">// Add EPG bindings to existing ports</div>
                    </div>
                    <div class="terminal-input-area">
                        <span class="terminal-prompt">❯</span>
                        <input type="text" class="terminal-input" id="epgaddInput" placeholder="Type response here..." onkeypress="handleInputKeypress(event, 'epgadd')" disabled>
                        <button class="terminal-submit" id="epgaddSubmitBtn" onclick="submitInput('epgadd')" disabled>Send</button>
                    </div>
                </div>
            </div>

            <!-- EPG Delete Screen -->
            <div id="epgdeleteScreen" class="hidden" style="flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden;">
                <div class="header-bar">
                    <div class="header-title">
                        <h2>EPG Delete - Remove EPGs from Ports</h2>
                        <span class="header-badge epgdelete">DELETE</span>
                    </div>
                    <div class="header-actions">
                        <button class="header-btn" onclick="clearTerminal('epgdelete')">Clear</button>
                        <button class="header-btn danger" onclick="stopScript()" id="epgdeleteStopBtn" disabled>Stop</button>
                        <button class="header-btn primary" onclick="runScript('epgdelete')" id="epgdeleteRunBtn">Run Script</button>
                    </div>
                </div>

                <div class="config-panel">
                    <div class="csv-toggle-group">
                        <button class="csv-toggle active" onclick="toggleCsvMode('epgdelete', 'file')">📁 Use CSV File</button>
                        <button class="csv-toggle" onclick="toggleCsvMode('epgdelete', 'inline')">✏️ Edit Inline</button>
                    </div>
                    <div id="epgdeleteFileMode" class="config-row">
                        <div class="config-group">
                            <label class="config-label">CSV File Path</label>
                            <input type="text" class="config-input" id="epgdeleteCsvPath" value="epg_delete.csv">
                        </div>
                    </div>
                    <div id="epgdeleteInlineMode" style="display: none;">
                        <div class="csv-editor-section" style="padding: 0;">
                            <div class="csv-editor-header">
                                <span class="csv-editor-title">Inline CSV Editor</span>
                                <div class="csv-editor-actions">
                                    <button class="csv-editor-btn add" onclick="addCsvRow('epgdelete')">+ Add Row</button>
                                    <button class="csv-editor-btn" onclick="exportCsv('epgdelete')">Export CSV</button>
                                </div>
                            </div>
                            <table class="csv-editor-table" id="epgdeleteCsvTable">
                                <thead>
                                    <tr>
                                        <th>Switch</th>
                                        <th>Port</th>
                                        <th>VLANS</th>
                                        <th class="row-actions"></th>
                                    </tr>
                                </thead>
                                <tbody>
                                    <tr>
                                        <td><input type="text" placeholder="EDCLEAFACC1501"></td>
                                        <td><input type="text" placeholder="1/68"></td>
                                        <td><input type="text" placeholder="32,64-67"></td>
                                        <td class="row-actions"><button class="delete-row" onclick="deleteCsvRow(this)">✕</button></td>
                                    </tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div class="csv-reference" id="epgdeleteCsvRef">
                    <div class="csv-reference-header">
                        <span class="csv-reference-title">📋 CSV Format Reference</span>
                        <span class="csv-reference-toggle" onclick="toggleCsvRef('epgdelete')">Hide</span>
                    </div>
                    <table class="csv-table">
                        <tr><th>Switch</th><th>Port</th><th>VLANS</th></tr>
                        <tr><td>Switch name</td><td>Port (e.g., 1/68)</td><td>VLAN IDs to remove</td></tr>
                    </table>
                    <div class="csv-example">
                        <div class="csv-example-label"># Example:</div>
                        EDCLEAFACC1501,1/68,"32,64-67"
                    </div>
                </div>

                <div class="terminal-container">
                    <div class="terminal-header">
                        <div class="terminal-dots"><div class="terminal-dot red"></div><div class="terminal-dot yellow"></div><div class="terminal-dot green"></div></div>
                        <span class="terminal-title">epg-delete-console</span>
                        <div class="terminal-status" id="epgdeleteTerminalStatus"><div class="terminal-status-dot"></div><span>Ready</span></div>
                    </div>
                    <div class="terminal-output" id="epgdeleteOutput">
                        <div class="terminal-line muted">// EPG Delete Console</div>
                        <div class="terminal-line muted">// Remove EPG bindings from ports</div>
                    </div>
                    <div class="terminal-input-area">
                        <span class="terminal-prompt">❯</span>
                        <input type="text" class="terminal-input" id="epgdeleteInput" placeholder="Type response here..." onkeypress="handleInputKeypress(event, 'epgdelete')" disabled>
                        <button class="terminal-submit" id="epgdeleteSubmitBtn" onclick="submitInput('epgdelete')" disabled>Send</button>
                    </div>
                </div>
            </div>
        </main>
    </div>

    <script>
        let currentView = 'welcome';
        let isRunning = false;
        let pollInterval = null;
        let csvModes = { vpc: 'file', individual: 'file', epgadd: 'file', epgdelete: 'file' };

        function selectView(view) {
            currentView = view;
            document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active'));
            const navItem = document.querySelector(`.nav-item.${view}`);
            if (navItem) navItem.classList.add('active');

            document.getElementById('welcomeScreen').classList.add('hidden');
            document.getElementById('vpcScreen').classList.add('hidden');
            document.getElementById('individualScreen').classList.add('hidden');
            document.getElementById('settingsScreen').classList.add('hidden');
            document.getElementById('readmeScreen').classList.add('hidden');
            document.getElementById('epgaddScreen').classList.add('hidden');
            document.getElementById('epgdeleteScreen').classList.add('hidden');

            const screen = document.getElementById(view + 'Screen');
            if (screen) {
                screen.classList.remove('hidden');
                screen.style.display = 'flex';
            } else if (view === 'welcome') {
                document.getElementById('welcomeScreen').classList.remove('hidden');
            }
        }

        // CSV Editor Functions
        function toggleCsvMode(type, mode) {
            csvModes[type] = mode;
            const fileMode = document.getElementById(type + 'FileMode');
            const inlineMode = document.getElementById(type + 'InlineMode');
            const toggles = document.querySelectorAll(`#${type}Screen .csv-toggle`);
            
            toggles.forEach(t => t.classList.remove('active'));
            event.target.classList.add('active');
            
            if (mode === 'file') {
                fileMode.style.display = 'flex';
                inlineMode.style.display = 'none';
            } else {
                fileMode.style.display = 'none';
                inlineMode.style.display = 'block';
            }
        }

        function addCsvRow(type) {
            const table = document.getElementById(type + 'CsvTable').getElementsByTagName('tbody')[0];
            const newRow = table.insertRow();
            
            const columns = type.startsWith('epg') ? 3 : (type === 'vpc' ? 6 : 6);
            const placeholders = {
                'epgadd': ['EDCLEAFACC1501', '1/68', '32,64-67'],
                'epgdelete': ['EDCLEAFACC1501', '1/68', '32,64-67'],
                'vpc': ['HOSTNAME', 'EDCLEAFACC1501', 'EDCLEAFACC1502', '25G', '32,64-67', 'WO123456'],
                'individual': ['HOSTNAME', 'EDCLEAFNSM2163', 'ACCESS', '1G', '2958', 'WO123456']
            };
            
            for (let i = 0; i < columns; i++) {
                const cell = newRow.insertCell();
                const input = document.createElement('input');
                input.type = 'text';
                input.placeholder = placeholders[type][i] || '';
                cell.appendChild(input);
            }
            
            const actionsCell = newRow.insertCell();
            actionsCell.className = 'row-actions';
            actionsCell.innerHTML = '<button class="delete-row" onclick="deleteCsvRow(this)">✕</button>';
        }

        function deleteCsvRow(btn) {
            const row = btn.closest('tr');
            const tbody = row.parentNode;
            if (tbody.rows.length > 1) {
                row.remove();
            }
        }

        function getInlineCsvData(type) {
            const table = document.getElementById(type + 'CsvTable');
            const rows = table.getElementsByTagName('tbody')[0].rows;
            const headers = Array.from(table.getElementsByTagName('th')).map(th => th.textContent).filter(h => h);
            
            let csvContent = headers.join(',') + '\\n';
            
            for (let row of rows) {
                const cells = row.getElementsByTagName('input');
                const values = Array.from(cells).map(input => {
                    let val = input.value.trim();
                    if (val.includes(',') || val.includes('-')) {
                        val = '"' + val + '"';
                    }
                    return val;
                });
                if (values.some(v => v)) {
                    csvContent += values.join(',') + '\\n';
                }
            }
            
            return csvContent;
        }

        function exportCsv(type) {
            const csvContent = getInlineCsvData(type);
            const blob = new Blob([csvContent], { type: 'text/csv' });
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = type + '_export.csv';
            a.click();
            window.URL.revokeObjectURL(url);
        }

        function switchReadmeTab(tab) {
            document.querySelectorAll('.readme-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.readme-tab-content').forEach(c => c.classList.remove('active'));
            
            event.target.classList.add('active');
            const tabMap = {
                'ui': 'readmeTabUi',
                'vpc': 'readmeTabVpc',
                'individual': 'readmeTabIndividual',
                'epgadd': 'readmeTabEpgadd',
                'epgdelete': 'readmeTabEpgdelete',
                'troubleshoot': 'readmeTabTroubleshoot'
            };
            document.getElementById(tabMap[tab]).classList.add('active');
        }

        function toggleCsvRef(type) {
            const ref = document.getElementById(type + 'CsvRef');
            const table = ref.querySelector('.csv-table');
            const example = ref.querySelector('.csv-example');
            const toggle = ref.querySelector('.csv-reference-toggle');
            if (table.style.display === 'none') {
                table.style.display = '';
                example.style.display = '';
                toggle.textContent = 'Hide';
            } else {
                table.style.display = 'none';
                example.style.display = 'none';
                toggle.textContent = 'Show';
            }
        }

        function addLine(type, text, lineType = 'normal') {
            const output = document.getElementById(type + 'Output');
            const line = document.createElement('div');
            line.className = 'terminal-line ' + lineType;
            line.textContent = text;
            output.appendChild(line);
            output.scrollTop = output.scrollHeight;
        }

        function clearTerminal(type) {
            document.getElementById(type + 'Output').innerHTML = '<div class="terminal-line muted">// Terminal cleared</div>';
        }

        function setStatus(type, text, running) {
            const status = document.getElementById(type + 'TerminalStatus');
            status.querySelector('span').textContent = text;
            status.classList.toggle('running', running);
            document.getElementById('globalStatus').classList.toggle('running', running);
            document.getElementById('statusText').textContent = running ? 'Running' : 'Ready';
        }

        function runScript(type) {
            const csvPath = document.getElementById(type + 'CsvPath').value;
            if (!csvPath) { addLine(type, '[ERROR] Please specify a CSV file path', 'error'); return; }

            isRunning = true;
            setStatus(type, 'Running', true);
            document.getElementById(type + 'RunBtn').disabled = true;
            document.getElementById(type + 'StopBtn').disabled = false;
            document.getElementById(type + 'Input').disabled = false;
            document.getElementById(type + 'SubmitBtn').disabled = false;

            clearTerminal(type);
            addLine(type, '[INFO] Starting script...', 'info');
            addLine(type, '[INFO] CSV: ' + csvPath, 'info');

            fetch('/api/run', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({type: type, csv_path: csvPath})
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'started') startPolling(type);
                else { addLine(type, '[ERROR] ' + data.message, 'error'); scriptEnded(type); }
            })
            .catch(err => { addLine(type, '[ERROR] ' + err, 'error'); scriptEnded(type); });
        }

        function startPolling(type) {
            pollInterval = setInterval(() => {
                fetch('/api/output').then(r => r.json()).then(data => {
                    data.lines.forEach(item => {
                        if (item.type === 'output') {
                            let lt = 'normal';
                            if (item.text.includes('===') || item.text.includes('---')) lt = 'header';
                            else if (item.text.includes('[SUCCESS]') || item.text.includes(' OK')) lt = 'success';
                            else if (item.text.includes('[ERROR]') || item.text.includes('[FAILED]')) lt = 'error';
                            else if (item.text.includes('[WARNING]')) lt = 'warning';
                            else if (item.text.includes('[INFO]')) lt = 'info';
                            else if (item.text.includes('Select') || item.text.includes(':')) lt = 'prompt';
                            addLine(type, item.text, lt);
                        } else if (item.type === 'exit') {
                            addLine(type, '[EXIT] Code: ' + item.code, item.code === 0 ? 'success' : 'error');
                            scriptEnded(type);
                        } else if (item.type === 'error') {
                            addLine(type, '[ERROR] ' + item.text, 'error');
                            scriptEnded(type);
                        }
                    });
                });
            }, 100);
        }

        function scriptEnded(type) {
            isRunning = false;
            if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
            setStatus(type, 'Ready', false);
            document.getElementById(type + 'RunBtn').disabled = false;
            document.getElementById(type + 'StopBtn').disabled = true;
            document.getElementById(type + 'Input').disabled = true;
            document.getElementById(type + 'SubmitBtn').disabled = true;
        }

        function stopScript() {
            fetch('/api/stop', {method: 'POST'}).then(() => {
                addLine(currentView, '[STOPPED] Terminated by user', 'warning');
                scriptEnded(currentView);
            });
        }

        function submitInput(type) {
            const input = document.getElementById(type + 'Input');
            if (!input.value && input.value !== '') return;
            addLine(type, '> ' + input.value, 'info');
            fetch('/api/input', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text: input.value})
            });
            input.value = '';
        }

        function handleInputKeypress(e, type) { if (e.key === 'Enter') submitInput(type); }

        function saveSettings() {
            const settings = {
                vpc_script: document.getElementById('settingsVpcScript').value,
                individual_script: document.getElementById('settingsIndividualScript').value,
                epgadd_script: document.getElementById('settingsEpgaddScript').value,
                epgdelete_script: document.getElementById('settingsEpgdeleteScript').value,
                version: document.getElementById('settingsVersion').value
            };
            fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(settings)
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'saved') {
                    alert('Settings saved!');
                    document.getElementById('versionBadge').textContent = 'v' + settings.version;
                }
            });
        }

        selectView('welcome');
    </script>
</body>
</html>
'''

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, config=load_config())

@app.route('/api/run', methods=['POST'])
def api_run():
    global running_process, output_queue
    if running_process is not None:
        return jsonify({'status': 'error', 'message': 'Script already running'})
    
    data = request.json
    config = load_config()
    script_type = data.get('type')
    
    # Map script type to config key
    script_map = {
        'vpc': 'vpc_script',
        'individual': 'individual_script',
        'epgadd': 'epgadd_script',
        'epgdelete': 'epgdelete_script'
    }
    
    script_key = script_map.get(script_type)
    if not script_key or script_key not in config:
        return jsonify({'status': 'error', 'message': f'Unknown script type: {script_type}'})
    
    script_path = config[script_key]
    
    if not os.path.exists(script_path):
        return jsonify({'status': 'error', 'message': f'Script not found: {script_path}'})
    
    while not output_queue.empty():
        try: output_queue.get_nowait()
        except: break
    
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

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print(" ACI BULK DEPLOYMENT WEB APPLICATION")
    print("=" * 60)
    print("\n Starting server...")
    print(" Open http://localhost:5000 in your browser")
    print("\n Press Ctrl+C to stop")
    print("=" * 60 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
