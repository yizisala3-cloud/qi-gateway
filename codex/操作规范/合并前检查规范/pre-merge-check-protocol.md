# Pre-Merge Check Protocol

> Updated 2026-10-13 (America/Los_Angeles): the user reaffirmed that AI executes short migrations directly; lengthy migrations may be handed off as complete files for the user to copy and paste. Apply the standing rule in §5 and record each migration's actual state before concluding the check.

## 1. Purpose and Scope

Confirm the exact merge inputs, complete change set, integration result, execution prerequisites, and final remote state. The outcome must prevent merging into the wrong repository or branch, omitting required changes, overwriting existing work, or reporting an unsuccessful push as complete.

Follow the [workspace documentation rules](../../控制台/目录说明.md) for document updates and status recording.

## 2. Establish the Inputs and Authorization

Before taking action, identify and record:

- Repository and absolute workspace path.
- Source branch and full commit SHA; target remote, repository, branch, and current SHA.
- The complete commit and file range, including dependencies, configuration, SQL migrations, tests, and documentation.
- Staged, unstaged, and untracked changes; distinguish this task's content from unrelated work.
- The user's existing authorization: checks only, or checks followed by commit, merge, and push.

For SQL needed by this selected change, the user's standing authorization in §5 covers direct execution of short migrations. Do not ask again merely because the target is production or the SQL is stored in a file. An explicit instruction to perform read-only checks, defer a particular migration, or avoid production changes takes precedence. Do not execute unrelated or explicitly deferred migrations.

Verify the actual remote state. A stale local remote-tracking reference is insufficient.

Preserve existing work. Do not overwrite unrelated changes, clean files, rewrite history, or force-push without explicit authorization. Authorization already given in this conversation remains valid; do not ask for it again.

## 3. Check the Complete Integration Result

Inspect the full source-to-target difference, not only the latest commit:

- Required commits, files, dependencies, configuration, migrations, and resources are included.
- Unrelated task changes, temporary files, credentials, and unwanted generated files are excluded.
- Target-branch content is not accidentally omitted, removed, or overwritten.
- Changes explicitly reverted or excluded by the user are not reintroduced.
- Branch divergence, conflicts, and unfinished merge operations are identified.

When branches diverge or conflicts exist, construct the actual merge result in an isolated workspace and check its final file tree. Preserve the original workspace. If resolving a conflict requires a product or business decision, identify the specific decision rather than guessing; continue independent checks that do not depend on it.

## 4. Run Necessary Verification

Run the project's required checks and the existing build, type, test, or integration checks appropriate to the merged content.

Record the exact version checked, commands or procedures used, results, failures, and skipped checks. Reuse existing evidence only when it covers the current content and relevant execution conditions. If source, target, dependencies, or relevant conditions have changed, repeat the affected checks.

Distinguish passed, failed, and not executed. Missing verification is not a pass. Do not alter business code during this check unless fixing it is already within the user's authorization; after an authorized change, repeat the affected checks against the new result.

## 5. Check Execution Prerequisites and Supabase SQL

For database, configuration, or deployment changes, establish:

- Required migrations, configuration, dependencies, and static resources are included.
- Necessary environment variables and permissions are available.
- Execution order, compatibility constraints, and outstanding steps are explicit.
- Verify database prerequisites against the intended environment's actual columns,
  tables, constraints, triggers, and RPC signatures, as well as migration records.
  A script included in Git or an unmatched file version does not establish its
  execution status; manually applied SQL may have a different or absent record.
- Validate forward migrations against the previous schema with representative
  existing data. Check backfill order against old CHECK / NOT NULL constraints
  and triggers, RPC replacement and overloads, repeat execution, and complete
  transaction rollback on failure. An empty-database replay alone is insufficient
  for a migration that converts existing data or replaces its constraints.
- Production actions outside the SQL rule below are within the user's authorization.

### Required migration inventory

Before issuing a merge conclusion, build an inventory of every database prerequisite used by the final candidate. Include dependencies introduced in earlier commits or source branches; a final frontend-only diff does not prove that its backend already has the required schema or RPCs. Read the current-state document and actual target database, and trace the candidate's table, column, trigger and RPC references.

For each required migration, record:

| Migration file / source version | Target project / environment | Dependency and execution order | Actual database evidence | Execution and verification state | Executor / next action |
| --- | --- | --- | --- | --- | --- |
| Full SQL file path and commit or fingerprint | Confirmed project and database | Including compatibility with the running code | Migration record plus actual objects/signatures/permissions | Pending AI execution, awaiting user execution, failed, executed awaiting verification, or verified | AI execution or user file handoff, with the next concrete step |

If no SQL is required, state that conclusion and its basis explicitly. SQL included in Git, a successful isolated PostgreSQL test, or a matching filename alone does not establish production execution. If manual execution or a tool uses a different migration version, record the source-file-to-remote-version mapping and verify the actual objects before deciding whether anything needs rerunning.

### Supabase SQL execution

Standing user instruction, reaffirmed 2026-10-13: **AI executes short migrations directly; lengthy migrations may be handed off to the user as complete files.** This applies to SQL required by the selected work, subject to any explicit restriction in the current task.

When this change requires Supabase SQL:

1. Confirm the intended Supabase project, the complete SQL and source version, its dependencies and compatibility with the running code, and whether it has already been applied. Do not blindly replay all historical migration files.
2. For short SQL that has not been applied, execute it directly using the appropriate migration tool, in dependency order, without requesting additional permission. Do not leave it as a reminder at the end of a successful push report. Record the actual execution result and verify it.
3. For lengthy SQL, the AI may provide a clickable full absolute path to the complete SQL file for the user to copy and paste in the intended project's SQL Editor. Identify the target project and environment, file order, prerequisites and the result the user should return. Once handed off, keep it marked awaiting user execution; after the user reports execution, independently verify the actual database objects and required cache state.
4. Use judgment about the complete script's length and practical execution limits; there is no user-confirmed fixed line-count cutoff. Do not truncate SQL or split a lengthy script merely to bypass the file handoff.
5. Mark SQL as executed only when there is execution evidence. Providing a file, receiving authorization, invoking a tool without a success result, or waiting for a reply does not mean it ran. If execution failed or the outcome is unknown, determine what actually ran before retrying.
6. After execution, verify the relevant tables, columns, constraints, triggers, unique RPC signatures, argument names, function contents and calling-role permissions. For RPC/schema changes, refresh PostgREST metadata and verify that the interface recognizes the objects. Record source fingerprints, the actual remote migration version and verification evidence. A cache-reload notification alone is not proof of successful business saving.

Separate code push, database migration, service deployment and live business validation. Under this workspace's convention, code merged into the remote may be marked **deployed** for code status; it must not imply that database migrations ran or the feature is usable online.

Before pushing code that can be deployed automatically and requires these objects, complete the verified compatible migration sequence. If the SQL requires coordinated code/database deployment, make that sequence concrete before proceeding. A required migration that is pending, failed or unverified prevents an unconditional **READY TO MERGE** or a claim that the feature is ready online. Use the conditional or not-ready conclusion and lead with the exact database blocker. An accepted deferral of unrelated bugs does not waive a database prerequisite. If the user explicitly directs a staged push before migration, preserve the pending condition and carry the agreed migration/handoff through to its actual result.

## 6. Report the Check Result

Provide one consolidated result containing:

1. **Merge inputs:** repository, workspace, source and target SHAs, and complete change range.
2. **Integration result:** included content, conflicts, and preservation of existing work.
3. **Verification:** passed, failed, skipped, and unexecuted checks, with evidence.
4. **Database prerequisites:** the per-migration inventory, actual target, execution/verification evidence, and any file handoff still awaiting the user. State not applicable with a basis when no SQL is required.
5. **Pending actions:** execution dependencies and arrangements already confirmed by the user.
6. **Conclusion:** exactly one of the following:
   - **READY TO MERGE:** required checks passed and no execution-blocking prerequisite remains.
   - **READY AFTER SPECIFIED CONDITIONS:** list the exact outstanding conditions.
   - **NOT READY TO MERGE:** state the failed checks or blocking facts.

If the user explicitly accepts an outstanding condition and directs execution, record that decision while preserving the actual check results. Do not relabel a failure or unexecuted step as passed.

Keep check results and evidence organized by feature under the document root. After a merge or push, record its actual outcome in:

`C:/Users/Administrator/Desktop/codex/代码修改日志/功能/合并与同步-YY.M.D.HH.mm.md`

Use the actual feature name and the user's current local execution time and timezone. Include evidence paths and any pending database or live-validation steps. Pure protocol/document edits do not create a code modification log.

## 7. Execute and Confirm Completion

If merge or push is already authorized and its conditions are satisfied, continue without asking for the same authorization again. If authorization covers checks only, stop after reporting the check result.

Immediately before execution, confirm that source and target have not changed in a way that invalidates the result. Recheck affected parts if they have.

After execution, verify:

- The merge result matches the checked candidate.
- The actual remote branch contains the intended commit or commits.
- Local-to-remote differences are as expected.
- Existing staged, unstaged, and untracked work has been preserved.
- Current-state documentation reflects the actual merge, push, SQL, and live-validation status.

Before the final response, recheck the migration inventory: short migrations have been executed and verified, or a concrete failure/restriction is reported; any lengthy SQL handed off has a complete file link, execution order and explicit pending state. Required SQL must not disappear from the report merely because the remote SHA matches. Synchronize actual execution and remaining steps to the current-state document and control index; keep historical reports unchanged.

If execution fails or the remote state does not match expectations, report the actual state and remaining action. Do not claim completion based solely on issuing a command.
