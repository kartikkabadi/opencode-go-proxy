# Publishing

This repository is publish-ready for GitHub as
`kartikkabadi/opencode-go-proxy`.

## Release surface

- Package: `deepseek-responses-proxy`
- Current version: `0.1.0`
- CLI entry point: `deepseek-responses-proxy`
- Python: `>=3.11`
- Build backend: `uv_build`
- Verification: `uv run python -m pytest tests -v`,
  `uvx ruff check`, `uv build`
- AUR staging package: `aur/deepseek-responses-proxy-git`

## GitHub setup

```bash
gh repo create kartikkabadi/opencode-go-proxy --public --source . --remote origin --push
```

Use `--private` instead of `--public` if this should remain private
while the Codex/DeepSeek behavior is still moving.

## AUR setup

The fast Arch path is the VCS package `deepseek-responses-proxy-git`. The
staging files live under `aur/deepseek-responses-proxy-git/`:

- `PKGBUILD`
- `.SRCINFO`

Quick publish flow:

```bash
git clone ssh://aur@aur.archlinux.org/deepseek-responses-proxy-git.git aur-publish
cp aur/deepseek-responses-proxy-git/PKGBUILD aur-publish/
cp aur/deepseek-responses-proxy-git/.SRCINFO aur-publish/
cd aur-publish
git add PKGBUILD .SRCINFO
git commit -m "Initial import"
git push
```

Before pushing, regenerate `.SRCINFO` after every PKGBUILD edit:

```bash
makepkg --printsrcinfo > .SRCINFO
makepkg --verifysource
```

The AUR package installs the console script and a user service at
`/usr/lib/systemd/user/deepseek-responses-proxy.service`. Users still need to
provide the upstream key via `DEEPSEEK_API_KEY` or the macOS keychain.

## License

MIT. See [LICENSE](LICENSE).

## Pre-release checklist

1. Confirm the repository visibility.
2. Run the verification commands.
3. Create the GitHub repository and push `main`.
4. Enable branch protection after CI is green.
