Checklist
* [x] Used a [personal fork of the feedstock to propose changes](https://conda-forge.org/docs/maintainer/updating_pkgs.html#forking-and-pull-requests)
* [ ] Bumped the build number (if the version is unchanged)
* [x] Reset the build number to `0` (if the version changed)
* [ ] [Re-rendered]( https://conda-forge.org/docs/maintainer/updating_pkgs.html#rerendering-feedstocks ) with the latest `conda-smithy` (Use the phrase <code>@<space/>conda-forge-admin, please rerender</code> in a comment in this PR for automated rerendering)
* [x] Ensured the license file is being packaged.

No re-render needed: this bump changes only context.version, its sha256 and build_number, none of which affect .ci_support.
