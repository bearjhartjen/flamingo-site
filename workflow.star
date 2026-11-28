# A workflow for a project with no tracker and no forge: the items are in local.db, and the result is a branch in the source repository.
# Add an item with `beehave item add`, then run it with `beehave run <item> --workflow workflow.star`.
#
# Four things are set for OpenCode on this machine, and they differ from the stock template:
#   * every agent step names `runner = "opencode"`, because a step with no runner asks for
#     Claude, and this config has no Claude runner;
#   * every agent step names a model as provider/model, because that is how OpenCode takes one;
#   * every agent step sets timeout and idle_timeout. A free model can go silent when it is
#     over its quota: OpenCode then retries and prints nothing, and without idle_timeout the
#     step waits for hours. It now fails in about two minutes instead;
#   * no step passes `sandbox = sandbox()`, because OpenCode has no sandbox and the engine
#     refuses a sandboxed step with it. The project file sets sandbox = "off" to match.

load("@beehave//review_fix.star", "is_void", "void_names")

# The model of every agent step. It is a free model, so a run costs no money.
# Change it here, in one place, to use another one: it must be provider/model.
# space-bunny-free and longcat-2.5-preview-free left the catalog on 2026-11-25.
MODEL = "opencode/deepseek-v4.1-flash-free"

# The gates in a row that may give no verdict before the item stops.
VOID_LIMIT = 3

def capped(step, name, item):
    """Stops the item when a cap ended the agent step. The output `stop` is "turns", "budget" or "context", and the work of a capped run may be half done."""
    if step.stop != "":
        fail("REFUSE: the %s step stopped at the %s cap before it finished. Raise the cap or split the item. A plain beehave resume replays this stop. To start the item again, run beehave resume %s, then beehave cancel %s, then beehave run %s." % (name, step.stop, item.id, item.id, item.id))

def judged(item, rerun_void):
    """Runs the gate and returns the first result that judged the code. A gate that failed a step is red and judged the code.
    A gate with an unknown step and no failed step is void: the host killed it, or it did not run. It gave no verdict, so the gate runs again.
    After VOID_LIMIT void results in a row the item stops: the code was never judged, and a fix agent would change working code."""
    for _ in range(VOID_LIMIT):
        check = gate(name = "check")  # The gate command is the one in the project file.

        if not rerun_void or not is_void(check):
            return check

    fail("REFUSE: the gate gave no verdict %d times in a row. The steps with no result: %s. The code was not judged. Fix the gate command, then run beehave run %s again." % (VOID_LIMIT, void_names(check), item.id))

def main(item):
    # The agent steps read and edit the work folder. The engine runs the gate itself.
    plan = agent(name = "plan", prompt = "prompts/plan.md", model = MODEL, runner = "opencode", timeout = "10m", idle_timeout = "2m", edits = False, inputs = {"item": item})
    capped(plan, "plan", item)

    # The implement step sits behind patched. The true branch is the new code: it reads the facts and gives the implement step the base branch as the input base.
    # A new run, and an open run that did not reach this call, take it. The false branch is the old code, unchanged: a run that already recorded the implement step replays it.
    if patched("implement-base"):
        implement = agent(name = "implement", prompt = "prompts/implement.md", model = MODEL, runner = "opencode", timeout = "10m", idle_timeout = "2m", inputs = {"item": item, "plan": plan.text, "base": facts().base_branch})
    else:
        implement = agent(name = "implement", prompt = "prompts/implement.md", model = MODEL, runner = "opencode", timeout = "10m", idle_timeout = "2m", inputs = {"item": item, "plan": plan.text})
    capped(implement, "implement", item)

    # The rerun of a void gate sits behind patched. A run that already recorded a void gate and the fix step after it replays them as they were.
    rerun_void = patched("void-gate")

    # A pass is one gate, one review and, when the head is not approved, one fix. The loop stops at the first approved head.
    approved = False

    for _ in range(3):
        check = judged(item, rerun_void)
        findings = check.report

        if check.passed:
            review = agent(
                name = "review",
                prompt = "prompts/review.md",
                model = MODEL, runner = "opencode", timeout = "10m", idle_timeout = "2m",
                pin = check.head,
                outputs = ["verdict"],
                edits = False,
                inputs = {"item": item},
            )
            capped(review, "review", item)
            approved = review.verdict == "approve"
            findings = review.text

        if approved:
            break

        fix = agent(name = "fix", prompt = "prompts/fix.md", model = MODEL, runner = "opencode", timeout = "10m", idle_timeout = "2m", inputs = {"item": item, "findings": findings})
        capped(fix, "fix", item)

    # The branch goes into the source repository under the id of the item. An item that used all of its passes is not pushed.
    # The engine pushes the head that the gate and the review saw, so no step of the item needs a credential. With no lease, git takes only a new branch or a fast forward.
    # The push is the last step, so it needs no patched: a run that passed the old command push has ended, and a resume of a run whose push failed starts again at the failed step.
    if approved:
        push(name = "push", branch = item.id, head = check.head, expect = "")
