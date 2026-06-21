# Publishing

This repository is publish-ready for GitHub as
`kartikkabadi/opencode-go-proxy`.

## Release surface

- Package: `opencode-go-proxy`
- Current version: `0.1.1`
- CLI entry point: `opencode-go-proxy`
- Python: `>=3.11`
- Build backend: `uv_build`
- Verification: `uv run python -m pytest tests -v`,
  `uvx ruff check`, `uv build`
- AUR staging package: `aur/opencode-go-proxy-git`

## GitHub setup

```bash
gh repo create kartikkabadi/opencode-go-proxy --public --source . --remote origin --push
```

Use `--private` instead of `--public` if this should remain private
while the proxy behavior is still moving.

## AUR setup

The fast Arch path is the VCS package `opencode-go-proxy-git`. The
staging files live under `aur/opencode-go-proxy-git/`:

- `PKGBUILD`
- `.SRCINFO`
- `opencode-go-proxy.install`

Quick publish flow:

```bash
git clone ssh://aur@aur.archlinux.org/opencode-go-proxy-git.git aur-publish
cp aur/opencode-go-proxy-git/{PKGBUILD,.SRCINFO,opencode-go-proxy.install} aur-publish/
cd aur-publish
git add PKGBUILD .SRCINFO opencode-go-proxy.install
git commit -m "Initial import"
git push
```

Before pushing, regenerate `.SRCINFO` after every PKGBUILD edit:

```bash
makepkg --printsrcinfo > .SRCINFO
makepkg --verifysource
```

The AUR package installs the console script and a user service at
`/usr/lib/systemd/user/opencode-go-proxy.service`. Users still need to
provide the upstream key via `OPENCODE_GO_API_KEY` or the macOS keychain.

## License

MIT. See [LICENSE](LICENSE).

## Pre-release checklist

1. Confirm the repository visibility.
2. Run the verification commands.
3. Create the GitHub repository and push `main`.
4. Enable branch protection after CI is green.
