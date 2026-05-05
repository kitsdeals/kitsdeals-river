# Releasing kitsdeals-river to PyPI

The SDK lives at `dealer/kitsdeals-river/` (private monorepo), and is
published to PyPI as `kitsdeals-river` via a one-way release pipeline:

```
dealer/kitsdeals-river/  (source of truth, private)
        │
        │  scripts/release-kitsdeals-river.sh <version>
        │  → git subtree split + force-push + tag
        ▼
github.com/kitsdeals/kitsdeals-river  (public, release-only)
        │
        │  Actions: tag push triggers build + publish
        ▼
       PyPI
```

Day-to-day development happens in `dealer/`. The public repo only
receives tagged release commits — never PRs, never feature branches.

---

## One-time setup

### 1. PyPI account + API token

1. Sign up at https://pypi.org/account/register/ if you don't have an account
2. Visit https://pypi.org/manage/account/token/
3. Click "Add API token", scope it to "Entire account" for the first
   release (we'll narrow it to the `kitsdeals-river` project after the
   first publish — PyPI only allows project-scoped tokens after the
   project exists)
4. Copy the token (starts with `pypi-`)

### 2. Public repo

1. Create `kitsdeals/kitsdeals-river` on GitHub. Empty, no
   README — the first release-script run populates it.
2. Add the PyPI token as a repo secret:
   - Repo → Settings → Secrets and variables → Actions
   - New repository secret
   - Name: `PYPI_API_TOKEN`
   - Value: the token from step 1

### 3. Configure the public remote on your dealer checkout

On whichever machine you run releases from (probably your laptop):

```bash
cd /path/to/dealer
git remote add public-river git@github.com:kitsdeals/kitsdeals-river.git
```

(Or use the HTTPS URL if you prefer; SSH is recommended so you don't
have to enter creds every release.)

### 4. Verify the dealer release script

```bash
./scripts/release-kitsdeals-river.sh
# Should print: "Usage: ./scripts/release-kitsdeals-river.sh <version>"
```

If it errors on missing remote, redo step 3.

### 5. After the first release

Once the first publish lands on PyPI, narrow the API token:

1. PyPI → Account settings → API tokens → revoke the entire-account token
2. Generate a new token scoped to "Project: kitsdeals-river"
3. Update `PYPI_API_TOKEN` in the public repo's secrets

---

## Per-release process

Every release is three steps. Takes ~3 minutes including waiting for
the Action to finish.

### 1. Bump version

Edit two files in `kitsdeals-river/`:

- `pyproject.toml` — `version = "0.3.1"`
- `src/kitsdeals_river/__init__.py` — `__version__ = "0.3.1"`

The release script verifies these match and aborts otherwise — saves
you from publishing a mismatched build.

### 2. Commit on `dealer/main`

```bash
git add kitsdeals-river/pyproject.toml kitsdeals-river/src/kitsdeals_river/__init__.py
git commit -m "kitsdeals-river: bump to 0.3.1"
git push origin main
```

(If the bump is part of a substantive change PR, the commit can be the
PR's merge commit on main — the version bump just needs to be on main
before you run the release script.)

### 3. Run the release script

```bash
./scripts/release-kitsdeals-river.sh 0.3.1
```

What it does:

- Verifies `pyproject.toml` and `__init__.py` versions both equal `0.3.1`
- Verifies working tree is clean and you're on `main`
- Pulls latest `main` from origin
- `git subtree split --prefix=kitsdeals-river -b kitsdeals-river-release-0.3.1`
- Force-pushes the split branch to `public-river/main`
- Tags `v0.3.1` on the public remote
- Cleans up the local split branch

The tag push triggers the public repo's `release.yml` Action, which
builds the sdist + wheel and publishes to PyPI. Watch:

- Public Actions tab: https://github.com/kitsdeals/kitsdeals-river/actions
- PyPI project page: https://pypi.org/project/kitsdeals-river/

### 4. Verify

```bash
pip install --upgrade kitsdeals-river==0.3.1
python -c "import kitsdeals_river; print(kitsdeals_river.__version__)"
# → 0.3.1
```

---

## What if something goes wrong?

### "Tag does not match pyproject.toml version" in the Action

You bumped one file but not the other (or didn't commit the bump
before running the release script). The release script catches this
locally, but if you forced past, the Action catches it. Fix: bump both
files, commit, re-run release for a NEW version (don't reuse the
broken tag — yank it on the public repo and increment).

### Action fails on `twine upload` with 403

The PyPI token is wrong, expired, or scoped to a different project.
Regenerate (one-time setup step 1), update the repo secret, re-trigger
the Action by re-running it from the Actions UI.

### "Remote 'public-river' not configured"

Run the one-time setup step 3.

### I need to yank a release

PyPI doesn't allow re-uploading the same version, so:

1. Bump version (`0.3.1` → `0.3.2`)
2. Yank the bad version on PyPI: project page → Manage → Releases → Yank
3. Re-release from the new version

---

## Why this shape (vs. alternatives)

- **Why not develop directly in the public repo?** Cross-cutting
  changes that touch dealer/api and the SDK together would need
  coordinated PRs in two repos. The SDK is small enough that a
  one-way release pipeline is much less friction than dual-repo dev.

- **Why force-push to public/main rather than merging?** The public
  repo is a release artifact, not a development branch. Each release
  is a clean, replayable history of the subdirectory at that point
  in time. Force-push makes that explicit and avoids pseudo-merges
  that drift from the dealer repo's truth.

- **Why tag on the public remote, not local?** Tagging locally + push
  works too, but tagging on the remote keeps the dealer repo's tag
  namespace clean (no `v0.3.1` from kitsdeals-river mixing with
  potential dealer-side release tags later).
