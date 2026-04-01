#!/usr/bin/env python3
"""virtbox: manage libvirt VMs and virtiofs directory sharing."""
import argparse
import getpass
import grp
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import libvirt

LIBVIRT_URI = "qemu:///system"
VIRTBOX_NS = "urn:virtbox"
NIX_VSOCK_PORT = 9999  # must match guest-side unit in image.nix

# Suppress libvirt's default stderr error logging — we handle errors ourselves
libvirt.registerErrorHandler(lambda ctx, err: None, None)


def open_conn() -> libvirt.virConnect:
    conn = libvirt.open(LIBVIRT_URI)
    if conn is None:
        sys.exit(f"Error: failed to connect to {LIBVIRT_URI}")
    return conn


def find_ssh_pubkey() -> str | None:
    ssh_dir = Path.home() / ".ssh"
    for name in ["id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub", "id_dsa.pub"]:
        path = ssh_dir / name
        if path.exists():
            return path.read_text().strip()
    for path in sorted(ssh_dir.glob("*.pub")):
        return path.read_text().strip()
    return None


def upload_to_pool(conn: libvirt.virConnect, pool, vol_name: str, local_path: str):
    """Create a raw volume in the pool and upload a local file into it."""
    size = os.path.getsize(local_path)
    vol_xml = (
        f"<volume>"
        f"<name>{vol_name}</name>"
        f"<allocation>{size}</allocation>"
        f"<capacity>{size}</capacity>"
        f"<target><format type='raw'/></target>"
        f"</volume>"
    )
    vol = pool.createXML(vol_xml)
    stream = conn.newStream()
    vol.upload(stream, 0, size)
    with open(local_path, "rb") as f:
        while chunk := f.read(65536):
            stream.send(chunk)
    stream.finish()
    return vol


def _nix_hash(path: str) -> str:
    """Extract the hash from a Nix store path, falling back to sha256."""
    parts = Path(path).parts
    try:
        idx = parts.index("store")
        return parts[idx + 1].split("-")[0][:12]
    except (ValueError, IndexError):
        import hashlib
        return hashlib.sha256(path.encode()).hexdigest()[:12]


def _base_vol_name(image: str) -> str:
    return f"virtbox-base-{_nix_hash(image)}.qcow2"


def _ensure_base_in_pool(conn: libvirt.virConnect, pool, image: str) -> tuple[str, str]:
    """Ensure the base image is in the pool, uploading it if needed.

    Returns (vol_name, vol_path).
    """
    vol_name = _base_vol_name(image)
    pool.refresh()
    try:
        vol = pool.storageVolLookupByName(vol_name)
        return vol_name, vol.path()
    except libvirt.libvirtError:
        pass

    size = os.path.getsize(image)
    print(f"Importing base image as {vol_name} ({size // 1024 ** 2} MiB)...")
    vol_xml = (
        f"<volume>"
        f"<name>{vol_name}</name>"
        f"<allocation>0</allocation>"
        f"<capacity>{size}</capacity>"
        f"<target><format type='qcow2'/></target>"
        f"</volume>"
    )
    vol = pool.createXML(vol_xml)
    stream = conn.newStream()
    vol.upload(stream, 0, size)
    with open(image, "rb") as f:
        while chunk := f.read(65536):
            stream.send(chunk)
    stream.finish()
    print(f"Base image imported.")
    return vol_name, vol.path()


def create_seed_iso_volume(conn: libvirt.virConnect, pool, vmname: str):
    """Generate a cloud-init seed ISO and upload it to the storage pool."""
    username = os.environ.get("USER") or getpass.getuser()
    uid = os.getuid()
    gid = os.getgid()
    try:
        groupname = grp.getgrgid(gid).gr_name
    except KeyError:
        groupname = str(gid)
    ssh_key = find_ssh_pubkey()

    if ssh_key:
        ssh_keys_section = f"    ssh_authorized_keys:\n      - {ssh_key}\n"
    else:
        print("Warning: no SSH public key found in ~/.ssh; guest may be inaccessible via SSH")
        ssh_keys_section = ""

    user_data = (
        f"#cloud-config\n"
        f"hostname: {vmname}\n"
        f"fqdn: {vmname}.local\n"
        f"manage_etc_hosts: true\n"
        f"groups:\n"
        f"  {groupname}:\n"
        f"    gid: {gid}\n"
        f"users:\n"
        f"  - name: {username}\n"
        f"    uid: {uid}\n"
        f"    primary_group: {groupname}\n"
        f"    groups: wheel\n"
        f"    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        f"{ssh_keys_section}"
        f"runcmd:\n"
        f"  - loginctl enable-linger {username}\n"
        f"  - systemctl start user@{uid}.service\n"
    )
    meta_data = f"instance-id: {vmname}\nlocal-hostname: {vmname}\n"

    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "user-data").write_text(user_data)
        (Path(tmpdir) / "meta-data").write_text(meta_data)
        iso_path = os.path.join(tmpdir, "seed.iso")
        subprocess.run(
            [
                "xorriso", "-as", "mkisofs",
                "-output", iso_path,
                "-volid", "cidata",
                "-joliet", "-rock",
                "-input-charset", "utf-8",
                "user-data", "meta-data",
            ],
            check=True,
            cwd=tmpdir,
        )
        vol = upload_to_pool(conn, pool, f"{vmname}-seed.iso", iso_path)

    print(f"Created cloud-init seed ISO (user: {username})")
    return vol


def _selinux_enforcing() -> bool:
    getenforce = shutil.which("getenforce")
    if not getenforce:
        return False
    result = subprocess.run([getenforce], capture_output=True, text=True)
    return result.stdout.strip() == "Enforcing"


def _print_selinux_warning(hostdirs: list[str]):
    chcon_lines = "\n".join(f'  chcon -R -t svirt_image_t "{d}"' for d in hostdirs)
    fix_lines = "\n".join(
        f'  semanage fcontext -a -t svirt_image_t "{d}(/.*)?"\n  restorecon -Rv "{d}"'
        for d in hostdirs
    )
    print(f"""
WARNING: SELinux is Enforcing. QEMU/virtiofsd may be denied access to shared directories.

Quick fix (temporary, resets on relabel):
{chcon_lines}

Permanent fix:
{fix_lines}

You may also need:
  setsebool -P virt_use_fusefs on
""")


def _make_virtiofs_xml(hostdir, tag, readonly=False):
    ro = "<readonly/>" if readonly else ""
    return (
        f"<filesystem type='mount'>"
        f"<driver type='virtiofs'/>"
        f"<source dir='{hostdir}'/>"
        f"<target dir='{tag}'/>"
        f"{ro}"
        f"</filesystem>"
    )


def _wait_for_ssh(domain, username, timeout=120):
    deadline = time.time() + timeout
    cid = None
    while time.time() < deadline:
        root = ET.fromstring(domain.XMLDesc())
        cid_el = root.find(".//vsock/cid")
        if cid_el is not None:
            cid = cid_el.get("address")
        if cid:
            break
        time.sleep(2)

    if not cid:
        sys.exit("Error: timed out waiting for vsock CID")

    print(f"Waiting for SSH on vsock CID {cid}...")
    ssh_base = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-T", f"{username}@vsock%{cid}"]
    ssh_ready = False
    last_stderr = b""
    while time.time() < deadline:
        result = subprocess.run(
            ssh_base + ["true"],
            capture_output=True,
        )
        if result.returncode == 0:
            ssh_ready = True
            break
        last_stderr = result.stderr
        time.sleep(3)

    if not ssh_ready:
        if last_stderr:
            print(last_stderr.decode(errors="replace").strip(), file=sys.stderr)
        sys.exit("Error: timed out waiting for SSH")

    print("Waiting for cloud-init to finish...")
    while time.time() < deadline:
        result = subprocess.run(
            ssh_base + ["systemctl is-active cloud-final.service"],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() not in ("active", "activating"):
            break
        time.sleep(3)
    else:
        print("Warning: timed out waiting for cloud-init to finish")

    return cid


def _ensure_nix_vsock_proxy():
    """Set up a systemd user socket unit that proxies vsock connections to the host nix daemon."""
    host_socket = "/nix/var/nix/daemon-socket/socket"
    if not os.path.exists(host_socket):
        print("Note: host Nix daemon socket not found; skipping nix vsock proxy")
        return
    socat = shutil.which("socat")
    if not socat:
        print("Note: socat not found on PATH; skipping nix daemon vsock proxy")
        return

    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)

    (unit_dir / "nix-vsock-proxy.socket").write_text(
        f"[Socket]\nListenStream=vsock::{NIX_VSOCK_PORT}\nAccept=yes\n\n[Install]\nWantedBy=sockets.target\n"
    )
    (unit_dir / "nix-vsock-proxy@.service").write_text(
        f"[Service]\nExecStart={socat} - UNIX-CONNECT:{host_socket}\nStandardInput=socket\nStandardOutput=socket\n"
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", "nix-vsock-proxy.socket"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: could not enable nix vsock proxy: {result.stderr.strip()}")
    else:
        print(f"Nix daemon vsock proxy active on port {NIX_VSOCK_PORT}")


def _mount_share(cid, username, tag, guestmount):
    result = subprocess.run([
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-T",
        f"{username}@vsock%{cid}",
        f"mkdir -p '{guestmount}' && sudo mount -t virtiofs '{tag}' '{guestmount}'",
    ])
    if result.returncode != 0:
        print("Could not mount automatically. Run in the guest:")
        print(f"  mkdir -p {guestmount} && sudo mount -t virtiofs {tag} {guestmount}")
        print("Tip: use HOST:GUEST syntax (e.g. --share /var/home/user/dir:/home/user/dir) to set a custom guest path.")
    return result.returncode == 0


def _normalize_guest_path(hostdir: str) -> str:
    """Map a resolved host path to a sensible guest mount point.

    On ostree-based systems (e.g. Fedora Silverblue), $HOME resolves to
    /var/home/<user> rather than the conventional /home/<user>.  The guest
    is a standard Linux system that has no /var/home, so we substitute the
    canonical /home/<user> prefix when the resolved home directory differs
    from it.
    """
    home_resolved = str(Path.home().resolve())
    home_standard = f"/home/{getpass.getuser()}"
    if home_resolved != home_standard and hostdir.startswith(home_resolved):
        return home_standard + hostdir[len(home_resolved):]
    return hostdir


def _parse_shares(args):
    shares = []
    for entry_list, readonly, try_mode in [
        (args.share, False, False),
        (args.share_ro, True, False),
        (args.try_share, False, True),
        (args.try_share_ro, True, True),
    ]:
        for spec in entry_list:
            if ":" in spec:
                hostdir_raw, guestmount = spec.split(":", 1)
            else:
                hostdir_raw = spec
                guestmount = None
            hostdir = str(Path(hostdir_raw).resolve())
            if not os.path.isdir(hostdir):
                if try_mode:
                    continue
                sys.exit(f"Error: '{hostdir}' is not a directory or does not exist")
            if guestmount is None:
                guestmount = _normalize_guest_path(hostdir)
            shares.append((hostdir, guestmount, readonly))
    return shares


def cmd_create(args):
    cwd = os.getcwd()
    vmname = args.vm_name or f"virtbox-{os.path.basename(cwd)}"

    # Inject default shares before parsing
    args.share = [cwd] + args.share
    if not args.no_share_nix:
        args.share_ro = ["/nix:/nix"] + args.share_ro
    args.try_share = [
        str(Path.home() / ".claude.json"),
        str(Path.home() / ".claude"),
        str(Path.home() / ".config/opencode"),
        str(Path.home() / ".gemini"),
        str(Path.home() / ".vibe-kanban"),
    ] + args.try_share
    args.try_share_ro = [
        str(Path.home() / ".git"),
        str(Path.home() / ".config/git"),
    ] + args.try_share_ro
    if args.share_parent:
        args.share_ro = [str(Path(cwd).parent)] + args.share_ro

    image_path = Path(args.image).resolve()
    if image_path.is_dir():
        candidates = list(image_path.glob("*.qcow2"))
        if not candidates:
            sys.exit(f"Error: no .qcow2 file found in {image_path}")
        if len(candidates) > 1:
            sys.exit(f"Error: multiple .qcow2 files found in {image_path}: {candidates}")
        image_path = candidates[0]
    image = str(image_path)

    shares = _parse_shares(args)

    conn = open_conn()
    try:
        # Check domain name conflict
        try:
            conn.lookupByName(vmname)
            sys.exit(f"Error: VM '{vmname}' already exists")
        except libvirt.libvirtError:
            pass

        # Get default storage pool
        try:
            pool = conn.storagePoolLookupByName("default")
        except libvirt.libvirtError:
            sys.exit("Error: libvirt 'default' storage pool not found")

        # Ensure base image is in the pool
        base_vol_name, base_vol_path = _ensure_base_in_pool(conn, pool, image)

        # Check volume name conflicts up front
        for vol_name in [f"{vmname}.qcow2", f"{vmname}-seed.iso"]:
            try:
                pool.storageVolLookupByName(vol_name)
                sys.exit(f"Error: storage volume '{vol_name}' already exists in pool")
            except libvirt.libvirtError:
                pass

        # Create overlay volume backed by the pool copy of the base image
        base_vol = pool.storageVolLookupByName(base_vol_name)
        base_size = base_vol.info()[1]  # (type, capacity, allocation)
        requested = args.disk_size * 1024 ** 3
        if requested < base_size:
            print(f"Warning: --disk-size {args.disk_size} GiB is smaller than base image ({base_size / 1024**3:.1f} GiB); using base image size.")
            requested = base_size
        overlay_xml = (
            f"<volume>"
            f"<name>{vmname}.qcow2</name>"
            f"<allocation>0</allocation>"
            f"<capacity unit='B'>{requested}</capacity>"
            f"<target><format type='qcow2'/></target>"
            f"<backingStore>"
            f"<path>{base_vol_path}</path>"
            f"<format type='qcow2'/>"
            f"</backingStore>"
            f"</volume>"
        )
        overlay_vol = pool.createXML(overlay_xml)
        overlay = overlay_vol.path()
        print(f"Created overlay at {overlay} backed by {base_vol_name} (size: {requested // 1024**3} GiB, sparse)")

        # Create and upload cloud-init seed ISO
        seed_vol = create_seed_iso_volume(conn, pool, vmname)
        seed_iso = seed_vol.path()

        fs_devices = ""
        for hostdir, _guestmount, readonly in shares:
            tag = f"share-{os.path.basename(hostdir)}"[:31]
            fs_devices += "\n    " + _make_virtiofs_xml(hostdir, tag, readonly)

        guest_cwd = shares[0][1] if shares else None
        cwd_attr = f' cwd="{guest_cwd}"' if guest_cwd else ""
        domain_xml = f"""<domain type='kvm'>
  <name>{vmname}</name>
  <metadata>
    <virtbox:managed xmlns:virtbox="urn:virtbox" base="{base_vol_name}"{cwd_attr}/>
  </metadata>
  <memory unit='MiB'>{args.memory}</memory>
  <vcpu>{args.cpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <cpu mode='host-passthrough'/>
  <memoryBacking>
    <source type='memfd'/>
    <access mode='shared'/>
  </memoryBacking>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{overlay}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{seed_iso}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
    <serial type='pty'>
      <target port='0'/>
    </serial>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
    <vsock model='virtio'>
      <cid auto='yes'/>
    </vsock>{fs_devices}
  </devices>
</domain>"""

        domain = conn.defineXML(domain_xml)
        domain.create()
        print(f"VM '{vmname}' defined and started")

        if shares:
            username = os.environ.get("USER") or getpass.getuser()
            cid = _wait_for_ssh(domain, username)
            for hostdir, guestmount, _readonly in shares:
                tag = f"share-{os.path.basename(hostdir)}"[:31]
                _mount_share(cid, username, tag, guestmount)
            if not args.no_share_nix:
                _ensure_nix_vsock_proxy()
            if _selinux_enforcing():
                _print_selinux_warning([h for h, _, _ in shares])
    finally:
        conn.close()



def _is_managed(domain) -> bool:
    root = ET.fromstring(domain.XMLDesc())
    return root.find(f".//{{{VIRTBOX_NS}}}managed") is not None


def _find_vms_by_cwd(conn, cwd: str) -> list[str]:
    """Return names of all managed VMs whose guest cwd matches the given host path."""
    guest_cwd = _normalize_guest_path(cwd)
    matches = []
    for domain in conn.listAllDomains():
        if not _is_managed(domain):
            continue
        root = ET.fromstring(domain.XMLDesc())
        managed_el = root.find(f".//{{{VIRTBOX_NS}}}managed")
        if managed_el is not None and managed_el.get("cwd") == guest_cwd:
            matches.append(domain.name())
    return matches


def _get_base_of(domain) -> str | None:
    root = ET.fromstring(domain.XMLDesc())
    el = root.find(f".//{{{VIRTBOX_NS}}}managed")
    return el.get("base") if el is not None else None


def cmd_list(args):
    conn = open_conn()
    try:
        if args.images:
            try:
                pool = conn.storagePoolLookupByName("default")
            except libvirt.libvirtError:
                sys.exit("Error: libvirt 'default' storage pool not found")
            pool.refresh()
            base_vols = [v for v in pool.listAllVolumes()
                         if v.name().startswith("virtbox-base-")]
            if not base_vols:
                print("No virtbox base images found.")
                return
            domains = conn.listAllDomains(0)
            managed = [d for d in domains if _is_managed(d)]
            # Map base vol name → list of VM names using it
            usage: dict[str, list[str]] = {}
            for d in managed:
                b = _get_base_of(d)
                if b:
                    usage.setdefault(b, []).append(d.name())
            for vol in sorted(base_vols, key=lambda v: v.name()):
                users = usage.get(vol.name(), [])
                used_by = ", ".join(sorted(users)) if users else "-"
                size_mib = vol.info()[1] // 1024 ** 2
                print(f"  {vol.name():<45} {size_mib:>6} MiB  used by: {used_by}")
        else:
            domains = conn.listAllDomains(0)
            managed = [(d.name(), d.state()[0]) for d in domains if _is_managed(d)]
            if not managed:
                print("No virtbox managed VMs found.")
                return
            state_name = {
                libvirt.VIR_DOMAIN_RUNNING:  "running",
                libvirt.VIR_DOMAIN_PAUSED:   "paused",
                libvirt.VIR_DOMAIN_SHUTOFF:  "off",
                libvirt.VIR_DOMAIN_SHUTDOWN: "shutting down",
            }
            for name, state in sorted(managed):
                print(f"  {name:<30} {state_name.get(state, 'unknown')}")
    finally:
        conn.close()


def cmd_enter(args):
    username = args.ssh_user or os.environ.get("USER") or getpass.getuser()
    cwd = os.getcwd()

    if args.vm_name:
        vmname = args.vm_name
    else:
        conn = open_conn()
        try:
            matches = _find_vms_by_cwd(conn, cwd)
        finally:
            conn.close()

        if len(matches) > 1:
            names = ", ".join(sorted(matches))
            sys.exit(f"Error: multiple VMs found for this directory ({names}); specify a VM name")
        elif len(matches) == 1:
            vmname = matches[0]
        else:
            image = os.environ.get("VIRTBOX_BASE_IMAGE")
            if not image:
                sys.exit(
                    "Error: no VM found for this directory and VIRTBOX_BASE_IMAGE is not set; "
                    "run 'virtbox create --image <path>' to create one"
                )
            print("No VM found for this directory, creating one...")
            create_args = argparse.Namespace(
                vm_name=None,
                image=image,
                share=[],
                share_ro=[],
                try_share=[],
                try_share_ro=[],
                share_parent=False,
                no_share_nix=False,
                cpus=2,
                memory=2048,
                disk_size=20,
            )
            cmd_create(create_args)
            vmname = f"virtbox-{os.path.basename(cwd)}"

    conn = open_conn()
    try:
        try:
            domain = conn.lookupByName(vmname)
        except libvirt.libvirtError:
            sys.exit(f"Error: VM '{vmname}' not found")

        if not _is_managed(domain):
            sys.exit(f"Error: VM '{vmname}' is not managed by virtbox")

        state, _ = domain.state()
        if state != libvirt.VIR_DOMAIN_RUNNING:
            print(f"Starting VM '{vmname}'...")
            domain.create()
            cid = _wait_for_ssh(domain, username)
        else:
            root = ET.fromstring(domain.XMLDesc())
            cid_el = root.find(".//vsock/cid")
            cid = cid_el.get("address") if cid_el is not None else None
            if not cid:
                sys.exit(f"Error: could not determine vsock CID for VM '{vmname}'")

        root = ET.fromstring(domain.XMLDesc())
        managed_el = root.find(f".//{{{VIRTBOX_NS}}}managed")
        guest_cwd = managed_el.get("cwd") if managed_el is not None else None
    finally:
        conn.close()

    _ensure_nix_vsock_proxy()

    ssh_args = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-A",
    ]
    if guest_cwd:
        ssh_args += ["-t", f"{username}@vsock%{cid}", f"cd {guest_cwd} && exec $SHELL -l"]
    else:
        ssh_args += [f"{username}@vsock%{cid}"]
    os.execvp("ssh", ssh_args)


def _confirm(prompt: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_rmi(args):
    if not args.image_name and not args.prune and not args.all:
        sys.exit("Error: provide an image name, --prune, or --all")

    conn = open_conn()
    try:
        try:
            pool = conn.storagePoolLookupByName("default")
        except libvirt.libvirtError:
            sys.exit("Error: libvirt 'default' storage pool not found")
        pool.refresh()

        if args.image_name:
            # Single image removal
            try:
                vol = pool.storageVolLookupByName(args.image_name)
            except libvirt.libvirtError:
                sys.exit(f"Error: base image '{args.image_name}' not found")
            domains = conn.listAllDomains(0)
            users = [d.name() for d in domains if _get_base_of(d) == args.image_name]
            if users:
                sys.exit(
                    f"Error: '{args.image_name}' is in use by: {', '.join(sorted(users))}\n"
                    f"Remove those VMs first with: virtbox rm <name>"
                )
            if not _confirm(f"Remove base image '{args.image_name}'?", args.yes):
                sys.exit("Aborted.")
            vol.delete(0)
            print(f"Removed base image: {args.image_name}")
            return

        # Bulk removal (--prune or --all)
        all_base_vols = [v for v in pool.listAllVolumes()
                         if v.name().startswith("virtbox-base-")]
        if not all_base_vols:
            print("No virtbox base images found.")
            return

        domains = conn.listAllDomains(0)
        usage: dict[str, list[str]] = {}
        for d in domains:
            b = _get_base_of(d)
            if b:
                usage.setdefault(b, []).append(d.name())

        if args.all:
            blocked = {v.name(): usage[v.name()] for v in all_base_vols if v.name() in usage}
            if blocked:
                lines = "\n".join(
                    f"  {name}: used by {', '.join(sorted(vms))}"
                    for name, vms in sorted(blocked.items())
                )
                sys.exit(f"Error: cannot remove all — some images are in use:\n{lines}\n"
                         f"Remove those VMs first with: virtbox rm <name>")
            to_remove = all_base_vols
        else:
            # --prune: only unused
            to_remove = [v for v in all_base_vols if v.name() not in usage]
            if not to_remove:
                print("No unused base images to prune.")
                return

        names = [v.name() for v in to_remove]
        print("Base images to remove:")
        for n in sorted(names):
            print(f"  {n}")
        if not _confirm(f"Remove {len(to_remove)} base image(s)?", args.yes):
            sys.exit("Aborted.")
        for vol in to_remove:
            vol.delete(0)
            print(f"Removed: {vol.name()}")
    finally:
        conn.close()


def cmd_rm(args):
    vmname = args.vm_name

    conn = open_conn()
    try:
        try:
            domain = conn.lookupByName(vmname)
        except libvirt.libvirtError:
            sys.exit(f"Error: VM '{vmname}' not found")

        if not _is_managed(domain):
            sys.exit(f"Error: VM '{vmname}' is not managed by virtbox")

        # Collect disk paths before undefining the domain
        root = ET.fromstring(domain.XMLDesc())
        disk_paths = [
            el.get("file")
            for el in root.findall(".//disk[@type='file']/source")
            if el.get("file")
        ]

        if not _confirm(f"Remove VM '{vmname}' and its storage volumes?", args.yes):
            sys.exit("Aborted.")

        state, _ = domain.state()
        if state == libvirt.VIR_DOMAIN_RUNNING:
            domain.destroy()

        domain.undefine()
        print(f"Removed VM '{vmname}'")

        for path in disk_paths:
            try:
                vol = conn.storageVolLookupByPath(path)
                vol.delete(0)
                print(f"Deleted volume: {path}")
            except libvirt.libvirtError as e:
                print(f"Warning: could not delete volume {path}: {e}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(prog="virtbox", description="Manage libvirt VMs")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Create and start a new virtbox VM")
    p_create.add_argument("vm_name", nargs="?", default=None,
                          help="Name for the VM instance (default: basename of current directory)")
    _implicit_image = os.environ.get("VIRTBOX_BASE_IMAGE")
    p_create.add_argument(
        "--image",
        required=_implicit_image is None,
        default=_implicit_image,
        help=argparse.SUPPRESS if _implicit_image else "Path to base qcow2 image",
    )
    p_create.add_argument("--share", action="append", default=[], metavar="HOST[:GUEST]",
                          help="Share host directory into guest via virtiofs (read-write)")
    p_create.add_argument("--share-ro", action="append", default=[], metavar="HOST[:GUEST]",
                          dest="share_ro", help="Share host directory into guest via virtiofs (read-only)")
    p_create.add_argument("--try-share", action="append", default=[], metavar="HOST[:GUEST]",
                          dest="try_share", help="Share host directory via virtiofs (skip silently if missing)")
    p_create.add_argument("--try-share-ro", action="append", default=[], metavar="HOST[:GUEST]",
                          dest="try_share_ro", help="Share host directory via virtiofs read-only (skip silently if missing)")
    p_create.add_argument("--share-parent", action="store_true", default=False,
                          dest="share_parent", help="Share parent of current directory into guest (read-only)")
    p_create.add_argument("--no-share-nix", action="store_true", default=False,
                          dest="no_share_nix",
                          help="Do not share host /nix or Nix daemon socket into the guest")
    p_create.add_argument("--cpus", type=int, default=2, metavar="N",
                          help="Number of vCPUs (default: 2)")
    p_create.add_argument("--memory", type=int, default=2048, metavar="MiB",
                          help="RAM in MiB (default: 2048)")
    p_create.add_argument("--disk-size", type=int, default=20, metavar="GiB",
                          dest="disk_size",
                          help="Overlay disk size in GiB (default: 20). Must be >= base image size.")
    p_create.set_defaults(func=cmd_create)

    p_connect = sub.add_parser("enter", help="Enter a running VM with an interactive SSH session")
    p_connect.add_argument("vm_name", nargs="?", default=None,
                           help="Name of the VM (default: look up by current directory)")
    p_connect.add_argument("--ssh-user", default=None, dest="ssh_user",
                           help="SSH user (default: current user)")
    p_connect.set_defaults(func=cmd_enter)

    p_list = sub.add_parser("list", help="List virtbox managed VMs or base images")
    p_list.add_argument("-i", "--images", action="store_true", default=False,
                        help="List base images instead of VMs")
    p_list.set_defaults(func=cmd_list)

    p_rmi = sub.add_parser("rmi", help="Remove a virtbox base image from the pool")
    p_rmi.add_argument("image_name", nargs="?", default=None,
                       help="Name of the base image (e.g. virtbox-base-abc123.qcow2)")
    p_rmi_mode = p_rmi.add_mutually_exclusive_group()
    p_rmi_mode.add_argument("--prune", action="store_true", default=False,
                            help="Remove all unused base images (no dependent VMs)")
    p_rmi_mode.add_argument("--all", action="store_true", default=False,
                            help="Remove all base images (fails if any are in use)")
    p_rmi.add_argument("-y", "--assumeyes", dest="yes", action="store_true",
                       help="Do not prompt for confirmation")
    p_rmi.set_defaults(func=cmd_rmi)

    p_rm = sub.add_parser("rm", help="Remove a virtbox managed VM and its storage volumes")
    p_rm.add_argument("vm_name", help="Name of the VM to remove")
    p_rm.add_argument("-y", "--assumeyes", dest="yes", action="store_true",
                      help="Do not prompt for confirmation")
    p_rm.set_defaults(func=cmd_rm)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
