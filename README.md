# virtbox

A libvirt VM manager with virtiofs directory sharing and cloud-init provisioning. Ships a NixOS-based QEMU base image (git, Podman, Nix proxy) that consumers can extend with additional packages.

## Prerequisites

- Nix with flakes enabled
- libvirt/KVM (`qemu:///system` accessible, `libvirtd` running)
- libvirt `default` storage pool configured
- SSH key in `~/.ssh` (used to provision guest access)
- `ssh` client with vsock support (for `user@vsock%CID` addresses)

## Build

Build `virtbox` with the base image (git, Podman, cloud-init):

```sh
nix build .#virtbox
```

To build the raw disk image separately:

```sh
nix build .#image
```

Format the Nix sources:

```sh
nix fmt
```

## Customizing the image

Use `withPackages` to bake additional packages into the base image and optionally set default `create` arguments. This is designed for consuming virtbox from another flake (e.g. via home-manager).

### Adding packages

```nix
# flake.nix (consumer)
{
  inputs = {
    virtbox.url = "github:maxouverzou/virtbox";
    llm-agents.url = "github:numtide/llm-agents.nix";
  };

  # ...
}
```

```nix
# home.nix
home.packages = let
  virtbox = inputs.virtbox.packages.${pkgs.system}.virtbox;
  llm-agents = inputs.llm-agents.packages.${pkgs.system};
in [
  (virtbox.withPackages [ llm-agents.claude-code ])
];
```

### Adding packages with extra create defaults

If the packages you add need host directories shared into the guest, pass an attrset with `createArgs`:

```nix
home.packages = let
  virtbox = inputs.virtbox.packages.${pkgs.system}.virtbox;
  llm-agents = inputs.llm-agents.packages.${pkgs.system};
in [
  (virtbox.withPackages {
    packages = [
      llm-agents.claude-code
      llm-agents.vibe-kanban
    ];
    createArgs = [
      "--try-share" "$HOME/.claude"
      "--try-share" "$HOME/.claude.json"
      "--try-share" "$HOME/.vibe-kanban"
    ];
  })
];
```

The `createArgs` are injected as defaults whenever `virtbox create` is run. Users can still pass additional flags on the command line as usual.

## Usage

### virtbox

Run from the directory you want shared into the VM. The base image is baked in, so no `--image` flag is needed:

```sh
# Create a VM (name defaults to virtbox-<cwd-basename>)
virtbox create

# Named VM
virtbox create my-dev-vm

# Override the base image
virtbox create --image /path/to/image.qcow2 [vm-name]

# Enter a running VM (SSH over vsock, no IP needed)
virtbox enter <vm-name>

# List managed VMs
virtbox list

# List cached base images in the storage pool
virtbox list -i

# Remove a VM and its storage volumes
virtbox rm <vm-name>

# Remove an unused base image
virtbox rmi virtbox-base-<hash>.qcow2

# Prune all unused base images
virtbox rmi --prune

# Remove all base images (fails if any are in use)
virtbox rmi --all
```

### virtbox create options

| Flag | Default | Description |
|------|---------|-------------|
| `--image PATH` | `$VIRTBOX_BASE_IMAGE` | Base qcow2 image (or directory containing one) |
| `--cpus N` | `2` | Number of vCPUs |
| `--memory MiB` | `2048` | RAM in MiB |
| `--disk-size GiB` | `20` | Overlay disk size (must be ≥ base image size) |
| `--share HOST[:GUEST]` | — | Share host directory read-write via virtiofs |
| `--share-ro HOST[:GUEST]` | — | Share host directory read-only via virtiofs |
| `--try-share HOST[:GUEST]` | — | Like `--share`, silently skip if path missing |
| `--try-share-ro HOST[:GUEST]` | — | Like `--share-ro`, silently skip if path missing |
| `--share-parent` | — | Share parent of current directory read-only |

The `VIRTBOX_BASE_IMAGE` environment variable can substitute for `--image`.

When virtbox is built with `withPackages { createArgs = [...]; }`, those args are injected as defaults via the `VIRTBOX_EXTRA_CREATE_ARGS` environment variable. Command-line flags take precedence.


## SELinux

On systems with SELinux in Enforcing mode (e.g. Fedora), virtiofsd may be denied access to shared directories. virtbox will print the required `chcon` or `semanage` commands when this is detected.
