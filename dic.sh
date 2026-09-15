# this file must be sourced from bash;
# it is recommended to include in the .bashrc

export DIC_MODEL=groq+qwen
export DIC_SYSTEM="Keep your response short, between 1-20 lines. Focus on a high signal to noise ratio (audience has math/cs phd). If the question is about a computer, respond for the following system: $(uname -a)."
[[ $- == *i* ]] && export DIC_SESSION="$$"

alias qwen='dic -m groq+qwen'
alias fable='dic -m anthropic+fable'
alias opus='dic -m anthropic+opus'
alias deepseek='dic -m openrouter+deepseek'
alias gemini='dic -m openrouter+gemini'
