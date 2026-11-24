# simulate-action

Composite GitHub Action that runs a Plurai agent simulation in CI with `plurai ci run`. It publishes the result as the job
summary, as a sticky PR comment and as artifacts, and it ends the job with the command's exit code.

The action is the only place that knows the GitHub event. `plurai ci run` reads no GitHub token and no GitHub event: the
action maps the trigger to a selection, finds the base commit and main's last run, and hands the command plain arguments.

## Use

The `/simulation-ci` skill renders this workflow. It has two jobs, and the split is a security boundary: the `simulate` job
runs the agent's code with a read-only token, and the `report` job holds the only write permission and never runs that code.

The action runs on whatever events the caller's workflow names. A run makes many calls and spends credits, so the workflow
starts by hand (`workflow_dispatch`, `smoke` unless the run picks `full`) and every other trigger is the caller's choice,
including a run on every pull request.

```yaml
on:
  workflow_dispatch:
    inputs:
      selection:
        type: choice
        options: [smoke, full]
        default: smoke
  # Each trigger below is opt-in, and each run of it spends credits.
  # pull_request:                  # smoke
  # merge_group:                   # smoke; only with pull_request
  # push:                          # full
  #   branches: [main]
  # schedule:                      # full, nightly
  #   - cron: '0 3 * * *'
permissions: {}
jobs:
  simulate:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      actions: read          # fetch main's run.json for "vs main"
    steps:
      - uses: actions/checkout@<sha>
        with:
          persist-credentials: false   # the agent's code must not find the job token in .git/config
      # --- user-owned: install your agent's dependencies ---
      - uses: astral-sh/setup-uv@<sha>
      - run: uv sync --directory agents/support
      # --- end user-owned ---
      - uses: plurai-ai/simulate-action@v1
        with:
          workdir: agents/support/plurai_sim
          token: ${{ secrets.PLURAI_TOKEN }}
          selection: ${{ inputs.selection }}   # workflow_dispatch only
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}   # the names in plurai.config.yaml agent.env

  report:
    needs: simulate
    if: always()
    runs-on: ubuntu-latest
    # Drop this whole job to turn off both the comment and the check.
    permissions:
      pull-requests: write   # the PR comment
      checks: write          # the check run that shows the report
    steps:
      - uses: plurai-ai/simulate-action/comment@v1
        with:
          result: ${{ needs.simulate.result }}
```

Why two jobs. The agent's code runs in the `simulate` job as the same user as the action's own scripts, so it can read
the job's credentials from the process list and the checkout, and it can rewrite the action's scripts. Whatever runs after it
in that job is therefore not trusted. The `simulate` job holds a read-only token and nothing after the agent's run uses a
credential. The `report` job runs on a fresh runner, downloads the `simulate` job's report artifact, treats it as text, edits
the sticky comment and creates the check run. Keep `pull-requests: write` and `checks: write` in that job only. Dropping the
job turns off both the comment and the check.

Use one action step per job. Two steps in one job share an out dir and an artifact name; give each agent its own workflow file
(the template does). The `report` job's `job` input names the `simulate` job's id (`simulate` by default); change it when the
job has another id.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `workdir` | `.` | The agent's `plurai_sim/` directory. `plurai ci run` runs from here. |
| `url` | empty | A non-production backend URL, for Plurai's own staging and dev runs. Empty runs against production; a customer repository leaves it out. |
| `token` | empty | A Plurai service-account key (`secrets.PLURAI_TOKEN`). Empty on a fork PR: the run still starts and the command exits 20, "not measured: no secrets". |
| `selection` | empty | `smoke` or `full`. Honoured on `workflow_dispatch`, where empty means `smoke`; ignored, with a warning, on every other event. |
| `include_content` | `auto` | Transcripts, judge text, the quotes in `run.json` and the suite diff body. `auto` is on for a `private` repository only; `internal` counts as public. |
| `retention_days` | `7` | Days GitHub keeps the transcripts artifact, 1 to 90. `run.json` and the reports keep the repository's retention. |

`token`, and `url` when it is set, reach `plurai ci run` as `PLURAI_TOKEN` and `PLURAI_URL`, set on that one step. They never appear in a job's
`env:`, so no other step sees the token, and a backend rename is an action release, not a re-render in every customer repo.

### The `comment` sub-action

`plurai-ai/simulate-action/comment@v1`, for the `report` job. It posts the sticky PR comment and creates a check run that shows the report.
The job needs `pull-requests: write` for the comment and `checks: write` for the check.

| Input | Default | Meaning |
| --- | --- | --- |
| `job` | `simulate` | The id of the job that runs the action. It names the report artifact and the sticky comment. |
| `result` | empty | `${{ needs.simulate.result }}`. A job that left no report artifact (cancelled, crashed) gets a comment that says so when the PR has none yet; an existing comment is left as it is, with a note in the job summary. A skipped job gets none. |
| `pr_comment` | `true` | `false` makes the comment step a no-op. |
| `test_report` | `true` | `false` makes the check-run step a no-op. |
| `check_name` | empty | The check run's name. Empty names it after the workflow: `Plurai simulation results (<agent>)` for a workflow from the plugin template. It differs from the `simulate` job's check, the one branch protection requires. |

Output `comment-url`: the comment's URL, empty when none was posted. The sub-action posts only on a same-repository
`pull_request`; a fork PR and a Dependabot PR get a note in the job summary instead, and a token that cannot write is a note,
never a failed job. The comment records the head sha of its run on a third marker line. A run that is no longer the head of the
PR (read with the `report` job's token) leaves the comment alone and logs why, and a cancelled run does not replace a comment
written for another head unless it is the PR's head.

The check run follows every event that ran the `simulate` job, not only `pull_request`. The sub-action creates it through the
Checks API with the job's `GITHUB_TOKEN`, from the downloaded `report.md` (the same report the job summary holds), and no outside
action is involved. Its fields:

| Field | Value |
| --- | --- |
| `name` | `check_name`: `Plurai simulation results (<agent>)`, stable per agent, never the required `Plurai simulation (<agent>)`. |
| `head_sha` | The PR head on `pull_request`, else `GITHUB_SHA`. |
| `conclusion` | `success` when the report's heading is ✅, `failure` for ❌, `neutral` for ⛔ (not measured; GitHub counts `neutral` as success for a required check, and this check is never the required one). |
| `output.title` | The heading's counts line, for example `❌ 2 failed · 0 unstable · 0 advisory · 0 not measured`. |
| `output.summary` | The rest of `report.md`, cut to 65,535 characters with a pointer to the job summary. |
| `details_url` | The run's page. |

Output `check-url`: the check run's URL, empty when none was created. The token is scoped to this repository and this run, with
`checks: write` and `pull-requests: write` and every other scope at `none`: it can create check runs and write to the repository's
pull requests and their comments. It cannot push code, read secrets or touch workflows. A fork PR and a Dependabot PR get a note
in the job summary instead (their token cannot create a check). The step never fails the job: a token without `checks: write`
leaves a note, any other API error a warning, and a run that left no `report.md` creates no check.

## Outputs

| Output | Meaning |
| --- | --- |
| `out-dir` | The directory `plurai ci run` wrote into. |
| `selection` | `smoke` or `full`. |
| `base` | The `--base` value, empty when none could be derived. |
| `main-run` | The `--main-run` path, empty when none was found. |
| `summary-path` | The job summary the action wrote, as a file. |
| `sdk-version` | The SDK version the run uses: the project's own when it is in range, else the pin the action installed. |

## Triggers

| Event | Selection | Base commit for the suite diff |
| --- | --- | --- |
| `pull_request` | `smoke` | On the default merge checkout (`refs/pull/N/merge`), the first parent of the merge commit, so the diff is the PR's own change even when the base branch moved after the branch point; on a checkout of the PR head, the merge-base of the PR base and head, from the compare API |
| `merge_group` | `smoke` | `merge_group.base_sha` |
| `push` | `full` | `github.event.before` (none on a new branch) |
| `schedule` | `full` | The head commit of main's last completed run of this workflow (a `schedule` event carries no repository, so the action reads the default branch from the ref it runs on and the visibility from the API) |
| `workflow_dispatch` | the `selection` input, else `smoke` | As `schedule` |

"Main's last run" means the last run of this workflow on the branch the run lands on: the PR's base branch for a
`pull_request`, the target branch for a `merge_group`, the default branch otherwise.

A job that is one leg of a `strategy.matrix` ends with exit 65: every leg would overwrite the others' report artifact and PR
comment. Use one `simulate` job per agent.

Any other event, including `pull_request_target`, ends with exit 65: `pull_request_target` would hand the PR's code the
repository secrets. The base commit is fetched into the shallow checkout with the job token, passed to that one `git fetch` through its environment
(the checkout keeps no credential); a failure is a warning and the sha is still passed. Every call to GitHub in the fetch step
and in the report job is a warning or a note when it fails, whether the cause is a status code, a timeout, a dropped connection
or a reply that is not JSON.

## The `plurai ci run` contract

This is the contract `plurai ci run` honours. The action starts, with `exec` so a cancel signal reaches the command and its exit code is the step's:

```
plurai ci run --repo <owner>/<repo> --selection <smoke|full> [--base <sha>] [--main-run <path>] [--visibility <public|private|internal>]
              [--include-content <true|false>] [--run-url <url>]
```

- Run from `workdir`. `PLURAI_URL` is set only when the `url` input is non-empty; with it unset the command runs against production. A `PLURAI_URL` inherited from the job is dropped. `PLURAI_TOKEN` is set, and unset when the `token` input is empty.
- `PLURAI_ACTION_VERSION` is the ref the workflow used after `@` (`v1`, `v1.2.3` or a full commit sha), `github.action_ref`. It is unset when that ref is empty (the action run from its own checkout), and a value inherited from the job is dropped. It never carries a credential. There is no committed version string beside it: it would need a manual bump for every tag. The command records it as `versions.action` in `run.json`.
- `--base` and `--visibility` are left out when unknown; `--main-run` when no main run was found.
- `--include-content` is passed only when the input is `true` or `false`. With `auto` the command derives it from `--visibility`.
- `--run-url` is this attempt's page, `$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/attempts/$GITHUB_RUN_ATTEMPT`: the PR comment links its "job summary" phrases there. It is left out when any of the four variables is empty.
- The environment holds no `GITHUB_TOKEN`, `GH_TOKEN`, `ACTIONS_*` credential, `GITHUB_OUTPUT`, `GITHUB_ENV` or `GITHUB_STEP_SUMMARY`, and none of the variables the action's own steps set. Any other name, including an `agent.env` name that starts with `SA_` or `INPUT_`, reaches the agent.
- `PLURAI_CI_OUT_DIR` names the directory to write into.
- Exit codes, as the summary headings read: `0` ✅, `1` ❌ N failed, `10` ⛔ upgrade required, `20` ⛔ not measured, `65` ⛔ refused, `77` ⛔ token rejected or refused.
  `10` is the backend refusing the SDK version (HTTP 426): the project updates the plugin and the SDK in its lockfile.
- Inside one SDK minor the action sends only the flags and variables that `X.Y.0` of its pin's minor accepts, so an older SDK of the range still understands the command. A new flag arrives with a new minor.

Files the command writes into `PLURAI_CI_OUT_DIR`:

| File | When | Holds |
| --- | --- | --- |
| `summary.md` | always, including a stopped run | The results body, heading first (`## Plurai simulation · …`). Without the suite diff: the action appends it. |
| `comment.md` | always | The PR comment body, opening with `<!-- plurai-simulation-report -->`. |
| `report.md` | always | The full report. |
| `run.json` | every run that measured something, partial on a cancel or exit 20 | The run record. Absent on a refusal. |
| `junit.xml` | with `run.json` | The JUnit mapping. |
| `suite-diff.md` | when committed ids moved | The full suite diff. |
| `suite-diff-comment.md` | with `suite-diff.md` | The short change list for the PR comment. |
| `transcripts/` | per scenario | Transcript files. The action uploads them only when content is included. |

The suite diff files are written before the first scenario starts, each by writing a temporary file in the same directory and
renaming it, so a reader never sees a partial file. The action does not publish them while the run is going: a publisher would
need a token alive next to the agent's code. The suite diff reaches the reader in the final comment and the job summary.

## What the action publishes

- **Job summary.** The results body from `summary.md`, then the suite diff, then the action's own notes (a stale managed block,
  no main run fetched). GitHub uploads a step's summary when the step ends, so the summary is written once, after the run. A
  run that wrote no `summary.md` gets a heading that says what stopped: a cancel, a step before `plurai ci run` that failed
  (the heading names it: the plan, the fetch, the read token for the SDK source or the SDK install), a command that ended without a
  report, or a command that finished green and left none. Over 1 MiB, the suite diff loses its
  `<details>` bodies first and then becomes a pointer to `suite-diff.md`.
- **PR comment**, posted by the `comment` sub-action in the `report` job (same-repository pull requests). One comment per PR,
  workflow and job, found by its marker and the `github-actions[bot]` author, edited in place on later runs. It holds the
  results, then the short suite diff. The main action writes `comment-final.md` into the report artifact; the sub-action posts
  it, cut to the 65,536-character limit.
- **Artifacts**, uploaded with `if: always()` so a red run keeps its data:
  `plurai-<workflow>-<job>-report` (`run.json`, `report.md`, `junit.xml`, `suite-diff.md`, `comment-final.md`; the repository's
  retention) and, when content is included, `plurai-<workflow>-<job>-transcripts` (`retention_days`).
- **Main's run.** The action downloads `run.json` from the latest completed run of the same workflow that ran this repository's
  own code on the default branch (`push`, `schedule` or `workflow_dispatch`; a `pull_request` run never counts, whatever its
  branch is named) and that still has its artifact (success or failure; cancelled runs are skipped), and passes it as
  `--main-run`. The job token stays in that one step.

The action sets no `timeout-minutes`: the caller's job bounds the run.

## SDK install

`release.json` holds the one tested SDK pin per action release: version and source. The project's lockfile is the source of
truth for the SDK, and the action looks only at the project's interpreter: the nearest `.venv` above `workdir`, else
`$VIRTUAL_ENV`, else `python3`. It does not read lockfiles; the SDK's own check refuses a run whose lock names another version.

- **In range:** an SDK installed in the interpreter whose `major.minor` equals the pin's and whose version is at or above the pin
  is used as is. Pre-releases order below their release (`1.1.0.dev405` < `1.1.0rc1` < `1.1.0`), so the pin `1.1.0rc1` accepts
  `1.1.0` and `1.1.3`, and refuses `1.1.0.dev405` and `1.2.0`.
- **None installed:** the action installs `plurai-simulation-sdk==<pin>` from PyPI, with no extras, using `uv pip install` when
  `uv` is on the runner and `pip` otherwise. A failed install ends with exit 65, and the summary and the comment name it.
- **Outside the range:** exit 65, and the message names the range (`>=<pin>,<next minor>`).
- It also refuses (65) when the interpreter is older than Python 3.12.

`release.json` also holds `managed_rev`, the managed-block revision this release expects. A workflow whose first lines carry
`# plurai:managed rev=<n>` with an older `n` gets a warning and a summary note.

## Security

- The `token` is a service-account key with full org access: Plurai has one key type. Every same-repo PR runs its own
  `simulate.py` with it, and the agent holds it (its mock client calls the backend with it). The boundary is code-owner review
  of `.github/workflows/plurai-*.yml` and `plurai_sim/`, enforced on the protected branch with "Require review from Code
  Owners" and "Dismiss stale pull request approvals".
- The `simulate` job holds a read-only `GITHUB_TOKEN` (`contents: read`, `actions: read`), read in one step of the action (the
  fetch of main's run). That step runs after the caller's own install steps (`uv sync` and the like), so the project's code
  has already run on the runner. It is hardened for that: the interpreter is the runner's own and the plan refuses one inside
  the workspace; the scripts run with `python3 -S -E`, which skips the project's `.pth` files and `PYTHON*` variables; git runs
  with `-c core.hooksPath=/dev/null -c gc.auto=0 -c http.proxy= -c http.sslVerify=true -c protocol.ext.allow=never` in front, so the checkout's own config
  cannot add a hook or a proxy, and without `GITHUB_TOKEN` in its environment (the one credential git gets is the scoped header
  of the base fetch). Git gets no `GIT_*`, proxy or `CURL_*` variable, no system or global config, and its `ext::` transport is off (the fifth override); the credentialed fetch runs in an empty scratch repository outside the workspace, from this repository's URL on the job's server, so the checkout's own config (`url.*.insteadOf`, a URL-specific proxy or `sslVerify`) never applies to it; a second fetch with no credential brings the commit into the workspace. Only a full commit id ever reaches a git command line. The token is never in the environment of `plurai ci run`, whatever the caller's job `env:` holds. Set
  `persist-credentials: false` on the checkout so the token is not stored in `.git/config`. Activate a project environment
  (`source .venv/bin/activate`) in the step that runs the agent, not before this action: it would put a workspace interpreter
  first on `PATH`.
- No process that holds a token lives while the agent runs, and no step after the agent's run holds a GitHub or Plurai
  credential: those steps write the job summary and upload artifacts. Agent code can rewrite the action's scripts and the
  runner's step files on the runner, so nothing is trusted from them afterwards. The one credential those steps still use is the
  runner's own artifact credential, so a hostile run can at most change the artifacts of its own run.
- The interpreter, `uv` and `git` the steps use are resolved to absolute paths in the first step, before any step runs project
  code, so a `PATH` changed later cannot swap them. The project's own interpreter and the `plurai` console script come from the
  project environment on purpose. The probes of that interpreter and the installer run with the same deny-list of credential
  and step-file variables as the agent.
- The only write permissions, `pull-requests: write` and `checks: write`, are in the `report` job. It runs on a fresh runner and
  never runs the agent. It reads one file per output of the report artifact as text (`comment-final.md` for the comment,
  `report.md` for the check; a regular file, size-capped), so a hostile artifact can change the text of the comment and of
  the check and nothing else.
- Main's `run.json` is read only from a run of this repository's own code on a trusted event, so a fork PR whose branch is
  named `main` cannot plant one.
- Fork PRs get no secrets and end as "not measured: no secrets"; the job is never skipped, because a skipped required check
  passes. Use `pull_request`, never `pull_request_target`.
- The OIDC token request variables are emptied for every step after the plan, so the agent's code, the install probes and the
  installer cannot request an OIDC token, even when the caller grants `id-token: write` to the job for its own steps.
- Third-party actions inside this action are pinned by full SHA.

## Tests

```
uv run --group dev pytest
uv run --group dev ruff check . && uv run --group dev ruff format --check .
```

The pytest suite runs each script as a subprocess against a fake GitHub API (`tests/fake_github.py`) and the stub `plurai`.
`tests/stub/plurai` records its argv and environment, writes the files above and exits with `STUB_PLURAI_EXIT`. It is a stand-in
for the SDK, which has no stub backend; the real command is covered by the SDK's own tests and by the live seam on a dogfood repo.

`.github/workflows/fixture.yml` runs the action against the stub on a fixture project (`fixture/agent`), with the plugin
template's triggers and concurrency groups, on every PR. It asserts the exit-code paths 0, 1, 10, 20, 65 and 77, the empty-`url`
run against production (no `PLURAI_URL` reaches the command), the summary heading, the artifacts after a failed run,
transcripts by `include_content`, the command line per trigger, the absence of `GITHUB_TOKEN` in the command's environment, and, through its `report` jobs, that a second post on one PR edits
the first comment and that `pr_comment: false` posts none. The fixture's `report` jobs check out the repository to reach the
local `./comment` action; a customer's job does not. A fork PR's skipped comment and a cancelled job's signal are covered by
the pytest suite, since a fork PR cannot be opened from the fixture itself.

`.github/workflows/action-ref.yml` covers what `uses: ./` cannot: `github.action_ref` is empty there. Its one job runs
`plurai-ai/simulate-action@<ref>` by name at the top level of the job, with the stub `plurai` on `PATH`, and asserts that the
stub started with `PLURAI_ACTION_VERSION` equal to that ref, over a job-supplied value that must lose. It runs on
`workflow_dispatch` and on pushes to the lane branch, not on pull requests, because `owner/repo@ref` fetches the ref and not
the PR's head. `uses:` takes no expression, so the ref is a literal that moves to `main` with the lane.

Releasing: [RELEASING.md](RELEASING.md).
