---
description: Select and verify the available Claude account with the most remaining quota
---

Run this installed ccpick command, then briefly report its actual result:

```sh
{{CCPICK_COMMAND}}
```

- Exit 0: report the account selected and the reason from the command output.
- Exit 2: report that the current account remains the best choice.
- Exit 3: explain why no account is available, including reset times if reported.
- Exit 1: report the error. Do not invent usage figures or retry a failing switch indefinitely.

Do not enroll new accounts, open an authorization page, or change settings as part of this command.
