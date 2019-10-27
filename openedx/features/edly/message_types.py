from openedx.core.djangoapps.ace_common.message import BaseMessageType


class OutlineChangeNotification(BaseMessageType):
    """
    A message for notifying user about new changes in the course outline.
    """
    pass


class HandoutChangeNotification(BaseMessageType):
    """
    A message for notifying user about new changes in the course handouts.
    """
    pass


class CommentVoteNotification(BaseMessageType):
    """
    A message for notifying user about vote on foum discussion comment.
    """
    pass


class ThreadCreateNotification(BaseMessageType):
    """
    A message for notifying users about new foum discussion thread.
    """
    pass


class ThreadVoteNotification(BaseMessageType):
    """
    A message for notifying user about vote on foum discussion thread.
    """
    pass


class CommentReplyNotification(BaseMessageType):
    """
    A message for notifying users about vote on foum discussion thread.
    """
    pass
