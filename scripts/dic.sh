# This file configures dic.  It should be sourced from .bashrc.

export DIC_MODEL=groq+qwen
export DIC_SYSTEM="Keep your response short, between 1-20 lines. Focus on a high signal to noise ratio (audience has strong math/cs background). If the question is about a computer, respond for: $(uname -a)."
[[ $- == *i* ]] && export DIC_SESSION="$$"

alias qwen='dic -m groq+qwen'
alias fable='dic -m anthropic+fable'
alias opus='dic -m anthropic+opus'
alias sonnet='dic -m anthropic+sonnet'
alias haiku='dic -m anthropic+haiku'
alias deepseek='dic -m openrouter+deepseek'
alias gemini='dic -m openrouter+gemini'

# --- tab completion -----------------------------------------------------

# The message picker: `dic --log`'s rows into fzf, and the mid it returns.
# fzf draws on the terminal and writes the choice on stdout, so bash is what
# captures it; --with-nth hides the mid from the display while {1} still
# hands --show the full value to preview.  Without an fzf the same rows come
# back as a plain list, one mid per line.
_dic_mids() {
  if command -v fzf >/dev/null 2>&1; then
    dic --log 2>/dev/null | fzf \
        --height=40% --reverse --no-multi --prompt='mid> ' \
        --header-lines=1 --with-nth=2.. \
        --preview 'dic --show {1}' --preview-window=right:60% \
      | awk 'NF { print $1; exit }'
  else
    dic --log 2>/dev/null | tail -n +2 | cut -f1
  fi
}

# Fill COMPREPLY with one picked mid, or with the list to pick from.  A $1
# prefix turns the pick into --mid=REF, which is how -c names a message: -c
# itself takes no value, so the ref arrives beside it.
_dic_complete_mid() {
  local prefix=$1 cur=$2 list
  list=$(_dic_mids)
  if [[ -z $list ]]; then
    COMPREPLY=()
  elif [[ $list != *$'\n'* ]]; then
    COMPREPLY=( "$prefix$list" )
  else
    COMPREPLY=( $(compgen -W "$list" -- "$cur") )
  fi
}

_dic_complete() {
  local cur=${COMP_WORDS[COMP_CWORD]} prev=${COMP_WORDS[COMP_CWORD-1]}

  case $prev in
    # tab complete model names
    -m|--model)
      COMPREPLY=( $(compgen -W "$(dic --models)" -- "$cur") )
      return ;;
    # tab complete files
    -a|--attachment|--path|--models-file)
      compopt -o default 2>/dev/null
      COMPREPLY=( $(compgen -f -- "$cur") )
      return ;;
    # tab complete a message to continue: -c has no value of its own, so the
    # pick becomes --mid=REF beside it, while --mid, --show and --from each
    # take the ref they pick directly
    -c|--continue)
      _dic_complete_mid "--mid=" "$cur"
      return ;;
    --mid|--show|--from)
      _dic_complete_mid "" "$cur"
      return ;;
  esac

  if [[ $cur == -* ]]; then
    COMPREPLY=( $(compgen -W "-m --model -s --system -a --attachment \
                              -x --extract -f --force -c --continue \
                              --mid --show --from --log --limit --all \
                              --cache --tools --path --mime-type \
                              --pv-thinking --no-pv-thinking \
                              --clipboard --no-clipboard --stats" -- "$cur") )
  fi
}
complete -F _dic_complete dic

# The same picker on a keystroke: TAB always opens it, which costs a
# caller who already knows the mid; binding it to \C-x\C-m lets that fast
# path stay fast and makes the window onto the tree opt-in.  The chosen
# mid is spliced in at the cursor as --mid=REF, which is how -c reaches a
# message -- -c takes no value of its own, so the ref travels beside it.
_dic_pick() {
  local mid=$(_dic_mids)
  [[ -z $mid ]] && return
  READLINE_LINE="${READLINE_LINE:0:$READLINE_POINT} --mid=$mid ${READLINE_LINE:$READLINE_POINT}"
  READLINE_POINT=$((READLINE_POINT + 8 + ${#mid}))
}
bind -x '"\C-x\C-m":_dic_pick'

# --- prompt-expansion widget --------------------------------------------
# This widget introduces a new syntax $$(...) for command substitution.
# When this widget encounters
#
#     $$(...)
#
# the line above will be replaced with
#
#     $ ...
#     $(...)
#
# This syntax is useful for constructing complex prompts using heredocs.
# For example:
#
#     dic <<EOF
#     $$(files-to-prompt src/)
#     $$(git diff HEAD~2)
#     $$(pytest)
#
#     Why did the previous commits cause these tests to fail?
#     EOF
#
# gets expanded to
#
#     dic <<EOF
#     $ files-to-prompt src/
#     $(files-to-prompt src/)
#     $ git diff HEAD~2
#     $(git diff HEAD~2)
#     $ pytest
#     $(pytest)
#
#     Why did the previous commits cause these tests to fail?
#     EOF
#
# The widget expansion happens immediately when the user presses enter.
# The $(...) is not run immediately and will be expanded by the shell like normal.
#
# NOTE:
# This widget expansion happens at the readline level.
# It will therefore expand *everywhere* in bash, and not just in heredocs.
# For example, it will expand in the standard repl and quoted heredocs <<'EOF'.
# This is unlikely to cause errors because $$(...) is never valid shell.
# The widget will never expand in non-interactive sessions.

_dic_expand() {
  [[ $READLINE_LINE =~ ^\$\$\((.*)\)$ ]] || return
  local cmd=${BASH_REMATCH[1]}
  READLINE_LINE=$'$ '"$cmd"$'\n$('"$cmd"$')'
  READLINE_POINT=${#READLINE_LINE}
}
bind -x '"\C-x\C-r":_dic_expand'
bind '"\C-m": "\C-x\C-r\C-j"'
