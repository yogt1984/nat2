# 05 — Mount the volume

**Effort** 20 min · **Depends on** 04 · **Blocks** 06 · **Status** todo

## What
Mount the volume at `/home/onat/nat2/data` **before** the repo is cloned, and
guard against the daemon writing into an empty mountpoint.

## How
Order matters: cloning first makes `install.sh` create `data/raw` on the root
disk, and mounting over it afterwards hides those files and splits the tape
across two filesystems at the next unmount.

```
sudo mkfs.ext4 -F /dev/disk/by-id/scsi-0HC_Volume_<ID>
sudo mkdir -p /home/onat/nat2/data
echo '/dev/disk/by-id/scsi-0HC_Volume_<ID> /home/onat/nat2/data ext4 \
discard,nofail,x-systemd.device-timeout=30s 0 0' | sudo tee -a /etc/fstab
sudo mount -a && sudo chown -R onat:onat /home/onat/nat2
touch /home/onat/nat2/data/.volume-ok
```

`nofail` keeps the box bootable without the volume — and is therefore also what
makes the empty-mountpoint case reachable. A `--user` unit **can** order against
mount units (only targets are no-ops there), but the auto-generated
`RequiresMountsFor` covers the repo root, not `data/`. Add a drop-in with an
explicit `RequiresMountsFor=/home/onat/nat2/data` and an
`ExecStartPre=/usr/bin/test -f …/.volume-ok`.

## Verify
```
df -hT /home/onat/nat2/data     # the volume, ext4
```

## Done when
The volume is mounted at the path the units already expect.
