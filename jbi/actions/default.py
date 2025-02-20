"""
Default action is listed below.
`init` is required; and requires at minimum the `jira_project_key` parameter.
The `label_field` parameter configures which Jira field is used to store the
labels generated from the Bugzilla status whiteboard.

`init` should return a __call__able
"""
import logging
from typing import Any

from jbi import ActionResult, Operation
from jbi.environment import get_settings
from jbi.errors import ActionError
from jbi.models import ActionLogContext, BugzillaBug, BugzillaWebhookEvent, JiraContext
from jbi.services import bugzilla, jira

settings = get_settings()

logger = logging.getLogger(__name__)

JIRA_DESCRIPTION_CHAR_LIMIT = 32767
JIRA_REQUIRED_PERMISSIONS = {
    "ADD_COMMENTS",
    "CREATE_ISSUES",
    "DELETE_ISSUES",
    "EDIT_ISSUES",
}


def init(jira_project_key, sync_whiteboard_labels=True, **kwargs):
    """Function that takes required and optional params and returns a callable object"""
    return DefaultExecutor(
        jira_project_key=jira_project_key,
        sync_whiteboard_labels=sync_whiteboard_labels,
        **kwargs,
    )


class DefaultExecutor:
    """Callable class that encapsulates the default action."""

    def __init__(self, jira_project_key, **kwargs):
        """Initialize DefaultExecutor Object"""
        self.jira_project_key = jira_project_key
        self.sync_whiteboard_labels = kwargs.get("sync_whiteboard_labels", True)

        self.bugzilla_client = bugzilla.get_client()
        self.jira_client = jira.get_client()

    def __call__(  # pylint: disable=inconsistent-return-statements
        self,
        bug: BugzillaBug,
        event: BugzillaWebhookEvent,
    ) -> ActionResult:
        """Called from BZ webhook when default action is used. All default-action webhook-events are processed here."""
        target = event.target  # type: ignore
        if target == "comment":
            return self.comment_create_or_noop(bug=bug, event=event)  # type: ignore
        if target == "bug":
            return self.bug_create_or_update(bug=bug, event=event)
        logger.debug(
            "Ignore event target %r",
            target,
            extra=ActionLogContext(
                bug=bug,
                event=event,
                operation=Operation.IGNORE,
            ).dict(),
        )
        return False, {}

    def comment_create_or_noop(
        self, bug: BugzillaBug, event: BugzillaWebhookEvent
    ) -> ActionResult:
        """Confirm issue is already linked, then apply comments; otherwise noop"""
        linked_issue_key = bug.extract_from_see_also()

        log_context = ActionLogContext(
            event=event,
            bug=bug,
            operation=Operation.COMMENT,
            jira=JiraContext(
                issue=linked_issue_key,
                project=self.jira_project_key,
            ),
        )
        if not linked_issue_key:
            logger.debug(
                "No Jira issue linked to Bug %s",
                bug.id,
                extra=log_context.dict(),
            )
            return False, {}

        if bug.comment is None:
            logger.debug(
                "No matching comment found in payload",
                extra=log_context.dict(),
            )
            return False, {}

        formatted_comment = bug.map_event_as_comment(event)
        jira_response = self.jira_client.issue_add_comment(
            issue_key=linked_issue_key,
            comment=formatted_comment,
        )
        logger.debug(
            "Comment added to Jira issue %s",
            linked_issue_key,
            extra=log_context.dict(),
        )
        return True, {"jira_response": jira_response}

    def jira_fields(self, bug: BugzillaBug):
        """Extract bug info as jira issue dictionary"""
        fields: dict[str, Any] = {
            "summary": bug.summary,
        }

        if self.sync_whiteboard_labels:
            fields["labels"] = bug.get_jira_labels()

        return fields

    def jira_comments_for_update(
        self,
        bug: BugzillaBug,
        event: BugzillaWebhookEvent,
    ):
        """Returns the comments to post to Jira for a changed bug"""
        return bug.map_changes_as_comments(event)

    def update_issue(
        self,
        bug: BugzillaBug,
        event: BugzillaWebhookEvent,
        linked_issue_key: str,
        is_new: bool,
    ):
        """Allows sub-classes to modify the Jira issue in response to a bug event"""

    def bug_create_or_update(
        self, bug: BugzillaBug, event: BugzillaWebhookEvent
    ) -> ActionResult:  # pylint: disable=too-many-locals
        """Create and link jira issue with bug, or update; rollback if multiple events fire"""
        linked_issue_key = bug.extract_from_see_also()  # type: ignore
        if not linked_issue_key:
            return self.create_and_link_issue(bug=bug, event=event)

        log_context = ActionLogContext(
            event=event,
            bug=bug,
            operation=Operation.LINK,
            jira=JiraContext(
                issue=linked_issue_key,
                project=self.jira_project_key,
            ),
        )

        logger.debug(
            "Update fields of Jira issue %s for Bug %s",
            linked_issue_key,
            bug.id,
            extra=log_context.dict(),
        )
        jira_response_update = self.jira_client.update_issue_field(
            key=linked_issue_key, fields=self.jira_fields(bug)
        )

        comments = self.jira_comments_for_update(bug=bug, event=event)
        jira_response_comments = []
        for i, comment in enumerate(comments):
            logger.debug(
                "Create comment #%s on Jira issue %s",
                i + 1,
                linked_issue_key,
                extra=log_context.update(operation=Operation.COMMENT).dict(),
            )
            jira_response_comments.append(
                self.jira_client.issue_add_comment(
                    issue_key=linked_issue_key, comment=comment
                )
            )

        self.update_issue(bug, event, linked_issue_key, is_new=False)

        return True, {"jira_responses": [jira_response_update, jira_response_comments]}

    def create_and_link_issue(  # pylint: disable=too-many-locals
        self,
        bug,
        event,
    ) -> ActionResult:
        """create jira issue and establish link between bug and issue; rollback/delete if required"""
        log_context = ActionLogContext(
            event=event,
            bug=bug,
            operation=Operation.CREATE,
            jira=JiraContext(
                project=self.jira_project_key,
            ),
        )
        logger.debug(
            "Create new Jira issue for Bug %s",
            bug.id,
            extra=log_context.dict(),
        )
        comment_list = self.bugzilla_client.get_comments(bug.id)
        description = comment_list[0].text[:JIRA_DESCRIPTION_CHAR_LIMIT]

        fields = {
            **self.jira_fields(bug),  # type: ignore
            "issuetype": {"name": bug.issue_type()},
            "description": description,
            "project": {"key": self.jira_project_key},
        }

        jira_response_create = self.jira_client.create_issue(fields=fields)

        # Jira response can be of the form: List or Dictionary
        if isinstance(jira_response_create, list):
            # if a list is returned, get the first item
            jira_response_create = jira_response_create[0]

        if isinstance(jira_response_create, dict):
            # if a dict is returned or the first item in a list, confirm there are no errors
            if any(
                element in ["errors", "errorMessages"] and jira_response_create[element]
                for element in jira_response_create.keys()
            ):
                raise ActionError(f"response contains error: {jira_response_create}")

        jira_key_in_response = jira_response_create.get("key")

        log_context.jira.issue = jira_key_in_response

        # In the time taken to create the Jira issue the bug may have been updated so
        # re-retrieve it to ensure we have the latest data.
        bug = self.bugzilla_client.get_bug(bug.id)

        jira_key_in_bugzilla = bug.extract_from_see_also()
        _duplicate_creation_event = (
            jira_key_in_bugzilla is not None
            and jira_key_in_response != jira_key_in_bugzilla
        )
        if _duplicate_creation_event:
            logger.warning(
                "Delete duplicated Jira issue %s from Bug %s",
                jira_key_in_response,
                bug.id,
                extra=log_context.update(operation=Operation.DELETE).dict(),
            )
            jira_response_delete = self.jira_client.delete_issue(
                issue_id_or_key=jira_key_in_response
            )
            return True, {"jira_response": jira_response_delete}

        jira_url = f"{settings.jira_base_url}browse/{jira_key_in_response}"
        logger.debug(
            "Link %r on Bug %s",
            jira_url,
            bug.id,
            extra=log_context.update(operation=Operation.LINK).dict(),
        )
        bugzilla_response = self.bugzilla_client.update_bug(
            bug.id, see_also={"add": [jira_url]}
        )

        bugzilla_url = f"{settings.bugzilla_base_url}/show_bug.cgi?id={bug.id}"
        logger.debug(
            "Link %r on Jira issue %s",
            bugzilla_url,
            jira_key_in_response,
            extra=log_context.update(operation=Operation.LINK).dict(),
        )
        # icon_url = f"{settings.bugzilla_base_url}/favicon.ico"
        jira_response = self.jira_client.create_or_update_issue_remote_links(
            issue_key=jira_key_in_response,
            link_url=bugzilla_url,
            title=bugzilla_url,
            # icon_url=icon_url,
            # icon_title=icon_url,
        )

        self.update_issue(
            bug=bug, event=event, linked_issue_key=jira_key_in_response, is_new=True
        )

        return True, {
            "bugzilla_response": bugzilla_response,
            "jira_response": jira_response,
        }
