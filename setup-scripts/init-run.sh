#!/bin/bash
# This script is used to setup Driod VM template base on Rocky 8 Linux 8.6 or Rocky 9

# install dependencies for test tools
dnf update -y
dnf install perl -y

# Rocky 8 uses powertools, Rocky 9 uses crb
if [ -f /etc/os-release ]; then
    . /etc/os-release
    if [ "$VERSION_ID" = "9" ] || [[ "$VERSION_ID" == 9.* ]]; then
        REPO="crb"
    else
        REPO="powertools"
    fi
else
    REPO="powertools"
fi

dnf --enablerepo=$REPO install perl-List-MoreUtils -y
dnf --enablerepo=$REPO install perl-Readonly -y
dnf install tcpdump -y
dnf install policycoreutils-python-utils -y

# setup firewall port for 4/tcp/udp and 5001/tcp/udp
firewall-cmd --zone=public --add-port=4/tcp --permanent
firewall-cmd --zone=public --add-port=4/udp --permanent
firewall-cmd --zone=public --add-port=5001/tcp --permanent
firewall-cmd --zone=public --add-port=5001/udp --permanent
firewall-cmd --reload
firewall-cmd --state

# setup static-ip service
cp /root/setup-scripts/static-ip.sh /root
chmod 755 /root/static-ip.sh
semanage fcontext -a -t bin_t '/root/static-ip.sh'
restorecon -Fv /root/static-ip.sh
cp /root/setup-scripts/startup-ip.service /lib/systemd/system/
systemctl enable startup-ip.service
systemctl start startup-ip.service
systemctl status startup-ip.service
