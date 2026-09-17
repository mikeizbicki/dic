# This file defines the minimal coding agent `geni`,
# which is a thin wrapper around simonw's `llm` cli tool and git.
# It is intended as a beginner-friendly intro to the "unix philosophy"
# and how AI coding agents work.

function geni() {

    ####################
    # STEP 1: sanity check the git repo
    ####################
    # `git am` refuses to run if the working tree or index is dirty,
    # so we check up front to give a clearer error message.
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

    ####################
    # STEP 2: set up a place to stash intermediate files
    ####################
    # Everything goes under .git/.geni so it survives across invocations
    # and can be inspected when debugging a failed patch.
    # `git rev-parse --git-dir` works even from subdirectories of the repo.
    local geni_dir
    geni_dir="$(git rev-parse --git-dir)/.geni"
    mkdir -p "$geni_dir"

    local raw_file="$geni_dir/raw"

    ####################
    # STEP 3: invoke the llm
    ####################

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

    # We pass the user's request as positional args to llm.
    # Use a subshell so `set -o pipefail` doesn't leak into the caller's shell.
    if ! (
        set -o pipefail
        $llm_command -s "$(geni_prompt)" "$@" |\
            pv -N 'downloading llm output' -btr > "$raw_file"
    ); then
        echo "geni-error: $llm_command failed" >&2
        return 1
    fi

    ####################
    # STEP 4: apply the patch
    ####################
    # We directly run `git apply` on the output of the llm.
    # `git apply` ignores any text before the first "diff --git" line,
    # so the commit message above the patch is skipped over.
    # Finally, it either fully succeeds or leaves the tree untouched.
    # So on error, the repo remains exactly as if nothing had happened.
    if ! git apply --index --recount --ignore-whitespace "$raw_file"; then
        echo "geni-error: git apply failed to apply the patch" >&2
        echo "geni-hint: inspect the raw patch at: $raw_file" >&2
        return 1
    fi

    # `git apply` staged the changes, so no separate `git add` is required.
    # We tag the commits by modifying the subject with [geni]
    # and setting the committer fields.
    local commit_message
    commit_message="[geni] $(sed '/^diff --git/,$d' "$raw_file")"
    if ! GIT_COMMITTER_NAME='geni' GIT_COMMITTER_EMAIL='geni@agent' git commit --quiet -m "$commit_message"; then
        echo "geni-error: git commit failed" >&2
        return 1
    fi

    ####################
    # STEP 5: success
    ####################
    # Show a short summary of the commit we just made.
    git show HEAD --stat --format='%h %s'
}


# geni_prompt is a global function so that the user can always
# call the function to inspect the contents of the system prompt.
# It should be side-effect free.
function geni_prompt() {
    cat <<EOF
You are a coding agent. The user will describe a change they want made to
a git repository. You must respond with a commit message (Tim Pope style)
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
- Do NOT wrap your response in markdown code fences.
- Do NOT include any prose before or after the patch.
- The first line of your response MUST begin with the commit message.
    - Do not output any reasoning, explanation, or whitespace before it.
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


# geni_tee streams the llm output through to stdout while printing a
# human-readable progress indicator to stderr. It is adapted from the
# yaml-oriented version in shell/geni.sh to instead understand the
# unified-diff / mbox format that this version of geni uses.
function geni_tee() {
    printf "${__ORANGE}request sent... " >&2

    local first_line=true
    local output=""
    local current_path=""
    local in_hunk=false
    local line_counter=0

    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$first_line" == true ]]; then
            printf "receiving..." >&2
            first_line=false
        fi

        output+="$line"$'\n'

        # NOTE:
        # the code below "dynamically parses" the unified diff output;
        # it is not fully correct, but is close enough for a progress display.
        # we do not use a full diff parser because we want to stream the
        # progress as the data arrives. Any misparse here affects only the
        # progress indicator, not the final patch that gets applied.

        # Detect a new file in the diff: "diff --git a/foo b/foo"
        if [[ "$line" =~ ^diff\ --git\ a/(.+)\ b/(.+)$ ]]; then
            current_path="${BASH_REMATCH[2]}"
            in_hunk=false
            line_counter=0
            continue
        fi

        # Detect a new-file marker: "--- /dev/null" on the previous-style line
        # means the next +++ b/path is a brand new file.
        if [[ "$line" =~ ^\+\+\+\ b/(.+)$ ]]; then
            current_path="${BASH_REMATCH[1]}"
            continue
        fi

        # Detect "new file mode" marker -> announce a full new file
        if [[ "$line" =~ ^new\ file\ mode ]]; then
            printf " $current_path(new)..." >&2
            in_hunk=false
            line_counter=0
            continue
        fi

        # Detect a hunk header: "@@ -1,2 +1,3 @@"
        if [[ "$line" =~ ^@@ ]]; then
            if [[ "$in_hunk" == false && -n "$current_path" ]]; then
                printf " $current_path(patch)..." >&2
            fi
            in_hunk=true
            line_counter=0
            continue
        fi

        # Print a dot every 10 lines while we're inside a hunk / file body.
        if [[ -n "$current_path" ]]; then
            ((line_counter++))
            if (( line_counter % 10 == 0 )); then
                printf "." >&2
            fi
        fi
    done
    printf "\n" >&2
    printf '%s' "$output"
}
