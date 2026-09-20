#!/bin/sh
# PID 1 in the isolated benchmark VM; no system services or host changes.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /run /workspace
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /run
modprobe 9pnet_virtio
modprobe 9p
mount -t 9p -o trans=virtio,version=9p2000.L workspace /workspace
hostname mptcp-benchmark
ip link set lo up
python3 /workspace/mptcp_exp/native_mptcp_probe.py --output /workspace/mptcp_exp/results/native_mptcp/vm_capability.json
echo NATIVE_MPTCP_VM_READY
last_job=''
while true; do
    job=/workspace/mptcp_exp/native_guest_job.sh
    if [ -f "$job" ]; then
        current_job=$(sha256sum "$job" | cut -d' ' -f1)
        if [ "$current_job" != "$last_job" ]; then
            last_job=$current_job
            bash "$job"
            echo "NATIVE_JOB_EXIT=$?"
            sync
        fi
    fi
    sleep 2
done
