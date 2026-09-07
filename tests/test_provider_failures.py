import unittest

from agent_coop import coop_provider_failures


class ProviderFailureClassification(unittest.TestCase):
    def test_claude_monthly_spend_limit_is_non_retryable_quota(self):
        failure = coop_provider_failures.classify_provider_failure(
            "claude",
            exit_code=1,
            output=(
                "You've hit your monthly spend limit · raise it at "
                "claude.ai/settings/usage"
            ),
        )

        self.assertEqual(
            failure,
            coop_provider_failures.ProviderFailure(
                classification="provider_quota_exhausted",
                retryable=False,
            ),
        )

    def test_known_login_signatures_are_non_retryable_auth(self):
        cases = (
            ("claude", "Not logged in. Run /login to continue."),
            ("codex", "Not logged in. Run `codex login` to continue."),
            ("grok", "Authentication required. Run `grok login`."),
        )

        for provider, output in cases:
            with self.subTest(provider=provider):
                failure = (
                    coop_provider_failures.classify_provider_failure(
                        provider,
                        exit_code=1,
                        output=output,
                    )
                )
                self.assertEqual(
                    failure,
                    coop_provider_failures.ProviderFailure(
                        classification="provider_auth_required",
                        retryable=False,
                    ),
                )

    def test_successful_exit_is_never_classified_as_provider_failure(self):
        failure = coop_provider_failures.classify_provider_failure(
            "claude",
            exit_code=0,
            output="You've hit your monthly spend limit",
        )
        self.assertIsNone(failure)

    def test_generic_limit_payment_and_auth_words_do_not_overmatch(self):
        outputs = (
            "rate limit may recover shortly",
            "402 Payment Required",
            "authentication handshake timed out",
            "monthly limit calculation completed",
        )

        for output in outputs:
            with self.subTest(output=output):
                self.assertIsNone(
                    coop_provider_failures.classify_provider_failure(
                        "claude",
                        exit_code=1,
                        output=output,
                    )
                )

    def test_signatures_are_provider_specific(self):
        self.assertIsNone(
            coop_provider_failures.classify_provider_failure(
                "codex",
                exit_code=1,
                output="You've hit your monthly spend limit",
            )
        )
        self.assertIsNone(
            coop_provider_failures.classify_provider_failure(
                "unknown",
                exit_code=1,
                output="Not logged in. Run /login to continue.",
            )
        )

    def test_nonretryable_worker_failures_stop_the_run(self):
        for classification in (
            "worker_protocol_failed",
            "resume_failed",
            "worker_cleanup_failed",
            "worker_start_failed",
            "capability_activation_failed",
            "process_tree_cleanup_failed",
            "capability_shutdown_failed",
            "turn_timeout",
        ):
            with self.subTest(classification=classification):
                self.assertEqual(
                    coop_provider_failures.terminal_reason({
                        "classification": classification,
                        "retryable": False,
                    }),
                    classification,
                )

    def test_retryable_worker_protocol_failure_does_not_stop_the_run(self):
        self.assertIsNone(
            coop_provider_failures.terminal_reason({
                "classification": "worker_protocol_failed",
                "retryable": True,
            })
        )


if __name__ == "__main__":
    unittest.main()
