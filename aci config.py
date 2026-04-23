#!/usr/bin/env python3
"""
aci_config.py — Shared ACI Datacenter Configuration Module
============================================================
Provides interactive DC selection at script startup.

Features:
  - Toggle which datacenters (D1/D2/D3) are active for the run
  - Confirm or edit the APIC URL for each active DC
  - Auto-detect environment from switch names (NSM→D3, SDC→D2, else→D1)
  - Manual override: reassign any deployment to a different DC

Usage (in deploy scripts):
    from aci_config import select_datacenters, detect_environment, apply_dc_override

    # 1. At startup — returns the populated APIC_URLS dict (only active DCs)
    APIC_URLS = select_datacenters()

    # 2. Per-deployment — auto-detects DC from switch name
    env = detect_environment(switch_name)           # "D1" / "D2" / "D3"

    # 3. Optional manual override prompt before each deployment
    env = apply_dc_override(env, APIC_URLS, switch_name)

CCIE Automation note:
  This module demonstrates separation of concerns — configuration/discovery
  logic is decoupled from deployment logic, a pattern tested in both the
  written and practical CCIE Automation exams.  Compare to how Nornir
  separates inventory (RunnerConfiguration) from task execution.

Author: Network Automation
Version: 1.0.0
"""

import sys
import re

# ---------------------------------------------------------------------------
# Defaults — scripts may override these before calling select_datacenters()
# ---------------------------------------------------------------------------

# Pre-seeded URLs: if a DC's URL is already set here, it becomes the default
# shown during the interactive prompt (user can still edit it).
DEFAULT_URLS = {
    "D1": "",   # ACC switches  (e.g. https://apic-d1.example.com)
    "D2": "",   # SDC switches  (e.g. https://apic-d2.example.com)
    "D3": "",   # NSM switches  (e.g. https://apic-d3.example.com)
}

DC_LABELS = {
    "D1": "D1  (ACC switches  — default for non-NSM/SDC)",
    "D2": "D2  (SDC switches  — switch names contain 'SDC')",
    "D3": "D3  (NSM switches  — switch names contain 'NSM')",
}


# ---------------------------------------------------------------------------
# Environment detection  (unchanged from original scripts)
# ---------------------------------------------------------------------------

def detect_environment(switch_name: str) -> str:
    """
    Auto-detect datacenter from switch name.

    Rules (in priority order):
        'NSM' in name  →  D3
        'SDC' in name  →  D2
        anything else  →  D1

    This mirrors the naming convention used across all four deploy scripts
    and is the single source of truth for environment detection.
    """
    upper = switch_name.upper()
    if "NSM" in upper:
        return "D3"
    elif "SDC" in upper:
        return "D2"
    return "D1"


# ---------------------------------------------------------------------------
# Interactive DC selection
# ---------------------------------------------------------------------------

def _prompt(text: str) -> str:
    """Flush-safe prompt (works inside subprocess pipes / web UI)."""
    sys.stdout.write(text)
    sys.stdout.flush()
    return input()


def select_datacenters(default_urls: dict = None) -> dict:
    """
    Interactive startup prompt: choose which DCs are active and set their URLs.

    Returns a dict of {dc_key: url} containing ONLY the active DCs.
    Scripts should replace their module-level APIC_URLS with this return value.

    Example return value (D1 and D3 active):
        {"D1": "https://apic-d1.example.com", "D3": "https://apic-d3.example.com"}

    The D2 key will be absent, so any deployment auto-detected as D2 will be
    caught and reported as "environment not available" — clean fail-fast.
    """
    seeds = {**DEFAULT_URLS, **(default_urls or {})}

    print("\n" + "=" * 70)
    print(" DATACENTER SELECTION")
    print("=" * 70)
    print()
    print("  Select which datacenters are in scope for this run.")
    print("  You can activate one, two, or all three.")
    print()

    # ── Step 1: toggle active DCs ──────────────────────────────────────────
    active = {"D1": True, "D2": True, "D3": True}   # default all on

    print("  Current selection  (all active by default):")
    print()
    for key in ("D1", "D2", "D3"):
        status = "✓ ACTIVE" if active[key] else "✗ SKIP  "
        url_hint = f"  [{seeds[key]}]" if seeds[key] else ""
        print(f"    [{key}]  {status}  {DC_LABELS[key]}{url_hint}")

    print()
    print("  Enter DC keys to toggle  (e.g. 'D2' to disable, 'D1 D3' for two).")
    print("  Press Enter to keep all three active.")
    print()

    raw = _prompt("  Toggle DC(s): ").strip().upper()
    if raw:
        tokens = re.findall(r'D[123]', raw)
        for t in tokens:
            active[t] = not active[t]

    # Re-print current state so user can see what's active
    print()
    print("  ── Active datacenters ──────────────────────────────────────")
    chosen = [k for k in ("D1", "D2", "D3") if active[k]]
    skipped = [k for k in ("D1", "D2", "D3") if not active[k]]
    for k in chosen:
        print(f"    ✓  {DC_LABELS[k]}")
    for k in skipped:
        print(f"    ✗  {DC_LABELS[k]}  (skipped)")
    print()

    if not chosen:
        print("  [ERROR] At least one datacenter must be active.")
        sys.exit(1)

    # ── Step 2: confirm / set APIC URL for each active DC ─────────────────
    print("  ── APIC URLs ───────────────────────────────────────────────")
    print("  Press Enter to keep the shown URL, or type a new one.")
    print()

    result = {}
    for key in chosen:
        current = seeds.get(key, "")
        hint = f" [{current}]" if current else " [not set]"
        raw_url = _prompt(f"  {key} APIC URL{hint}: ").strip()
        url = raw_url if raw_url else current

        if not url:
            print(f"  [ERROR] {key} is active but has no URL. Enter a URL or disable {key}.")
            url = _prompt(f"  {key} APIC URL (required): ").strip()
            if not url:
                print(f"  [SKIP] Disabling {key} — no URL provided.")
                continue

        # Strip trailing slash
        result[key] = url.rstrip("/")
        print(f"    → {key}: {result[key]}")

    print()
    print("  ── Summary ────────────────────────────────────────────────")
    for k, v in result.items():
        print(f"    {k}: {v}")
    if skipped:
        print(f"    Skipped: {', '.join(skipped)}")
    print()

    confirm = _prompt("  Proceed with these datacenters? (yes/no): ").strip().lower()
    if confirm not in ("yes", "y"):
        print("\n[CANCELLED]")
        sys.exit(0)

    return result


# ---------------------------------------------------------------------------
# Per-deployment manual override
# ---------------------------------------------------------------------------

def apply_dc_override(auto_env: str, apic_urls: dict, switch_name: str) -> str:
    """
    Offer a manual override of the auto-detected DC for a single deployment.

    Call this after detect_environment() if you want to give the operator a
    chance to reassign the deployment to a different active DC.

    Returns the (possibly overridden) DC key.

    Example usage in deploy loop:
        env = detect_environment(dep['switch'])
        env = apply_dc_override(env, APIC_URLS, dep['switch'])
    """
    available = list(apic_urls.keys())

    print(f"\n  Auto-detected environment: {auto_env}  (switch: {switch_name})")

    # If auto-detected DC is not active, force a choice
    forced = auto_env not in apic_urls
    if forced:
        print(f"  [WARNING] {auto_env} is not in the active DC list.")

    if not forced:
        raw = _prompt(f"  Override DC? Press Enter to accept [{auto_env}], or type {'/'.join(available)}: ").strip().upper()
        if not raw:
            return auto_env
    else:
        print(f"  Available DCs: {', '.join(available)}")
        raw = _prompt(f"  Select DC for this deployment: ").strip().upper()

    if raw in apic_urls:
        print(f"    → Overriding to {raw}")
        return raw

    # Invalid input — keep auto-detected (or warn if forced)
    if forced:
        print(f"  [ERROR] Invalid selection. Skipping deployment.")
        return None   # caller should skip when None is returned
    return auto_env


# ---------------------------------------------------------------------------
# Convenience: pretty-print active DC table (call after select_datacenters)
# ---------------------------------------------------------------------------

def print_dc_summary(apic_urls: dict) -> None:
    """Print a compact summary of active DCs — useful in script headers."""
    print("\n  Active Datacenters:")
    for k, v in apic_urls.items():
        print(f"    {k}: {v}")
    print()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("aci_config.py — standalone test")
    urls = select_datacenters()
    print("\nReturned APIC_URLS:")
    for k, v in urls.items():
        print(f"  {k}: {v}")

    print("\nDetection tests:")
    for sw in ["EDCLEAFACC1501", "EDCLEAFNSM2163", "SDCLEAF0101", "MYSWITCH99"]:
        env = detect_environment(sw)
        in_scope = env in urls
        print(f"  {sw:<25} → {env}  {'(active)' if in_scope else '(not in scope)'}")
