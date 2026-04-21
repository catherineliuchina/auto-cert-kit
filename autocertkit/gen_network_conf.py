#!/usr/bin/env python3
# Copyright (c) 2005-2022 Citrix Systems Inc.
# Copyright (c) 2023 Cloud Software Group, Inc.
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
from common import *

# Constants
ACK_DIR = "/opt/xensource/packages/files/auto-cert-kit"

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

def ask_multi_select(message, choices, min_select=2, allow_empty=False, note=None):
    """Prompt user to select multiple items from a list."""
    while True:
        if allow_empty:
            print("\n%s (comma-separated, Enter to skip)" % message)
        else:
            print("\n%s (min %d, comma-separated)" % (message, min_select))
        if note:
            print(note)
        for i, choice in enumerate(choices, 1):
            print("  %d. %s" % (i, choice))
        print("  q. Exit")

        try:
            value = input("Select: ").strip().lower()
            if value in ('q', 'quit', 'exit'):
                return None
            if not value:
                if allow_empty:
                    return []
                print("Select at least %d valid options." % min_select)
                continue

            indices = [int(x.strip()) - 1 for x in value.split(",")]
            if all(0 <= i < len(choices) for i in indices) and len(indices) >= min_select:
                return indices
            if not allow_empty:
                print("Select at least %d valid options." % min_select)
        except Exception:
            print("Invalid input.")

def ask_sriov_config(nic_label=None):
    """Prompt for SR-IOV VF configuration."""
    if nic_label:
        print("\nConfigure VF for %s:" % nic_label)
    print("Leave empty to use driver already present in Droid VM image.")
    print("Fill these only when ACK should install a custom VF driver package into Droid VM.")

    driver = ask_input("VF driver name (e.g igbvf)", "")
    if driver is None:
        return None

    pkg = ask_input("VF driver pkg filename (e.g igbvf-2.3.9.6-1.x86_64.rpm)", "")
    if pkg is None:
        return None
    if pkg and not os.path.exists(os.path.join(ACK_DIR, pkg)):
        print("Warning: %s not found." % os.path.join(ACK_DIR, pkg))
        if not ask_yes_no("Continue?", False):
            return None

    if not driver and not pkg:
        print("Info: ACK will use the driver already available in Droid VM.")

    max_vf = ask_input("Max VF (e.g 8)", "")
    if max_vf is None:
        return None

    return {
        'driver': driver,
        'pkg': pkg,
        'max_vf': max_vf
    }

def cancel_operation():
    """Exit with cancellation message."""
    sys.exit("\nCancelled.")

def run_command(cmd):
    """Execute a shell command and return stdout if successful."""
    result = make_local_call(cmd, shell=True, logging=False)
    return result['stdout'] if result['returncode'] == 0 else ""

def parse_xe_records(output):
    """Parse xe --multiple output into a list of dict records."""
    records = []
    current = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        key = key.split('(', 1)[0].strip()
        current[key] = value.strip()

    if current:
        records.append(current)
    return records

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

def write_config_file(output_path, config):
    """Write generated config to disk."""
    with open(output_path, 'w') as config_file:
        config_file.write(generate_config(config))
    print("Generated: %s" % output_path)

def create_base_config():
    """Create base config structure."""
    return {'xs_ver': get_xs_version(), 'nics': []}

def load_available_nics():
    """Load NICs and return those available on all hosts."""
    all_nics = get_nics()
    if not all_nics:
        sys.exit("Error: No NICs found.")

    available_nics = [n for n in all_nics if n['on_all_hosts']]
    return all_nics, available_nics

def apply_sriov_config_to_nics(nics, sriov_cfg, warn_on_skip=False):
    """Apply a shared SR-IOV config to all master-capable NICs."""
    for nic in nics:
        if nic['sriov_master']:
            nic['sriov_cfg'] = sriov_cfg.copy()
        elif warn_on_skip:
            print("Warning: %s does not support SR-IOV on master, skipping" % nic['dev'])

def get_nics():
    """Get NICs available on all hosts with SR-IOV status."""
    master = run_command("xe pool-list params=master --minimal")
    hosts_str = run_command("xe host-list params=uuid --minimal")
    hosts = [h.strip() for h in hosts_str.split(",") if h.strip()]
    slaves = [h for h in hosts if h != master]

    master_pifs_output = run_command(
        "xe pif-list physical=true host-uuid=%s "
        "params=device,capabilities,vendor-name,device-name,management "
        "--multiple" % master
    )
    if not master_pifs_output:
        return []

    nics = {}
    for rec in parse_xe_records(master_pifs_output):
        device = rec.get('device', '')
        if not device or device in nics:
            continue
        capabilities = rec.get('capabilities', '')
        has_sriov = 'sriov' in capabilities.lower() if capabilities else False
        nics[device] = {
            'dev': device,
            'vendor': rec.get('vendor-name', '') or "Unknown",
            'model': rec.get('device-name', '') or "Unknown",
            'sriov_master': has_sriov,
            'sriov_all': has_sriov,
            'on_all_hosts': True,
            'management': rec.get('management', '').lower() == 'true'
        }

    # Check slave hosts for matching NICs and SR-IOV capability
    for slave in slaves:
        slave_pifs_output = run_command(
            "xe pif-list physical=true host-uuid=%s params=device,capabilities --multiple" % slave
        )
        slave_devices = {}
        for rec in parse_xe_records(slave_pifs_output):
            device = rec.get('device', '')
            if device:
                capabilities = rec.get('capabilities', '')
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
        lines.append("network_id = %s" % nic.get('network_id', 0))

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

    # Generate static IP sections for each network_id
    static_configs = config.get('static_configs', {})
    for net_id in sorted(static_configs.keys()):
        static_cfg = static_configs[net_id]
        lines.append("[static_%s_0]" % net_id)
        lines.append("ip_start = %s" % static_cfg['start'])
        lines.append("ip_end = %s" % static_cfg['end'])
        lines.append("netmask = %s" % static_cfg['mask'])
        lines.append("gw = %s" % static_cfg['gw'])
        lines.append("")

    # Legacy support for single static config
    if 'static' in config and 'static_configs' not in config:
        static_config = config['static']
        lines.append("[static_0_0]")
        lines.append("ip_start = %s" % static_config['start'])
        lines.append("ip_end = %s" % static_config['end'])
        lines.append("netmask = %s" % static_config['mask'])
        lines.append("gw = %s" % static_config['gw'])
        lines.append("")

    return "\n".join(lines)

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

def group_nics_by_network_id(nics):
    """Group NIC records by network_id."""
    network_groups = {}
    for nic in nics:
        net_id = nic.get('network_id', 0)
        if net_id not in network_groups:
            network_groups[net_id] = []
        network_groups[net_id].append(nic)
    return network_groups

def iter_network_groups(nics):
    """Yield sorted (network_id, nic_list) pairs."""
    network_groups = group_nics_by_network_id(nics)
    for net_id in sorted(network_groups.keys()):
        yield net_id, network_groups[net_id]

def collect_required_fields(prompts):
    """Collect required fields from prompts, cancel on empty or exit."""
    values = {}
    for key, prompt, example in prompts:
        value = ask_input("%s (%s)" % (prompt, example))
        if value is None:
            cancel_operation()
        if not value:
            print("Error: %s is required." % prompt)
            cancel_operation()
        values[key] = value
    return values

def configure_static_ips(config):
    """Prompt for static IP configuration per network_id."""
    config['static_configs'] = {}

    for net_id, nics_in_group in iter_network_groups(config['nics']):
        nic_names = ", ".join(n['dev'] for n in nics_in_group)
        print("\nStatic IP for network_%d (%s):" % (net_id, nic_names))

        prompts = [
            ('start', 'IP range start', 'e.g. 192.168.%d.2' % net_id),
            ('end', 'IP range end', 'e.g. 192.168.%d.10' % net_id),
            ('mask', 'Netmask', 'e.g. 255.255.255.0'),
            ('gw', 'Gateway', 'e.g. 192.168.%d.1' % net_id)
        ]
        config['static_configs'][net_id] = collect_required_fields(prompts)

def configure_sriov(config):
    """Prompt for SR-IOV settings on eligible NICs."""
    sriov_nics = [n for n in config['nics'] if n['sriov_master']]
    if not sriov_nics:
        return

    sriov_devs = ", ".join(n['dev'] for n in sriov_nics)
    print("\nSR-IOV capable NICs: %s" % sriov_devs)
    test_sriov = ask_yes_no("Do you want to test SR-IOV function?")
    if test_sriov is None:
        cancel_operation()
    if not test_sriov:
        return

    models = set((n['vendor'], n['model']) for n in sriov_nics)
    same_model = len(models) == 1

    if same_model and len(sriov_nics) > 1:
        print("\nAll SR-IOV NICs are same model, using unified VF config.")
        sriov_cfg = ask_sriov_config()
        if sriov_cfg is None:
            cancel_operation()
        apply_sriov_config_to_nics(sriov_nics, sriov_cfg)
        return

    if len(sriov_nics) > 1:
        print("\nSR-IOV NICs have different models, configure separately.")

    for nic in sriov_nics:
        label = "%s (%s)" % (nic['dev'], nic['model'])
        sriov_cfg = ask_sriov_config("", label)
        if sriov_cfg is None:
            cancel_operation()
        nic['sriov_cfg'] = sriov_cfg

def configure_vlans(config):
    """Prompt for VLAN IDs per network_id."""
    print("\nVLAN configuration (optional, Enter to skip)")
    print("Note: NICs in same network_id must use same VLAN IDs.")

    for net_id, nics_in_group in iter_network_groups(config['nics']):
        nic_names = ", ".join(n['dev'] for n in nics_in_group)
        vlan_ids = ask_input("VLAN IDs for network_%d (%s), e.g. 200 or 100,200" % (net_id, nic_names), "")
        if vlan_ids is None:
            cancel_operation()
        if vlan_ids:
            for nic in nics_in_group:
                nic['vlan_ids'] = vlan_ids

def print_config_summary(config, header=False, sriov_label="SR-IOV Test"):
    """Print a summary of the selected configuration."""
    has_sriov = any(n.get('sriov_cfg') for n in config['nics'])
    ip_mode = "Static IP" if config.get('static_configs') else "DHCP"

    if header:
        print("\n" + "=" * 50)
        print("Summary:")

    for net_id, nics_in_group in iter_network_groups(config['nics']):
        nic_names = ", ".join(n['dev'] for n in nics_in_group)
        prefix = "  " if header else ""
        print("%snetwork_%d: %s" % (prefix, net_id, nic_names))
        vlan_ids = ""
        for nic in nics_in_group:
            if nic.get('vlan_ids'):
                vlan_ids = nic.get('vlan_ids')
                break
        print("%sVLAN IDs: %s" % (prefix, vlan_ids if vlan_ids else "Not set"))

    print("IP Mode: %s" % ip_mode)
    if config.get('static_configs'):
        for net_id in sorted(config['static_configs'].keys()):
            s = config['static_configs'][net_id]
            print("  network_%d static: %s - %s / %s, gw %s" % (
                net_id, s['start'], s['end'], s['mask'], s['gw']))

    print("%s: %s" % (sriov_label, "Yes" if has_sriov else "No"))
    if has_sriov:
        # Group NICs with identical SR-IOV config
        groups = {}
        for nic in config['nics']:
            cfg = nic.get('sriov_cfg')
            if cfg:
                key = (cfg['driver'], cfg['pkg'], cfg['max_vf'])
                groups.setdefault(key, []).append(nic['dev'])
        for (driver, pkg, max_vf), devs in groups.items():
            print("  %s: driver=%s pkg=%s max_vf=%s" % (
                ", ".join(devs),
                driver or "(empty)",
                pkg or "(empty)",
                max_vf or "(empty)"))

    if header:
        print("=" * 50)

def select_nics_with_network_id(available_nics, next_network_id, is_first_group=True):
    """Select NICs and assign network_id. Returns (selected_nics, next_network_id) or (None, None) on cancel."""
    if not available_nics:
        return [], next_network_id
    
    nics = sorted(available_nics, key=lambda x: x['dev'])
    
    if is_first_group:
        if len(nics) < 2:
            print("\nError: ACK requires at least 2 NICs. Only %d available." % len(nics))
            return None, None
        min_select = 2
        allow_empty = False
        msg = "Select NICs for network_%d (ACK requires 2+):" % next_network_id
        note = "Requirement: NICs selected for network_%d must be on the SAME Layer 2 network." % next_network_id
    else:
        min_select = 1
        allow_empty = True
        msg = "Select NICs for network_%d (Enter to skip):" % next_network_id
        note = None
    
    while True:
        choices = [format_nic_display(n) for n in nics]
        
        if is_first_group and len(nics) == 2:
            print("\nDetected 2 NICs (both selected for network_%d):" % next_network_id)
            for nic in nics:
                print("  - %s" % format_nic_display(nic))
            indices = [0, 1]
        else:
            indices = ask_multi_select(msg, choices, min_select, allow_empty, note)
            if indices is None:
                return None, None
            if not indices and allow_empty:
                return [], next_network_id
        
        selected = [nics[i].copy() for i in indices]
        
        print("\nSelected NICs: %s" % ", ".join(n['dev'] for n in selected))
        same_l2 = ask_yes_no("Are selected NICs on the same Layer 2 network?", True)
        if same_l2 is None:
            return None, None
        if not same_l2:
            print("Please re-select NICs.")
            continue
        
        # Assign network_id with option to modify
        net_id_input = ask_input("Network ID", str(next_network_id))
        if net_id_input is None:
            return None, None
        try:
            net_id = int(net_id_input)
        except ValueError:
            print("Invalid network_id, using %d" % next_network_id)
            net_id = next_network_id
        
        for nic in selected:
            nic['network_id'] = net_id
        
        return selected, max(net_id + 1, next_network_id + 1)


def assign_nics_to_network_groups(config, available_nics):
    """Interactive NIC assignment loop for one or more network_id groups."""
    remaining_nics = available_nics[:]
    next_network_id = 0
    is_first_group = True

    while True:
        selected_nics, next_network_id = select_nics_with_network_id(
            remaining_nics, next_network_id, is_first_group
        )
        if selected_nics is None:
            cancel_operation()

        # Allow skipping additional groups after the first assignment.
        if not selected_nics and not is_first_group:
            break

        config['nics'].extend(selected_nics)

        selected_devs = set(nic['dev'] for nic in selected_nics)
        remaining_nics = [nic for nic in remaining_nics if nic['dev'] not in selected_devs]
        is_first_group = False

        if not remaining_nics:
            print("\nAll NICs have been assigned.")
            break

        add_more = ask_yes_no("\nAdd more NICs with different network_id?", False)
        if add_more is None:
            cancel_operation()
        if not add_more:
            break


def main():
    """Main entry point for network configuration tool."""
    print("=" * 50)
    print("  ACK Network Configuration (q to exit)")
    print("=" * 50)

    config = create_base_config()
    print("\nXenServer: %s" % config['xs_ver'])

    all_nics, available_nics = load_available_nics()

    if not available_nics:
        print("\nError: No NICs available on ALL pool hosts.")
        for nic in all_nics:
            print("  - %s (%s %s)" % (nic['dev'], nic['vendor'], nic['model']))
        sys.exit(1)

    if len(available_nics) < 2:
        sys.exit("\nError: ACK requires at least 2 NICs. Only %d available." % len(available_nics))

    # NIC selection loop - support multiple network_id groups
    assign_nics_to_network_groups(config, available_nics)

    # DHCP/Static IP configuration
    dhcp_mode = run_command("xe pif-list params=IP-configuration-mode --minimal")
    dhcp_available = 'DHCP' in dhcp_mode.upper()
    print("\nDHCP: %s" % ('available' if dhcp_available else 'not detected'))
    use_dhcp = dhcp_available and ask_yes_no("Use DHCP?", True)
    if use_dhcp is None:
        cancel_operation()
    if not use_dhcp:
        configure_static_ips(config)

    configure_sriov(config)

    configure_vlans(config)

    print_config_summary(config, header=True)
    if not ask_yes_no("Generate network.conf?"):
        cancel_operation()
    output_path = "/opt/xensource/packages/files/auto-cert-kit/network.conf"
    write_config_file(output_path, config)


if __name__ == "__main__":
    main()
