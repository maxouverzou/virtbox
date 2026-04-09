{
  description = "virtbox: libvirt VM manager";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      treefmt-nix,
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      treefmtEval = treefmt-nix.lib.evalModule pkgs {
        projectRootFile = "flake.nix";
        programs.nixfmt.enable = true;
      };

      mkSystem =
        modules:
        nixpkgs.lib.nixosSystem {
          inherit system modules;
        };

      mkImage = extraModules: (mkSystem ([ ./image.nix ] ++ extraModules)).config.system.build.images.qemu;

    in
    {
      nixosConfigurations.image = mkSystem [ ./image.nix ];

      packages.${system} = {
        image = mkImage [ ];
        virtbox =
          let
            base = import ./lib/mkVirtbox.nix {
              inherit pkgs;
              baseImage = self.packages.${system}.image;
            };
          in
          base.overrideAttrs (old: {
            passthru = (old.passthru or { }) // {
              withPackages =
                args:
                let
                  argSet =
                    if builtins.isList args then
                      {
                        packages = args;
                        createArgs = [ ];
                      }
                    else
                      {
                        packages = args.packages or [ ];
                        createArgs = args.createArgs or [ ];
                      };
                  customImage = mkImage [ { environment.systemPackages = argSet.packages; } ];
                in
                import ./lib/mkVirtbox.nix {
                  inherit pkgs;
                  baseImage = customImage;
                  extraCreateArgs = argSet.createArgs;
                };
            };
          });
      };

      formatter.${system} = treefmtEval.config.build.wrapper;
    };
}
