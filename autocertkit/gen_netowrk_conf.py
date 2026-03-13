#!/usr/bin/env python3

# Copyright (c) 2026. Citrix Systems, Inc. All Rights Reserved. Confidential & Proprietary
#
# Redistribution and use in source and binary forms,
# with or without modification, are permitted provided
# that the following conditions are met:
#
# *   Redistributions of source code must retain the above
#     copyright notice, this list of conditions and the
#     following disclaimer.
# *   Redistributions in binary form must reproduce the above
#     copyright notice, this list of conditions and the
#     following disclaimer in the documentation and/or other
#     materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND
# CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES,
# INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.

"""Interactive network.conf configuration tool for XenServer ACK."""

import os
import sys
import subprocess
import argparse


def run_command(cmd):
    """Execute a shell command and return stdout if successful."""
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()
    if proc.returncode == 0:
        if isinstance(stdout, bytes):
            stdout = stdout.decode('utf-8', errors='replace')
        return stdout.strip()
    return ""

def get_xs_version():
    """Get XenServer version string from inventory file."""
    try:
        with open('/etc/xensource-inventory') as f:
            for line in f:
                if line.startswith('PRODUCT_VERSION='):
                    return line.split('=')[1].strip().strip("'\"")
    except Exception as e:
        print("Error: Failed to read XenServer version: %s" % e)
        sys.exit(1)
    return 'unknown'

def ask_input(message, default=None):
    """Prompt user for input with optional default value."""
    try:
        prompt = "%s [%s]: " % (message, default) if default else "%s: " % message
        value = input(prompt).strip()
        if value.lower() in ('q', 'quit', 'exit'):
            return None
        return value or default
    except Exception:
        return None

def ask_yes_no(message, default=True):
    """Prompt user for yes/no confirmation."""
    try:
        hint = "Y/n" if default else "y/N"
        value = input("%s [%s]: " % (message, hint)).strip().lower()
        if value in ('q', 'quit', 'exit'):
            return None
        if not value:
            return default
        return value in ('y', 'yes')
    except Exception:
        return None

def ask_multi_select(message, choices, min_select=2):
    """Prompt user to select multiple items from a list."""
    print("\n%s (min %d, comma-separated)" % (message, min_select))
    for i, choice in enumerate(choices, 1):
        print("  %d. %s" % (i, choice))
    print("  q. Exit")
    try:
        value = input("Select: ").strip().lower()
        if value in ('q', 'quit', 'exit'):
            return None
        indices = [int(x.strip()) - 1 for x in value.split(",")]
        if all(0 <= i < len(choices) for i in indices) and len(indices) >= min_select:
            return indices
        print("Select at least %d valid options." % min_select)
        return ask_multi_select(message, choices, min_select)
    except Exception:
        print("Invalid input.")
        return ask_multi_select(message, choices, min_select)

def get_pif_param(uuid, param_name):
    """Get a PIF parameter value by UUID."""
    return run_command("xe pif-param-get uuid=%s param-name=%s" % (uuid, param_name))

def get_nics():
    """Get NICs available on all hosts with SR-IOV status."""
    master = run_command("xe pool-list params=master --minimal")
    hosts_str = run_command("xe host-list params=uuid --minimal")
    hosts = [h.strip() for h in hosts_str.split(",") if h.strip()]
    slaves = [h for h in hosts if h != master]

    pif_uuids = run_command(
        "xe pif-list physical=true host-uuid=%s params=uuid --minimal" % master
    )
    if not pif_uuids:
        return []

    nics = {}
    for uuid in [u.strip() for u in pif_uuids.split(",") if u.strip()]:
        device = get_pif_param(uuid, "device")
        if not device or device in nics:
            continue
        capabilities = get_pif_param(uuid, "capabilities")
        has_sriov = 'sriov' in capabilities.lower() if capabilities else False
        nics[device] = {
            'dev': device,
            'vendor': get_pif_param(uuid, "vendor-name") or "Unknown",
            'model': get_pif_param(uuid, "device-name") or "Unknown",
            'pci_id': get_pif_param(uuid, "pci-bus-path") or "",
            'sriov_master': has_sriov,
            'sriov_all': has_sriov,
            'on_all_hosts': True,
            'management': get_pif_param(uuid, "management").lower() == 'true',
            'network': get_pif_param(uuid, "network-uuid")
        }

    # Check slave hosts for matching NICs and SR-IOV capability
    for slave in slaves:
        slave_pifs = run_command(
            "xe pif-list physical=true host-uuid=%s params=uuid --minimal" % slave
        )
        slave_devices = {}
        for uuid in [u.strip() for u in slave_pifs.split(",") if u.strip()]:
            device = get_pif_param(uuid, "device")
            if device:
                capabilities = get_pif_param(uuid, "capabilities")
                slave_devices[device] = 'sriov' in capabilities.lower() if capabilities else False

        for device in nics:
            if device not in slave_devices:
                nics[device]['on_all_hosts'] = False
                nics[device]['sriov_all'] = False
            elif not slave_devices[device]:
                nics[device]['sriov_all'] = False

    return list(nics.values())

def generate_config(config):
    """Generate network.conf file content."""
    xs_version = config.get('xs_ver')
    lines = [
        "# Auto-generated network.conf",
        "# XenServer: %s" % xs_version,
        ""
    ]

    for nic in config['nics']:
        lines.append("[%s]" % nic['dev'])
        lines.append("network_id = 0")

        vlan_ids = nic.get('vlan_ids', '')
        if vlan_ids:
            lines.append("vlan_ids = %s" % vlan_ids)

        sriov_config = nic.get('sriov_cfg')
        if sriov_config:
            lines.append("vf_driver_name = %s" % sriov_config['driver'])
            lines.append("vf_driver_pkg = %s" % sriov_config['pkg'])
            lines.append("max_vf_num = %s" % sriov_config['max_vf'])
        else:
            lines.append("# vf_driver_name =")
            lines.append("# vf_driver_pkg =")
            lines.append("# max_vf_num =")
        lines.append("")

    static_config = config.get('static')
    if static_config:
        lines.append("[static_0]")
        lines.append("ip_start = %s" % static_config['start'])
        lines.append("ip_end = %s" % static_config['end'])
        lines.append("netmask = %s" % static_config['mask'])
        lines.append("gw = %s" % static_config['gw'])
        lines.append("")

    return "\n".join(lines)

def cancel_operation():
    """Exit with cancellation message."""
    sys.exit("\nCancelled.")

def format_nic_display(nic):
    """Format NIC info for display with tags."""
    tags = []
    if nic['management']:
        tags.append("MGMT")
    if nic['sriov_all']:
        tags.append("SR-IOV")
    elif nic['sriov_master']:
        tags.append("SR-IOV:master")
    tag_str = " [%s]" % ", ".join(tags) if tags else ""
    return "%s - %s %s%s" % (nic['dev'], nic['vendor'], nic['model'], tag_str)

def format_nic_summary(nic):
    """Format NIC info for summary display."""
    parts = [nic['dev']]
    if nic.get('sriov_cfg'):
        parts.append("+SR-IOV")
    if nic.get('vlan_ids'):
        parts.append("VLAN:%s" % nic['vlan_ids'])
    return " ".join(parts)

def main():
    """Main entry point for network configuration tool."""
    print("=" * 50)
    print("  ACK Network Configuration (q to exit)")
    print("=" * 50)

    config = {'xs_ver': get_xs_version()}
    print("\nXenServer: %s" % config['xs_ver'])

    all_nics = get_nics()
    if not all_nics:
        sys.exit("Error: No NICs found. Run on Dom0.")

    # Filter to NICs available on all hosts and sort by device name
    nics = sorted([n for n in all_nics if n['on_all_hosts']], key=lambda x: x['dev'])
    if not nics:
        print("\nError: No NICs available on ALL pool hosts.")
        for nic in all_nics:
            print("  - %s (%s %s)" % (nic['dev'], nic['vendor'], nic['model']))
        sys.exit(1)

    if len(nics) < 2:
        sys.exit("\nError: ACK requires at least 2 NICs. Only %d available." % len(nics))

    # NIC selection loop
    while True:
        if len(nics) == 2:
            print("\nDetected 2 NICs (both selected):")
            for nic in nics:
                print("  - %s" % format_nic_display(nic))
            config['nics'] = [n.copy() for n in nics]
        else:
            choices = [format_nic_display(n) for n in nics]
            indices = ask_multi_select("Select NICs (ACK requires 2+):", choices)
            if indices is None:
                cancel_operation()
            config['nics'] = [nics[i].copy() for i in indices]

        print("\nNote: NICs should be on same network segment.")
        proceed = ask_yes_no("Proceed?")
        if proceed is None:
            cancel_operation()
        if proceed:
            break

    # DHCP/Static IP configuration
    dhcp_mode = run_command("xe pif-list params=IP-configuration-mode --minimal")
    dhcp_available = 'DHCP' in dhcp_mode.upper()
    print("\nDHCP: %s" % ('available' if dhcp_available else 'not detected'))

    use_dhcp = dhcp_available and ask_yes_no("Use DHCP?", True)
    if use_dhcp is None:
        cancel_operation()

    if not use_dhcp:
        print("\nStatic IP configuration (enter your values):")
        config['static'] = {}
        prompts = [
            ('start', 'IP range start', 'e.g. 192.168.0.2'),
            ('end', 'IP range end', 'e.g. 192.168.0.10'),
            ('mask', 'Netmask', 'e.g. 255.255.255.0'),
            ('gw', 'Gateway', 'e.g. 192.168.0.1')
        ]
        for key, prompt, example in prompts:
            value = ask_input("%s (%s)" % (prompt, example))
            if value is None:
                cancel_operation()
            if not value:
                print("Error: %s is required." % prompt)
                cancel_operation()
            config['static'][key] = value

    # SR-IOV configuration - ask once, then configure per-NIC if yes
    sriov_nics = [n for n in config['nics'] if n['sriov_all']]
    if sriov_nics:
        sriov_devs = ", ".join(n['dev'] for n in sriov_nics)
        print("\nSR-IOV capable NICs: %s" % sriov_devs)
        test_sriov = ask_yes_no("Do you want to test SR-IOV function?")
        if test_sriov is None:
            cancel_operation()
        if test_sriov:
            # Check if all SR-IOV NICs have same model and PCI ID (same card type)
            models = set((n['model'], n['pci_id'].split('/')[0] if n['pci_id'] else '') for n in sriov_nics)
            same_model = len(models) == 1
            
            if same_model and len(sriov_nics) > 1:
                # Same model - configure once for all
                print("\nAll SR-IOV NICs are same model, using unified VF config.")
                driver = ask_input("VF driver (e.g. igbvf)")
                if driver is None:
                    cancel_operation()
                pkg = ask_input("VF driver pkg (or empty)", "")
                if pkg is None:
                    cancel_operation()
                if pkg and not os.path.exists(pkg):
                    print("Warning: %s not found." % pkg)
                    if not ask_yes_no("Continue?", False):
                        cancel_operation()
                max_vf = ask_input("Max VF", "8")
                if max_vf is None:
                    cancel_operation()
                sriov_cfg = {'driver': driver, 'pkg': pkg, 'max_vf': max_vf}
                for nic in sriov_nics:
                    nic['sriov_cfg'] = sriov_cfg.copy()
            else:
                # Different models - configure each separately
                if len(sriov_nics) > 1:
                    print("\nSR-IOV NICs have different models, configure separately.")
                for nic in sriov_nics:
                    print("\nConfigure VF for %s (%s):" % (nic['dev'], nic['model']))
                    driver = ask_input("VF driver (e.g. igbvf)")
                    if driver is None:
                        cancel_operation()
                    pkg = ask_input("VF driver pkg (or empty)", "")
                    if pkg is None:
                        cancel_operation()
                    if pkg and not os.path.exists(pkg):
                        print("Warning: %s not found." % pkg)
                        if not ask_yes_no("Continue?", False):
                            cancel_operation()
                    max_vf = ask_input("Max VF", "8")
                    if max_vf is None:
                        cancel_operation()
                    nic['sriov_cfg'] = {
                        'driver': driver,
                        'pkg': pkg,
                        'max_vf': max_vf
                    }

    # VLAN configuration - group by network_id
    print("\nVLAN configuration (optional, Enter to skip)")
    print("Note: NICs in same network_id must use same VLAN IDs.")
    
    # Group NICs by network_id
    network_groups = {}
    for nic in config['nics']:
        net_id = nic.get('network_id', 0)
        if net_id not in network_groups:
            network_groups[net_id] = []
        network_groups[net_id].append(nic)
    
    # Configure VLAN for each network group
    for net_id in sorted(network_groups.keys()):
        nics_in_group = network_groups[net_id]
        nic_names = ", ".join(n['dev'] for n in nics_in_group)
        vlan_ids = ask_input("VLAN IDs for network_%d (%s), e.g. 200 or 100,200" % (net_id, nic_names), "")
        if vlan_ids is None:
            cancel_operation()
        if vlan_ids:
            for nic in nics_in_group:
                nic['vlan_ids'] = vlan_ids

    # Summary
    nic_names = ", ".join(n['dev'] for n in config['nics'])
    has_sriov = any(n.get('sriov_cfg') for n in config['nics'])
    ip_mode = "Static IP" if config.get('static') else "DHCP"
    sriov_test = "Yes" if has_sriov else "No"
    print("\n" + "=" * 50)
    print("Summary:")
    print("Tested Network: %s" % nic_names)
    print("IP Mode: %s" % ip_mode)
    print("SR-IOV Test: %s" % sriov_test)
    print("=" * 50)

    if not ask_yes_no("Generate network.conf?"):
        cancel_operation()

    with open("network.conf", 'w') as f:
        f.write(generate_config(config))
    print("Generated: network.conf")


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Generate network.conf for XenServer ACK',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  Interactive mode:
    ./gen_netowrk_conf.py

  Silent mode with DHCP:
    ./gen_netowrk_conf.py --silent --nics eth1,eth2

  Silent mode with Static IP:
    ./gen_netowrk_conf.py --silent --nics eth1,eth2 --static \\
        --ip-start 192.168.0.2 --ip-end 192.168.0.10 \\
        --netmask 255.255.255.0 --gateway 192.168.0.1

  Silent mode with SR-IOV:
    ./gen_netowrk_conf.py --silent --nics eth1,eth2 --sriov \\
        --vf-driver ixgbevf --max-vf 8

  Silent mode with VLAN:
    ./gen_netowrk_conf.py --silent --nics eth1,eth2 --vlan 200
''')
    parser.add_argument('--silent', '-s', action='store_true',
                        help='Run in silent mode (no interactive prompts)')
    parser.add_argument('--nics', '-n', type=str,
                        help='Comma-separated list of NICs to test (e.g. eth1,eth2)')
    parser.add_argument('--static', action='store_true',
                        help='Use static IP instead of DHCP')
    parser.add_argument('--ip-start', type=str,
                        help='Static IP range start (e.g. 192.168.0.2)')
    parser.add_argument('--ip-end', type=str,
                        help='Static IP range end (e.g. 192.168.0.10)')
    parser.add_argument('--netmask', type=str,
                        help='Static IP netmask (e.g. 255.255.255.0)')
    parser.add_argument('--gateway', type=str,
                        help='Static IP gateway (e.g. 192.168.0.1)')
    parser.add_argument('--sriov', action='store_true',
                        help='Enable SR-IOV testing')
    parser.add_argument('--vf-driver', type=str,
                        help='SR-IOV VF driver name (e.g. ixgbevf)')
    parser.add_argument('--vf-pkg', type=str, default='',
                        help='SR-IOV VF driver package name')
    parser.add_argument('--max-vf', type=str, default='8',
                        help='Maximum number of VFs to test (default: 8)')
    parser.add_argument('--vlan', type=str,
                        help='VLAN ID(s) for testing (e.g. 200 or 100,200)')
    parser.add_argument('--output', '-o', type=str, default='network.conf',
                        help='Output file path (default: network.conf)')
    return parser.parse_args()


def silent_mode(args):
    """Run in silent mode with command line arguments."""
    # Validate required arguments
    if not args.nics:
        sys.exit("Error: --nics is required in silent mode")
    
    nic_names = [n.strip() for n in args.nics.split(',') if n.strip()]
    if len(nic_names) < 2:
        sys.exit("Error: At least 2 NICs are required")
    
    # Validate static IP arguments
    if args.static:
        if not all([args.ip_start, args.ip_end, args.netmask, args.gateway]):
            sys.exit("Error: --static requires --ip-start, --ip-end, --netmask, --gateway")
    
    # Validate SR-IOV arguments
    if args.sriov and not args.vf_driver:
        sys.exit("Error: --sriov requires --vf-driver")
    
    # Get available NICs
    all_nics = get_nics()
    if not all_nics:
        sys.exit("Error: No NICs found. Run on Dom0.")
    
    available_nics = {n['dev']: n for n in all_nics if n['on_all_hosts']}
    
    # Validate requested NICs exist
    config = {'xs_ver': get_xs_version(), 'nics': []}
    for nic_name in nic_names:
        if nic_name not in available_nics:
            available = ', '.join(sorted(available_nics.keys()))
            sys.exit("Error: NIC '%s' not found. Available: %s" % (nic_name, available))
        config['nics'].append(available_nics[nic_name].copy())
    
    # Static IP configuration
    if args.static:
        config['static'] = {
            'start': args.ip_start,
            'end': args.ip_end,
            'mask': args.netmask,
            'gw': args.gateway
        }
    
    # SR-IOV configuration
    if args.sriov:
        sriov_cfg = {
            'driver': args.vf_driver,
            'pkg': args.vf_pkg or '',
            'max_vf': args.max_vf
        }
        for nic in config['nics']:
            if nic['sriov_all']:
                nic['sriov_cfg'] = sriov_cfg.copy()
            else:
                print("Warning: %s does not support SR-IOV, skipping" % nic['dev'])
    
    # VLAN configuration
    if args.vlan:
        for nic in config['nics']:
            nic['vlan_ids'] = args.vlan
    
    # Generate config file
    with open(args.output, 'w') as f:
        f.write(generate_config(config))
    print("Generated: %s" % args.output)
    
    # Print summary
    nic_names_str = ", ".join(n['dev'] for n in config['nics'])
    has_sriov = any(n.get('sriov_cfg') for n in config['nics'])
    ip_mode = "Static IP" if config.get('static') else "DHCP"
    print("NICs: %s" % nic_names_str)
    print("IP Mode: %s" % ip_mode)
    print("SR-IOV: %s" % ("Yes" if has_sriov else "No"))
    if args.vlan:
        print("VLAN: %s" % args.vlan)


if __name__ == "__main__":
    args = parse_args()
    if args.silent:
        silent_mode(args)
    else:
        main()
