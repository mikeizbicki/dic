# this file must be sourced from bash;
# it is recommended to include in the .bashrc

export DIC_MODEL=groq+qwen
export DIC_SYSTEM="Keep your response short, between 1-20 lines. Focus on a high signal to noise ratio (audience has math/cs phd). If the question is about a computer, respond for the following system: $(uname -a)."

# One alias per configured entry that has an "alias" key, generated from the
# model table itself, so shell names and model names cannot drift apart.
# Add your own by adding "alias": "name" to an entry in
# ~/.config/fac/models.json.
eval "$(dic --aliases)"
