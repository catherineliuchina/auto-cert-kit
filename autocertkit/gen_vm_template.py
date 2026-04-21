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
"""
Droid VM Template Preparation Script (Rocky Linux)

Usage:
    python gen_vm_template.py <VM_IP>

This script performs 6 steps:
1. Pre-checks: Verify setup-scripts dir exists and VM is SSH-reachable
2. Copy setup-scripts to the VM via SSH/SCP
3. Run init-run.sh inside the VM (installs dependencies, configures firewall, etc.)
4. Reboot the VM and wait for SSH to come back
5. Find the VM UUID by IP (requires XenTools installed in VM)
6. Shut down and export the VM as vpx-dlvm.xva

Note: The generated XVA file must be manually copied to slave hosts.
"""

import os
import sys
import time
import uuid
import socket
import logging
import argparse
from common import *

set_logger(logging.getLogger(__name__))

# Constants
ACK_DIR = "/opt/xensource/packages/files/auto-cert-kit"
SETUP_SCRIPTS_SRC = os.path.join(ACK_DIR, "setup-scripts")
REMOTE_SETUP_DIR = "/root/setup-scripts"
XVA_NAME = "vpx-dlvm.xva"
XVA_PATH = os.path.join(ACK_DIR, XVA_NAME)

DEFAULT_PASSWORD = "xenserver"
DEFAULT_USERNAME = "root"


def get_vm_uuid_by_ip(vm_ip):
    """Find VM UUID by its IP address using xe vm-list."""
    result = make_local_call(["xe", "vm-list", "params=uuid,networks", "--multiple"], logging=False)
    if result['returncode'] != 0:
        return None
    
    lines = result['stdout'].strip().split('\n')
    current_uuid = None
    current_networks = None
    
    for line in lines:
        line = line.strip()
        if line.startswith("uuid"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                current_uuid = parts[1].strip()
        elif line.startswith("networks"):
            parts = line.split(":", 1)
            if len(parts) == 2:
                current_networks = parts[1].strip()
        elif line == "" and current_uuid and current_networks:
            if vm_ip in current_networks:
                return current_uuid
            current_uuid = None
            current_networks = None
    
    if current_uuid and current_networks and vm_ip in current_networks:
        return current_uuid
    
    return None


def _tcp_reachable(ip, port=22, timeout=3):
    """Return True if TCP port is accepting connections."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_ssh(vm_ip, vm_pass, max_tries=60, interval=5):
    """Wait for SSH to become available on the VM.
    """
    print("Waiting for SSH on %s..." % vm_ip)
    for i in range(max_tries):
        if _tcp_reachable(vm_ip):
            result = ssh_command(vm_ip, DEFAULT_USERNAME, vm_pass, "echo ok",
                                 attempts=1, timeout=30)
            if result['returncode'] == 0:
                print("SSH is available on %s" % vm_ip)
                return True
        time.sleep(interval)
    raise RuntimeError("Timeout waiting for SSH on %s" % vm_ip)


def main():
    parser = argparse.ArgumentParser(description="Prepare Droid VM template for ACK (Rocky Linux)")
    parser.add_argument("vm_ip", help="IP address of the VM")
    args = parser.parse_args()

    vm_ip = args.vm_ip
    vm_pass = DEFAULT_PASSWORD

    # Step 1: Pre-checks
    print("[1/7] Pre-checks")
    if os.path.exists(XVA_PATH):
        print("ERROR: %s already exists. Please remove it first." % XVA_PATH)
        sys.exit(1)
    if not os.path.isdir(SETUP_SCRIPTS_SRC):
        raise RuntimeError("Cannot find setup scripts dir: %s" % SETUP_SCRIPTS_SRC)

    try:
        wait_for_ssh(vm_ip, vm_pass, max_tries=10, interval=5)
    except RuntimeError as e:
        print("\n%s" % str(e))
        print("Troubleshooting tips:")
        print("  1. Check if VM is powered on: xe vm-list")
        print("  2. Verify correct IP address for the VM")
        print("  3. Check network/firewall on VM host")
        print("  4. Make sure VM has network configured (DHCP or static IP)")
        sys.exit(1)

    result = ssh_command(vm_ip, DEFAULT_USERNAME, vm_pass,
                         "rpm -q xe-guest-utilities xe-guest-utilities-xenstore",
                         attempts=1, timeout=60)
    if result.get('returncode') != 0:
        print("ERROR: VM prerequisite RPMs are missing: xe-guest-utilities and/or xe-guest-utilities-xenstore")
        if result.get('stdout'):
            print("stdout: %s" % result.get('stdout'))
        if result.get('stderr'):
            print("stderr: %s" % result.get('stderr'))
        sys.exit(1)

    # Step 2: Copy setup-scripts into VM
    print("[2/7] Copy setup-scripts into VM")
    channel = SecureChannel(vm_ip, DEFAULT_USERNAME, vm_pass, timeout=300)
    channel.run_cmd("rm -rf %s" % REMOTE_SETUP_DIR)
    scp_cmd = channel._wrap_cmd(
        "%s -r %s %s@%s:/root/" % (SCP, SETUP_SCRIPTS_SRC, DEFAULT_USERNAME, vm_ip)
    )
    result = make_local_call(scp_cmd, shell=True, timeout=1800)
    if result['returncode'] != 0:
        print("Error copying setup scripts: %s" % result['stderr'])
        sys.exit(1)

    # Step 3: Run Rocky init-run.sh inside VM
    print("[3/7] Run Rocky init-run.sh inside VM")
    # Use longer timeout for init-run.sh (dnf update can take 10-15 minutes)
    channel = SecureChannel(vm_ip, DEFAULT_USERNAME, vm_pass, timeout=1800)
    channel.run_cmd_ext("command -v semanage || dnf install -y policycoreutils-python-utils")
    # Convert CRLF to LF in case scripts were edited on Windows
    channel.run_cmd_ext("sed -i 's/\\r$//' %s/*.sh" % REMOTE_SETUP_DIR)
    result = channel.run_cmd_ext("chmod +x %s/init-run.sh && bash %s/init-run.sh" % (REMOTE_SETUP_DIR, REMOTE_SETUP_DIR))
    if result.get('returncode', 1) == 0:
        print("init-run.sh completed successfully")
    else:
        print("init-run.sh finished with exit code: %s" % result.get('returncode'))
        if result.get('stderr'):
            print("stderr: %s" % result.get('stderr'))
    # Step 4: Reboot VM and wait for SSH
    print("[4/7] Reboot VM and wait for SSH back")
    # Use nohup + background to let SSH exit cleanly before reboot kicks in
    channel.run_cmd("nohup sh -c 'sleep 2; reboot' >/dev/null 2>&1 &")
    time.sleep(10)
    wait_for_ssh(vm_ip, vm_pass)

    # Step 5: Verify VM changes after reboot
    print("[5/7] Verify VM configuration after reboot")
    channel = SecureChannel(vm_ip, DEFAULT_USERNAME, vm_pass, timeout=300)

    # Check startup-ip service
    result = channel.run_cmd_ext("systemctl is-enabled startup-ip.service")
    print("  startup-ip.service enabled: %s" % ("yes" if result.get('returncode') == 0 else "no"))
    if result.get('returncode') != 0:
        print("ERROR: startup-ip.service is not enabled")
        sys.exit(1)

    # Check firewall state
    result = channel.run_cmd_ext("firewall-cmd --state")
    fw_state = result.get('stdout', '').strip()
    print("  firewall state: %s" % fw_state)
    if fw_state != "running":
        print("ERROR: firewall is not running")
        sys.exit(1)

    # Check firewall ports
    result = channel.run_cmd_ext("firewall-cmd --list-ports")
    ports = result.get('stdout', '').strip()
    print("  firewall ports: %s" % ports)
    required_ports = ["4/tcp", "4/udp", "5001/tcp", "5001/udp"]
    for port in required_ports:
        if port not in ports:
            print("ERROR: Required firewall port %s is missing" % port)
            sys.exit(1)

    # Check required packages
    result = channel.run_cmd_ext("rpm -q perl tcpdump")
    print("  packages installed: %s" % ("yes" if result.get('returncode') == 0 else "no"))
    if result.get('returncode') != 0:
        print("ERROR: Required packages (perl, tcpdump) are not installed")
        sys.exit(1)

    # Check perl modules
    result = channel.run_cmd_ext("perl -e 'use List::MoreUtils; use Readonly; print \"perl modules OK\"'")
    print("  perl modules (List::MoreUtils, Readonly): %s" % ("yes" if result.get('returncode') == 0 else "no"))
    if result.get('returncode') != 0:
        print("ERROR: Required perl modules (List::MoreUtils, Readonly) are not installed")
        sys.exit(1)

    # Set pm_freeze_timeout (runtime param, doesn't survive reboot)
    channel.run_cmd_ext("echo 300000 > /sys/power/pm_freeze_timeout")
    result = channel.run_cmd_ext("cat /sys/power/pm_freeze_timeout")
    pm_timeout = result.get('stdout', '').strip()
    print("  pm_freeze_timeout: %s" % pm_timeout)
    if pm_timeout != "300000":
        print("ERROR: pm_freeze_timeout is not 300000 (got: %s)" % pm_timeout)
        sys.exit(1)

    # Step 6: Find VM UUID
    print("[6/7] Find VM UUID")
    vm_uuid = None

    # Try to get VM UUID
    result = ssh_command(vm_ip, DEFAULT_USERNAME, vm_pass, "cat /sys/hypervisor/uuid")
    if result['returncode'] == 0 and result['stdout']:
        for line in result['stdout'].strip().split('\n'):
            try:
                vm_uuid = str(uuid.UUID(line.strip()))
                break
            except ValueError:
                continue
    if not vm_uuid:
        vm_uuid = get_vm_uuid_by_ip(vm_ip)
    if not vm_uuid:
        print("Could not auto-detect VM UUID for IP %s" % vm_ip)
        sys.exit(1)
    print("VM UUID: %s" % vm_uuid)

    # Step 7: Shutdown and export VM
    print("[7/7] Shutdown and export VM as %s" % XVA_NAME)
    result = make_local_call(["xe", "vm-param-get", "uuid=%s" % vm_uuid, "param-name=name-label"], logging=False)
    vm_name = result['stdout'].strip() if result['returncode'] == 0 else "unknown"
    print("Shutting down VM %s (%s)..." % (vm_name, vm_uuid))
    make_local_call(["xe", "vm-shutdown", "uuid=%s" % vm_uuid], logging=False)
    time.sleep(5)
    print("Exporting VM to %s..." % XVA_PATH)
    result = make_local_call(["xe", "vm-export", "vm=%s" % vm_uuid, "filename=%s" % XVA_PATH], logging=True)
    if result['returncode'] != 0:
        print("Export failed: %s" % result['stderr'])
        sys.exit(1)

    print("\n" + "="*60)
    print("SUCCESS: VM template exported to %s" % XVA_PATH)
    print("="*60)


if __name__ == "__main__":
    main()
        