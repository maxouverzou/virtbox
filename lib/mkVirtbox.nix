{ pkgs, baseImage ? null }:
let
  python = pkgs.python3.withPackages (ps: [ ps.libvirt ]);
  baseImageEnv = pkgs.lib.optionalString (baseImage != null)
    "export VIRTBOX_BASE_IMAGE=${baseImage}";
in
pkgs.writeShellApplication {
  name = "virtbox";
  runtimeInputs = [
    python
    pkgs.xorriso
  ];
  text = ''
    ${baseImageEnv}
    exec python3 ${./virtbox.py} "$@"
  '';
}
