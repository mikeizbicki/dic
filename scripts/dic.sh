# This file configures dic.  It should be sourced from .bashrc.

export DIC_MODEL=groq+qwen
export DIC_SYSTEM="Keep your response short, between 1-20 lines. Focus on a high signal to noise ratio (audience has strong math/cs background). If the question is about a computer, respond for: $(uname -a)."
[[ $- == *i* ]] && export DIC_SESSION="$$"

alias qwen='dic -m groq+qwen'
alias fable='dic -m anthropic+fable'
alias opus='dic -m anthropic+opus'
alias deepseek='dic -m openrouter+deepseek'
alias gemini='dic -m openrouter+gemini'
