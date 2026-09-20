#!/bin/bash
# Run as root in Ubuntu WSL, after installing QEMU and a generic Linux kernel.
# Creates a separate guest root under /var/lib; never boots the WSL root itself.
set -euo pipefail
base=/var/lib/p4-native-mptcp
root="$base/root"
workspace=/mnt/e/p4-workspace
mkdir -p "$root" "$base"
[[ "$(realpath "$base")" == /var/lib/p4-native-mptcp ]]
# Guard the Windows disk that stores Ubuntu as well as the ext4 free space.
host_free=$(df -Pk /mnt/f | awk 'NR==2 {print $4}')
[[ "$host_free" -gt 12582912 ]] || { echo 'Need at least 12 GiB free on F:'; exit 1; }
if [[ ! -f "$base/root-prepared" ]]; then
    for dir in usr bin sbin lib lib64 etc; do
        cp -a "/$dir" "$root/"
    done
    mkdir -p "$root"/{dev,proc,sys,run,tmp,var/tmp,root,workspace}
    chmod 1777 "$root/tmp" "$root/var/tmp"
    touch "$base/root-prepared"
fi
cp "$workspace/mptcp_exp/native_vm_init.sh" "$root/native-init"
chmod +x "$root/native-init"
# These are disposable copies, not the host /usr/lib directories.
for candidate in "$root/usr/lib/wsl" "$root/usr/lib/firmware"; do
    if [[ -d "$candidate" ]]; then
        resolved=$(realpath "$candidate")
        [[ "$resolved" == "$root/usr/lib/wsl" || "$resolved" == "$root/usr/lib/firmware" ]]
        if mountpoint -q "$candidate"; then echo "Refusing mounted path $candidate"; exit 1; fi
        rm -rf -- "$candidate"
    fi
done
kernel=$(find /boot -maxdepth 1 -name 'vmlinuz-*-generic' | sort -V | tail -1)
[[ -n "$kernel" ]] || { echo 'No generic Linux kernel installed'; exit 1; }
version=${kernel##*/vmlinuz-}
grep '^CONFIG_MPTCP=y' "/boot/config-$version"
if [[ ! -f "$base/image-complete" ]]; then
    # Only discard the known incomplete, generated image from the failed build.
    [[ ! -f "$base/root.ext4" ]] || mv "$base/root.ext4" "$base/root.failed.ext4"
    truncate -s 12G "$base/root.building.ext4"
    mkfs.ext4 -F -d "$root" "$base/root.building.ext4"
    mv "$base/root.building.ext4" "$base/root.ext4"
    touch "$base/image-complete"
fi
# A normal initramfs mounts the guest disk, then runs /native-init as PID 1.
mkdir -p "$base/image-mount"
mount -o loop "$base/root.ext4" "$base/image-mount"
trap 'umount "$base/image-mount"' EXIT
if [[ ! -d "$base/image-mount/lib/modules/$version" ]]; then
    cp -a "/lib/modules/$version" "$base/image-mount/lib/modules/"
fi
cp "$workspace/mptcp_exp/native_vm_init.sh" "$base/image-mount/native-init"
chmod +x "$base/image-mount/native-init"
umount "$base/image-mount"
trap - EXIT
if [[ ! -f "/boot/initrd.img-$version" ]]; then
    mkinitramfs -o "$base/initrd.img-$version" "$version"
    initrd="$base/initrd.img-$version"
else
    initrd="/boot/initrd.img-$version"
fi
exec qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 4096 \
    -kernel "$kernel" -initrd "$initrd" \
    -append 'root=/dev/vda rw console=ttyS0 init=/native-init noresume quiet' \
    -drive "file=$base/root.ext4,format=raw,if=virtio" \
    -virtfs "local,path=$workspace,mount_tag=workspace,security_model=none,id=workspace" \
    -display none -serial stdio -monitor none -nic none -no-reboot
