- Tasks are GitHub issues. `_docs/tasks.md` was the seed backlog; once
  the issues exist, they are the only backlog
- Every change reaches `main` through a pull request
- Commit regularly, push as you go

Labels

- `mvp` - needed for the v1 defined in `_docs/SPEC.md`
- `post-mvp` - real work, deliberately not now. The SPEC's cut list lands
  here when time runs short
- Every issue carries exactly one of the two, new ones included
- `groomed` - the PM has rewritten it with `_docs/task-template.md`.
  Nothing is implemented without it
- `serial` - the issue runs Docker Compose stacks or touches the kind
  cluster. See "Shared machine" below

Background

- `_docs/SPEC.md` - the problem, behaviour and acceptance criteria
  (`AC-…`). Read the sections an issue references before grooming,
  implementing or checking it. Do not reopen a decision in the spec
  without changing the spec first, in its own PR
- Where an issue and the spec disagree, stop and comment on the issue


Roles

- PM - grooms a task before anyone implements it, follows _docs/team/pm.md
- Engineer - implements one groomed task, follows _docs/team/software-engineer.md
- QA - checks the result against the acceptance criteria, follows _docs/team/qa-engineer.md

Each role is also a Claude Code subagent in `.claude/agents/` (`pm`,
`software-engineer`, `qa-engineer`) that reads its file from
`_docs/team/`.


Orchestrator

Work in isolated git worktrees and run up to five agents in parallel.

You are the orchestrator. Your job is to:

- Split the backlog into independent issue tracks.
- Give each agent a separate worktree and clear file ownership.
- Keep the agent slots full while eligible work remains.
- Send completed work to a fresh reviewer.
- Return review findings to the correct implementer.
- Merge approved work into the main branch one issue at a time.
- Coordinate the work. Never implement the issues yourself.

The main session is the orchestrator. It launches the PM, the engineer
and QA as subagents. It does not groom, implement or test itself, and
subagents do not launch other agents.

The orchestrator owns three things the subagents cannot see: the
dependency order of the backlog, the worktrees, and the merge queue.

Every prompt to a subagent names the issue number, the absolute path of
its worktree, the branch, the worktree's `DATABASE_URL`, and the session
scratchpad path. A subagent starts in the main checkout, so without the
path it works in the wrong place.

Review findings go back to the engineer who wrote the branch. Continue
that agent with SendMessage when it is still available; otherwise start
a fresh engineer with the issue, the PR and the QA comment as input.
Every QA run is a fresh agent that never saw the implementation.


Working in parallel

Work runs in waves. A wave is a set of issues that can be built at the
same time without waiting on each other.

- Up to 5 agents run at once
- Every issue in a wave gets its own git worktree and its own branch
- Nothing is implemented in the main checkout. Main is for grooming,
  integration and the docs

An issue may enter a wave only when all of these hold:

- It is labelled `groomed`
- Every issue under its "Depends on" is merged into main
- No other issue in the same wave adds an Alembic migration. Two
  branches that each add one produce two heads, and the second merge
  breaks `alembic upgrade head`
- No other `serial` issue is in progress
- The orchestrator has read its Constraints section and knows which
  shared files it will touch. Two issues that both rewrite the same
  module do not run together

Everything else waits for the next wave. A wave is often smaller than 5
because the backlog runs out of independent work, not because the limit
was reached - that is normal, do not pad a wave to fill it.

QA starts on an issue as soon as its engineer finishes. QA does not wait
for the rest of the wave, and the next issue can start as soon as a slot
frees up.


Shared machine

Everything runs on one VM with 4 vCPU and 5.3 GB of RAM. Agents are
cheap; what they start is not.

- Postgres is one container for every worktree, `runwatch-pg` (see
  below). Databases inside it are cheap; a second container is not
- A Compose stack with the observability services, or the kind cluster,
  takes most of the machine and binds fixed ports (8080, 3000, 9090…).
  Only one `serial` issue runs at a time, and its agent stops the stack
  with `docker compose stop` when done
- Compose and the kind cluster never run at the same time
- Tests that start the API or the worker as processes bind a free port
  the test picks, never a fixed one. Two worktrees run their suites at
  the same time


Worktrees

One issue, one worktree, one branch, created from the main checkout:

    git fetch origin
    git worktree add ~/projects/runwatch-wt/<n> -b issue-<n>-<slug> origin/main

The worktrees live outside the repository so nothing inside it ever sees
them.

Postgres, once per machine. If `docker ps -a` already lists
`runwatch-pg`, `docker start runwatch-pg` is enough:

    docker run -d --name runwatch-pg -p 5432:5432 \
      -e POSTGRES_PASSWORD=postgres postgres:16

Each worktree is a full checkout and needs its own setup before an agent
touches it:

- `uv sync` - the worktree has its own `.venv`
- `cd frontend && npm ci` once `frontend/` exists - its own
  `node_modules`
- A database of its own:
  `docker exec runwatch-pg psql -U postgres -c 'CREATE DATABASE runwatch_wt<n>'`.
  If the name is already taken, pick a fresh one (`runwatch_wt<n>b`)
  rather than dropping it
- `.env` copied from `.env.example` once it exists, with `DATABASE_URL`
  pointed at that database

Two worktrees sharing a database drop each other's tables in the middle
of a run, and the failures look like impossible bugs in the code rather
than what they are. So every test command is run with the database named
explicitly, in the URL scheme `.env.example` uses:

    DATABASE_URL=<scheme>://postgres:postgres@localhost:5432/runwatch_wt<n> make test

A variable exported in the terminal beats `.env`, which is why the
prefix is not optional.

One suite at a time inside a worktree. The database is per worktree, not
per process, so two runs started together in the same worktree reset it
underneath each other. That produces a scatter of failures that look
like a real regression and are not. A run that fails strangely gets
repeated alone before it is believed. Before starting a run, confirm no
`pytest` is already alive in that worktree (`pgrep -af pytest`).

One agent per worktree at a time. An engineer commits, pushes and is done
before QA is pointed at that worktree; the orchestrator does not overlap
them.

Waiting is part of the job. Run the suite in the foreground, or poll in
a loop that ends. Nothing wakes an agent that has stopped, so "I will
report when it finishes" is where the work ends: the run completes and
nobody reads it. The same goes for anything else slow - a container
coming back, a CI run, a rollout. Poll it with a ceiling, and if it
never arrives say so. "Did not recover within two minutes" is a finding,
and often a FAIL. Silence is not.

Never `pkill -f pytest`, `pkill uvicorn` or anything else matched by
name. Every worktree runs its own suite and its own processes, so that
pattern reaches into another agent's run and kills it. Kill a background
job by the id the tool gave you, or let it finish. If you did not start
it, do not kill it.

A merged worktree is left where it is. Reuse it for the next issue in
the same area, or leave it alone. A stale checkout and an idle database
cost nothing next to a stalled run.


Destructive commands stall the run

The harness checks commands that destroy things and asks the person
running the session to approve them. That is the right behaviour, but it
means the work stops dead until someone is at the keyboard. A wave of
five agents can sit idle overnight on one `rm`.

So nothing in this process deletes. Not worktrees, not branches, not
databases, not containers, not volumes, not temporary files.

- Restore a file you changed on purpose with `git restore <path>`, never
  by copying it aside and deleting the copy
- `git checkout <commit> -- <path>` *stages* what it writes, so a later
  `git restore <path>` finds nothing to undo and leaves the old file in
  place. After restoring anything, `git status` and `git diff HEAD` both
  have to be empty before the work is called done
- Temporary files go in the session scratchpad, outside the repository,
  never in `/tmp` and never next to the code
- Stop containers, do not remove them. `docker compose stop`, not
  `down`, and never `-v`
- Merge PRs without `--delete-branch`
- Never force-push, with or without a lease

If something genuinely has to be removed, that is the user's call. Say
what should go and why, and let them run it.


Pull requests

The engineer opens one PR per issue from its branch, with `Closes #<n>`
in the body, and pushes to it as the work goes. QA posts its verdict as
a comment on the PR. Everyone uses one GitHub account, and GitHub does
not let an account approve its own PR, so the verdict is a comment, not
a review.

Once CI exists (issue for AC-28), a PR with failing checks is not
mergeable, whatever QA said.


Integration

PRs merge one at a time, never in parallel, in dependency order. In the
issue's worktree:

1. `git fetch origin && git merge origin/main`. A merge, not a rebase:
   the branch stays a fast-forward of what is on origin, so the push
   that follows is an ordinary one
2. If the merge conflicts in a shared file (`pyproject.toml`, `uv.lock`,
   `Makefile`, `compose.yaml`, `frontend/package-lock.json`,
   `.env.example`, `AGENTS.md`), resolve it - take both sides, then
   `uv lock` or `npm install` to regenerate a lockfile. Any conflict in
   application code goes back to the engineer as a FAIL
3. Run the whole suite, ruff, and `alembic heads` (exactly one head)
   after the merge
4. Push the branch, wait for CI to pass once it exists
   (`gh pr checks <n> --watch`, with a timeout)
5. `gh pr merge <n> --squash` - one commit per issue on main, and
   `Closes #<n>` closes the issue
6. `git pull` in the main checkout

Step 3 is what keeps the wave honest. The second PR to merge is being
tested against code its author never saw, so it re-runs against the
merged result before it is trusted.

A merge that breaks the branch goes back to that branch's engineer with
the failure, as a FAIL. The orchestrator does not fix it.


Lifecycle

1. Pick the next wave: groomed issues whose dependencies are all merged
2. PM grooms each ungroomed issue that could be in the next wave. PMs
   can run in parallel with engineers, since grooming touches only
   GitHub
3. Set up a worktree per issue, then launch one engineer per issue, in
   parallel
4. QA verifies each one in its own worktree as its engineer finishes
5. On FAIL, back to step 3 for that issue alone, with the QA comment as
   input. The rest of the wave carries on
6. On PASS, integrate that PR through the merge queue
7. Leave the worktree and its database in place
8. Repeat until no open `mvp` issue is left


Rules

- One issue per worktree, one engineer per issue
- Do not skip step 2, even when the task looks obvious
- The engineer does not merge or close, QA does not fix the code
- Do not commit until the tests pass
- An agent stays inside its own worktree. Reading main is fine, writing
  to it or to another worktree is not
- Only the orchestrator merges PRs, and nobody deletes worktrees
