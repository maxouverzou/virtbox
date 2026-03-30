{
  description = "virtbox: libvirt VM manager";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    llm-agents = {
      url = "github:numtide/llm-agents.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      llm-agents,
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
          specialArgs = { inherit llm-agents; };
        };

    in
    {
      nixosConfigurations.image = mkSystem [ ./image.nix ];

      packages.${system} = {
        image = self.nixosConfigurations.image.config.system.build.images.qemu;
        virtbox = import ./lib/mkVirtbox.nix {
          inherit pkgs;
          baseImage = self.packages.${system}.image;
        };
      };

      formatter.${system} = treefmtEval.config.build.wrapper;
    };
}
