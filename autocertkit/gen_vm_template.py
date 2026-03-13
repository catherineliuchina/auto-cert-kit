#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Droid VM Template Preparation Script (Rocky Linux)

Usage:
    python create_vm_template.py <VM_IP>

This script:
1. Copies setup-scripts to the VM via SSH/SCP
2. Runs init-run.sh inside the VM (installs dependencies, configures firewall, etc.)
3. Reboots the VM and waits for SSH to come back
4. Finds the VM UUID by IP (requires XenTools installed in VM)
5. Shuts down and exports the VM as vpx-dlvm.xva
6. Distributes the XVA to all pool hosts' ACK directory
"""

import os
import sys
import time
import subprocess
import argparse

# Constants
ACK_DIR = "/opt/xensource/packages/files/auto-cert-kit"
SETUP_SCRIPTS_SRC = os.path.join(ACK_DIR, "setup-scripts")
REMOTE_SETUP_DIR = "/root/setup-scripts"
XVA_NAME = "vpx-dlvm.xva"
XVA_PATH = os.path.join(ACK_DIR, XVA_NAME)

SSH_COMMON_OPTS = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"


def run_cmd(cmd, check=True, capture=False, shell=False):
    """Run a shell command and return the result."""
    if shell:
        print("[CMD] %s" % cmd)
    else:
        print("[CMD] %s" % ' '.join(cmd))
    if capture:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, shell=shell)
    else:
        result = subprocess.run(cmd, universal_newlines=True, shell=shell)
    if check and result.returncode != 0:
        print("Command failed with return code %d" % result.returncode)
        if capture:
            print("stdout: %s" % result.stdout)
            print("stderr: %s" % result.stderr)
        raise RuntimeError("Command failed: %s" % str(cmd))
    return result


def ssh_cmd(vm_ip, vm_pass, remote_cmd, check=True):
    """Execute a command on the remote VM via SSH using expect."""
    # Use expect to handle password authentication
    # Use heredoc to avoid quote escaping issues
    expect_script = '''expect << 'EXPECT_EOF'
set timeout 300
spawn ssh %s root@%s {%s}
expect {
    "password:" { send "%s\\r"; exp_continue }
    "Password:" { send "%s\\r"; exp_continue }
    eof
}
catch wait result
exit [lindex $result 3]
EXPECT_EOF
''' % (SSH_COMMON_OPTS, vm_ip, remote_cmd, vm_pass, vm_pass)
    return run_cmd(expect_script, check=check, capture=True, shell=True)


def scp_to_vm(vm_ip, vm_pass, local_path, remote_path):
    """Copy files to the remote VM via SCP using expect."""
    expect_script = '''expect << 'EXPECT_EOF'
set timeout 300
spawn scp -r %s %s root@%s:%s
expect {
    "password:" { send "%s\r"; exp_continue }
    "Password:" { send "%s\r"; exp_continue }
    eof
}
catch wait result
exit [lindex $result 3]
EXPECT_EOF
''' % (SSH_COMMON_OPTS, local_path, vm_ip, remote_path, vm_pass, vm_pass)
    return run_cmd(expect_script, check=True, capture=False, shell=True)


def scp_to_host(host_addr, local_path, remote_path):
    """Copy files to a pool host via SCP (uses pool secret for auth)."""
    # Get pool secret for authentication between hosts
    try:
        proc = subprocess.Popen(["cat", "/etc/xensource/ptoken"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        pool_secret = stdout.strip()
    except Exception:
        pool_secret = ""
    
    if pool_secret:
        # Use expect with pool secret
        expect_script = '''expect << 'EXPECT_EOF'
set timeout 600
spawn scp -r %s %s root@%s:%s
expect {
    "password:" { send "%s\r"; exp_continue }
    "Password:" { send "%s\r"; exp_continue }
    eof
}
catch wait result
exit [lindex $result 3]
EXPECT_EOF
''' % (SSH_COMMON_OPTS, local_path, host_addr, remote_path, pool_secret, pool_secret)
        return run_cmd(expect_script, check=False, capture=True, shell=True)
    else:
        # Fallback to key-based auth
        cmd = ["scp"] + SSH_COMMON_OPTS.split() + [local_path, "root@%s:%s" % (host_addr, remote_path)]
        return run_cmd(cmd, check=False)


def wait_for_ssh(vm_ip, vm_pass, max_tries=60, interval=5):
    """Wait for SSH to become available on the VM."""
    print("Waiting for SSH on %s..." % vm_ip)
    for i in range(max_tries):
        try:
            result = ssh_cmd(vm_ip, vm_pass, "echo ok", check=False)
            if result.returncode == 0:
                print("SSH is available on %s" % vm_ip)
                return True
        except Exception:
            pass
        time.sleep(interval)
    raise RuntimeError("Timeout waiting for SSH on %s" % vm_ip)


def find_vm_uuid_by_ip(vm_ip):
    """Find VM UUID by its IP address using xe vm-list."""
    result = run_cmd(["xe", "vm-list", "params=uuid,networks", "--multiple"], capture=True)
    
    lines = result.stdout.strip().split('\n')
    current_uuid = None
    current_networks = None
    
    for line in lines:
        line = line.strip()
        if line.startswith("uuid"):
            # Extract UUID value: "uuid ( RO)            : <uuid>"
            parts = line.split(":", 1)
            if len(parts) == 2:
                current_uuid = parts[1].strip()
        elif line.startswith("networks"):
            # Extract networks value
            parts = line.split(":", 1)
            if len(parts) == 2:
                current_networks = parts[1].strip()
        elif line == "" and current_uuid and current_networks:
            # End of a VM record
            if vm_ip in current_networks:
                return current_uuid
            current_uuid = None
            current_networks = None
    
    # Check last record
    if current_uuid and current_networks and vm_ip in current_networks:
        return current_uuid
    
    return None


def get_pool_hosts():
    """Get list of all pool host UUIDs."""
    result = run_cmd(["xe", "host-list", "--minimal"], capture=True)
    host_uuids = result.stdout.strip()
    if not host_uuids:
        return []
    return host_uuids.split(",")


def get_host_address(host_uuid):
    """Get the IP address of a host by UUID."""
    result = run_cmd(["xe", "host-param-get", "uuid=%s" % host_uuid, "param-name=address"], capture=True)
    return result.stdout.strip()


def get_local_addresses():
    """Get local IP addresses."""
    try:
        result = subprocess.run(["hostname", "-I"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)
        return result.stdout.strip().split()
    except Exception:
        return []


def require_cmd(cmd_name):
    """Check if a command exists."""
    result = subprocess.run(["which", cmd_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError("Missing required command: %s" % cmd_name)


def main():
    parser = argparse.ArgumentParser(description="Prepare Droid VM template for ACK (Rocky Linux)")
    parser.add_argument("vm_ip", help="IP address of the VM")
    args = parser.parse_args()

    vm_ip = args.vm_ip
    vm_pass = "xenserver"

    # Step 1: Pre-checks
    print("[1/7] Pre-checks")
    require_cmd("xe")
    require_cmd("expect")
    
    if not os.path.isdir(SETUP_SCRIPTS_SRC):
        raise RuntimeError("Cannot find setup scripts dir: %s" % SETUP_SCRIPTS_SRC)

    # Step 2: Copy setup-scripts into VM
    print("[2/7] Copy setup-scripts into VM")
    ssh_cmd(vm_ip, vm_pass, "rm -rf %s" % REMOTE_SETUP_DIR)
    # Copy setup-scripts dir to /root (will create /root/setup-scripts)
    scp_to_vm(vm_ip, vm_pass, SETUP_SCRIPTS_SRC, "/root/")

    # Step 3: Run Rocky init-run.sh inside VM
    print("[3/7] Run Rocky init-run.sh inside VM")
    # Run commands separately to avoid multi-line issues
    ssh_cmd(vm_ip, vm_pass, "command -v semanage || dnf install -y policycoreutils-python-utils", check=False)
    ssh_cmd(vm_ip, vm_pass, "chmod +x %s/init-run.sh && bash %s/init-run.sh" % (REMOTE_SETUP_DIR, REMOTE_SETUP_DIR), check=False)
    print("init-run.sh completed")

    # Step 4: Reboot VM and wait for SSH
    print("[4/7] Reboot VM and wait for SSH back")
    ssh_cmd(vm_ip, vm_pass, "reboot", check=False)
    time.sleep(10)
    wait_for_ssh(vm_ip, vm_pass)

    # Step 5: Find VM UUID
    print("[5/7] Find VM UUID")
    vm_uuid = None
    
    # Method 1: Try to get UUID from inside VM via /sys/hypervisor/uuid
    result = ssh_cmd(vm_ip, vm_pass, "cat /sys/hypervisor/uuid", check=False)
    if result.returncode == 0 and result.stdout:
        # Extract UUID from output
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if len(line) == 36 and '-' in line:
                vm_uuid = line
                break
    
    # Method 2: Try xe vm-list by IP
    if not vm_uuid:
        vm_uuid = find_vm_uuid_by_ip(vm_ip)
    
    # Method 3: Ask user
    if not vm_uuid:
        print("Could not find VM UUID automatically.")
        print("Please enter VM UUID or name-label:")
        user_input = input("VM UUID or name: ").strip()
        if not user_input:
            print("No input provided. Exiting.")
            sys.exit(1)
        if "-" in user_input and len(user_input) == 36:
            vm_uuid = user_input
        else:
            result = run_cmd(["xe", "vm-list", "name-label=%s" % user_input, "--minimal"], capture=True)
            vm_uuid = result.stdout.strip()
            if not vm_uuid:
                print("Could not find VM with name: %s" % user_input)
                sys.exit(1)
    print("VM_UUID=%s" % vm_uuid)

    # Step 6: Shutdown VM and export to XVA
    print("[6/7] Shutdown VM and export to XVA: %s" % XVA_PATH)
    if os.path.exists(XVA_PATH):
        os.remove(XVA_PATH)
    
    # Best-effort shutdown
    run_cmd(["xe", "vm-shutdown", "uuid=%s" % vm_uuid, "--force"], check=False)
    
    # Export
    run_cmd(["xe", "vm-export", "uuid=%s" % vm_uuid, "filename=%s" % XVA_PATH, "compress=true"])

    # Step 7: Distribute XVA to all pool hosts
    print("[7/7] Distribute XVA to all pool hosts into %s" % ACK_DIR)
    host_uuids = get_pool_hosts()
    local_addrs = get_local_addresses()

    for host_uuid in host_uuids:
        addr = get_host_address(host_uuid)
        if not addr:
            continue
        
        # Skip if this is the local host
        if addr in local_addrs:
            continue
        
        print("Copying to host %s:%s/%s" % (addr, ACK_DIR, XVA_NAME))
        scp_to_host(addr, XVA_PATH, "%s/%s" % (ACK_DIR, XVA_NAME))

    print("DONE. Exported and distributed: %s" % XVA_PATH)


if __name__ == "__main__":
    main()