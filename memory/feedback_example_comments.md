---
name: feedback_example_comments
description: Usage example comments go directly above the function they illustrate, one per function
metadata:
  type: feedback
---

Place usage example comments directly above the function definition they illustrate — one example per function, not a grouped block above several functions.

**Why:** User reorganized a grouped example block into per-function examples placed immediately above each `def`. Confirmed this is the preferred style.

**How to apply:** Whenever adding an example comment for a function, put it on the line(s) immediately preceding `def function_name(...)`, not in a shared block further above.
