{ pkgs, baseImage ? null }:

let
  version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
in
pkgs.python3Packages.buildPythonApplication {
  pname = "virtbox";
  inherit version;

  src = ./.;

  pyproject = true;

  build-system = [
    pkgs.python3Packages.setuptools
  ];

  propagatedBuildInputs = [
    pkgs.python3Packages.libvirt
  ];

  nativeBuildInputs = [
    pkgs.makeWrapper
  ];

  postInstall = ''
    wrapProgram $out/bin/virtbox \
      --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.xorriso ]} \
      ${pkgs.lib.optionalString (baseImage != null)
          "--set VIRTBOX_BASE_IMAGE \"${baseImage}\""}
  '';

  doCheck = false;
}
