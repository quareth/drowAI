Use the **tester** subagent to handle this. Delegate to the tester subagent—do not run tests in the main agent.

Pass the user's request and any selected code or file as context. The tester will clarify scope, pick the right runner (pytest/vitest etc.), run tests, and report in its standard format.
