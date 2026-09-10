# this file must be sourced from bash;
# it is recommended to include in the .bashrc

DIC_MODEL=groq-qwen
DIC_SYSYEM="Keep your response short, between 1-20 lines. Focus on a high signal to noise ratio (audience has math/cs phd). If the question is about a computer, respond for the following system: $(uname -a)."

alias haiku="dic -m claude-haiku-4.5"
alias sonnet="dic -m claude-sonnet-5"
alias opus="dic -m claude-opus-5"
alias fable="dic -m openrouter/anthropic/claude-fable-5.1"
alias sol="dic -m gpt-5.6-sol"
alias gpt-mini="dic -m gpt-5-mini"
alias gpt-nano="dic -m gpt-5-nano"
alias llama3="dic -m groq/llama-3.3-70b-versatile"
alias qwen="dic -m groq/qwen"
