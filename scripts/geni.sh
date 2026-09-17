# This file defines the minimal coding agent `geni`,
# which is a thin wrapper around simonw's `llm` cli tool and git.
# It is intended as a beginner-friendly intro to the "unix philosophy"
# and how AI coding agents work.

function geni() {
    # only allow geni to run if the repo is clean
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "geni-error: not inside a git repository" >&2
        return 1
    fi
    if ! git diff --quiet --cached; then
        echo "geni-error: staging area is non-empty" >&2
        return 1
    fi
    if ! git diff --quiet; then
        echo "geni-error: working tree has uncommitted changes" >&2
        return 1
    fi

    # generate and apply the patch
    geni-mkpatch "$@"
    geni-apply
}

function geni-patchfile() {
    # Output the absolute path to the temporary file that will store the patch.
    # Everything goes under .git/.geni so it survives across invocations
    # and can be inspected when debugging a failed patch.
    # `git rev-parse --git-dir` works even from subdirectories of the repo.
    echo "$(git rev-parse --git-dir)/.geni-patchfile"
}

function geni-mkpatch() {
    # `dic` is a more efficient version of simonw's `llm` command;
    # if available, we use `dic`; otherwise we use `llm`.
    if command -v dic >/dev/null 2>&1; then
        llm_command=dic
    elif command -v llm >/dev/null 2>&1; then
        llm_command=llm
    else
        echo "geni-error: neither dic nor llm installed" >&2
        return 1
    fi

    # We pass the user's request as positional args to llm_command.
    # Use a subshell so `set -o pipefail` doesn't leak into the caller's shell.
    if ! (
        set -o pipefail
        $llm_command -s "$(geni-prompt)" "$@" |\
            pv -N patch -btr > "$(geni-patchfile)"
    ); then
        echo "geni-error: $llm_command failed" >&2
        return 1
    fi
}

function geni-apply() {
    local patch_file=$(geni-patchfile)

    # We directly run `git apply` on the output of the llm.
    # `git apply` ignores any text before the first "diff --git" line,
    # so the commit message above the patch is skipped over.
    # Finally, it either fully succeeds or leaves the tree untouched.
    # So on error, the repo remains exactly as if nothing had happened.
    if ! git apply --index --recount --ignore-whitespace "$patch_file"; then
        echo "geni-error: git apply failed to apply the patch" >&2
        echo "geni-hint: fix the raw patch at: $patch_file" >&2
        echo "geni-hint: after fixing, rerun geni-apply" >&2
        return 1
    fi

    # `git apply` staged the changes, so no separate `git add` is required.
    # We tag the commits by modifying the subject with [geni]
    # and setting the committer fields.
    local commit_message
    commit_message="[geni] $(sed '/^diff --git/,$d' "$patch_file")"
    if ! GIT_COMMITTER_NAME='geni' GIT_COMMITTER_EMAIL='geni@agent' git commit --quiet -m "$commit_message"; then
        echo "geni-error: git commit failed" >&2
        return 1
    fi

    # Show a short summary of the commit we just made.
    git show HEAD --stat --format='%h %s'
}

function geni-prompt() {
    # Print the system prompt used by geni.
    # It is a global function so that users can always run it to inspect the prompt.
    # All commands used in constructing the prompt must be side effect free.
    cat <<EOF
You are a coding agent. The user describes a change they want made to
a git repository. You respond with a commit message (Tim Pope style)
followed by a patch (and nothing else).  Here is an example:

\`\`\`
imperative summary (Tim Pope format, 50 chars max)

<optional longer explanation paragraph>

diff --git a/path/to/file b/path/to/file
--- a/path/to/file
+++ b/path/to/file
@@ -<old_start>,<old_count> +<new_start>,<new_count> @@
 context line
-removed line
+added line
 context line
\`\`\`

Rules:
- No other content.
    - Do NOT wrap your response in markdown code fences.
    - Do NOT include any prose before or after the patch.
    - The first line of your response MUST be the commit message.
- Use standard unified diff syntax with '--- a/...' and '+++ b/...' headers.
- For new files use '--- /dev/null' and '+++ b/path'.
    - You must also specify the mode of the new file
      (Add the text "new file mode 100644")
- For deleted files use '--- a/path' and '+++ /dev/null'.
- Patches will be applied with \`git apply --recount\`
    - Hunk line numbers do not have to be exact,
      but the context lines must be recognizable in the current file.
    - Include 2-3 lines of unchanged context around each change.
    - These context lines must exactly match the original document.
      (Including whitespace, quotation marks, and other punctuation.)
- Prefer small, focused patches.

Use the following information to help you write the code:

$ git ls-files
$(git ls-files)
EOF
}
