# GitHub Actions Setup Guide
## Get @claude working in your P2P repo in ~10 minutes

---

## What you get after setup

| Trigger | What Claude does |
|---------|-----------------|
| Open an issue titled `@claude — add retry for 503 in PR5` | Claude edits the file, commits, opens a PR |
| Comment `@claude` on any PR | Claude reviews the change against CLAUDE.md |
| Comment `@claude update PR2 to add the urgent flag` | Claude edits PR2_REQUISITION.md, commits, opens a PR |
| Every Monday 6am UTC (automatic) | Claude checks all 7 skill files for gaps, opens Issues for anything it finds |
| Click "Run workflow" in GitHub Actions tab | Manually trigger the weekly review any time |

---

## Step 1 — Install the Claude GitHub App (2 min)

**Option A — Automatic (recommended if you have Claude Code installed):**
```bash
# In your terminal, inside your project folder:
claude
# Then type:
/install-github-app
# Follow the prompts. It sets up everything and opens a PR.
```

**Option B — Manual:**
1. Go to: **https://github.com/apps/claude**
2. Click **Install**
3. Choose your repository
4. Accept the permissions (Contents read/write, Pull Requests read/write, Issues read/write)

---

## Step 2 — Add your Anthropic API key as a GitHub Secret (2 min)

1. Go to your repo on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Name: `ANTHROPIC_API_KEY`
5. Value: your key from **https://console.anthropic.com**
6. Click **Add secret**

> Get an API key at console.anthropic.com → API Keys → Create Key.
> Keep it confidential — never paste it in a workflow file or issue comment.

---

## Step 3 — Add the workflow files to your repo (2 min)

Copy these files from this zip into your repository:

```
your-p2p-repo/
├── .github/
│   ├── workflows/
│   │   ├── claude.yml                   ← @claude mention handler
│   │   └── claude_weekly_review.yml     ← Monday morning auto-review
│   ├── ISSUE_TEMPLATE/
│   │   └── skill_update.md              ← Pre-filled issue form
│   └── pull_request_template.md         ← PR checklist
```

Then commit and push:
```bash
git add .github/
git commit -m "feat: add Claude Code GitHub Actions"
git push
```

---

## Step 4 — Test it (1 min)

Open a new Issue in your repo:

**Title:**
```
@claude — what files are in this project?
```

**Body:** (leave blank)

Within ~30 seconds you should see Claude reply in the issue with a list of files. That confirms the whole pipeline is working.

---

## How to ask Claude for updates

### Update a skill file
```
@claude

Update PR5_PURCHASE_ORDER.md to add the createAmendment action sequence.
The API path is:
  POST /purchaseOrders/{POHeaderUniqId}/action/createAmendment

Add it after the existing "PO Amendment Flow" section.
Follow the same format as the other action sequences in that file.
```

### Fix an error in the retry module
```
@claude

In src/oracle_retry.py, the 408 status code is classified as RETRY_SAFE
for POST calls. It should be RETRY_WITH_CHECK because Oracle may have
processed the request. Please fix the classify_error function.
```

### Add a new config value
```
@claude

Add a new config value to config/config.yaml called
`invoice_match_tolerance_pct` with a default value of 5.
Then reference it in PR6_RECEIVING.md in the 3-way match section
instead of the hardcoded "5%" that's currently there.
```

### Run the weekly review right now
Go to: **Actions tab → Claude Code Nightly Skill Review → Run workflow**

---

## What Claude will NOT do automatically

- Push directly to `main` without a PR (all changes go through PR review)
- Modify Oracle credentials or config secrets
- Delete files
- Change anything outside the `skills/`, `src/`, `config/`, `samples/` folders

These guardrails come from the `permissions` block in the workflow files
and the rules in `CLAUDE.md`.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Claude doesn't respond to @mention | Check Actions tab — is the workflow enabled? Is `ANTHROPIC_API_KEY` secret set? |
| `Resource not accessible by integration` error | Re-install the Claude GitHub App and confirm it has PR write access |
| Claude opens a PR but edits the wrong file | Add more context to your issue — filename and section help |
| Weekly review never runs | Check the cron syntax and that the workflow file is on the `main` branch |

---

*Setup time: ~10 minutes | Requires: GitHub repo admin access + Anthropic API key*
