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
  esac

  if [[ $cur == -* ]]; then
    COMPREPLY=( $(compgen -W "-m --model -s --system -a --attachment \
                              -x --extract -f --force -c --continue \
                              --mid --cache --tools --path --mime-type \
                              --pv-thinking --no-pv-thinking --stats" -- "$cur") )
  fi
}
complete -F _dic_complete dic

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
