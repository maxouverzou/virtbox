{ pkgs, llm-agents, ... }:
let
  agents = llm-agents.packages.${pkgs.stdenv.hostPlatform.system};
in
{
  system.stateVersion = "25.11";

  boot.initrd.availableKernelModules = [
    "virtio_blk"
    "virtio_pci"
    "virtio_scsi"
  ];
  boot.initrd.kernelModules = [
    "virtiofs"
    "virtio_balloon"
  ];
  boot.kernelParams = [ "console=ttyS0,115200n8" ];

  networking.useNetworkd = true;

  systemd.network.networks."05-default" = {
    matchConfig.Name = "en* eth*";
    networkConfig = {
      DHCP = "yes";
      MulticastDNS = true;
    };
  };

  services.resolved = {
    enable = true;
    settings.Resolve.MulticastDNS = "yes";
  };

  # Disable guest nix-daemon — host's /nix is mounted read-only, all nix ops
  # go through the host daemon proxied via vsock (port 9999, must match NIX_VSOCK_PORT in virtbox.py).
  nix.enable = false;
  nix.settings.experimental-features = [
    "nix-command"
    "flakes"
  ];

  # Point nix clients to the host daemon socket forwarded by virtbox into
  # the user's XDG runtime dir (writable by the user, set by systemd-logind).
  environment.sessionVariables.NIX_REMOTE = "unix://$XDG_RUNTIME_DIR/nix-daemon.socket";
  # nix.enable = false doesn't set NIX_PATH; point legacy nix-shell/<nixpkgs> to the registry.
  environment.variables.NIX_PATH = "nixpkgs=flake:nixpkgs";

  nixpkgs.config.allowUnfree = true;

  services.getty.autologinUser = "root";

  networking.firewall.allowedTCPPorts = [ 22 ];
  networking.firewall.allowedUDPPorts = [ 5353 ];

  services.cloud-init = {
    enable = true;
    network.enable = false;
    settings.network.config = "disabled";
    settings.cloud_init_modules = [
      "migrator"
      "seed_random"
      "bootcmd"
      "write-files"
      "growpart"
      "resizefs"
      "set_hostname"
      "update_hostname"
      "update_etc_hosts"
      "ca-certs"
      "users-groups"
      "ssh"
    ];
  };

  services.openssh = {
    enable = true;
    settings.PermitRootLogin = "no";
    settings.StreamLocalBindUnlink = "yes";
  };

  systemd.sockets.sshd-vsock = {
    description = "SSH over vsock";
    wantedBy = [ "sockets.target" ];
    socketConfig.ListenStream = "vsock::22";
  };

  systemd.services.sshd-vsock = {
    description = "SSH over vsock";
    requires = [ "sshd.service" ];
    after = [ "sshd.service" ];
    serviceConfig.ExecStart = "/run/current-system/sw/lib/systemd/systemd-socket-proxyd 127.0.0.1:22";
  };

  # Make the shared profile the default for nix profile commands.
  environment.variables.NIX_PROFILE = "/nix/var/nix/profiles/virtbox";

  networking.firewall.enable = false;

  virtualisation.podman = {
    enable = true;
    dockerCompat = true;
    dockerSocket.enable = true;
  };

  # Proxy nix daemon socket: guest listens on $XDG_RUNTIME_DIR/nix-daemon.socket,
  # each connection is forwarded to the host via vsock CID 2 (VMADDR_CID_HOST), port 9999.
  systemd.user.sockets.nix-daemon-vsock-proxy = {
    description = "Nix daemon socket via vsock";
    wantedBy = [ "sockets.target" ];
    socketConfig = {
      ListenStream = "%t/nix-daemon.socket";
      Accept = "yes";
    };
  };

  systemd.user.services."nix-daemon-vsock-proxy@" = {
    description = "Nix daemon vsock proxy instance";
    serviceConfig = {
      ExecStart = "${pkgs.socat}/bin/socat - VSOCK-CONNECT:2:9999";
      StandardInput = "socket";
      StandardOutput = "socket";
    };
  };

  systemd.user.services.vibe-kanban = {
    description = "vibe-kanban";
    wantedBy = [ "default.target" ];
    after = [ "network.target" ];
    serviceConfig = {
      ExecStart = "${agents.vibe-kanban}/bin/vibe-kanban";
      WorkingDirectory = "%h";
      Restart = "on-failure";
      Environment = [
        "PORT=8080"
        "HOST=0.0.0.0"
      ];
    };
  };

  programs.nix-index.enable = true;
  programs.starship.enable = true;

  environment.systemPackages = with agents; [
    vibe-kanban
    claude-code
    gemini-cli
    opencode
    openspec

    pkgs.git
    pkgs.socat
    pkgs.cloud-init
  ];
}
