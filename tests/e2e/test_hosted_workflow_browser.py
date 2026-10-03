"""Hosted user completes the coding/fix/review/merge loop in Chromium."""

from tests.e2e.hosted_fixture import hosted_browser
from tests.unit.test_hosted_workflow import workflow_app  # noqa: F401


def complete_coding_loop(page, base, playwright, repo):
    page.goto(base)
    page.get_by_role("textbox", name="Session task").fill("Create the greeting and run tests")
    page.get_by_title("Select repository").click()
    page.get_by_role("button", name=repo, exact=True).click()
    page.get_by_role("button", name="Start session", exact=True).click()
    playwright.expect(
        page.get_by_text("Greeting created; 1 passed.", exact=False).first
    ).to_be_visible(timeout=15000)
    author_url = page.url
    page.get_by_role("button", name="Changes", exact=True).last.click()
    playwright.expect(page.get_by_role("button", name="hello.txt", exact=False)).to_be_visible(
        timeout=10000
    )
    page.get_by_role("button", name="Create draft PR", exact=True).click()
    page.get_by_role("button", name="Create pull request", exact=True).click()
    playwright.expect(page.get_by_role("button", name="Launch independent review")).to_be_visible(
        timeout=10000
    )
    page.get_by_role("button", name="Launch independent review").click()
    playwright.expect(page.get_by_text("Changes requested", exact=True)).to_be_visible(
        timeout=15000
    )
    playwright.expect(
        page.get_by_text("Replace the placeholder greeting with the fixed greeting.")
    ).to_be_visible()
    page.get_by_role("textbox", name="Follow-up message").fill(
        "Fix the requested greeting and run tests"
    )
    page.get_by_role("button", name="Send follow-up", exact=True).click()
    playwright.expect(
        page.get_by_text("Greeting fixed; 1 passed.", exact=False).first
    ).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Update pull request", exact=True).click()
    page.get_by_role("checkbox", name="Draft pull request").uncheck()
    page.get_by_role("dialog").get_by_role("button", name="Update pull request", exact=True).click()
    page.get_by_role("button", name="Launch independent review").click()
    playwright.expect(page.get_by_text("Review passed", exact=True)).to_be_visible(timeout=15000)
    page.get_by_role("button", name="Merge pull request", exact=True).click()
    page.get_by_role("button", name="Confirm merge", exact=True).click()
    playwright.expect(page.get_by_text("Pull request merged.", exact=True)).to_be_visible(
        timeout=15000
    )
    page.reload()
    playwright.expect(page.get_by_text("Pull request merged.", exact=True)).to_be_visible(
        timeout=10000
    )
    return author_url.rsplit("/", 1)[-1]


def test_browser_coding_fix_review_merge(workflow_app):  # noqa: F811
    app, user, _, repo = workflow_app
    token = app.state.auth_store.create_session(user.id)[1]
    with hosted_browser(app, token) as (page, base, playwright):
        complete_coding_loop(page, base, playwright, repo)
