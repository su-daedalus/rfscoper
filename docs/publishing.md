# Publishing rfscoper to GitHub

A one-time checklist for putting this project on GitHub, written for the
project owner. Each step is one idea; run them in order.

## 1. Tell git who you are (once per machine)

```bash
git config --global user.name "su-daedalus"
git config --global user.email "ID+su-daedalus@users.noreply.github.com"
git config --global init.defaultBranch main
```

The `noreply` address keeps your real email out of the public commit history.
Find the exact one at GitHub → Settings → Emails, with "Keep my email
addresses private" turned on.

## 2. Start the repository

```bash
cd rfscoper
git init
git add --dry-run .     # look: this is exactly what will be published
git add .
git commit -m "Initial commit: rfscoper 0.1.0"
```

`git add --dry-run` lists what *would* be added without adding anything.
Captures, plots, `.venv` and caches must not appear in that list; if one does,
stop and fix `.gitignore` before committing.

### Safety check before every push

```bash
git ls-files | grep -Ei '\.(csv|json|pcap|iq|env|key|pem|db)$|captures/|(^|/)probe-[^/]+/' \
  && echo "STOP: private data is staged" || echo "OK: no captures or secrets"
git grep -nIiE 'ghp_|github_pat_|BEGIN .*PRIVATE KEY|password *=' -- ':!docs/publishing.md' \
  && echo "STOP: possible secret" || echo "OK: no secrets"
```

If either says STOP, unstage the file (`git rm --cached FILE`) and add a
rule to `.gitignore`. Never force-add an ignored file with `git add -f`.
A file that was pushed once stays in the history even after it's deleted, so
check before the push, not after.

## 3. Create the empty repository on GitHub

On github.com: New repository → name `rfscoper` → leave README, .gitignore
and license **unchecked** (the project already has them) → Create.

## 4. Connect and push

```bash
git remote add origin https://github.com/su-daedalus/rfscoper.git
git push -u origin main
```

GitHub no longer accepts account passwords on the command line. Either run
`gh auth login` (GitHub CLI) once, or create a personal access token and paste
it when asked for a password.

## 5. Tag the release

```bash
git tag -a v0.1.0 -m "rfscoper 0.1.0"
git push origin v0.1.0
```

Then on GitHub: Releases → Draft a new release → choose tag `v0.1.0` → paste
the matching section of `CHANGELOG.md`.

## 6. Finish the repository page

- Add a description and topics: `sdr`, `hackrf`, `spectrum-analyzer`, `python`, `rf`
- Wait for the first CI run and confirm the badge in the README turns green
- Settings → Code security: turn on **secret scanning** and **push
  protection** (GitHub refuses a push that contains a token or key), plus
  **Dependabot alerts** and **private vulnerability reporting** (what
  `SECURITY.md` points people to)
- Settings → Branches: add a rule for `main` that requires the CI checks to pass
- Your account: turn on **two-factor authentication** (Settings → Password and
  authentication) if it isn't already

## Day-to-day

```bash
git status              # what changed
git add -p              # review and stage changes piece by piece
git commit -m "..."     # save a snapshot with a message
git push                # publish
```
