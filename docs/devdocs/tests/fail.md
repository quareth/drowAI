# Accepted Pre-existing Test Failures

These failures existed before the LLM router orchestration refactor began and
are accepted as the locked Phase 1 baseline. They are not caused by this
refactor and must remain stable while the refactor proceeds.

- `test_managed_connection_test_authorizes_and_uses_guarded_transport` — expected Hugging Face inventory URL differs from the active configured target.
- `test_anthropic_selection_preference_write_does_not_require_credential` — active selection now requires a deployment binding.
- `test_provider_credential_routes_support_anthropic_credential` — active credential response includes `deployment_ref`.
- `test_selection_get_rejects_invalid_provider_neutral_row` — active failure reason is `deployment_unmapped` instead of `model_unavailable`.
- `test_create_conversation_keeps_openai_lifecycle_behavior` — conversation creation rejects selections without a deployment origin.
- `test_create_conversation_fails_before_openai_sdk_without_capability` — deployment-origin validation now fails before the expected capability response.
