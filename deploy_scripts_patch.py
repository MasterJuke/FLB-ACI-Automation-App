#!/usr/bin/env python3
"""
deploy_scripts_patch.py
========================
Run this script once to apply the datacenter selection patch to all four
deploy scripts. It makes exactly one change per script:

  BEFORE (in main()):
      # Check configuration
      missing_urls = [dc for dc, url in APIC_URLS.items() if not url]

  AFTER (in main()):
      # Select which datacenters are in scope for this run
      APIC_URLS = select_datacenters(APIC_URLS)

      # Check configuration
      missing_urls = [dc for dc, url in APIC_URLS.items() if not url]

It also adds 'select_datacenters' to the import from aci_port_utils in
each script.

Usage:
    python3 deploy_scripts_patch.py

Place this file in the same directory as your ACI scripts and run it once.
"""

import os
import re
import sys


SCRIPTS = [
    "aci_bulk_epg_add.py",
    "aci_bulk_epg_delete.py",
    "aci_bulk_individual_deploy.py",
    "aci_bulk_vpc_deploy.py",
]

# The 2 lines inserted before the check configuration block
INSERTION = (
    "    # Select which datacenters are in scope for this run\n"
    "    APIC_URLS = select_datacenters(APIC_URLS)\n"
    "\n"
)

# Exact target string to insert before
CHECK_CONFIG = (
    "    # Check configuration\n"
    "    missing_urls = [dc for dc, url in APIC_URLS.items() if not url]\n"
)


def patch_script(path):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    # Already patched?
    if "APIC_URLS = select_datacenters(APIC_URLS)" in src:
        return "already patched — skipped"

    # Insert before the check configuration block
    if CHECK_CONFIG not in src:
        return "ERROR: target block not found — check manually"

    src = src.replace(CHECK_CONFIG, INSERTION + CHECK_CONFIG, 1)

    # Add select_datacenters to the aci_port_utils import
    # Handles both possible import styles (epg scripts vs deploy scripts)
    if "from aci_port_utils import" in src and "select_datacenters" not in src:
        # Find the import block and append to it
        # Pattern: last item before the closing paren or last import name
        src = re.sub(
            r'(from aci_port_utils import \([^)]+?)'
            r'(\n\))',
            r'\1,\n    select_datacenters\2',
            src,
            count=1,
            flags=re.DOTALL
        )
        # Fallback for single-line imports
        if "select_datacenters" not in src:
            src = re.sub(
                r'(from aci_port_utils import .+?)(\n)',
                r'\1, select_datacenters\2',
                src,
                count=1
            )

    with open(path, "w", encoding="utf-8") as f:
        f.write(src)

    return "OK"


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))

    print("\nACI Datacenter Selection Patch")
    print("=" * 50)

    any_found = False
    for script_name in SCRIPTS:
        path = os.path.join(script_dir, script_name)
        if not os.path.exists(path):
            print(f"  {script_name:<40} NOT FOUND — skipped")
            continue

        any_found = True
        result = patch_script(path)
        print(f"  {script_name:<40} {result}")

    if not any_found:
        print("\n  [ERROR] No target scripts found in:", script_dir)
        print("  Place this patcher in the same directory as your ACI scripts.")
        sys.exit(1)

    print()
    print("Next step:")
    print("  Add select_datacenters() to aci_port_utils.py by appending")
    print("  the contents of aci_port_utils_addition.py before the")
    print("  'if __name__ == \"__main__\":' block at the bottom.")
    print()
    print("  Or run: python3 patch_port_utils.py")
    print()


if __name__ == "__main__":
    main()
