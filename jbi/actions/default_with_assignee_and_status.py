"""
Extended action that provides some additional features over the default:
  * Updates the Jira assignee when the bug's assignee changes.
  * Optionally updates the Jira status when the bug's resolution or status changes.

`init` is required; and requires at minimum the `jira_project_key` parameter. `status_map` is optional.

`init` should return a __call__able
"""
import logging

from jbi import Operation
from jbi.actions.default import (
    JIRA_REQUIRED_PERMISSIONS as DEFAULT_JIRA_REQUIRED_PERMISSIONS,
)
from jbi.actions.default import DefaultExecutor
from jbi.models import ActionLogContext, BugzillaBug, BugzillaWebhookEvent, JiraContext

logger = logging.getLogger(__name__)


JIRA_REQUIRED_PERMISSIONS = DEFAULT_JIRA_REQUIRED_PERMISSIONS


def init(status_map=None, resolution_map=None, **kwargs):
    """Function that takes required and optional params and returns a callable object"""
    return AssigneeAndStatusExecutor(
        status_map=status_map or {}, resolution_map=resolution_map or {}, **kwargs
    )


class AssigneeAndStatusExecutor(DefaultExecutor):
    """Callable class that encapsulates the default_with_assignee_and_status action."""

    def __init__(self, status_map, resolution_map, **kwargs):
        """Initialize AssigneeAndStatusExecutor Object"""
        super().__init__(**kwargs)
        self.status_map = status_map
        self.resolution_map = resolution_map

    def jira_comments_for_update(
        self,
        bug: BugzillaBug,
        event: BugzillaWebhookEvent,
    ):
        """Returns the comments to post to Jira for a changed bug"""
        return bug.map_changes_as_comments(
            event, status_log_enabled=False, assignee_log_enabled=False
        )

    def update_issue(
        self,
        bug: BugzillaBug,
        event: BugzillaWebhookEvent,
        linked_issue_key: str,
        is_new: bool,
    ):
        changed_fields = event.changed_fields() or []

        log_context = ActionLogContext(
            event=event,
            bug=bug,
            operation=Operation.UPDATE,
            jira=JiraContext(
                project=self.jira_project_key,
                issue=linked_issue_key,
            ),
            extra={
                "changed_fields": ", ".join(changed_fields),
            },
        )

        def clear_assignee():
            # New tickets already have no assignee.
            if not is_new:
                logger.debug("Clearing assignee", extra=log_context.dict())
                self.jira_client.update_issue_field(
                    key=linked_issue_key, fields={"assignee": None}
                )

        # If this is a new issue or if the bug's assignee has changed then
        # update the assignee.
        if is_new or "assigned_to" in changed_fields:
            if bug.assigned_to == "nobody@mozilla.org":
                clear_assignee()
            else:
                logger.debug(
                    "Attempting to update assignee",
                    extra=log_context.dict(),
                )
                # Look up this user in Jira
                users = self.jira_client.user_find_by_user_string(query=bug.assigned_to)
                if len(users) == 1:
                    try:
                        # There doesn't appear to be an easy way to verify that
                        # this user can be assigned to this issue, so just try
                        # and do it.
                        self.jira_client.update_issue_field(
                            key=linked_issue_key,
                            fields={"assignee": {"accountId": users[0]["accountId"]}},
                        )
                    except IOError as exception:
                        logger.debug(
                            "Setting assignee failed: %s",
                            exception,
                            extra=log_context.dict(),
                        )
                        # If that failed then just fall back to clearing the
                        # assignee.
                        clear_assignee()
                else:
                    logger.debug(
                        "No assignee found",
                        extra=log_context.update(operation=Operation.IGNORE).dict(),
                    )
                    clear_assignee()

        # If this is a new issue or if the bug's status or resolution has
        # changed then update the issue status.
        if is_new or "status" in changed_fields or "resolution" in changed_fields:
            # If action has configured mappings for the issue resolution field, update it.
            bz_resolution = bug.resolution
            jira_resolution = self.resolution_map.get(bz_resolution)
            if jira_resolution:
                logger.debug(
                    "Updating Jira resolution to %s",
                    jira_resolution,
                    extra=log_context.dict(),
                )
                self.jira_client.update_issue_field(
                    key=linked_issue_key,
                    fields={"resolution": jira_resolution},
                )
            else:
                logger.debug(
                    "Bug resolution was not in the resolution map.",
                    extra=log_context.update(
                        operation=Operation.IGNORE,
                        extra={
                            **log_context.extra,
                            "resolution_map": self.resolution_map,
                        },
                    ).dict(),
                )

            # We use resolution if one exists or status otherwise.
            bz_status = bz_resolution or bug.status
            jira_status = self.status_map.get(bz_status)
            if jira_status:
                logger.debug(
                    "Updating Jira status to %s",
                    jira_status,
                    extra=log_context.dict(),
                )
                self.jira_client.set_issue_status(
                    linked_issue_key,
                    jira_status,
                )
            else:
                logger.debug(
                    "Bug status was not in the status map.",
                    extra=log_context.update(
                        operation=Operation.IGNORE,
                        extra={**log_context.extra, "status_map": self.status_map},
                    ).dict(),
                )
