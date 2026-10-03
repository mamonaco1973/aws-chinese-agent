# ==============================================================================
# bedrock-config.sh
# ==============================================================================
# Single source of truth for the Bedrock models the app offers. Sourced by
# apply.sh, destroy.sh and check_env.sh so all three stay in sync.
#
# Each entry is one model a user can pick when starting a conversation. The
# choice is locked to the conversation by its first message: history and the
# sandbox carry across messages, and one model per chat keeps them coherent.
#
# Format:
#     "<key>|<model id>|<label>|<image input>|<prompt caching>"
#
#   key             short name stored on the conversation. Keep it stable --
#                   changing it orphans existing conversations (they fall
#                   back to the default).
#   model id        a us.* or global.* inference profile, or a bare
#                   foundation-model id for on-demand-only models.
#   label           shown in the model picker and on each conversation.
#   image input     true if Converse accepts image blocks for this model:
#                   show_file then returns the PNG so the model can check its
#                   own render. false: the user still sees every file (S3 +
#                   signed URL); only the model's own look is skipped.
#   prompt caching  true if the model accepts cachePoint blocks. Models
#                   without caching reject any request that carries one.
#
# The model must support tool use. Run ./probe_bedrock.py to see every model
# this account can use, with tool use, image input and caching measured by
# real calls -- it prints these columns ready to paste.
#
# Probed 2026-10-02 in us-east-1:
#   Sonnet 4.6     tool use, image input, caching all work
#   DeepSeek V3.2  tool use works; image blocks rejected (ValidationException);
#                  any cachePoint rejected (AccessDeniedException). On-demand
#                  only -- there is no us.* profile for it.
#
# Token use counts the same for every model: the per-user budget exists to
# cap the bill, not to price models against each other.
# ==============================================================================

BEDROCK_MODELS=(
  "sonnet|us.anthropic.claude-sonnet-4-6|Claude Sonnet 4.6|true|true"
  "deepseek|deepseek.v3.2|DeepSeek V3.2|false|false"
  # Qwen3 VL 235B: closest to the Sonnet experience -- it sees its own images,
  # so show_file's self-check works. No caching.
  "qwen3-vl|qwen.qwen3-vl-235b-a22b|Qwen3 VL 235B|true|false"
  # Qwen3 Coder Next: best fit for "write Python, run it in the sandbox".
  # Text-only, no caching.
  "qwen3-coder|qwen.qwen3-coder-next|Qwen3 Coder Next|false|false"
)

# Key new conversations start on. Must be one of the keys above.
export BEDROCK_DEFAULT="deepseek"


# ==============================================================================
# Helpers
# ==============================================================================

# JSON array of model objects, shaped for the Terraform `models` variable.
bedrock_models_json() {
  local entry key model_id label image caching
  for entry in "${BEDROCK_MODELS[@]}"; do
    IFS='|' read -r key model_id label image caching <<< "${entry}"
    jq -n \
      --arg key      "${key}" \
      --arg model_id "${model_id}" \
      --arg label    "${label}" \
      --argjson image_input    "${image}" \
      --argjson prompt_caching "${caching}" \
      '{key: $key, model_id: $model_id, label: $label,
        image_input: $image_input, prompt_caching: $prompt_caching}'
  done | jq -s -c '.'
}

# Model ids, one per line -- what check_env.sh probes.
bedrock_model_ids() {
  local entry
  for entry in "${BEDROCK_MODELS[@]}"; do
    printf '%s\n' "${entry}" | cut -d'|' -f2
  done
}

# Keys, one per line.
bedrock_model_keys() {
  local entry
  for entry in "${BEDROCK_MODELS[@]}"; do
    printf '%s\n' "${entry}" | cut -d'|' -f1
  done
}
