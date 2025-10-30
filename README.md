# MergeMate

**MergeMate** is a Slack bot that:

- Accepts Git patches (and optionally source files) from Slack  
- Links to or creates a Merge Request (MR)  
- Uses **ChatGPT** for AI-powered code review  
- Can optionally **approve the MR** automatically based on review  

We should be cautious with both the application and the workflow. I can confirm that introducing AI carries a real risk of negative impact. We should handle as much as possible in the background and keep prompts tightly focused on specific issue types: syntax, showstoppers, and typos. I estimate the potential upside to be about 15%, roughly equivalent to adding two mid-level engineers across all of R&D.

---

## Features

- **Slack Integration**  
  Trigger reviews with a slash command (`/submit-patch`) or by mentioning the bot.

- **Patch & MR Handling**  
  - Accept `.patch` files or source files directly in Slack.  
  - Post a link to an existing or new Merge Request.

- **AI-Powered Review**  
  - Sends the diff to ChatGPT for review.  
  - Posts review summary and inline suggestions in Slack.

- **Optional Auto-Approval**  
  - If ChatGPT recommends it, MergeMate can approve the MR via the GitLab  API.

- **Fix my English**  
  - ChatGPT refactors code comments, improves variable names, and fixes typos.
  - ChatGPT can also correct arbitrary English text.

---

## GitLab CI/CD Integration (Push-Triggered)

**Goal**  
On a push tragged in a certain way, collect context (diff, final versions of relevant files, JIRA ticket if present), build a structured prompt, send it to MergeMate, and post the **MR review** (or a link to it) in Slack.

### How it works

1. **Trigger**  
   The GitLab pipeline runs on `push` and (optionally) on `merge_request_event`.

2. **Collect context**  
   - Compute the diff vs. the target branch (for MRs) or the previous commit (for push).  
   - Gather final versions of changed files (apply size/type filters). This step could involve AI for picking the relevant code, ignoring certain MRs.
   - Detect a JIRA key from the branch name or commit messages (e.g., `ABC-123`) and fetch issue metadata (optional).

3. **Build prompt**  
   Assemble a compact, token-friendly payload with a `git diff` output, file excerpts, and, if available, JIRA title/description. The code supports at least 2 different prompt templates.

4. **Send to MergeMate**  
   POST the payload to the MergeMate backend endpoint.

5. **Result in Slack**  
   MergeMate runs the AI review and posts a **review summary + inline suggestions** or a **link to the MR review** into Slack (channel/user configurable).

6. **Follow-up** 
   Keep the conversation going in Slack: reply in-thread on the existing review, link to the MR, and mark items resolved.

---

### Two entry points

1) **Slack-first path**

```plaintext
[Slack User]
    |
    v
[Slack Bot: Slash Cmd / Mention]
    |
    v
[MergeMate Backend (Flask/FastAPI)]
    - Receives patch/source upload
    - Calls GitHub/GitLab API (link or create MR/PR)
    - Calls OpenAI (ChatGPT) for review (a new session)
    - Posts results back to Slack
```


```plaintext
[Developer Push / MR Event]
    |
    v
[GitLab CI Job]
    - Collect diff & changed files
    - (Optional) fetch JIRA issue by key
    - Build compact review payload
    - POST /v1/review to MergeMate
    |
    v
[MergeMate Backend (Flask/FastAPI)]
    - Calls GitHub/GitLab API (link or create MR/PR)
    - Calls OpenAI (ChatGPT) for review (a new session)
    - Posts summary + link or inline comments to Slack
```

## Key Components:

* Slack App (Bot User, Slash Command, Event Subscription)
* Python Backend (Flask or FastAPI)
* OpenAI API (ChatGPT for code review)
* Git Provider API (GitHub/GitLab for MR creation & approval)



# Links 

* https://arxiv.org/pdf/2507.09089 Design: Randomized controlled trial (tasks on mature open-source repos; devs allowed vs. disallowed to use early-2025 AI). Result: Allowing AI increased completion time by 19% (slower). Authors probed confounders (task/project properties, prior tool experience) and still found the hit. Closest we have to “experienced engineers doing real maintenance on real code” under controlled conditions. If our team skews senior and is doing non-boilerplate work, we may initially see drag rather than lift.

* https://arxiv.org/html/2412.18531v2  Design: Case study in a software division (10 projects; 4,335 PRs; 1,568 auto-reviewed). Results: 73.8% of bot comments were acted on (resolved). But average PR closure time rose from ~5h52m → ~8h20m overall; devs reported minor code-quality improvement and noted noise/irrelevancies. For small/mid teams, this mirrors what we often see: better hygiene & earlier defect surfacing, but slower merges unless we tune scope/policies.


* https://tao-xiao.github.io/files/Copilot4PR_FSE_2024.pdf Design: Quasi-experiments over 18,256 PRs using Copilot-for-PRs vs 54,188 not using it (146 repos), controlling for 17 confounders Results: −19.3 hours average review time; 1.57× higher chance of merge for assisted PRs. (Early-adopter bias applies.)  If we adopt PR-specific AI (summaries/checklists) rather than broad “AI everywhere,” we can compress review cycles—even on small/mid repos—provided the feature fits our host (GitHub) and process.

* https://arxiv.org/pdf/2412.06603 Design: Large internal survey (N=669) + usability testing. Results: Users reported work felt easier/faster on average; code understanding (not codegen) was the top use. Only 2–4% used raw outputs verbatim; most modified or used outputs for learning/ideas. For maintenance, assistants that explain legacy code/tests often outperform pure generation.

* https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4945566 Design: Three field experiments (random access to an AI code-completion assistant), 4,867 devs. Results: Pooled estimate +26.08% completed tasks; bigger gains for less-experienced devs. : Strong external validity, but skewed to very large orgs and code-completion usage. Expect larger gains on routine work, smaller on knotty maintenance.

* https://arxiv.org/abs/2406.17910 Design: Task-level eval of Copilot across 15 tasks. Results: Up to 50% time saved on docs/autocomplete; 30–40% on repetitive coding, unit tests, debugging, pair-style tasks; struggles on complex, multi-file C/C++ and proprietary contexts. For small/mid teams, aim AI at documentation, test generation, common patterns first; don’t expect magic on hairy refactors.

* https://arxiv.org/pdf/2505.16339 Design: Mixed-methods empirical work proposing AI co-reviewer and interactive assistant (with RAG). Takeaway: Integrations that supply the right context (diff + relevant files) and enable interactive Q&A yield better outcomes than “one-shot” reviews.

* https://arxiv.org/abs/2411.10213 Design: Empirical comparisons on SWE-bench–style bug-fix tasks. Results: LLM/agent systems can fix non-trivial bugs via iterative runs, but performance varies widely; system design matters as much as the base model.

* https://www.alibabacloud.com/en/product/lingma?_p_lc=1 a bug fixing system developed by Alibaba that combines code knowledge graphs with LLMs. It constructs a code knowledge graph for the code repository and utilizes LLMs to perform Monte Carlo Tree Search based on issue information to locate code snippets related to the issue throughout the entire repository. This approach effectively alleviates the issue of short context supported by LLMs but places high demands on the reasoning capabilities of the LLMs

* https://www.marscode.com/extension a bug fixing system that combines code knowledge graphs, software analysis techniques, and LLMs. 

* https://gru.ai/  a workflow-based bug fixing system developed by Gru. It first uses an LLM to select files related to the issue, then the LLM makes decisions on which files to change and how to change them.

* https://workweave.dev/blog/the-price-of-mandatory-code-reviews  more code reviews lead to fewer bugs. The biggest gains happen between 0 and 0.5 reviews per PR - after that, diminishing returns kick in.
