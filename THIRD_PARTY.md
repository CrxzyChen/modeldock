# Third-party licensing boundaries

Apache-2.0 applies to original ModelDock code and documentation, including its MediaCenter-named modules. It does not replace third-party licenses.

- JavaScript dependencies: retain upstream license and attribution files. `package-lock.json` records dependency versions and available license metadata; it is not a substitute for upstream license texts.
- Electron distributions: retain `LICENSE.electron.txt` and `LICENSES.chromium.html` supplied with the packaged application.
- Python dependencies and container OS/runtime packages: follow the licenses of the exact versions distributed. Container build definitions are not a relicensing of their contents.
- `mediacenter/sdxl_config/`: upstream-derived SDXL configuration/tokenizer material, not original ModelDock material. `PROVENANCE.json` identifies the source revision. Consult the upstream SDXL repository and applicable component licenses before redistribution; these files are excluded from the project's blanket Apache-2.0 grant.
- Downloaded/imported model weights, LoRA, VAE and generated user assets are not licensed by this repository's LICENSE. Obtain the applicable model permissions separately.

This boundary notice is not a complete dependency license audit or a statement that every possible model/runtime can be commercially redistributed. Preserve upstream notices and review the components actually shipped in your distribution.
